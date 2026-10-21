import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { canCommand, canInterrupt, mobile } from '../src/mobile.ts'

// TB-12 (Codex review P2): the gateway refuses these anyway, but a phone must not even show them.
// Each control button must sit in a JSX expression that starts with `!mobile.active&&` — except K-027's three
// interrupt buttons, which start with `canInterrupt()&&` (a phone shows them only with the `interrupt` scope).
// Confirmation modals are exempt: they open only from an already hidden button.
const CONTROL = /<button[^>]*onClick=\{\(\)=>(void )?(retry|cancel|cancelWait|cancelQueued|pause|resume|setConfirmRecover|setConfirmContinue)\(/g

test('control buttons are hidden on a phone', () => {
  for (const file of ['work/SessionView.tsx', 'work/LoopView.tsx', 'work/WorkView.tsx']) {
    const src = readFileSync(new URL(`../src/components/${file}`, import.meta.url), 'utf8')
    for (const m of src.matchAll(CONTROL)) {
      const before = src.slice(0, m.index)
      if (before.lastIndexOf('<Modal') > before.lastIndexOf('</Modal>')) continue
      const line = before.split('\n').length
      const guard = ['cancel', 'cancelWait', 'cancelQueued'].includes(m[2]) ? /^\{canInterrupt\(\)&&/ : /^\{!mobile\.active&&/
      assert.match(before.slice(before.lastIndexOf('{')), guard, `${file}:${line} ${m[2]}`)
    }
  }
})

// TB-20 (Codex review P3): the preview drawer and GitHub are Mac-only; K-033: Settings and “Yeni çalışma” need `command`.
test('settings and preview buttons are hidden on a phone', () => {
  for (const [file, label, guard] of [['components/WorkPanel.tsx', 'Yeni çalışma<', 'canCommand()'],
    ['components/work/WorkView.tsx', "'Önizleme'", '!mobile.active'], ['components/work/WorkView.tsx', '>GitHub<', '!mobile.active'], ['components/work/WorkView.tsx', '>Ana klasöre getir<', '!mobile.active'], ['components/work/WorkView.tsx', '>Testler<', '!mobile.active']] as const) {   // TB-21, TB-26
    const src = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8')
    const at = src.indexOf(label); assert.ok(at > 0, `${file}: ${label} not found`)
    const before = src.slice(0, src.lastIndexOf('<button', at))
    assert.ok(before.slice(before.lastIndexOf('{')).replace(/\s/g, '').startsWith(`{${guard}&&`), `${file} ${label}`)
  }
})

test('K-033: giving work needs the command scope on a phone, never on the Mac', () => {
  const saved = { ...mobile }
  try {
    Object.assign(mobile, { active: false, scopes: [] }); assert.equal(canCommand(), true)
    Object.assign(mobile, { active: true, scopes: ['view', 'approve', 'interrupt'] }); assert.equal(canCommand(), false)
    Object.assign(mobile, { active: true, scopes: ['view', 'command'] }); assert.equal(canCommand(), true)
  } finally { Object.assign(mobile, saved) }
})

test('K-027: interrupt buttons need the scope on a phone, never on the Mac', () => {
  const saved = { ...mobile }
  try {
    Object.assign(mobile, { active: false, scopes: [] }); assert.equal(canInterrupt(), true)
    Object.assign(mobile, { active: true, scopes: ['view', 'approve'] }); assert.equal(canInterrupt(), false)
    Object.assign(mobile, { active: true, scopes: ['view', 'approve', 'interrupt'] }); assert.equal(canInterrupt(), true)
  } finally { Object.assign(mobile, saved) }
})
