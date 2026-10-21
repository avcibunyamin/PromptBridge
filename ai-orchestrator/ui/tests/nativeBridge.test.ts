import { test } from 'node:test'
import assert from 'node:assert/strict'
import { hasNativeFolderPicker, installFolderSelectedListener, requestNativeFolder } from '../src/components/work/nativeBridge.ts'

test('native folder picker is available only when the WebKit handler exists', () => {
  assert.equal(hasNativeFolderPicker({}), false)
  assert.equal(hasNativeFolderPicker({ webkit: { messageHandlers: {} } }), false)
  assert.equal(hasNativeFolderPicker({ webkit: { messageHandlers: { selectFolder: { postMessage: () => {} } } } }), true)
})

test('requestNativeFolder posts the expected narrow message', () => {
  const calls: unknown[] = []
  requestNativeFolder({ webkit: { messageHandlers: { selectFolder: { postMessage: body => calls.push(body) } } } })
  assert.deepEqual(calls, ['open'])
})

test('folderSelected listener updates and is always removable', () => {
  const listeners = new Map<string, (event: CustomEvent<string>) => void>()
  const target = {
    addEventListener: (name: string, listener: (event: CustomEvent<string>) => void) => { listeners.set(name, listener) },
    removeEventListener: (name: string, listener: (event: CustomEvent<string>) => void) => {
      if (listeners.get(name) === listener) listeners.delete(name)
    },
  }
  const selected: string[] = []
  const cleanup = installFolderSelectedListener(target, path => selected.push(path))
  listeners.get('folderSelected')?.({ detail: '/tmp/project' } as CustomEvent<string>)
  cleanup()
  assert.deepEqual(selected, ['/tmp/project'])
  assert.equal(listeners.has('folderSelected'), false)
})
