"""Owned POSIX process groups; identity is recorded before provider execution."""
import asyncio
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import time
import psutil


def executable(env_key, name, candidates=()):
    configured = os.environ.get(env_key)
    for path in [configured] if configured else [shutil.which(name), *candidates]:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return str(Path(path).resolve())
    raise FileNotFoundError(f'{name} not found; configure {env_key}')


def identity(pid):
    try:
        proc = psutil.Process(pid)
        return {'pid': pid, 'start_time': proc.create_time(), 'pgid': os.getpgid(pid),
                'boot_time': psutil.boot_time()}
    except (psutil.NoSuchProcess, ProcessLookupError):
        return None


def same_identity(a, b):
    return bool(a and b and all(a[k] == b[k] for k in ('pid','start_time','pgid','boot_time')))


def group_members(pgid):
    members = []
    for proc in psutil.process_iter(['pid']):
        try:
            if os.getpgid(proc.pid) == pgid:
                item = identity(proc.pid)
                if item:
                    members.append(item)
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
    return members


def group_exists(pgid):
    return bool(group_members(pgid))


class ProcessCleanupError(RuntimeError):
    pass


# TB-21: no process Conductor starts (providers, previews) may act with the user's GitHub/git identity.
CREDENTIAL_PREFIXES = ('GH_', 'GITHUB_', 'GIT_', 'SSH_')
_signed_out_gh = None


def without_credentials(env=None):
    """`env` (default: ours) minus every GitHub/git/SSH variable, plus settings that leave git and gh signed out:
    an empty gh config, no system/global git config (the Xcode one sets osxkeychain), the credential helper list
    reset after the workspace's own .git/config, no prompts, no git over ssh. ponytail: this stops git and gh from
    finding credentials by themselves; a process that calls the keychain or reads ~/.ssh directly is bounded by
    its sandbox / tools, not by this."""
    global _signed_out_gh
    if _signed_out_gh is None or not os.path.isdir(_signed_out_gh):
        _signed_out_gh = tempfile.mkdtemp(prefix='conductor-gh-')   # 0700, stays empty
    clean = {k: v for k, v in (os.environ if env is None else env).items() if not k.startswith(CREDENTIAL_PREFIXES)}
    return {**clean, 'GH_CONFIG_DIR': _signed_out_gh, 'GH_PROMPT_DISABLED': '1',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
            'GIT_SSH_COMMAND': 'false', 'GIT_CONFIG_COUNT': '2',
            'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': '',
            'GIT_CONFIG_KEY_1': 'credential.interactive', 'GIT_CONFIG_VALUE_1': 'false'}


async def spawn_owned(args, cwd, env=None):
    # A separate gate FD prevents model execution until the ownership event has
    # been committed by the consumer. EOF (e.g. server crash) exits without exec.
    read_fd, write_fd = os.pipe()
    code = ('import os,sys; fd=int(sys.argv[1]); token=os.read(fd,1); os.close(fd); '
            'os.execv(sys.argv[2],sys.argv[2:]) if token==b"G" else sys.exit(125)')
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-c', code, str(read_fd), *args, cwd=cwd, env=without_credentials(env),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
            pass_fds=(read_fd,), limit=4*1024*1024)
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    try:
        owner = identity(process.pid)
        if owner is None or owner['pgid'] != process.pid:
            raise ProcessCleanupError('could not capture new process identity')
    except BaseException:
        os.close(write_fd)
        await process.wait()  # Gate EOF makes the wrapper exit without exec.
        raise
    return process, owner, write_fd


def release_gate(fd):
    os.write(fd, b'G')
    os.close(fd)


def remember_group(owner):
    if not same_identity(owner, identity(owner['pid'])):
        raise ProcessCleanupError('cannot establish ownership before shutdown')
    owner['members'] = group_members(owner['pgid'])
    return owner


async def drain_stderr(stream):
    while await stream.read(8192):
        pass


async def stop_process(process, owner=None, *, timeout=2.0):
    try:
        owner = owner or identity(process.pid)
        pgid = process.pid
        members = group_members(pgid)
        if members:
            current = identity(process.pid)
            if current is not None and not same_identity(owner, current):
                raise ProcessCleanupError('process identity changed; refuse signal')
            if current is None:
                # A normal server exit may precede helper exit by a few ms. Wait
                # without signalling unknown processes, then verify saved members.
                deadline = time.monotonic() + timeout
                while members and time.monotonic() < deadline:
                    await asyncio.sleep(.025)
                    members = group_members(pgid)
                trusted = {m['pid']: m for m in (owner or {}).get('members', [])}
                if any(not same_identity(m, trusted.get(m['pid'])) for m in members):
                    raise ProcessCleanupError('leader missing and unknown group members remain')
            else:
                trusted = {m['pid']: m for m in members}
            if not members:
                await asyncio.wait_for(process.wait(), timeout)
                return
            os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            remaining = group_members(pgid)
            if remaining:
                if any(not same_identity(m, trusted.get(m['pid'])) for m in remaining):
                    raise ProcessCleanupError('unrecognized process in owned group')
                os.killpg(pgid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), timeout)
        deadline = time.monotonic() + timeout
        while group_exists(pgid):
            if time.monotonic() >= deadline:
                raise ProcessCleanupError('process group remains after termination')
            await asyncio.sleep(.025)
    except ProcessLookupError:
        if group_exists(process.pid):
            raise ProcessCleanupError('group ownership unresolved')
        await asyncio.wait_for(process.wait(), timeout)
    except ProcessCleanupError:
        raise
    except Exception as exc:
        raise ProcessCleanupError('Cannot confirm provider process termination') from exc


async def _await_group_gone(pgid, timeout):
    deadline = time.monotonic() + timeout
    while group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(.05)
    return True


async def reap_owned(owner, trusted_members=(), *, timeout=2.0):
    """Reap an owned process group from PERSISTED ownership only (no live handle).

    Recovery cannot use stop_process (it needs the live asyncio subprocess). This
    verifies/kills from the recorded owner {pid,pgid,start_time,boot_time} plus the
    trusted member identities captured in the attempt's process_group_snapshot.

    Returns a proof dict; ok=True only when the group is proven gone, so the caller
    releases the workspace lock ONLY then. Never signals a process whose identity
    does not match the recorded ownership (reboot, PID reuse, or a reused PGID).
    """
    pgid, pid = owner['pgid'], owner['pid']
    proof = {'pid': pid, 'pgid': pgid, 'signals': [], 'ok': False}
    # Gate 1 — reboot: old pid/pgid are meaningless; every recorded process is gone.
    if psutil.boot_time() != owner['boot_time']:
        return {**proof, 'result': 'dead', 'reason': 'boot_time_changed', 'ok': True}
    leader_alive = same_identity(owner, identity(pid))
    members = group_members(pgid)
    # Gate 2 — nothing alive in the recorded group.
    if not members:
        return {**proof, 'result': 'dead', 'reason': 'group_empty', 'ok': True}
    trusted = {m['pid']: m for m in trusted_members}
    if leader_alive:
        trusted[pid] = owner
    try:
        if leader_alive:
            # A live, matching leader means its pid was never reused; with
            # start_new_session the leader's pid == pgid, so the whole live group is
            # our descendants and may be signalled as a group.
            os.killpg(pgid, signal.SIGTERM)
            proof['signals'].append('SIGTERM')
            if not await _await_group_gone(pgid, timeout):
                os.killpg(pgid, signal.SIGKILL)
                proof['signals'].append('SIGKILL')
                await _await_group_gone(pgid, timeout)
        else:
            # Leader gone: the PGID may have been reused. Only signal members that
            # match the recorded snapshot, one PID at a time, re-checking identity
            # immediately before the signal so a reused PID is never killed.
            unknown = [m['pid'] for m in members if not same_identity(m, trusted.get(m['pid']))]
            if unknown:
                return {**proof, 'result': 'blocked', 'reason': 'unattributable_members',
                        'blocking': unknown, 'ok': False}
            for m in members:
                if same_identity(m, identity(m['pid'])):
                    try:
                        os.kill(m['pid'], signal.SIGKILL)
                        proof['signals'].append('kill:%d' % m['pid'])
                    except ProcessLookupError:
                        pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not [m for m in group_members(pgid) if same_identity(m, trusted.get(m['pid']))]:
                    break
                await asyncio.sleep(.05)
    except ProcessLookupError:
        pass
    # Verify death. Leader-alive: the entire group must be gone. Leader-dead: only the
    # recorded members must be gone; a newly appeared unattributable process keeps the lock.
    live = group_members(pgid)
    if leader_alive:
        blocking = [m['pid'] for m in live]
    else:
        blocking = [m['pid'] for m in live if same_identity(m, trusted.get(m['pid']))]
        unknown_now = [m['pid'] for m in live if not same_identity(m, trusted.get(m['pid']))]
        if unknown_now:
            return {**proof, 'result': 'blocked', 'reason': 'unattributable_members',
                    'blocking': unknown_now, 'ok': False}
    if blocking:
        return {**proof, 'result': 'blocked', 'reason': 'survived_termination',
                'blocking': blocking, 'ok': False}
    return {**proof, 'result': 'reaped', 'ok': True}
