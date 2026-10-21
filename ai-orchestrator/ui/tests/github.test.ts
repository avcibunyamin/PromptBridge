import { test } from 'node:test'
import assert from 'node:assert/strict'
import { defaultSelection, filterRepos, needsSecretAck, statusLabel, type GhFile, type GhRepo } from '../src/githubRules.ts'

const file = (path: string, secret = false, workflow = false): GhFile => ({ path, status: 'M', secret, workflow })

test('K3: secret-looking files are listed but not preselected', () => {
  const files = [file('app.py'), file('.env', true), file('.github/workflows/ci.yml', false, true)]
  assert.deepEqual(defaultSelection(files), ['app.py', '.github/workflows/ci.yml'])
  assert.equal(needsSecretAck(files, defaultSelection(files)), false)
  assert.equal(needsSecretAck(files, ['app.py', '.env']), true)
})

test('repository search ignores case and puts archived ones last', () => {
  const repo = (nameWithOwner: string, isArchived = false): GhRepo => ({ nameWithOwner, isArchived, isPrivate: false, updatedAt: '' })
  const items = [repo('octo/Old', true), repo('octo/demo'), repo('other/thing')]
  assert.deepEqual(filterRepos(items, 'OCTO').map(r => r.nameWithOwner), ['octo/demo', 'octo/Old'])
  assert.deepEqual(filterRepos(items, '').map(r => r.nameWithOwner), ['octo/demo', 'other/thing', 'octo/Old'])
})

test('status codes read as words', () => {
  assert.deepEqual(['??', 'M', ' M', 'MM', 'A', 'D', 'R', 'X'].map(statusLabel), ['yeni', 'değişti', 'değişti', 'değişti', 'eklendi', 'silindi', 'yeniden adlandırıldı', 'X'])
})
