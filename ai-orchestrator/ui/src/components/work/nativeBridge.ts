type BridgeWindow = {
  webkit?: { messageHandlers?: { selectFolder?: { postMessage: (body: unknown) => void } } }
}

type EventTargetLike = {
  addEventListener: (name: string, listener: (event: CustomEvent<string>) => void) => void
  removeEventListener: (name: string, listener: (event: CustomEvent<string>) => void) => void
}

function asBridgeWindow(w: unknown): BridgeWindow {
  return (w && typeof w === 'object') ? w as BridgeWindow : {}
}

export function hasNativeFolderPicker(w: unknown): boolean {
  const bridge = asBridgeWindow(w)
  return typeof bridge.webkit?.messageHandlers?.selectFolder?.postMessage === 'function'
}

export function requestNativeFolder(w: unknown) {
  asBridgeWindow(w).webkit?.messageHandlers?.selectFolder?.postMessage('open')
}

export function installFolderSelectedListener(target: EventTargetLike, onSelected: (path: string) => void) {
  const listener = (event: CustomEvent<string>) => { onSelected(event.detail) }
  target.addEventListener('folderSelected', listener)
  return () => target.removeEventListener('folderSelected', listener)
}
