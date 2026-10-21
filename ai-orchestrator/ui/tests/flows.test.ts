// F3-01b review fixes: reload, lost response and same-id reconciliation rules (no browser needed).
// Run: npm test   (node --experimental-strip-types --test tests/)
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { chainNeedsAcceptance, startWork, resumeCreated, discardMessage, discardCreate, teamPreviewer, type PreviewAnswer } from '../src/components/work/flows.ts'
import { pendingCreate, pendingMessage, type PendingCreate } from '../src/components/work/pending.ts'

const http = (status: number) => Object.assign(new Error(`HTTP ${status}`), { status })
const network = () => new TypeError('Failed to fetch')          // no status: the outcome is unknown
const msg = { client_message_id: 'm-1', text: 'goal', policy_digest: 'd1' }
const create = (): PendingCreate => ({ client_work_id: 'w-1', title: 'T', workspace_path: '/ws', message: msg })

beforeEach(() => { pendingCreate.clear(); pendingMessage.clear('w-1') })

test('P2-1: the chain must be accepted when the newest message used another digest (and for a first message)', () => {
  assert.equal(chainNeedsAcceptance('d1', null), true)
  assert.equal(chainNeedsAcceptance('d1', undefined), true)
  assert.equal(chainNeedsAcceptance('d1', 'd1'), false)
  assert.equal(chainNeedsAcceptance('d2', 'd1'), true)
})

test('P2-2: a reload after the work was created reopens it instead of an empty form', async () => {
  let reachedMessage = false
  const never = new Promise<never>(() => {})
  void startWork(create(), {
    createWork: async () => ({ id: 'w-1' }),
    addMessage: async () => { reachedMessage = true; return never },   // the page unloads while this is in flight
  })
  await new Promise(r => setImmediate(r))
  assert.equal(reachedMessage, true)
  assert.deepEqual(pendingMessage.get('w-1'), msg)                    // the work view will retry the same id
  assert.equal(resumeCreated(), 'w-1')                                // the reloaded form opens the work
  assert.equal(pendingCreate.get(), null)
  assert.equal(resumeCreated(), null)                                 // only once
})

test('P2-2: create outcome unknown keeps the record (no work id); a same-id retry opens the work', async () => {
  const r1 = await startWork(create(), { createWork: async () => { throw network() }, addMessage: async () => ({}) })
  assert.equal(r1.kind, 'create_uncertain')
  assert.equal(pendingCreate.get()?.client_work_id, 'w-1')
  assert.equal(resumeCreated(), null)
  const ids: string[] = []
  const r2 = await startWork(pendingCreate.get()!, {
    createWork: async b => { ids.push(b.client_work_id); return { id: b.client_work_id } },   // idempotent replay
    addMessage: async (_w, m) => { ids.push(m.client_message_id); return {} },
  })
  assert.deepEqual(r2, { kind: 'opened', workId: 'w-1', rejected: undefined })
  assert.deepEqual(ids, ['w-1', 'm-1'])                               // same ids, not new ones
  assert.equal(pendingCreate.get(), null); assert.equal(pendingMessage.get('w-1'), null)
})

test('definite answers release the ids', async () => {
  const rejected = await startWork(create(), { createWork: async () => { throw http(422) }, addMessage: async () => ({}) })
  assert.equal(rejected.kind, 'create_rejected'); assert.equal(pendingCreate.get(), null)
  const first = await startWork(create(), { createWork: async () => ({ id: 'w-1' }), addMessage: async () => { throw http(409) } })
  assert.equal(first.kind, 'opened'); assert.equal((first as { rejected?: { status: number } }).rejected?.status, 409)
  assert.equal(pendingMessage.get('w-1'), null)
})

test('lost message response: the record stays for a same-id retry', async () => {
  const r = await startWork(create(), { createWork: async () => ({ id: 'w-1' }), addMessage: async () => { throw network() } })
  assert.equal(r.kind, 'opened')
  assert.deepEqual(pendingMessage.get('w-1'), msg)
  assert.equal(pendingCreate.get(), null)
})

test('P2-3: an uncertain message is discarded only after the server was asked about the same id', async () => {
  pendingMessage.set('w-1', msg)
  assert.equal(await discardMessage('w-1', msg, async () => { throw network() }), 'kept')
  assert.deepEqual(pendingMessage.get('w-1'), msg)                    // offline: the id is kept
  assert.equal(await discardMessage('w-1', msg, async () => ({ items: [{ client_message_id: 'm-1' }] })), 'sent')
  assert.equal(pendingMessage.get('w-1'), null)                       // the server has it: nothing to resend
  pendingMessage.set('w-1', msg)
  assert.equal(await discardMessage('w-1', msg, async () => ({ items: [{ client_message_id: 'other' }] })), 'discarded')
  assert.equal(pendingMessage.get('w-1'), null)
})

test('P2-3: an uncertain create is discarded only when the server says the work does not exist', async () => {
  pendingCreate.set(create())
  assert.equal(await discardCreate(create(), async () => { throw network() }), 'kept')
  assert.equal(pendingCreate.get()?.client_work_id, 'w-1')
  assert.equal(await discardCreate(create(), async () => { throw http(500) }), 'kept')
  assert.equal(await discardCreate(create(), async () => ({ id: 'w-1' })), 'sent')   // it exists: open, don't recreate
  assert.equal(pendingCreate.get(), null)
  pendingCreate.set(create())
  assert.equal(await discardCreate(create(), async () => { throw http(404) }), 'discarded')
  assert.equal(pendingCreate.get(), null)
})

test('F3-03: previews answering in reverse order never apply the older selection', async () => {
  const resolvers: Record<string, (v: string) => void> = {}
  const applied: PreviewAnswer<string>[] = []
  const request = teamPreviewer(team => new Promise<string>(r => { resolvers[team.author[0]] = r }), a => applied.push(a))
  request({ author: ['codex'], reviewer: ['claude'] })     // old selection
  request({ author: ['claude'], reviewer: ['codex'] })     // newest selection
  resolvers.claude('digest-new'); await new Promise(r => setImmediate(r))
  resolvers.codex('digest-old'); await new Promise(r => setImmediate(r))
  assert.deepEqual(applied, [{ team: { author: ['claude'], reviewer: ['codex'] }, ok: true, value: 'digest-new' }])
})

test('F3-03: a late failure of an older preview is dropped too', async () => {
  let failOld: (e: unknown) => void = () => {}
  const applied: PreviewAnswer<string>[] = []
  const request = teamPreviewer(team => team.author[0] === 'codex'
    ? new Promise<string>((_, reject) => { failOld = reject }) : Promise.resolve('ok'), a => applied.push(a))
  request({ author: ['codex'], reviewer: ['claude'] })
  request({ author: ['claude'], reviewer: ['claude'] })
  await new Promise(r => setImmediate(r)); failOld(http(422)); await new Promise(r => setImmediate(r))
  assert.equal(applied.length, 1); assert.equal(applied[0].ok, true)
})

test('TB-26: the isolate choice reaches createWork only when set (a retried create sends the same body)', async () => {
  const bodies: unknown[] = []
  const deps = { createWork: async (b: unknown) => { bodies.push(b); return { id: 'w-iso' } }, addMessage: async () => ({}) }
  await startWork({ ...create(), isolate: true }, deps)
  await startWork(create(), deps)
  assert.equal((bodies[0] as { isolate?: boolean }).isolate, true)
  assert.equal('isolate' in (bodies[1] as object), false)
})
