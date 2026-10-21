/** TB-12 (K-024): the panel on a phone, through the Tailscale-served gateway.
 *  Pure enough for `npm test`: token storage, mode detection, an SSE parser and an EventSource look-alike that
 *  can send the device token (a real EventSource cannot set headers). On the Mac (8765) nothing changes. */

const KEY = 'conductor-mobile-token'

/** Set once at start-up; components read it to hide what a phone may not do (the gateway refuses it anyway). */
export const mobile = { active: false, token: null as string | null, scopes: [] as string[] }
export const canApprove = () => !mobile.active || mobile.scopes.includes('approve')
/** K-027: stop a run, cancel a quota wait, take a queued message back — only a device the Mac allowed. */
export const canInterrupt = () => !mobile.active || mobile.scopes.includes('interrupt')
/** K-033: start a work (in a folder a work already uses), send a message, theme and default team — Mac-granted. */
export const canCommand = () => !mobile.active || mobile.scopes.includes('command')

/** A revoked or unknown token: forget it and let the app show the pairing screen. */
const unauthorizedListeners = new Set<() => void>()
export function onUnauthorized(cb: () => void) { unauthorizedListeners.add(cb); return () => { unauthorizedListeners.delete(cb) } }
export function unauthorized() { saveToken(null); unauthorizedListeners.forEach(cb => cb()) }

export function loadToken(storage: Pick<Storage, 'getItem'> | null = safeStorage()) {
  try { mobile.token = storage?.getItem(KEY) ?? null } catch { mobile.token = null }
  return mobile.token
}
export function saveToken(token: string | null, storage: Pick<Storage, 'setItem' | 'removeItem'> | null = safeStorage()) {
  mobile.token = token
  try { if (token) storage?.setItem(KEY, token); else storage?.removeItem(KEY) } catch { /* private mode: token lives in memory */ }
}
function safeStorage(): Storage | null { try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null } }

/** Adds the device token when there is one (the gateway never accepts cookies). */
export function withAuth(opts: RequestInit = {}): RequestInit {
  if (!mobile.token) return opts
  const headers = new Headers(opts.headers)
  headers.set('Authorization', `Bearer ${mobile.token}`)
  return { ...opts, headers }
}

export type Mode = { kind: 'desktop' } | { kind: 'mobile'; scopes: string[] } | { kind: 'pair' }

/** 8765 answers `/api/mobile/me` with 404 (desktop); the gateway with 200 (paired) or 401 (not paired).
 *  Offline (the service worker served the shell), a phone with a token stays a phone, view only until reload. */
export async function detectMode(fetchImpl: typeof fetch = fetch): Promise<Mode> {
  let r: Response
  try { r = await fetchImpl('/api/mobile/me', withAuth()) } catch {
    if (!mobile.token) return { kind: 'desktop' }                     // the Mac panel never holds a device token
    Object.assign(mobile, { active: true, scopes: [] })
    return { kind: 'mobile', scopes: [] }
  }
  if (r.status === 200) {
    const me = await r.json() as { scopes?: string[] }
    Object.assign(mobile, { active: true, scopes: me.scopes ?? [] })
    return { kind: 'mobile', scopes: mobile.scopes }
  }
  if (r.status === 401 || r.status === 403) { saveToken(null); return { kind: 'pair' } }
  return { kind: 'desktop' }
}

export type SSEMessage = { type: string; data: string; lastEventId: string }

/** Splits a text/event-stream buffer into complete events; the unfinished tail is returned for the next chunk. */
export function parseSSE(buffer: string): { events: SSEMessage[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, '\n').split('\n\n')
  const rest = blocks.pop() ?? ''
  const events: SSEMessage[] = []
  for (const block of blocks) {
    let type = 'message', id = ''; const data: string[] = []
    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue
      const i = line.indexOf(':'), field = i < 0 ? line : line.slice(0, i)
      const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '')
      if (field === 'event') type = value
      else if (field === 'data') data.push(value)
      else if (field === 'id') id = value
    }
    if (data.length) events.push({ type, data: data.join('\n'), lastEventId: id })
  }
  return { events, rest }
}

type Listener = (e: MessageEvent) => void

/** EventSource look-alike over fetch, so the token travels in a header (never in the URL, which logs keep).
 *  Reconnects with Last-Event-ID; a 401/403 (e.g. the device was revoked) stops it and asks to pair again. */
export class TokenEventSource {
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: Listener | null = null
  private listeners = new Map<string, Set<Listener>>()
  private abort = new AbortController()
  private lastId = ''
  private closed = false
  private url: string
  private fetchImpl: typeof fetch
  private retryMs: number
  constructor(url: string, fetchImpl: typeof fetch = fetch, retryMs = 3000) {
    // A bare `fetch` must not be called as a method of this object (browsers throw "Illegal invocation").
    this.url = url; this.fetchImpl = (input, init) => fetchImpl(input, init); this.retryMs = retryMs
    void this.loop()
  }

  addEventListener(type: string, cb: Listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type)!.add(cb)
  }
  close() { this.closed = true; this.abort.abort() }

  private dispatch(m: SSEMessage) {
    const event = { type: m.type, data: m.data, lastEventId: m.lastEventId } as MessageEvent
    if (m.type === 'message') this.onmessage?.(event)
    this.listeners.get(m.type)?.forEach(cb => cb(event))
  }

  private async loop() {
    while (!this.closed) {
      try {
        const headers: Record<string, string> = { Accept: 'text/event-stream' }
        if (this.lastId) headers['Last-Event-ID'] = this.lastId
        const r = await this.fetchImpl(this.url, { ...withAuth({ headers }), signal: this.abort.signal })
        if (r.status === 401 || r.status === 403) { this.closed = true; this.onerror?.(); unauthorized(); return }
        if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`)
        this.onopen?.()
        const reader = r.body.getReader(), decoder = new TextDecoder()
        let buffer = ''
        for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          const parsed = parseSSE(buffer + decoder.decode(value, { stream: true }))
          buffer = parsed.rest
          for (const m of parsed.events) { if (m.lastEventId) this.lastId = m.lastEventId; this.dispatch(m) }
        }
      } catch { /* network drop or abort */ }
      if (this.closed) return
      this.onerror?.()
      await new Promise(r => setTimeout(r, this.retryMs))
    }
  }
}

/** The one place panels open a live stream: EventSource on the Mac, TokenEventSource on a phone. */
export function events(url: string): Pick<EventSource, 'addEventListener' | 'close'> & { onopen: (() => void) | null; onerror: (() => void) | null; onmessage: Listener | null } {
  return (mobile.active ? new TokenEventSource(url) : new EventSource(url)) as never
}
