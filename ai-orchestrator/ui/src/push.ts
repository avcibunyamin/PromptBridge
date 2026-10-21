/** TB-29 (K-036): the phone's notifications. The browser makes a Web Push subscription for this Mac's public key;
 *  the Mac keeps it with this device and sends notices through the browser's push service. */
import { checkedFetch } from './api'

export type PushState = 'unsupported' | 'denied' | 'on' | 'off'

const send = (method: string, body?: unknown): RequestInit =>
  ({ method, headers: { 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })

function keyBytes(b64url: string): Uint8Array {
  const b64 = b64url.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - b64url.length % 4) % 4)
  return Uint8Array.from(atob(b64), c => c.charCodeAt(0))
}

export const pushSupported = () =>
  typeof navigator !== 'undefined' && 'serviceWorker' in navigator && typeof window !== 'undefined' && 'PushManager' in window && 'Notification' in window

export async function pushState(): Promise<PushState> {
  if (!pushSupported()) return 'unsupported'
  if (Notification.permission === 'denied') return 'denied'
  const reg = await navigator.serviceWorker.ready
  const local = await reg.pushManager.getSubscription()
  const server = await (await checkedFetch('/api/push/subscription')).json() as { subscribed: boolean }
  return local && server.subscribed ? 'on' : 'off'
}

export async function enablePush(): Promise<PushState> {
  if (!pushSupported()) return 'unsupported'
  if (await Notification.requestPermission() !== 'granted') return 'denied'
  const { key } = await (await checkedFetch('/api/push/key')).json() as { key: string }
  const reg = await navigator.serviceWorker.ready
  let sub = await reg.pushManager.getSubscription()
  if (sub) await sub.unsubscribe()   // one made for another key (or by a previous pairing) would never verify
  sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) as BufferSource })
  await checkedFetch('/api/push/subscription', send('PUT', sub.toJSON()))
  return 'on'
}

export async function disablePush(): Promise<PushState> {
  if (pushSupported()) {
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription()
    if (sub) await sub.unsubscribe()
  }
  await checkedFetch('/api/push/subscription', send('DELETE'))
  return 'off'
}

export async function testPush(): Promise<void> {
  await checkedFetch('/api/push/test', send('POST'))
}
