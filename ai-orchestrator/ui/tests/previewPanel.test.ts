import { test } from 'node:test'
import assert from 'node:assert/strict'
import { PREVIEW_IFRAME_SANDBOX } from '../src/components/work/previewRules.ts'

test('preview iframe sandbox stays narrow', () => {
  const flags = PREVIEW_IFRAME_SANDBOX.split(/\s+/).sort()
  // K-066: same-origin for the preview's own server; the panel's frame-ancestors 'none' keeps it from escaping.
  assert.deepEqual(flags, ['allow-forms', 'allow-same-origin', 'allow-scripts'])
  assert.equal(flags.includes('allow-top-navigation'), false)
})
