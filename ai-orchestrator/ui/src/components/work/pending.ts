/** Durable keys for requests whose outcome is unknown (F3-01b idempotency).
 *
 * A record is written *before* the POST and removed only when the server has
 * answered for that id (created, replayed, or definitely rejected) or when a
 * reload finds the id already stored server-side. While a record exists its
 * payload is frozen: retries resend the same id and bytes; an edit needs a new
 * id. Only this app's own keys are touched; storage may be unavailable, in
 * which case we fall back to memory for the current page.
 */
export type PendingMessage = { client_message_id: string; text: string; policy_digest: string;
  team?: { author: string[]; reviewer: string[] }   // F3-03: resent byte-for-byte (the team is in the payload hash)
  wait_on_quota?: boolean; wait_deadline_hours?: number; wait_max_attempts?: number   // F3-04: hashed the same way
  recipe?: string   // TB-45: frozen in the payload hash
  read_only?: boolean }   // K-057: hashed too; the author may not change files either
export type PendingCreate = { client_work_id: string; title: string; workspace_path: string; isolate?: boolean; message: PendingMessage;
  created_work_id?: string }   // set once the server created the work (reload reopens it)

const memory = new Map<string, string>()
const read = (key: string): string | null => {
  try { return localStorage.getItem(key) } catch { return memory.get(key) ?? null }
}
const write = (key: string, value: string | null) => {
  try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value) }
  catch { if (value === null) memory.delete(key); else memory.set(key, value) }
}
const parse = <T,>(raw: string | null): T | null => { try { return raw ? JSON.parse(raw) as T : null } catch { return null } }

const msgKey = (workId: string) => `conductor.pending-message.${workId}`
const CREATE_KEY = 'conductor.pending-create'

export const pendingMessage = {
  get: (workId: string) => parse<PendingMessage>(read(msgKey(workId))),
  set: (workId: string, p: PendingMessage) => write(msgKey(workId), JSON.stringify(p)),
  clear: (workId: string) => write(msgKey(workId), null),
}
export const pendingCreate = {
  get: () => parse<PendingCreate>(read(CREATE_KEY)),
  set: (p: PendingCreate) => write(CREATE_KEY, JSON.stringify(p)),
  clear: () => write(CREATE_KEY, null),
}

/** True when the server definitely answered (the request arrived and was judged). */
export const answered = (e: unknown) => {
  const status = (e as { status?: number } | null)?.status
  return typeof status === 'number' && status < 500
}

/** A first message the server definitely rejected right after its work was created: handed to the work view
 *  (memory only — nothing was stored, so the text returns to the composer with the reason). F3-03: the
 *  selected team comes along, so the work view re-previews it (problems beside the selection, re-accept on 409). */
export const rejectedFirst = new Map<string, { text: string; message: string; code?: string;
  team?: { author: string[]; reviewer: string[] }; wait?: { wait_on_quota: true; wait_deadline_hours: number; wait_max_attempts: number } | null;
  read_only?: boolean }>()
