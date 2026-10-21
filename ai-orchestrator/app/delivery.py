"""G5: Atomic file delivery to claude/inbox/.

deliver(conn, eid, *, inbox_dir, actor='system') → Path
    Reads the approved content from DB, writes atomically via
    mkstemp + fsync + os.link, then records DELIVERED in SQLite.

reconcile(conn, *, inbox_dir, actor='system') → list[dict]
    On startup, resolves any envelopes stuck in DELIVERING state.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from . import store
from .transitions import Event, transition

log = logging.getLogger(__name__)

# ── helpers ────────────────────────────────────────────────────────────────────

_SLUG_STRIP = re.compile(r"[^\w\s-]")
_SLUG_SPACE = re.compile(r"[\s_]+")
_SLUG_MAX = 40


class DeliveryConflict(RuntimeError):
    """The intended destination already contains different bytes."""


def _slugify(text: str | None) -> str:
    if not text:
        return "untitled"
    t = text.lower()
    t = _SLUG_STRIP.sub("", t)
    t = _SLUG_SPACE.sub("-", t).strip("-")
    return t[:_SLUG_MAX] or "untitled"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dest_name(eid: str, title: str | None) -> str:
    return f"{eid}__{_slugify(title)}.md"


# ── atomic write ───────────────────────────────────────────────────────────────

def _atomic_write(content: bytes, dest: Path) -> Path:
    """Write *content* to a tmp file next to *dest*, then os.link to *dest*.

    Returns *dest*.  A different existing file is a hard conflict: it is never
    overwritten and never redirected to a suffix path.
    """
    inbox_dir = dest.parent
    inbox_dir.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=inbox_dir, prefix=".tmp_conductor_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())

        try:
            os.link(tmp_path, dest)
        except FileExistsError:
            existing_hash = _sha256_file(dest)
            new_hash = hashlib.sha256(content).hexdigest()
            if existing_hash != new_hash:
                raise DeliveryConflict(
                    f"destination exists with different hash: {dest}")
            log.info("deliver: %s already exists with matching hash – idempotent",
                     dest.name)
        else:
            # Persist the directory entry before recording DELIVERED.
            dir_fd = os.open(inbox_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return dest


def _publish_and_complete(conn, envelope: dict, *, actor: str) -> Path:
    """Publish a previously recorded DELIVERING intent and complete it."""
    vid = envelope.get("approved_version_id")
    dest_path = envelope.get("delivered_path")
    if not vid or not dest_path:
        raise ValueError(f"Envelope {envelope['id']} has incomplete delivery intent")
    content = store.get_content(conn, vid)
    dest = Path(dest_path)
    try:
        final = _atomic_write(content, dest)
        actual_sha = _sha256_file(final)
        transition(conn, envelope["id"], Event.DELIVER_OK,
                   expected_state="DELIVERING", actor=actor,
                   payload={"actual_sha256": actual_sha})
        return final
    except Exception as exc:
        try:
            transition(conn, envelope["id"], Event.DELIVER_ERR,
                       expected_state="DELIVERING", actor=actor,
                       payload={"reason": str(exc)[:200]})
        except Exception:
            log.exception("could not record DELIVER_ERR for %s", envelope["id"])
        raise


# ── public API ─────────────────────────────────────────────────────────────────

def deliver(conn, eid: str, *, inbox_dir: Path, actor: str = "system") -> Path:
    """Deliver an APPROVED envelope to *inbox_dir*.

    State path: APPROVED → DELIVERING → DELIVERED

    Returns the Path of the delivered file.
    Raises ValueError / store.VersionMismatch on precondition failures.
    """
    inbox_dir = Path(inbox_dir)
    envelope = store.get_envelope(conn, eid)
    if envelope is None:
        raise KeyError(f"Envelope not found: {eid}")
    if envelope["state"] != "APPROVED":
        raise ValueError(
            f"deliver: envelope {eid} is in state {envelope['state']!r}, expected APPROVED"
        )

    vid = envelope["approved_version_id"]
    content: bytes = store.get_content(conn, vid)

    dest_name = _dest_name(eid, envelope.get("title"))
    dest = inbox_dir / dest_name

    # --- record intent: APPROVED → DELIVERING --------------------------------
    transition(
        conn, eid, Event.ROUTE,
        expected_state="APPROVED",
        actor=actor,
        payload={"version_id": vid, "dest_path": str(dest)},
    )

    # --- atomic write + verify + complete ------------------------------------
    final_path = _publish_and_complete(conn, store.get_envelope(conn, eid), actor=actor)

    log.info("delivered %s → %s", eid, final_path)
    return final_path


def reconcile(conn, *, inbox_dir: Path, actor: str = "system") -> list[dict]:
    """Resolve envelopes stuck in DELIVERING after a restart.

    For each DELIVERING envelope:
    - If the file exists and hash matches → mark DELIVERED.
    - If absent, re-publish the frozen approved snapshot.
    - If present with a different hash, fail without overwriting it.

    Returns a list of result dicts for reporting.
    """
    inbox_dir = Path(inbox_dir)
    delivering = store.list_by_state(conn, "DELIVERING")
    results = []
    for env in delivering:
        eid = env["id"]
        dest_path = env.get("delivered_path")
        result = {"eid": eid, "action": None, "path": dest_path}

        if not dest_path:
            result["action"] = "skipped_no_path"
            results.append(result)
            continue

        dest = Path(dest_path)
        if dest.exists():
            actual_sha = _sha256_file(dest)
            expected_sha = store.get_version(conn, env["approved_version_id"])["sha256"]
            try:
                if actual_sha != expected_sha:
                    raise DeliveryConflict(
                        f"destination hash mismatch: expected {expected_sha}, got {actual_sha}")
                transition(conn, eid, Event.DELIVER_OK,
                           expected_state="DELIVERING", actor=actor,
                           payload={"actual_sha256": actual_sha})
                result["action"] = "recovered_delivered"
                result["sha256"] = actual_sha
                log.info("reconcile: %s → DELIVERED (%s)", eid, dest.name)
            except Exception as exc:
                result["action"] = "hash_mismatch"
                result["error"] = str(exc)
                try:
                    transition(conn, eid, Event.DELIVER_ERR,
                               expected_state="DELIVERING", actor=actor,
                               payload={"reason": str(exc)[:200]})
                except Exception:
                    log.exception("reconcile: could not mark %s FAILED", eid)
                log.warning("reconcile: %s hash mismatch → FAILED", eid)
        else:
            try:
                final = _publish_and_complete(conn, env, actor=actor)
                result["action"] = "redelivered"
                result["path"] = str(final)
                log.info("reconcile: %s re-published → DELIVERED", eid)
            except Exception as exc:
                result["action"] = "error"
                result["error"] = str(exc)

        results.append(result)

    return results
