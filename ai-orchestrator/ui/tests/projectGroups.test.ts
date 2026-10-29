// K-055: the sidebar groups works by project; an isolated copy belongs to its source folder.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { groupByProject } from '../src/settingsRules.ts'

test('groupByProject keys isolated copies by their source and keeps first-appearance order', () => {
  const works = [
    { id: 'a', workspace_path: '/w/copies/app-1', isolation: { source: '/p/App' } },
    { id: 'b', workspace_path: '/p/Other', isolation: null },
    { id: 'c', workspace_path: '/w/copies/app-2', isolation: { source: '/p/App' } },
    { id: 'd', workspace_path: '/p/App' },
  ]
  const groups = groupByProject(works)
  assert.deepEqual(groups.map(g => [g.name, g.works.map(w => w.id)]), [['App', ['a', 'c', 'd']], ['Other', ['b']]])
  assert.equal(groupByProject([{ workspace_path: '/' }])[0].name, '/')
})
