"""F3-01a single-owner dispatcher: turns PENDING work messages into executions.

Runs only inside the worker lifespan (which holds the runner flock). Wakeups from
message commits, run completion and startup are coalesced into one asyncio.Event;
a slow periodic scan covers missed wakeups. The DB CAS in works_store.dispatch_next
is what prevents double dispatch — not this object's memory.
"""
import asyncio
import logging
from app import store, runner_store as repo, works_store

log = logging.getLogger(__name__)
SCAN_SECONDS = 5.0


class WorkDispatcher:
    def __init__(self, db_path, runner, jobs):
        self.db_path, self.runner, self.jobs = db_path, runner, jobs
        self._event = asyncio.Event()
        self._loop = None
        self._task = None
        self._last_reason = {}   # work_id -> last reported wait reason (avoids event spam)

    def start(self):
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name='work-dispatcher')
        self.wake()

    def wake(self):
        """Thread-safe: sync FastAPI handlers run in a worker thread."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._event.set)

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self):
        while True:
            try:
                await asyncio.wait_for(self._event.wait(), SCAN_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._event.clear()
            try:
                self.dispatch_pass()
            except Exception:
                log.exception('work dispatch pass failed')

    def dispatch_pass(self):
        conn = store.connect(self.db_path)
        try:
            works_store.sync_run_status(conn)
            for work_id in works_store.works_with_pending(conn):
                self._try(conn, work_id)
        finally:
            conn.close()

    def _try(self, conn, work_id):
        try:
            sid, reason = works_store.dispatch_next(conn, work_id)
        except repo.WorkspaceBusy:
            sid, reason = None, 'workspace_busy'
        except Exception as exc:
            # Visible, durable and not hot-looped: the message stays PENDING.
            sid, reason = None, 'dispatch_error:' + type(exc).__name__
            log.warning('dispatch failed for work %s: %s', work_id, type(exc).__name__)
        if sid and reason is None:
            self._last_reason.pop(work_id, None)
            self._spawn(sid)
            return
        if sid and reason == 'rejected':
            self._last_reason.pop(work_id, None)
            return
        if reason not in ('no_pending',) and self._last_reason.get(work_id) != reason:
            self._last_reason[work_id] = reason
            works_store.append_event(conn, work_id, 'dispatch_waiting', {'reason': reason})

    def _spawn(self, sid):
        task = asyncio.get_running_loop().create_task(self.runner.run_session(sid))
        self.jobs[sid] = task

        def finished(_):
            self.jobs.pop(sid, None)
            self.wake()   # trigger (2): after run_session persisted its outcome and released cleanup

        task.add_done_callback(finished)
