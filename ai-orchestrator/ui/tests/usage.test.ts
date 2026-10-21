// K-039: context fill and quota group of a chain link.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { contextOf, lastContext, lowest, percent, quotaFor, quotaGroupKey, resetText, tokens, tone, type QuotaSnapshot } from '../src/components/work/usage.ts'

const snap: QuotaSnapshot = { running: [], accounts: {
  'agy@h1': { error: null, groups: [
    { key: 'gemini', title: 'Gemini Models', models: null, windows: [{ label: 'W', kind: 'weekly', remaining: 99.4 }, { label: 'F', kind: 'five_hour', remaining: 12 }] },
    { key: 'claude_gpt', title: 'Claude And Gpt Models', models: null, windows: [{ label: 'W', kind: 'weekly', remaining: 100 }] },
    { key: 'argon', title: 'Gemini 4 Argon', models: null, windows: [{ label: 'W', kind: 'weekly', remaining: 0 }] }] },
  codex: { error: null, groups: [{ key: 'codex', title: 'Codex', models: null, windows: [{ label: '30 gün', kind: 'other', remaining: 63 }] }] },
} }

test('K-039: each agy model maps to its own quota group; Claude and Codex to theirs', () => {
  assert.equal(quotaGroupKey('agy@h1/gemini-4-argon-high'), 'argon')
  assert.equal(quotaGroupKey('agy@h1/gemini-3.8-flash-low'), 'gemini')
  assert.equal(quotaGroupKey('agy@h1/claude-opus-5-5-high'), 'claude_gpt')
  assert.equal(quotaGroupKey('agy@h1/gpt-oss-120b-medium'), 'claude_gpt')
  assert.equal(quotaGroupKey('claude/opus~high'), 'claude')
  assert.equal(quotaGroupKey('ollama/qwen'), null)
  assert.equal(quotaFor(snap, 'agy@h1/gemini-3.8-flash-high')?.group?.key, 'gemini')
  assert.equal(lowest(quotaFor(snap, 'agy@h1/gemini-3.8-flash-high')?.group), 12)          // the window that runs out first
  assert.equal(tone(lowest(quotaFor(snap, 'agy@h1/gemini-4-argon-low')?.group)), 'bad')
  assert.equal(tone(12), 'warn'); assert.equal(tone(63), 'ok'); assert.equal(tone(null), 'idle')
  assert.equal(quotaFor(snap, 'agy@h2/gemini-3.8-flash-high'), null)                      // another account: not read yet
  assert.equal(quotaFor(snap, 'codex/gpt-6-luna~high')?.group?.key, 'codex')
  assert.equal(quotaFor(null, 'codex'), null)
})

test('K-039: context from each provider\'s usage; agy uses the known window and says so', () => {
  const claude = JSON.stringify({ input_tokens: 4, cache_read_input_tokens: 13261, cache_creation_input_tokens: 1468, output_tokens: 517, context_window: 200000 })
  assert.deepEqual(contextOf(claude, 'claude/sonnet'), { used: 15250, window: 200000, approx: false })
  assert.equal(percent(contextOf(claude, 'claude/sonnet')), 8)
  const agy = JSON.stringify({ input_tokens: 18328, output_tokens: 810, thinking_tokens: 555, cache_read_tokens: 8094, total_tokens: 19138 })
  assert.deepEqual(contextOf(agy, 'agy@h1/gemini-4-argon-high'), { used: 19138, window: 1048576, approx: true })
  assert.deepEqual(contextOf(agy, 'agy@h1/mystery-model'), { used: 19138, window: null, approx: true })
  assert.equal(percent(contextOf(agy, 'agy@h1/mystery-model')), null)
  const codex = JSON.stringify({ total: { inputTokens: 900000 }, last: { inputTokens: 52000, outputTokens: 1000 }, modelContextWindow: 258400 })
  assert.deepEqual(contextOf(codex, 'codex/gpt-6-luna'), { used: 53000, window: 258400, approx: false })
  assert.deepEqual(contextOf(JSON.stringify({ input_tokens: 10 }), 'claude'), { used: 10, window: 200000, approx: true })   // default model
  assert.equal(contextOf(JSON.stringify({ input_tokens: 10 }), 'codex')?.window, null)
  assert.equal(contextOf('null', 'claude'), null); assert.equal(contextOf('{bad', 'claude'), null); assert.equal(contextOf(null, 'claude'), null)
  assert.equal(percent({ used: 900, window: 100, approx: false }), 100)
})

test('K-039: the context is the link\'s own newest step, effort ignored', () => {
  const steps = [
    { agent_name: 'agy@h1/gemini-4-argon-high', usage_info: JSON.stringify({ input_tokens: 1000 }) },
    { agent_name: 'claude/sonnet~low', usage_info: JSON.stringify({ input_tokens: 10, context_window: 200000 }) },
    { agent_name: 'agy@h1/gemini-4-argon-high', usage_info: JSON.stringify({ input_tokens: 5000 }) },
    { agent_name: 'claude/sonnet~high', usage_info: JSON.stringify({ input_tokens: 20, context_window: 200000 }) },
  ]
  assert.equal(lastContext(steps, 'agy@h1/gemini-4-argon-high')?.used, 5000)
  assert.equal(lastContext(steps, 'claude/sonnet~max')?.used, 20)
  assert.equal(lastContext(steps, 'agy@h2/gemini-4-argon-high'), null)                     // another account: a new context
  // A step that has not run yet (no usage) does not hide the one before it.
  assert.equal(lastContext([...steps, { agent_name: 'agy@h1/gemini-4-argon-high', usage_info: null }], 'agy@h1/gemini-4-argon-high')?.used, 5000)
  assert.equal(contextOf(JSON.stringify({ input_tokens: 10 }), 'claude/sonnet~high')?.window, 200000)   // a Claude alias
})

test('K-039: tokens and reset times read naturally', () => {
  assert.equal(tokens(512), '512'); assert.equal(tokens(15250), '15k'); assert.equal(tokens(1048576), '1.0M'); assert.equal(tokens(2_000_000), '2M')
  assert.equal(resetText({ label: '', kind: 'weekly', remaining: 1, resets_in: '167h 25m' }), '6 gün 23 sa')
  assert.equal(resetText({ label: '', kind: 'five_hour', remaining: 1, resets_in: '4h 25m' }), '4 sa 25 dk')
  assert.equal(resetText({ label: '', kind: 'five_hour', remaining: 1, resets_in: '7m' }), '7 dk')
  assert.equal(resetText({ label: '', kind: 'other', remaining: 1, resets_at: 1000 + 90 * 60 }, 1000), '1 sa 30 dk')
  assert.equal(resetText({ label: '', kind: 'other', remaining: 1 }), null)
})
