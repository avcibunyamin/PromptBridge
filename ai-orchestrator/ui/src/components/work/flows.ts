/** F3-01b review fixes (Codex, 2026-09-29): pure send/reconcile rules, testable without a browser
 *  (`npm test`). Components call these; storage is `pending.ts` (localStorage with memory fallback). */
import { pendingMessage, pendingCreate, answered, type PendingCreate, type PendingMessage } from './pending.ts'

/** P2-1: the chain must be (re)accepted whenever the current policy differs from the one this work's
 *  newest message was accepted with (or there is no message yet). `latestDigest` comes from the work
 *  summary, not from the capped message list, so neither a reload nor a long work can skip it. */
export function chainNeedsAcceptance(currentDigest: string, latestDigest: string | null | undefined,
                                     settingsDigest?: string | null): boolean {
  if (settingsDigest && settingsDigest === currentDigest) return false   // TB-19 K1: accepted when saved in Settings
  return !latestDigest || latestDigest !== currentDigest
}

type CreateDeps = {
  createWork: (b: { client_work_id: string; title: string; workspace_path: string; isolate?: boolean }) => Promise<{ id: string }>
  addMessage: (workId: string, m: PendingMessage) => Promise<unknown>
}
export type StartResult =
  | { kind: 'opened'; workId: string; rejected?: unknown }      // work exists; open it (first message sent, rejected or uncertain)
  | { kind: 'create_rejected'; error: unknown }                // server definitely did not create it
  | { kind: 'create_uncertain' }                               // outcome unknown; the record stays for a same-id retry

/** P2-2: the created work id is recorded *before* the first message is sent, so a reload between the
 *  two requests reopens that work (see `resumeCreated`) instead of showing an empty form. */
export async function startWork(p: PendingCreate, deps: CreateDeps): Promise<StartResult> {
  pendingCreate.set(p)
  let workId: string
  try { workId = (await deps.createWork({ client_work_id: p.client_work_id, title: p.title, workspace_path: p.workspace_path, ...(p.isolate ? { isolate: true } : {}) })).id }
  catch (e) {
    if (answered(e)) { pendingCreate.clear(); return { kind: 'create_rejected', error: e } }
    return { kind: 'create_uncertain' }
  }
  pendingMessage.set(workId, p.message)
  pendingCreate.set({ ...p, created_work_id: workId })
  let rejected: unknown
  try { await deps.addMessage(workId, p.message); pendingMessage.clear(workId) }
  catch (e) { if (answered(e)) { pendingMessage.clear(workId); rejected = e } }   // not answered: the work view retries the same id
  pendingCreate.clear()
  return { kind: 'opened', workId, rejected }
}

/** On load: a create that reached the server but was interrupted before navigation. */
export function resumeCreated(): string | null {
  const id = pendingCreate.get()?.created_work_id ?? null
  if (id) pendingCreate.clear()
  return id
}

export type DiscardResult = 'sent' | 'discarded' | 'kept'

/** P2-3: an uncertain message may be dropped only after the server was asked about the same id.
 *  Found → it was stored ('sent'); definitely absent → 'discarded'; server unreachable → 'kept'. */
export async function discardMessage(workId: string, p: PendingMessage,
  list: () => Promise<{ items: { client_message_id: string }[] }>): Promise<DiscardResult> {
  let items: { client_message_id: string }[]
  try { items = (await list()).items } catch { return 'kept' }
  pendingMessage.clear(workId)
  return items.some(x => x.client_message_id === p.client_message_id) ? 'sent' : 'discarded'
}

/** P2-3 for work creation: the work id is the client id, so GET tells whether it exists. */
export async function discardCreate(p: PendingCreate, getWork: (id: string) => Promise<unknown>): Promise<DiscardResult> {
  try { await getWork(p.client_work_id) }
  catch (e) {
    if ((e as { status?: number } | null)?.status === 404) { pendingCreate.clear(); return 'discarded' }
    return 'kept'
  }
  pendingCreate.clear()   // it exists (its first message was never sent): the caller opens it
  return 'sent'
}

/** F3-03 (Codex §6): only the newest request's answer may be applied; a late answer for an older
 *  selection is dropped. `begin()` per request, `isCurrent(id)` when its answer arrives. */
export function latestOnly() {
  let current = 0
  return { begin: () => ++current, isCurrent: (id: number) => id === current }
}

export type Team = { author: string[]; reviewer: string[] }
export type PreviewAnswer<T> = { team: Team; ok: true; value: T } | { team: Team; ok: false; error: unknown }

/** F3-03: request a preview per selection; `apply` sees only the answer for the newest selection, so an
 *  older team's digest can never be accepted under a newer selection, whatever order answers arrive in. */
export function teamPreviewer<T>(fetchPreview: (t: Team) => Promise<T>, apply: (a: PreviewAnswer<T>) => void) {
  const seq = latestOnly()
  return (team: Team) => {
    const id = seq.begin()
    fetchPreview(team).then(value => { if (seq.isCurrent(id)) apply({ team, ok: true, value }) },
      error => { if (seq.isCurrent(id)) apply({ team, ok: false, error }) })
  }
}

/** TB-20 K1: a work's title from its first message: the first non-empty line, whitespace collapsed, at most 60
 *  characters (cut at a word when possible). The title stays editable. */
export function deriveTitle(text: string, max = 60): string {
  const line = text.split('\n').map(l => l.replace(/\s+/g, ' ').trim()).find(Boolean) ?? ''
  if (!line) return 'Yeni çalışma'
  if (line.length <= max) return line
  const cut = line.slice(0, max - 1), space = cut.lastIndexOf(' ')
  return (space > max / 2 ? cut.slice(0, space) : cut).trimEnd() + '…'
}

/** TB-20 K2: the one-line summary of a run card: who writes and who reviews (models, not account names). */
export function runLine(team: { author: string[]; reviewer: string[] }, label: (id: string) => string): string {
  const a = team.author[0], r = team.reviewer[0]
  return [a && `Yazar ${label(a)}`, r && `İnceleme ${label(r)}`].filter(Boolean).join(' · ')
}

/** TB-20 K2: a run card opens by itself when it needs a person (its controls must not hide in a folded card). */
export const needsAttention = (status: string): boolean =>
  status.startsWith('WAITING_') || status === 'RECOVERY_REQUIRED' || status === 'FAILED' || status === 'PAUSED'

/** Only the work's latest run opens by itself; older runs are settled history (each open card holds an SSE). */
export const opensByItself = (run: { id: string; status: string } | null | undefined, latestId: string | undefined): boolean =>
  !!run && run.id === latestId && needsAttention(run.status)
