// K-062: the chat always says whether something runs and, if not, how the last run ended.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { canResume, finalText, reasonKind, workState } from '../src/components/work/workStatus.ts'

test('states', () => {
  const s = (latestStatus: string | null, pendingMessages = 0) => workState({ latestStatus, pendingMessages })
  assert.deepEqual([s('RUNNING'), s('QUEUED'), s('WAITING_AUTH'), s('PAUSED'), s('SUCCEEDED'), s('FAILED'), s('CANCELLED'), s(null)],
    ['running', 'queued', 'waiting', 'waiting', 'done', 'failed', 'stopped', 'empty'])
  assert.equal(s('FAILED', 1), 'message_queued')   // a new message is waiting for the queue
  assert.equal(s('RUNNING', 2), 'running')
  assert.deepEqual([canResume('failed'), canResume('stopped'), canResume('done'), canResume('running')], [true, true, false, false])
})

test('reasons in plain words', () => {
  assert.deepEqual(['timeout', 'cancelled', 'auth', 'error', 'something new'].map(reasonKind), ['timeout', 'cancelled', 'auth', 'error', 'error'])
  assert.equal(reasonKind(null), null)
})

test('final answer is the last completed author report', () => {
  const st = (step_index: number, role: string, status: string, result_text: string | null) => ({ step_index, role, status, result_text })
  assert.equal(finalText([st(0, 'author', 'COMPLETED', 'first'), st(1, 'reviewer', 'COMPLETED', 'review'), st(2, 'author', 'COMPLETED', ' fixed ')]), 'fixed')
  assert.equal(finalText([st(0, 'author', 'COMPLETED', 'first'), st(2, 'author', 'FAILED', 'broken'), st(3, 'author', 'COMPLETED', '  ')]), 'first')
  assert.equal(finalText([st(1, 'reviewer', 'COMPLETED', 'only review')]), '')
})
