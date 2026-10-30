// K-059: the running execution's one-line status.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { elapsedMinutes, foldLive, lastLine, providerLabel } from '../src/components/work/live.ts'

test('folds attempt, output and command events into one line', () => {
  let s = foldLive(null, { kind: 'output_delta', payload: { text: 'orphan' } })
  assert.equal(s, null)   // nothing before an attempt starts
  s = foldLive(s, { kind: 'attempt_started', at: '2026-10-29T20:00:00Z', payload: { role: 'author', provider: 'agy@h1/gemini-4-argon-high' } })
  assert.deepEqual([s?.role, s?.provider, s?.since], ['author', 'agy@h1/gemini-4-argon-high', Date.parse('2026-10-29T20:00:00Z')])
  s = foldLive(s, { kind: 'output_delta', payload: { text: 'Reading the files.\nEditing ' } })
  s = foldLive(s, { kind: 'output_delta', payload: { text: 'src/app.ts   now\n\n' } })
  assert.deepEqual([s?.kind, s?.last], ['output', 'Editing src/app.ts now'])
  s = foldLive(s, { kind: 'command_result', payload: { command: 'npm test' } })
  assert.deepEqual([s?.kind, s?.last], ['command', 'npm test'])
  s = foldLive(s, { kind: 'activity', payload: { tool: 'run_command', detail: 'npm test' } })   // K-061: agy tool steps
  assert.deepEqual([s?.kind, s?.last], ['activity', 'run_command · npm test'])
  s = foldLive(s, { kind: 'attempt_started', payload: { role: 'reviewer', provider: 'codex' } })
  assert.deepEqual([s?.role, s?.last, s?.kind], ['reviewer', '', null])   // a new attempt starts clean
})

test('long lines keep their end; labels and minutes', () => {
  const line = lastLine('x'.repeat(300) + 'END')
  assert.equal(line.length, 140); assert.ok(line.startsWith('…') && line.endsWith('END'))
  assert.equal(providerLabel('agy@h1/gemini-4-argon-high'), 'agy@h1 · gemini-4-argon-high')
  assert.equal(elapsedMinutes(0, 179_000), 2); assert.equal(elapsedMinutes(10, 0), 0)
})
