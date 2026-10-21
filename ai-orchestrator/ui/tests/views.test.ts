// K-040: page addresses survive a reload and back/forward.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { HASHES, initialView, viewAfterHashChange, viewOf } from '../src/views.ts'

test('K-040: every page has its own address and a work link opens the work', () => {
  for (const [view, hash] of Object.entries(HASHES)) assert.equal(viewOf(hash), view)
  assert.equal(viewOf('#/works/abc'), 'work'); assert.equal(viewOf('#/sessions/x'), 'work'); assert.equal(viewOf('#/new'), 'work')
  assert.equal(viewOf('#/'), 'work'); assert.equal(viewOf(''), null); assert.equal(viewOf('#/elsewhere'), null)
})

test('K-040: the Mac opens on the overview, a phone on its list, a link on its page', () => {
  assert.equal(initialView('', false), 'overview'); assert.equal(initialView('#/', false), 'work')   // a reload on Çalışmalar stays there
  assert.equal(initialView('', true), 'work')
  assert.equal(initialView('#/works/abc', false), 'work')
  assert.equal(initialView('#/settings', true), 'settings')
})

test('K-040: back to the opening address shows the landing page; back to #/ shows Çalışmalar', () => {
  assert.equal(viewAfterHashChange('', false), 'overview'); assert.equal(viewAfterHashChange('', true), 'work')
  assert.equal(viewAfterHashChange('#/', false), 'work'); assert.equal(viewAfterHashChange('#/', true), 'work')
  assert.equal(viewAfterHashChange('#/approvals', false), 'bridge')
})
