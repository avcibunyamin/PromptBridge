import { mobile, unauthorized, withAuth } from './mobile.ts'
import { t } from './i18n/index.ts'   // used only when a request fails, so the i18n ↔ api import cycle is harmless
import type { AccountItem, AddAccountInput } from './accountRules'
import type { GhRepo, GhState, GhStatus, PrView, PushView } from './githubRules'
// API base — empty string so Vite proxy and prod static both work
const BASE = ''

export interface Envelope {
  id: string
  state: string
  title?: string
  source_agent: string
  target_agent: string
  type?: string
  created_at: string
  approved_version_id?: string
  captured_version_id: string
  delivered_path?: string
  delivered_sha256?: string
  delivered_at?: string
  note?: string
  bytes?: number
}

export interface Version {
  version_id: string
  kind: 'captured' | 'edited'
  sha256: string
  bytes: number
  created_at: string
  created_by?: string
}

export interface HistoryEntry {
  seq: number
  envelope_id: string
  at: string
  from_state?: string
  to_state: string
  event?: string
  actor?: string
  detail: Record<string, unknown>
}

export interface EnvelopeDetail {
  envelope: Envelope
  versions: Version[]
  history: HistoryEntry[]
}

/** An HTTP answer from the server (the request definitely arrived). Network failures stay plain errors. */
export class ApiError extends Error {
  status: number
  code?: string
  body?: Record<string, unknown>   // the full JSON answer (e.g. F3-02 resume: changes + diff_digest)
  constructor(message: string, status: number, code?: string, body?: Record<string, unknown>) {
    super(message)
    this.status = status
    this.code = code
    this.body = body
  }
}


export async function checkedFetch(path: string, opts?: RequestInit) {
  const r = await fetch(BASE + path, withAuth(opts)).catch(e => {
    // on the phone a network failure almost always means Tailscale is off or the Mac is asleep;
    // empty name so the String(e) banners show the sentence alone, cause keeps the browser's error
    throw mobile.active && e?.name !== 'AbortError' ? Object.assign(new Error(t('shell.offline')), { name: '', cause: e }) : e
  })   // TB-12: device token on the mobile gateway
  if (r.status === 401 && mobile.active) unauthorized()   // revoked on the Mac: back to the pairing screen
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }))
    const nested = typeof body?.detail === 'object' && body?.detail !== null && !Array.isArray(body.detail) ? body.detail as Record<string, unknown> : null
    const msg = typeof body?.detail === 'string' ? body.detail : (typeof nested?.detail === 'string' ? nested.detail : (r.statusText || `HTTP ${r.status}`))
    const code = typeof body?.code === 'string' ? body.code : (typeof nested?.code === 'string' ? nested.code : undefined)
    throw new ApiError(String(msg), r.status, code, body)
  }
  return r
}
const _fetch = checkedFetch

export async function listEnvelopes(state?: string): Promise<Envelope[]> {
  const q = state ? `?state=${encodeURIComponent(state)}` : ''
  return (await _fetch(`/api/envelopes${q}`)).json()
}

export async function getEnvelope(eid: string): Promise<EnvelopeDetail> {
  return (await _fetch(`/api/envelopes/${eid}`)).json()
}

export async function getContent(eid: string, versionId?: string): Promise<string> {
  const q = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return (await _fetch(`/api/envelopes/${eid}/content${q}`)).text()
}

export async function approveEnvelope(eid: string, actor = 'human', versionId?: string) {
  return (await _fetch(`/api/envelopes/${eid}/approve`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ actor, ...(versionId ? { version_id: versionId } : {}) }),
  })).json()
}

export async function rejectEnvelope(eid: string, actor = 'human', reason = '') {
  return (await _fetch(`/api/envelopes/${eid}/reject`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ actor, reason }),
  })).json()
}

export async function editEnvelope(eid: string, content: string): Promise<{version_id: string}> {
  return (await _fetch(`/api/envelopes/${eid}/edit`, {
    method: 'POST',
    headers: { 'content-type': 'application/octet-stream' },
    body: new TextEncoder().encode(content),
  })).json()
}

export async function reconcile() {
  return (await _fetch('/api/reconcile', { method: 'POST' })).json()
}

// ── F3-01b persistent works ──────────────────────────────────────────────────

export interface RunRef { id: string; status: string; git_changed?: string[]   // TB-23: .git hooks/config the run left
  tests?: TestRun | null; lint?: TestRun | null }   // TB-27/28: Conductor's sandboxed test / lint run before the reviewer
export type Check = 'tests' | 'lint'
export interface TestRun { status: 'passed' | 'failed' | 'timeout' | 'not_run'; reason?: string | null; command?: string | null
  exit_code?: number | null; duration_s?: number | null }
export interface TestsStatus { suggestion: { kind: string; label: string; cwd?: string; script_path: string | null; scripts?: [string, string][]; command_digest: string } | null
  approved: boolean; stale: boolean; approved_at: string | null }
export async function testsStatus(wid: string, check: Check = 'tests'): Promise<TestsStatus> {
  return (await _fetch(`/api/tests/works/${encodeURIComponent(wid)}?check=${check}`)).json()
}
export async function testsApprove(wid: string, command_digest: string, check: Check = 'tests'): Promise<TestsStatus> {
  return (await _fetch(`/api/tests/works/${encodeURIComponent(wid)}/approve`, json({ command_digest, check }))).json()
}
export async function testsRevoke(wid: string, check: Check = 'tests'): Promise<TestsStatus> {
  return (await _fetch(`/api/tests/works/${encodeURIComponent(wid)}/revoke?check=${check}`, { method: 'POST' })).json()
}

export interface Work {
  id: string
  title: string
  workspace_path: string
  created_at: string
  updated_at: string
  archived_at: string | null
  revision: number
  latest_session: RunRef | null
  account_wait?: { account: string; holder_work_id: string | null; holder_work_title: string | null } | null
  pending_messages: number
  latest_policy_digest: string | null   // newest message's accepted digest (not limited by the message page cap)
  latest_team: { author: string[]; reviewer: string[] } | null   // F3-03: follow-up default team
  blocked_reason: 'run_active' | 'run_waiting' | 'recovery_required' | 'needs_continue' | 'paused' | 'needs_review_decision' | null
  isolation?: { source: string; branch: string; removed: boolean } | null   // TB-26: runs in its own git worktree
}

/** TB-26 (Mac only): bring an isolated work's result into its source folder, or drop the copy. */
export interface ApplyView { source: string; target: string | null; branch: string; uncommitted: string[]
  commits: { sha: string; subject: string }[]; source_dirty: string[]; attention: string[]; digest: string }
export async function isolationPreview(wid: string): Promise<ApplyView> {
  return (await _fetch(`/api/isolation/works/${encodeURIComponent(wid)}/apply`)).json()
}
export async function isolationApply(wid: string, digest: string): Promise<{ target: string; commit: string }> {
  return (await _fetch(`/api/isolation/works/${encodeURIComponent(wid)}/apply`, json({ digest }))).json()
}
export async function isolationRemove(wid: string): Promise<{ removed: boolean; branch: string; branch_deleted: boolean }> {
  return (await _fetch(`/api/isolation/works/${encodeURIComponent(wid)}/remove`, { method: 'POST' })).json()
}

export interface WorkMessage {
  id: string
  work_id: string
  client_message_id: string
  seq: number
  text: string
  status: 'PENDING' | 'LINKED' | 'CANCELLED'
  policy_digest: string
  team: { author: string[]; reviewer: string[] }
  wait?: { deadline_hours: number; max_attempts: number } | null   // F3-04: frozen quota wait, if chosen
  recipe?: { id: string; title: string; version: number } | null
  created_at: string
  session: RunRef | null
}

export interface WorksPage {
  items: Work[]
  next_cursor: string | null
  total: number
  counts: { active: number; archived: number }
}

export interface LegacySession { id: string; status: string; workspace_path: string; created_at: string; recovered_by_human?: number }

const json = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })

export async function listWorks(archived: boolean, cursor?: string | null): Promise<WorksPage> {
  const q = new URLSearchParams({ archived: String(archived), limit: '50' })
  if (cursor) q.set('cursor', cursor)
  return (await _fetch(`/api/works?${q}`)).json()
}

export async function getWork(wid: string): Promise<Work> {
  return (await _fetch(`/api/works/${encodeURIComponent(wid)}`)).json()
}

/** All messages of a work, oldest first (server pages by seq). */
export async function listAllMessages(wid: string, maxPages = 20): Promise<{ items: WorkMessage[]; truncated: boolean }> {
  const items: WorkMessage[] = []
  let cursor: string | null = null
  for (let page = 0; page < maxPages; page++) {
    const q: string = cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''
    const r: { items: WorkMessage[]; next_cursor: string | null } =
      await (await _fetch(`/api/works/${encodeURIComponent(wid)}/messages?limit=100${q}`)).json()
    items.push(...r.items)
    cursor = r.next_cursor
    if (!cursor) return { items, truncated: false }
  }
  return { items, truncated: true }
}

export async function createWork(body: { client_work_id: string; title: string; workspace_path: string; isolate?: boolean }): Promise<Work> {
  return (await _fetch('/api/works', json(body))).json()
}

/** F3-03: the policy a message with this team would freeze (no side effects). 422 team_invalid carries `problems`. */
export async function previewTeam(team: { author: string[]; reviewer: string[] }): Promise<{ policy: { roles: Record<string, { provider: string; profile: string }[]>; max_context_continuations?: number;
  loop?: { max_turns: number; max_active_minutes: number } }; digest: string; warnings: string[]; catalog?: Record<string, string> }> {
  const q = new URLSearchParams({ author: team.author.join(','), reviewer: team.reviewer.join(',') })
  return (await checkedFetch(`/api/sessions/providers/preview?${q}`)).json()
}

export async function addMessage(wid: string, body: { client_message_id: string; text: string; policy_digest: string;
  team?: { author: string[]; reviewer: string[] }; wait_on_quota?: boolean; wait_deadline_hours?: number;
  wait_max_attempts?: number; recipe?: string }): Promise<WorkMessage> {
  return (await _fetch(`/api/works/${encodeURIComponent(wid)}/messages`, json(body))).json()
}

export async function cancelMessage(wid: string, mid: string): Promise<WorkMessage> {
  return (await _fetch(`/api/works/${encodeURIComponent(wid)}/messages/${encodeURIComponent(mid)}/cancel`, { method: 'POST' })).json()
}

export async function patchWork(wid: string, body: { expected_revision: number; title?: string; archived?: boolean }): Promise<Work> {
  return (await _fetch(`/api/works/${encodeURIComponent(wid)}`, { ...json(body), method: 'PATCH' })).json()
}

export async function deleteWork(wid: string, expectedRevision: number | { expected_revision: number }): Promise<Work & { deleted?: boolean; deleted_at?: string | null }> {
  const expected_revision = typeof expectedRevision === 'number' ? expectedRevision : expectedRevision.expected_revision
  return (await _fetch(`/api/works/${encodeURIComponent(wid)}?expected_revision=${encodeURIComponent(expected_revision)}`,
    { ...json({ expected_revision }), method: 'DELETE' })).json()
}

export async function continueWork(wid: string, expectedSessionId: string, expectedOutcome?: string): Promise<{ continued: boolean; changed: boolean }> {
  return (await _fetch(`/api/works/${encodeURIComponent(wid)}/continue`,
    json({ confirm: true, expected_session_id: expectedSessionId, ...(expectedOutcome ? { expected_outcome: expectedOutcome } : {}) }))).json()
}

export async function listLegacySessions(cursor?: string | null): Promise<{ items: LegacySession[]; next_cursor: string | null }> {
  const q = new URLSearchParams({ unlinked_only: 'true', limit: '50' })
  if (cursor) q.set('cursor', cursor)
  const r = await _fetch(`/api/sessions?${q}`)
  return { items: await r.json(), next_cursor: r.headers.get('X-Next-Cursor') }
}

// ── TB-13 local preview/dev-server manager ───────────────────────────────────

export interface PreviewSuggestion {
  workspace_path: string
  kind: string
  label: string
  argv: string[]
  env: Record<string, string>
  script_path: string
  script_digest: string | null
  scripts?: [string, string][]
  port: number
  url: string
  command_digest: string
  requires_approval: boolean
}

export interface PreviewServer {
  workspace_path: string
  status: 'stopped' | 'starting' | 'running' | 'failed'
  command_digest: string | null
  pid: number | null
  pgid: number | null
  port: number | null
  url: string | null
  log_tail: string
  error: string | null
  started_at: string | null
  updated_at: string
  suggestion?: PreviewSuggestion | null
}

export interface PreviewStatus {
  workspace_path: string
  suggestion: PreviewSuggestion | null
  approved: boolean
  server: PreviewServer | null
}

export async function previewStatus(workspacePath: string): Promise<PreviewStatus> {
  return (await _fetch(`/api/previews/status?workspace_path=${encodeURIComponent(workspacePath)}`)).json()
}

export async function approvePreview(workspacePath: string, commandDigest: string, port?: number): Promise<{ approved: boolean; suggestion: PreviewSuggestion }> {
  return (await _fetch('/api/previews/approve', json({ workspace_path: workspacePath, command_digest: commandDigest, ...(port ? { port } : {}), actor: 'human' }))).json()
}

export async function startPreview(workspacePath: string, commandDigest: string): Promise<{ server: PreviewServer }> {
  return (await _fetch('/api/previews/start', json({ workspace_path: workspacePath, command_digest: commandDigest }))).json()
}

export async function stopPreview(workspacePath: string): Promise<{ server: PreviewServer }> {
  return (await _fetch('/api/previews/stop', json({ workspace_path: workspacePath }))).json()
}

export async function restartPreview(workspacePath: string, commandDigest: string): Promise<{ server: PreviewServer }> {
  return (await _fetch('/api/previews/restart', json({ workspace_path: workspacePath, command_digest: commandDigest }))).json()
}

// TB-21: GitHub through this Mac's gh session (Mac only; the server never returns the token).
const gh = (path: string) => `/api/github${path}`
const work = (wid: string, step = '') => gh(`/works/${encodeURIComponent(wid)}${step}`)
export async function githubStatus(): Promise<GhStatus> { return (await _fetch(gh('/status'))).json() }
export async function githubRepos(): Promise<{ items: GhRepo[] }> { return (await _fetch(gh('/repos'))).json() }
export async function githubClone(repo: string): Promise<{ path: string; cloned: boolean }> {
  return (await _fetch(gh('/clone'), json({ repo }))).json()
}
export async function githubState(wid: string): Promise<GhState> { return (await _fetch(work(wid))).json() }
export async function githubCommit(wid: string, body: { files: string[]; message: string; expected_head: string | null; secrets_ack: boolean }):
  Promise<{ branch: string; commit: string }> { return (await _fetch(work(wid, '/commit'), json(body))).json() }
export async function githubPushView(wid: string): Promise<PushView> { return (await _fetch(work(wid, '/push'))).json() }
export async function githubPush(wid: string, digest: string, workflows_ack: boolean): Promise<{ branch: string }> {
  return (await _fetch(work(wid, '/push'), json({ digest, workflows_ack }))).json()
}
export async function githubPrView(wid: string): Promise<PrView> { return (await _fetch(work(wid, '/pr'))).json() }
export async function githubPr(wid: string, digest: string, title: string, body: string): Promise<{ url: string }> {
  return (await _fetch(work(wid, '/pr'), json({ digest, title, body }))).json()
}

// TB-39: provider accounts in .system/providers.yaml (Mac only; never exposed on the mobile gateway).
export type { AccountItem, AccountLogin, AccountProvider, AccountRole, AddAccountInput } from './accountRules'
export async function listAccounts(): Promise<{ accounts: AccountItem[] }> {
  return (await _fetch('/api/accounts')).json()
}
export async function addAccount(body: AddAccountInput): Promise<{ account: AccountItem | null; accounts: AccountItem[] }> {
  return (await _fetch('/api/accounts', json(body))).json()
}
export async function removeAccount(base: string): Promise<{ deleted: string; accounts: AccountItem[] }> {
  return (await _fetch(`/api/accounts/${encodeURIComponent(base)}`, { method: 'DELETE' })).json()
}
