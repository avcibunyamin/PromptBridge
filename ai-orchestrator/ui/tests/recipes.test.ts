import { test } from 'node:test'
import assert from 'node:assert/strict'
import { RECIPES } from '../src/recipes.ts'
import { matchSlash, slashQuery } from '../src/components/work/slash.ts'

const cmds = [
  { name: 'model', hint: 'Yazarın modelini seç' },
  { name: 'efor', hint: 'Yazarın eforunu seç' },
  { name: 'kota', hint: 'Context ve kota penceresi' },
  { name: 'takim', hint: 'Gelişmiş: yedekler ve inceleyici' },
  ...RECIPES.map(r => ({ name: 'tarif-' + r.id, hint: 'Tarif: ' + r.title })),
  { name: 'yeni', hint: 'Yeni çalışma' },
]

test('TB-45: /tarif lists all four recipes in order', () => {
  const q = slashQuery('/tarif')
  assert.equal(q, 'tarif')
  assert.deepEqual(matchSlash(cmds, q!).map(c => c.name), [
    'tarif-guvenlik',
    'tarif-hata',
    'tarif-mimari',
    'tarif-arayuz',
  ])
})

test('TB-45: /guv and /güv match tarif-guvenlik via matchSlash', () => {
  assert.deepEqual(matchSlash(cmds, slashQuery('/guv')!).map(c => c.name), ['tarif-guvenlik'])
  assert.deepEqual(matchSlash(cmds, slashQuery('/güv')!).map(c => c.name), ['tarif-guvenlik'])
})
