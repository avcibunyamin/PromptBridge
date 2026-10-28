// TB-10: identities and the provider/account → model → effort options of one chain link.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { agyFamilyLabel, agyGroups, agyName, agyParts, linkMissing, baseOf, covers, effortLabel, joinId, linkOptions, modelLabel, moveLink, splitId, type Cap, type Catalog } from '../src/components/work/team.ts'

const CLAUDE: Cap = { models: true, model_required: false, efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }
const AGY: Cap = { models: true, model_required: true, efforts: [] }
const CODEX: Cap = { models: true, model_required: false, efforts: ['low', 'medium', 'high', 'xhigh', 'max'] }
const caps: Record<string, Cap> = { claude: CLAUDE, agy: AGY, codex: CODEX }
const capOf = (id: string) => caps[splitId(id)?.kind ?? '']
const entry = (identity: string, source: 'live' | 'fallback' = 'live', display_name?: string, default_effort?: string) => {
  const s = splitId(identity)!
  return { identity, provider: s.kind, account: s.account, model: s.model!, display_name, default_effort, efforts: [], status: source === 'live' ? 'available' as const : 'unverified' as const, source }
}
const catalog: Catalog = {
  entries: [entry('claude/claude-opus-5-5', 'fallback', 'Claude Opus 5.5'), entry('claude/opus', 'fallback', 'Opus (latest alias)'), entry('claude/sonnet', 'fallback'),
    entry('agy@h1/gemini-3.1-pro-high'), entry('agy@h1/claude-sonnet-5-5-high'), entry('agy@h2/gemini-3.1-pro-high'),
    { ...entry('codex@g1/gpt-6-sol'), efforts: ['low', 'medium', 'high', 'xhigh', 'max'] },
    { ...entry('codex@g1/gpt-5.5', 'live', 'GPT-5.5', 'low'), efforts: ['low', 'medium', 'high', 'xhigh'] }],
  errors: {}, checked_at: 1, efforts: { claude: CLAUDE.efforts!, agy: [], codex: CODEX.efforts! },
}

test('identities split, join and keep old forms', () => {
  assert.deepEqual(splitId('agy@h1/gemini-3.1-pro-high'), { kind: 'agy', account: 'h1', model: 'gemini-3.1-pro-high', effort: null })
  assert.deepEqual(splitId('claude/opus~high'), { kind: 'claude', account: null, model: 'opus', effort: 'high' })
  assert.deepEqual(splitId('codex'), { kind: 'codex', account: null, model: null, effort: null })
  assert.equal(splitId('claude/opus~'), null)
  assert.equal(baseOf('agy@h1/gemini-3.1-pro-high'), 'agy@h1')
  assert.equal(joinId('claude', 'opus', 'high'), 'claude/opus~high')
  assert.equal(joinId('claude', null, null), 'claude')
})

test('allowed entries without a model or effort are wildcards; the account always matches', () => {
  assert.equal(covers('claude', 'claude/opus~high'), true)
  assert.equal(covers('agy@h1', 'agy@h1/x'), true)
  assert.equal(covers('agy@h1', 'agy@h2/x'), false)
  assert.equal(covers('claude/opus', 'claude/sonnet'), false)
  assert.equal(covers('claude/opus~low', 'claude/opus~high'), false)
  assert.equal(covers('claude~high', 'claude/sonnet~high'), true)
})

test('provider/account → model → effort follows the ceiling and the catalog', () => {
  const o = linkOptions(['claude', 'agy@h1', 'agy@h2/gemini-3.1-pro-high'], catalog, capOf)
  assert.deepEqual(o.bases, ['claude', 'agy@h1', 'agy@h2'])
  assert.deepEqual(o.modelsFor('claude'), ['claude-opus-5-5', 'opus', 'sonnet'])             // explicit ids first, aliases still available
  assert.deepEqual(o.modelsFor('agy@h1'), ['gemini-3.1-pro-high', 'claude-sonnet-5-5-high'])  // live, per account
  assert.deepEqual(o.modelsFor('agy@h2'), ['gemini-3.1-pro-high'])                          // a fixed model does not widen
  assert.deepEqual(o.effortsFor('claude', 'opus'), ['low', 'medium', 'high', 'xhigh', 'max'])
  assert.equal(o.defaultEffortFor('claude', 'opus'), null)                              // Claude does not publish a default
  assert.equal(o.defaultEffortFor('claude', 'sonnet'), null)                            // no guessed default
  assert.equal(o.defaultEffortFor('codex@g1', null), null)                              // no model-level default is known
  assert.equal(o.effortsFor('claude', 'opus').includes('ultra'), false)                     // never offered (TB-10)
  assert.deepEqual(o.effortsFor('agy@h1', 'gemini-3.1-pro-high'), [])                       // agy: effort is in the model name
  const fixed = linkOptions(['claude/opus~high'], catalog, capOf)
  assert.deepEqual(fixed.modelsFor('claude'), ['opus'])
  assert.deepEqual(fixed.effortsFor('claude', 'opus'), ['high'])
  const codex = linkOptions(['codex@g1'], catalog, capOf)
  assert.deepEqual(codex.modelsFor('codex@g1'), ['gpt-6-sol', 'gpt-5.5'])
  assert.deepEqual(codex.effortsFor('codex@g1', 'gpt-6-sol'), ['low', 'medium', 'high', 'xhigh', 'max'])
  assert.deepEqual(codex.effortsFor('codex@g1', 'gpt-5.5'), ['low', 'medium', 'high', 'xhigh'])
  assert.equal(codex.defaultEffortFor('codex@g1', 'gpt-5.5'), 'low')
})

test('labels show readable model names and the default effort explicitly', () => {
  assert.equal(modelLabel(catalog, 'claude', 'claude-opus-5-5'), 'Claude Opus 5.5')
  assert.equal(modelLabel(catalog, 'claude', 'opus'), 'Opus (latest alias)')
  assert.equal(modelLabel(catalog, 'codex@g1', 'gpt-5.5'), 'GPT-5.5')
  assert.equal(modelLabel(null, 'agy@h1', 'gemini-3.1-pro-high'), 'Gemini 3.1 Pro High')
  assert.equal(modelLabel(null, 'codex@g1', 'gpt-5.5'), 'GPT 5.5')
  assert.equal(modelLabel(null, 'claude', null), '(varsayılan model)')
  assert.equal(effortLabel('medium'), 'Medium')
})

test('a model-required tool is not selectable until a model is known', () => {
  const none = linkOptions(['claude', 'agy@h1'], null, capOf)                              // catalog not loaded
  assert.equal(none.ready('agy@h1'), false)
  assert.equal(none.ready('claude'), true)                                                 // Claude runs on its default
  assert.equal(none.firstOf('agy@h1', []), null)                                           // never a bare `agy@h1` link
  assert.equal(none.nextLink(['claude']), null)
})

test('backups prefer an unused account, then another model of a used one (K-022), never a duplicate', () => {
  const o = linkOptions(['claude', 'agy@h1', 'agy@h2'], catalog, capOf)
  assert.equal(o.nextLink(['claude']), 'agy@h1/gemini-3.1-pro-high')
  assert.equal(o.nextLink(['claude', 'agy@h1/gemini-3.1-pro-high']), 'agy@h2/gemini-3.1-pro-high')
  assert.equal(o.nextLink(['claude', 'agy@h1/gemini-3.1-pro-high', 'agy@h2/gemini-3.1-pro-high']), 'claude/claude-opus-5-5')
  const k022 = linkOptions(['agy@h1'], catalog, capOf)
  assert.equal(k022.nextLink(['agy@h1/claude-sonnet-5-5-high']), 'agy@h1/gemini-3.1-pro-high')
  assert.equal(k022.nextLink(['agy@h1/claude-sonnet-5-5-high', 'agy@h1/gemini-3.1-pro-high']), null)
})

test('TB-14: Ollama tags (with a colon) are identities; the model is required and there is no effort', () => {
  assert.deepEqual(splitId('ollama/gpt-oss:20b'), { kind: 'ollama', account: null, model: 'gpt-oss:20b', effort: null })
  const OLLAMA: Cap = { models: true, model_required: true, efforts: [] }
  const local: Catalog = { entries: [entry('ollama/gpt-oss:20b'), entry('ollama/qwen2.5-coder:14b')], errors: {}, checked_at: 1, efforts: { ollama: [] } }
  const o = linkOptions(['ollama'], local, () => OLLAMA)
  assert.deepEqual(o.modelsFor('ollama'), ['gpt-oss:20b', 'qwen2.5-coder:14b'])
  assert.deepEqual(o.effortsFor('ollama', 'gpt-oss:20b'), [])
  assert.equal(o.nextLink([]), 'ollama/gpt-oss:20b')
  assert.equal(linkOptions(['ollama'], null, () => OLLAMA).ready('ollama'), false)   // no catalog yet: not selectable
})

test('TB-14 review: a model without tool support is never an option, default or backup', () => {
  const OLLAMA: Cap = { models: true, model_required: true, efforts: [] }
  const embed = { ...entry('ollama/nomic-embed-text:latest'), status: 'unsupported' as const }
  const only: Catalog = { entries: [embed], errors: {}, checked_at: 1, efforts: { ollama: [] } }
  const o = linkOptions(['claude', 'ollama'], { ...only, entries: [...catalog.entries, embed] }, id => splitId(id)?.kind === 'ollama' ? OLLAMA : CLAUDE)
  assert.deepEqual(o.modelsFor('ollama'), [])
  assert.equal(o.ready('ollama'), false)                                              // not selectable at all
  assert.equal(o.firstOf('ollama', []), null)
  assert.equal(o.nextLink(['claude']), 'claude/claude-opus-5-5')                      // never the unsupported model
  const named = linkOptions(['ollama/nomic-embed-text:latest', 'ollama/gpt-oss:20b'],
    { ...only, entries: [embed, entry('ollama/gpt-oss:20b')] }, () => OLLAMA)
  assert.deepEqual(named.modelsFor('ollama'), ['gpt-oss:20b'])                         // even when the ceiling names it
})

test('K-038: agy names split into family + effort and join back to the same name', () => {
  const names = ['gemini-4-argon-medium', 'gemini-4-argon-high', 'gemini-4-argon-low', 'gemini-3.8-flash-high', 'gpt-oss-120b-medium', 'plain-model']
  const groups = agyGroups(names)
  assert.deepEqual([...groups.keys()], ['gemini-4-argon', 'gemini-3.8-flash', 'gpt-oss-120b', 'plain-model'])
  assert.deepEqual(groups.get('gemini-4-argon'), ['low', 'medium', 'high'])
  assert.deepEqual(groups.get('plain-model'), [])
  for (const n of names) { const p = agyParts(n); assert.equal(agyName(p.family, p.effort), n) }
  const live: Catalog = { entries: [entry('agy@h1/gemini-4-argon-high', 'live', 'Gemini 4 Argon (High)')], errors: {}, checked_at: 1, efforts: { agy: [] } }
  assert.equal(agyFamilyLabel(live, 'agy@h1', 'gemini-4-argon', ['high']), 'Gemini 4 Argon')
  assert.equal(agyFamilyLabel(null, 'agy@h1', 'gemini-3.8-flash', ['high']), 'Gemini 3.8 Flash')
  assert.equal(agyFamilyLabel(null, 'agy@h1', 'plain-model', []), 'Plain Model')
})

test('K-041: every link names a model, and an effort when its model offers efforts', () => {
  const o = linkOptions(['claude', 'agy@h1', 'codex@g1'], catalog, capOf)
  assert.equal(linkMissing('claude', o), 'model')
  assert.equal(linkMissing('claude/claude-opus-5-5', o), 'effort')
  assert.equal(linkMissing('claude/claude-opus-5-5~high', o), null)
  assert.equal(linkMissing('agy@h1/gemini-3.1-pro-high', o), null)          // agy: the effort is part of the name
  const unknown = linkOptions(['codex@zz'], { ...catalog, entries: [] }, capOf)
  assert.equal(linkMissing('codex@zz', unknown), null)                         // no list yet: nothing to choose, not blocked
})

test('K-041 review: a live list without efforts asks for none; backups and switches always name a model', () => {
  const live: Catalog = { entries: [...catalog.entries, { ...entry('codex@g1/plain-model'), efforts: [] }], errors: {}, checked_at: 1, efforts: { codex: ['low', 'high'] } }
  const o = linkOptions(['claude', 'codex@g1'], live, capOf)
  assert.deepEqual(o.effortsFor('codex@g1', 'plain-model'), [])                       // not the static Codex efforts
  assert.equal(linkMissing('codex@g1/plain-model', o), null)
  assert.deepEqual(o.effortsFor('claude', null), [])                                  // no effort without a model
  assert.equal(splitId(o.firstOf('claude', [])!)?.model, 'claude-opus-5-5')            // never the bare tool
  assert.notEqual(splitId(o.firstOf('claude', ['claude/claude-opus-5-5~high'])!)?.model, 'claude-opus-5-5')   // used model skipped, any effort
  const agy = linkOptions(['agy@h1'], { ...catalog, entries: [entry('agy@h1/foo'), entry('agy@h1/foo-high')] }, capOf)
  assert.equal(linkMissing('agy@h1/foo', agy), 'effort')                              // a family with efforts needs one
  assert.equal(linkMissing('agy@h1/foo-high', agy), null)
})

test('moveLink reorders links cleanly and never mutates the input chain', () => {
  const input = ['a', 'b', 'c']
  assert.deepEqual(moveLink(input, 2, 0), ['c', 'a', 'b'])
  assert.deepEqual(moveLink(input, 0, 2), ['b', 'c', 'a'])
  const same = moveLink(input, 1, 1)
  assert.deepEqual(same, ['a', 'b', 'c'])
  assert.notEqual(same, input)
  for (const [from, to] of [[-1, 0], [0, -1], [3, 0], [0, 3], [-1, 5]]) {
    const copy = moveLink(input, from, to)
    assert.deepEqual(copy, ['a', 'b', 'c'])
    assert.notEqual(copy, input)
  }
  assert.deepEqual(input, ['a', 'b', 'c'])
})
