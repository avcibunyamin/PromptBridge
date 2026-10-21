// TB-37 (K-044): pinned work ordering, pin toggling bounds, sidebar menu visibility, and delete error messages.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  deleteWorkErrorMessage,
  PINNED_WORKS_LIMIT,
  sortPinnedWorks,
  togglePinnedWork,
  workMenuItems,
} from '../src/settingsRules.ts'

test('sortPinnedWorks moves pinned works to the top while keeping relative order', () => {
  const works = [{ id: 'w1', title: 'Bir' }, { id: 'w2', title: 'İki' }, { id: 'w3', title: 'Üç' }, { id: 'w4', title: 'Dört' }]
  assert.deepEqual(sortPinnedWorks(works, ['w4', 'w2']).map(w => w.id), ['w2', 'w4', 'w1', 'w3'])
  assert.deepEqual(sortPinnedWorks(works, []).map(w => w.id), ['w1', 'w2', 'w3', 'w4'])
  assert.deepEqual(sortPinnedWorks(works, ['missing']).map(w => w.id), ['w1', 'w2', 'w3', 'w4'])
})

test('togglePinnedWork adds, removes, deduplicates and respects the 20-item limit', () => {
  assert.deepEqual(togglePinnedWork([], 'w1'), ['w1'])
  assert.deepEqual(togglePinnedWork(['w1', 'w2'], 'w1'), ['w2'])
  const full = Array.from({ length: PINNED_WORKS_LIMIT }, (_, i) => `w-${i}`)
  assert.equal(togglePinnedWork(full, 'w-extra').length, PINNED_WORKS_LIMIT)
  assert.equal(togglePinnedWork(full, 'w-0').length, PINNED_WORKS_LIMIT - 1)
})

test('workMenuItems respects phone mode, canCommand, archive state and isolation rules', () => {
  const activeWork = { archived_at: null, isolation: { removed: false } }
  const archivedIsolated = { archived_at: '2026-10-18T10:00:00Z', isolation: { removed: false } }
  const archivedRemovedIso = { archived_at: '2026-10-18T10:00:00Z', isolation: { removed: true } }

  // No command permission -> menu hidden entirely
  assert.deepEqual(workMenuItems(activeWork, { pinned: false, mobile: false, canCommand: false }), [])
  assert.deepEqual(workMenuItems(activeWork, { pinned: false, mobile: true, canCommand: false }), [])

  // Phone with command permission -> only Pin/Unpin and Rename
  assert.deepEqual(workMenuItems(activeWork, { pinned: false, mobile: true, canCommand: true }), ['pin', 'rename'])
  assert.deepEqual(workMenuItems(archivedIsolated, { pinned: true, mobile: true, canCommand: true }), ['unpin', 'rename'])

  // Desktop active work (isolation removal requires archived)
  assert.deepEqual(
    workMenuItems(activeWork, { pinned: false, mobile: false, canCommand: true }),
    ['pin', 'rename', 'archive', 'delete'],
  )

  // Desktop archived isolated work with unremoved copy -> includes remove_isolation
  assert.deepEqual(
    workMenuItems(archivedIsolated, { pinned: true, mobile: false, canCommand: true }),
    ['unpin', 'rename', 'unarchive', 'remove_isolation', 'delete'],
  )

  // Desktop archived isolated work whose copy is already removed -> omits remove_isolation
  assert.deepEqual(
    workMenuItems(archivedRemovedIso, { pinned: false, mobile: false, canCommand: true }),
    ['pin', 'rename', 'unarchive', 'delete'],
  )
})

test('deleteWorkErrorMessage returns Turkish warnings and extracts nested detail messages', () => {
  const msg = deleteWorkErrorMessage({ status: 409, code: 'work_active', message: 'work has an active run' })
  assert.match(msg, /yürütme/)
  const nested = deleteWorkErrorMessage({ status: 409, body: { detail: { code: 'work_active' } } })
  assert.match(nested, /yürütme/)
  const rev = deleteWorkErrorMessage({ status: 409, message: 'Conflict', body: { detail: { code: 'revision_conflict' } } })
  assert.match(rev, /güncellendi/)
  const nf = deleteWorkErrorMessage({ status: 404, message: 'Not Found' })
  assert.match(nf, /bulunamadı/)
  const mf = deleteWorkErrorMessage({ status: 403, body: { detail: { code: 'mobile_field' } } })
  assert.match(mf, /telefondan/)
  const custom = deleteWorkErrorMessage({ status: 409, code: 'dirty_worktree', message: 'Conflict', body: { detail: { code: 'dirty_worktree', detail: 'Kopyada kaydedilmemiş değişiklik var.' } } })
  assert.equal(custom, 'Kopyada kaydedilmemiş değişiklik var.')
})
