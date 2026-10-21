"""G4: Outbox file watcher.

Watches agents/chatgpt/outbox/ for new .md files and captures them into
SQLite (CAPTURED → PENDING_APPROVAL) via G2/G3 layers.

Public API
----------
    capture_file(path, conn, archive_dir=None) → (eid, vid)
    Called by the watcher *and* directly from the CLI.

run_watcher(root, db_path, *, observer_class=None, require_ready=False)
    Blocks until KeyboardInterrupt.  Pass PollingObserver in tests.
"""

from __future__ import annotations

import logging
import hashlib
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import store
from .routes import target_for
from .transitions import Event, transition

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────

_SUPPORTED_EXT = {".md"}
_DEBOUNCE_S = 0.75          # default stability window from G4
_EMPTY_RETRY_N = 3          # number of retries when file size is 0
_EMPTY_RETRY_WAIT = 0.10    # seconds between retries

# ── front-matter ───────────────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---\r?\n(.*?)\n---\r?\n", re.DOTALL)
_KV_RE = re.compile(r'^(\w+)\s*:\s*"?([^"\n]*)"?\s*$', re.MULTILINE)

_ALLOWED_TYPES = {"spec", "prose", "task", "code"}


def _parse_frontmatter(raw: bytes) -> tuple[dict, bytes]:
    """Return (meta_dict, body_bytes).  meta_dict has only safe, known keys."""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}, raw

    meta: dict = {}
    body = raw
    m = _FM_RE.match(text)
    if m:
        for key, val in _KV_RE.findall(m.group(1)):
            key = key.lower()
            val = val.strip()
            if key == "title" and val:
                meta["title"] = val[:200]
            elif key == "type" and val in _ALLOWED_TYPES:
                meta["type"] = val
            elif key == "note" and val:
                meta["note"] = val[:500]
        # body without the front-matter header
        body = text[m.end():].encode("utf-8")
    return meta, body


# ── msg-id generator ───────────────────────────────────────────────────────────

def _make_eid() -> str:
    """msg_YYYYMMDDTHHMMSS_<4 hex chars>"""
    import os
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rnd = os.urandom(2).hex()
    return f"msg_{ts}_{rnd}"


# ── duplicate detection ────────────────────────────────────────────────────────

def _origin_ref_exists(conn, origin_ref: str, sha256: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM envelopes e JOIN versions v
           ON v.envelope_id=e.id AND v.version_id=e.captured_version_id
           WHERE e.origin_ref=? AND v.sha256=?
           AND e.state NOT IN ('REJECTED','EXPIRED') LIMIT 1""",
        (origin_ref, sha256),
    ).fetchone()
    return row is not None


# ── core capture logic ─────────────────────────────────────────────────────────

def capture_file(path: Path, conn, archive_dir: Path | None = None,
                 *, source_agent: str = 'chatgpt') -> tuple[str, str] | None:
    """Read *path*, store in SQLite, advance to PENDING_APPROVAL.

    Returns (eid, vid) on success, or None if the file was skipped.
    Raises on unrecoverable errors (permissions, DB integrity, etc.).
    """
    path = Path(path)
    target_agent = target_for(source_agent)

    if path.suffix.lower() not in _SUPPORTED_EXT:
        log.debug("skip: unsupported extension %s", path.name)
        return None

    # --- read with empty-file retry -------------------------------------------
    raw: bytes | None = None
    for attempt in range(_EMPTY_RETRY_N):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            log.warning("capture_file: %s vanished before read", path)
            return None
        if data:
            raw = data
            break
        log.debug("capture_file: %s is empty (attempt %d/%d)", path.name,
                  attempt + 1, _EMPTY_RETRY_N)
        time.sleep(_EMPTY_RETRY_WAIT)

    if not raw:
        log.warning("capture_file: %s is still empty after %d retries – skipped",
                    path.name, _EMPTY_RETRY_N)
        return None

    # --- duplicate check: same source path and same bytes ---------------------
    origin_ref = str(path)
    sha256 = hashlib.sha256(raw).hexdigest()
    if _origin_ref_exists(conn, origin_ref, sha256):
        log.info("capture_file: already captured origin_ref=%s – skipped", origin_ref)
        return None

    # --- parse front-matter ---------------------------------------------------
    fm, body = _parse_frontmatter(raw)

    # store.insert_captured expects *full* content; we persist raw bytes so the
    # delivered file is byte-for-byte identical to what ChatGPT produced.
    content = raw

    eid = _make_eid()
    meta = {
        "id": eid,
        "schema_version": "1.0",
        "source_agent": source_agent,
        "target_agent": target_agent,
        "origin_ref": origin_ref,
        **fm,
    }

    # --- insert into DB (G2) + enqueue (G3) -----------------------------------
    eid, vid = store.insert_captured(conn, meta, content)
    transition(
        conn, eid, Event.ENQUEUE,
        expected_state="CAPTURED",
        actor="watcher",
        payload={},
    )
    if archive_dir is not None:
        archive = Path(archive_dir)
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / f"{eid}__{path.name}"
        try:
            path.replace(target)
        except FileNotFoundError:
            # A restart may have archived the source after the DB commit.
            pass
        except OSError:
            log.exception("capture_file: could not archive %s", path)
            raise
    log.info("captured %s → eid=%s vid=%s", path.name, eid, vid)
    return eid, vid


# ── watchdog handler ───────────────────────────────────────────────────────────

class _OutboxHandler(FileSystemEventHandler):
    """Handle created/moved events in the chatgpt outbox.

    SQLite connections cannot be shared across threads, so we open a
    dedicated connection inside the main thread on first use.
    """

    def __init__(self, db_path, archive_dir, debounce: float = _DEBOUNCE_S,
                 require_ready: bool = False, *, source_agent: str = 'chatgpt',
                 on_capture=None):
        super().__init__()
        target_for(source_agent)
        self._source_agent = source_agent
        self._on_capture = on_capture
        self._db_path = db_path
        self._archive_dir = Path(archive_dir)
        self._require_ready = require_ready
        self._thread_conn = None   # opened lazily in main thread
        self._debounce = debounce
        # path → last-event epoch, for debouncing
        self._pending: dict[str, tuple[float, tuple[int, int], bool]] = {}
        self._processing: set[str] = set()
        self._lock = threading.Lock()

    def _conn(self):
        if self._thread_conn is None:
            self._thread_conn = store.connect(self._db_path)
        return self._thread_conn

    def _candidate(self, src_path: str) -> Path | None:
        path = Path(src_path)
        if path.suffix.lower() == ".ready":
            path = path.with_suffix("")
        if path.suffix.lower() not in _SUPPORTED_EXT or path.name.startswith("."):
            return None
        if path.name.endswith((".tmp", ".part")) or not path.exists():
            return None
        if self._require_ready and not path.with_name(path.name + ".ready").exists():
            return None
        return path

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _schedule(self, src_path: str, *, immediate: bool = False) -> None:
        path = self._candidate(src_path)
        if path is None:
            return
        try:
            sig = self._signature(path)
        except FileNotFoundError:
            return
        deadline = time.monotonic() if immediate else time.monotonic() + self._debounce
        with self._lock:
            self._pending[str(path)] = (deadline, sig, immediate)

    def _flush(self) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [p for p, (deadline, _sig, _immediate) in self._pending.items()
                     if deadline <= now]
            for p in ready:
                _deadline, old_sig, immediate = self._pending.pop(p)
                path = Path(p)
                try:
                    current_sig = self._signature(path)
                except FileNotFoundError:
                    continue
                if not immediate and current_sig != old_sig:
                    self._pending[p] = (time.monotonic() + self._debounce, current_sig, immediate)
                    continue
                if p in self._processing:
                    continue
                self._processing.add(p)
        
            # We must process items outside the lock to avoid blocking _schedule
            items_to_process = [Path(p) for p in ready if p in self._processing]

        for path in items_to_process:
            p = str(path)
            try:
                result = capture_file(path, self._conn(), self._archive_dir,
                                      source_agent=self._source_agent)
                if result:
                    marker = path.with_name(path.name + ".ready")
                    if marker.exists():
                        marker.unlink()
                    if self._on_capture:
                        self._on_capture(result[0], 'PENDING_APPROVAL')
                    from app import push   # TB-29: a file waiting for approval reaches the phone
                    push.on_envelope(self._conn(), result[0])
                elif path.exists() and path.stat().st_size == 0:
                    failed = path.parent / '.failed'
                    failed.mkdir(parents=True, exist_ok=True)
                    path.replace(failed / path.name)
            except Exception:
                log.exception("capture_file failed for %s", p)
                try:
                    failed = path.parent / '.failed'
                    failed.mkdir(parents=True, exist_ok=True)
                    if path.exists():
                        path.replace(failed / path.name)
                except OSError:
                    log.exception("could not move failed source %s", p)
            finally:
                with self._lock:
                    self._processing.discard(p)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.dest_path, immediate=True)

    def scan_startup(self, outbox: Path) -> None:
        for path in outbox.iterdir():
            if path.is_file():
                self._schedule(str(path))

    def dispatch(self, event: FileSystemEvent) -> None:
        super().dispatch(event)
        # self._flush() removed: _flush is only called by main loop to ensure thread-safety


# ── public runner ──────────────────────────────────────────────────────────────

def run_watcher(root: Path, db_path, *, observer_class=None, require_ready=False,
                bidirectional=False, stop_event=None, on_capture=None) -> None:
    """Observe outboxes; all capture/DB work runs in this caller thread (K-006)."""
    stop_event = stop_event or threading.Event()
    ObserverCls = observer_class or Observer
    archive_dir = root / "archive" / "captured"
    observer = ObserverCls()
    handlers = []
    outboxes = []
    sources = ('chatgpt', 'claude') if bidirectional else ('chatgpt',)
    for source in sources:
        outbox = root / 'agents' / source / 'outbox'
        outbox.mkdir(parents=True, exist_ok=True)
        handler = _OutboxHandler(str(db_path), archive_dir,
                                require_ready=require_ready, source_agent=source,
                                on_capture=on_capture)
        handlers.append(handler)
        outboxes.append(outbox)
        observer.schedule(handler, str(outbox), recursive=False)
    observer.start()
    try:
        for handler, outbox in zip(handlers, outboxes):
            handler.scan_startup(outbox)
            log.info('watcher started on %s', outbox)
        while not stop_event.wait(0.1):
            for handler in handlers:
                handler._flush()
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        for handler in handlers:
            if handler._thread_conn is not None:
                handler._thread_conn.close()
        log.info("watcher stopped")
