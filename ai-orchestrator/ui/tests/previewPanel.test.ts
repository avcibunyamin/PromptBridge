import { test } from 'node:test'
import assert from 'node:assert/strict'
import { PREVIEW_IFRAME_SANDBOX } from '../src/components/work/previewRules.ts'

test('preview iframe sandbox stays narrow', () => {
  const flags = PREVIEW_IFRAME_SANDBOX.split(/\s+/).sort()
  assert.deepEqual(flags, ['allow-forms', 'allow-scripts'])
  assert.equal(flags.includes('allow-same-origin'), false)
  assert.equal(flags.includes('allow-top-navigation'), false)
})
