"""F3-02: the loop executor end to end (real store/runner/works, scripted fake providers)."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from app import store, runner_store as repo, works_store as ws, loop_store as loop, review_result as rr
from app.adapters.base import AdapterResult
from app.provider_policy import normalize, digest
from app.runner import SessionRunner, snapshot_exclusions


def review(verdict, *findings, reason=None):
    body = {'verdict': verdict, 'findings': [{'id': f'F{i + 1}', 'severity': 'high', 'summary': s, 'evidence': []}
                                             for i, s in enumerate(findings)]}
    if reason:
        body['blocked_reason'] = reason
    return 'İnceledim.\n```conductor-review\n' + json.dumps(body, ensure_ascii=False) + '\n```\n'


class Script:
    """Author (WRITE) calls may write a file; reviewer (READ_ONLY) calls answer from `reviews`."""
    def __init__(self, reviews=(), writes=None, delay=0.0, author_errors=(), reviewer_errors=()):
        self.reviews, self.writes, self.delay = list(reviews), writes, delay
        self.author_errors, self.reviewer_errors = list(author_errors), list(reviewer_errors)
        self.calls, self.n, self.started = [], 0, asyncio.Event()

    def __call__(self, provider):
        outer = self

        class Adapter:
            async def run(self, prompt, workspace, provider_session_id=None, *, profile):
                outer.n += 1
                n = outer.n
                outer.calls.append({'provider': provider, 'profile': profile, 'psid': provider_session_id, 'prompt': prompt})
                owner = {'pid': 700 + n, 'pgid': 700 + n, 'start_time': 1.0, 'boot_time': 1.0}
                yield {'type': 'process_started', 'ownership': owner}
                yield {'type': 'started', 'provider_session_id': provider_session_id or f'{provider}-t{n}'}
                outer.started.set()
                if outer.delay:
                    await asyncio.sleep(outer.delay)
                yield {'type': 'process_stopped', 'ownership': owner}
                errors = outer.author_errors if profile == 'WRITE' else outer.reviewer_errors
                if errors:
                    code = errors.pop(0)
                    result = AdapterResult('x', 'e', None, 'error', True, 'failed', 1, code, {'code': code})
                elif profile == 'WRITE':
                    if outer.writes is not None:
                        Path(workspace, 'out.txt').write_text(outer.writes(n))
                    result = AdapterResult(f'author {n}', provider_session_id or f'{provider}-t{n}', None, 'stop', False, 'completed', 0)
                else:
                    text = outer.reviews.pop(0) if outer.reviews else review('pass')
                    result = AdapterResult(text, provider_session_id or f'{provider}-t{n}', None, 'stop', False, 'completed', 0)
                yield {'type': 'completed' if result.successful else 'failed', 'result': result}
        return Adapter()


class LoopRunner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.db'
        self.ws_dir = Path(self.tmp.name) / 'ws'; self.ws_dir.mkdir()
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'döngü', str(self.ws_dir))

    def policy(self, turns=3, author=('codex',)):
        return normalize({'roles': {'author': list(author), 'reviewer': ['claude']}, 'loop': {'max_turns': turns}})

    def dispatch(self, text='hedef', policy=None):
        policy = policy or self.policy()
        ws.add_message(self.conn, self.wid, str(uuid4()), text, digest(policy), policy, digest(policy))
        sid, reason = ws.dispatch_next(self.conn, self.wid)
        self.assertTrue(sid, reason)
        return sid

    async def execute(self, script, sid):
        await SessionRunner(self.db, adapter_factory=script).run_session(sid)
        return repo.get_session(self.conn, sid)['status'], loop.state(self.conn, sid)

    def roles(self, sid):
        return [(s['role'], s['status']) for s in repo.get_session_steps(self.conn, sid)]

    async def wait_for_step(self, sid, *, role, status, timeout=2.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if any(s['role'] == role and s['status'] == status for s in repo.get_session_steps(self.conn, sid)):
                return
            await asyncio.sleep(0.01)
        self.fail(f'{role} step did not reach {status} within {timeout}s; steps={self.roles(sid)}')

    async def test_pass_in_turn_one_is_verified_and_frees_the_queue(self):
        sid = self.dispatch()
        script = Script([review('pass')])
        status, st = await self.execute(script, sid)
        self.assertEqual((status, st['outcome'], st['current_turn']), ('SUCCEEDED', 'verified', 1))
        self.assertEqual(self.roles(sid), [('author', 'COMPLETED'), ('reviewer', 'COMPLETED')])
        self.assertIn('conductor-review', script.calls[1]['prompt'])            # contract reaches the reviewer
        self.assertIsNone(ws.blocker(self.conn, self.wid))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 0)

    async def test_changes_then_pass_runs_turns_and_reviews_the_last_author_output(self):
        sid = self.dispatch()
        script = Script([review('changes_requested', 'Bölme hatası'), review('changes_requested', 'Test eksik'),
                         review('pass')], writes=lambda n: f'v{n}')
        status, st = await self.execute(script, sid)
        self.assertEqual((status, st['outcome'], st['current_turn']), ('SUCCEEDED', 'verified', 3))
        self.assertEqual([r for r, _ in self.roles(sid)], ['author', 'reviewer'] * 3)
        self.assertEqual(self.roles(sid)[-1], ('reviewer', 'COMPLETED'))       # last author output reviewed
        author2, reviewer2 = script.calls[2], script.calls[3]
        self.assertEqual(author2['psid'], script.calls[0]['psid'] or 'codex-t1')  # correction: same author thread
        self.assertIn('Bölme hatası', author2['prompt'])                        # findings reach the author as data
        self.assertIn('Bölme hatası', reviewer2['prompt'])                      # and the next reviewer
        self.assertIn('turn=2', reviewer2['prompt'])
        turns = loop.turns(self.conn, sid)
        self.assertEqual([t['verdict'] for t in turns], ['changes_requested', 'changes_requested', 'pass'])
        self.assertEqual([t['ended_reason'] for t in turns], ['next_turn', 'next_turn', 'verified'])

    async def test_turn_limit_blocks_queue_until_continue_with_the_outcome(self):
        sid = self.dispatch(policy=self.policy(turns=2))
        status, st = await self.execute(Script([review('changes_requested', 'A'), review('changes_requested', 'B')],
                                               writes=lambda n: str(n)), sid)
        self.assertEqual((status, st['outcome']), ('SUCCEEDED', 'turn_limit'))
        self.assertEqual(self.roles(sid)[-1][0], 'reviewer')
        self.assertEqual(ws.blocker(self.conn, self.wid), 'needs_review_decision')
        with self.assertRaises(ws.WorkError) as stale:
            ws.continue_work(self.conn, self.wid, sid, 'verified')
        self.assertEqual(stale.exception.code, 'stale_outcome')
        self.assertTrue(ws.continue_work(self.conn, self.wid, sid, 'turn_limit'))
        self.assertIsNone(ws.blocker(self.conn, self.wid))

    async def test_invalid_review_stops_without_a_repair_attempt(self):
        sid = self.dispatch()
        script = Script(['Her şey iyi görünüyor, pass.'])
        status, st = await self.execute(script, sid)
        self.assertEqual((status, st['outcome'], st['outcome_reason']), ('SUCCEEDED', 'review_invalid', 'no_block'))
        self.assertEqual(len(script.calls), 2)                                   # K2: no format repair
        self.assertEqual(ws.blocker(self.conn, self.wid), 'needs_review_decision')

    async def test_blocked_verdict(self):
        sid = self.dispatch()
        _, st = await self.execute(Script([review('blocked', reason='Kimlik bilgisi gerekiyor')]), sid)
        self.assertEqual(st['outcome'], 'reviewer_blocked')

    async def test_same_findings_and_unchanged_folder_is_no_progress(self):
        sid = self.dispatch(policy=self.policy(turns=5))
        _, st = await self.execute(Script([review('changes_requested', 'Aynı hata'),
                                           review('changes_requested', 'aynı  HATA')]), sid)   # author writes nothing
        self.assertEqual((st['outcome'], st['current_turn']), ('no_progress', 2))

    async def test_same_findings_but_changed_folder_keeps_going(self):
        sid = self.dispatch(policy=self.policy(turns=3))
        _, st = await self.execute(Script([review('changes_requested', 'Aynı hata'), review('changes_requested', 'Aynı hata'),
                                           review('pass')], writes=lambda n: f'v{n}'), sid)
        self.assertEqual(st['outcome'], 'verified')

    async def test_budget_runs_out_mid_attempt_as_a_clean_stop(self):
        sid = self.dispatch()
        self.conn.execute('UPDATE session_loop_state SET max_active_ms=300 WHERE session_id=?', (sid,))
        script = Script(delay=2.0)
        status, st = await self.execute(script, sid)
        self.assertEqual((status, st['outcome'], st['last_author_unreviewed']), ('SUCCEEDED', 'budget_exhausted', 1))
        self.assertEqual(self.roles(sid)[0], ('author', 'CANCELLED'))
        att = self.conn.execute("SELECT status, terminal_reason FROM session_attempts").fetchone()
        self.assertEqual(tuple(att), ('CANCELLED', 'budget_exhausted'))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM session_time_segments WHERE settled=0').fetchone()[0], 0)
        self.assertGreaterEqual(loop.used_ms(self.conn, sid), 250)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 0)
        self.assertEqual(ws.blocker(self.conn, self.wid), 'needs_review_decision')

    async def test_an_author_out_of_time_goes_to_review_and_the_next_turn(self):   # K-061
        sid = self.dispatch()
        script = Script([review('changes_requested', 'Yarım kaldı'), review('pass')], writes=lambda n: f'v{n}')
        made = []
        def factory(provider):
            made.append(provider); script.delay = 30 if len(made) == 1 else 0
            return script(provider)
        await SessionRunner(self.db, adapter_factory=factory, step_timeout=.2).run_session(sid)
        status, st = repo.get_session(self.conn, sid)['status'], loop.state(self.conn, sid)
        self.assertEqual((status, st['outcome']), ('SUCCEEDED', 'verified'))
        self.assertEqual(self.roles(sid), [('author', 'COMPLETED'), ('reviewer', 'COMPLETED')] * 2)
        self.assertEqual(repo.get_session_steps(self.conn, sid)[0]['terminal_reason'], 'step_timeout')
        self.assertIn('TIME LIMIT REACHED', script.calls[1]['prompt'])   # the reviewer knows the author ran out of time
        self.assertIn('Yarım kaldı', script.calls[2]['prompt'])          # the next author continues from the review

    async def test_budget_already_spent_stops_before_starting_a_step(self):
        sid = self.dispatch()
        seg = loop.open_segment(self.conn, sid, f'{sid}_t1a'); loop.settle(self.conn, seg, 31 * 60_000)
        script = Script()
        _, st = await self.execute(script, sid)
        self.assertEqual((st['outcome'], st['last_author_unreviewed'], script.calls), ('budget_exhausted', 0, []))

    async def test_pause_waits_for_the_step_boundary_then_resumes(self):
        sid = self.dispatch()
        script = Script([review('pass')], delay=0.4)
        runner = asyncio.create_task(self.execute(script, sid))
        await script.started.wait()
        self.assertTrue(loop.request_pause(self.conn, sid))                    # author is running
        status, st = await runner
        self.assertEqual((status, st['pause_revision']), ('PAUSED', 1))
        self.assertEqual(self.roles(sid), [('author', 'COMPLETED'), ('reviewer', 'PENDING')])   # not killed mid-step
        self.assertEqual(len(script.calls), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM workspace_locks').fetchone()[0], 0)
        self.assertEqual(ws.blocker(self.conn, self.wid), 'paused')
        current = repo.get_session(self.conn, sid)
        from app.handoff import snapshot
        now = snapshot(current['workspace_path'], excluded=snapshot_exclusions(self.db))   # canonical path, same scope
        self.assertFalse(loop.has_changes(loop.changes_since_pause(self.conn, st['pause_snapshot_id'], now)))
        loop.accept_resume(self.conn, sid, 1, loop.change_digest(self.conn, st['pause_snapshot_id'], now), False)
        status, st = await self.execute(Script([review('pass')]), sid)
        self.assertEqual((status, st['outcome']), ('SUCCEEDED', 'verified'))
        refs = self.conn.execute("SELECT count(*) FROM session_events WHERE session_id=? AND kind='resume_decision'", (sid,)).fetchone()[0]
        self.assertEqual(refs, 1)                                                # resuming is not a second decision

    async def test_resume_is_rejected_if_the_folder_changed_after_confirmation(self):
        sid = self.dispatch()
        script = Script(delay=0.3)
        runner = asyncio.create_task(self.execute(script, sid))
        await script.started.wait(); loop.request_pause(self.conn, sid)
        _, st = await runner
        from app.handoff import snapshot
        (self.ws_dir / 'edit.txt').write_text('dış değişiklik')
        seen = snapshot(repo.get_session(self.conn, sid)['workspace_path'], excluded=snapshot_exclusions(self.db))
        self.assertTrue(loop.has_changes(loop.changes_since_pause(self.conn, st['pause_snapshot_id'], seen)))
        loop.accept_resume(self.conn, sid, 1, loop.change_digest(self.conn, st['pause_snapshot_id'], seen), True)
        (self.ws_dir / 'edit.txt').write_text('yine değişti')                   # after the user confirmed
        status, st = await self.execute(Script(), sid)
        self.assertEqual((status, st['pause_revision']), ('PAUSED', 2))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM session_events WHERE session_id=? ORDER BY seq", (sid,))]
        self.assertIn('resume_rejected', kinds)

    async def test_terminal_outcome_beats_a_late_pause_request(self):
        sid = self.dispatch()
        script = Script([review('pass')], delay=0.3)
        runner = asyncio.create_task(self.execute(script, sid))
        await self.wait_for_step(sid, role='reviewer', status='RUNNING')          # reviewer running (last step)
        loop.request_pause(self.conn, sid)
        status, st = await runner
        self.assertEqual((status, st['outcome'], st['pause_requested_at']), ('SUCCEEDED', 'verified', None))

    async def test_retry_inside_a_turn_keeps_turn_and_budget(self):
        sid = self.dispatch()
        status, st = await self.execute(Script(reviewer_errors=['QUOTA_EXCEEDED']), sid)
        self.assertEqual(status, 'WAITING_CAPACITY')
        used = loop.used_ms(self.conn, sid)
        repo.retry_session(self.conn, sid)
        status, st = await self.execute(Script([review('pass')]), sid)
        self.assertEqual((status, st['outcome'], st['current_turn']), ('SUCCEEDED', 'verified', 1))
        self.assertGreaterEqual(loop.used_ms(self.conn, sid), used)             # not reset by the retry
        self.assertEqual(len(loop.turns(self.conn, sid)), 1)

    async def test_next_message_history_uses_the_last_author_step(self):
        sid = self.dispatch()
        await self.execute(Script([review('changes_requested', 'X'), review('pass')], writes=lambda n: str(n)), sid)
        runs = ws.previous_runs(self.conn, self.wid, 99)
        last_author = [s for s in repo.get_session_steps(self.conn, sid) if s['role'] == 'author'][-1]
        self.assertEqual(runs[0]['author_result'], last_author['result_text'])
        self.assertIn('"verdict": "pass"', runs[0]['review'])

    async def test_version_2_policy_still_runs_the_legacy_three_steps(self):
        legacy = normalize({'roles': {'author': ['codex'], 'reviewer': ['claude']}})
        sid = self.dispatch(policy=legacy)
        self.assertIsNone(loop.state(self.conn, sid))
        status, _ = await self.execute(Script(), sid)
        self.assertEqual((status, [r for r, _ in self.roles(sid)]), ('SUCCEEDED', ['author', 'reviewer', 'author']))


if __name__ == '__main__':
    unittest.main()
