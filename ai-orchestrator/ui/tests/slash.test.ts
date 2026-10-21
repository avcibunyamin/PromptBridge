// K-041: `/` shortcuts open only while the text is one `/word`, and match by prefix first.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fold, matchSlash, slashQuery } from '../src/components/work/slash.ts'

const cmds = [{ name: 'model', hint: '' }, { name: 'efor', hint: '' }, { name: 'kota', hint: '' }, { name: 'takim', hint: '' }, { name: 'onizleme', hint: '' }]

test('K-041: the menu is open only for a lone /word', () => {
  assert.equal(slashQuery('/'), ''); assert.equal(slashQuery('/Mo'), 'mo'); assert.equal(slashQuery('/önizleme'), 'onizleme')
  assert.equal(slashQuery('/model x'), null); assert.equal(slashQuery('metin /model'), null); assert.equal(slashQuery('/model\n'), null)
  assert.equal(slashQuery(''), null)
})

test('K-041: prefix matches first, then names containing the query', () => {
  assert.deepEqual(matchSlash(cmds, '').map(c => c.name), ['model', 'efor', 'kota', 'takim', 'onizleme'])
  assert.deepEqual(matchSlash(cmds, 'o').map(c => c.name), ['onizleme', 'model', 'efor', 'kota'])
  assert.deepEqual(matchSlash(cmds, 'ef').map(c => c.name), ['efor'])
  assert.deepEqual(matchSlash(cmds, 'zz'), [])
})

test('K-041: Turkish letters and capitals find the ASCII command', () => {
  assert.deepEqual(matchSlash(cmds, slashQuery('/önizleme')!).map(c => c.name), ['onizleme'])
  assert.deepEqual(matchSlash(cmds, slashQuery('/takım')!).map(c => c.name), ['takim'])
  assert.deepEqual(matchSlash(cmds, slashQuery('/TAKIM')!).map(c => c.name), ['takim'])
  assert.equal(fold('Çğüşİı'), 'cgusii')
})
