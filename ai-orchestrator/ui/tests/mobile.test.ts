// TB-12: token handling, mode detection, the SSE parser and the token-carrying event source.
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { mobile, saveToken, withAuth, detectMode, parseSSE, TokenEventSource, canApprove, onUnauthorized } from '../src/mobile.ts'

const mem = () => { const m = new Map<string, string>(); return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) } }
beforeEach(() => { Object.assign(mobile, { active: false, token: null, scopes: [] }) })

test('the token travels only as an Authorization header', () => {
  assert.equal(new Headers(withAuth().headers).get('Authorization'), null)
  saveToken('t0k', mem())
  const h = new Headers(withAuth({ headers: { 'Content-Type': 'application/json' } }).headers)
  assert.equal(h.get('Authorization'), 'Bearer t0k')
  assert.equal(h.get('Content-Type'), 'application/json')
})

test('mode: 404 desktop, 200 mobile with scopes, 401 asks to pair and forgets the token', async () => {
  const answer = (status: number, body = {}) => (async () => new Response(JSON.stringify(body), { status })) as typeof fetch
  assert.deepEqual(await detectMode(answer(404)), { kind: 'desktop' })
  assert.equal(mobile.active, false)
  assert.equal(canApprove(), true)                                    // the Mac panel is not restricted
  assert.deepEqual(await detectMode(answer(200, { scopes: ['view'] })), { kind: 'mobile', scopes: ['view'] })
  assert.equal(mobile.active, true)
  assert.equal(canApprove(), false)
  saveToken('old', mem())
  assert.deepEqual(await detectMode(answer(401)), { kind: 'pair' })
  assert.equal(mobile.token, null)
})

test('mode offline: a phone with a token stays a phone (view only), without one it is the Mac', async () => {
  const offline = (async () => { throw new TypeError('Failed to fetch') }) as typeof fetch
  assert.deepEqual(await detectMode(offline), { kind: 'desktop' })
  saveToken('t0k', mem())
  assert.deepEqual(await detectMode(offline), { kind: 'mobile', scopes: [] })
  assert.equal(mobile.active, true)
  assert.equal(canApprove(), false)
})

test('SSE parser: named events, ids, multi-line data, comments and a partial tail', () => {
  const { events, rest } = parseSSE('event: work_event\nid: 7\ndata: {"a":1}\n\n: keepalive\n\ndata: x\ndata: y\n\nevent: half\nda')
  assert.deepEqual(events, [{ type: 'work_event', data: '{"a":1}', lastEventId: '7' }, { type: 'message', data: 'x\ny', lastEventId: '' }])
  assert.equal(rest, 'event: half\nda')
})

test('TokenEventSource sends the token, dispatches events and resumes with Last-Event-ID', async () => {
  saveToken('t0k', mem())
  const calls: Headers[] = []
  const bodies = ['event: work_event\nid: 3\ndata: one\n\n', 'event: work_event\nid: 4\ndata: two\n\n']
  const fetchImpl = (async (_u: string, init: RequestInit) => {
    calls.push(new Headers(init.headers))
    const text = bodies.shift()
    if (text === undefined) return new Promise<Response>(() => {})          // stay "connected"
    return new Response(new Blob([text]).stream(), { status: 200 })
  }) as typeof fetch
  const got: string[] = []
  const es = new TokenEventSource('/api/works/w/events', fetchImpl, 1)
  es.addEventListener('work_event', e => got.push(`${e.lastEventId}:${e.data}`))
  await new Promise(r => setTimeout(r, 50))
  es.close()
  assert.deepEqual(got, ['3:one', '4:two'])
  assert.equal(calls[0].get('Authorization'), 'Bearer t0k')
  assert.equal(calls[1].get('Last-Event-ID'), '3')
})

test('TokenEventSource stops and forgets the token when the device is revoked (401)', async () => {
  saveToken('t0k', mem())
  let n = 0, errored = false, toPairing = 0
  const off = onUnauthorized(() => { toPairing++ })
  const es = new TokenEventSource('/x', (async () => { n++; return new Response('', { status: 401 }) }) as typeof fetch, 1)
  es.onerror = () => { errored = true }
  await new Promise(r => setTimeout(r, 30))
  off()
  assert.equal(n, 1); assert.equal(errored, true); assert.equal(mobile.token, null); assert.equal(toPairing, 1)
})

test('TokenEventSource never calls fetch as its own method (a browser fetch needs its global receiver)', async () => {
  saveToken('t0k', mem())
  let receiver: unknown = 'unset'
  const strictFetch = function (this: unknown) { receiver = this; return new Promise<Response>(() => {}) } as unknown as typeof fetch
  const es = new TokenEventSource('/x', strictFetch, 1)
  await new Promise(r => setTimeout(r, 10))
  es.close()
  assert.notEqual(receiver, es)
})
