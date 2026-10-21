/** TB-19 (K-026): pure settings rules (only the i18n `t` import, no fetch/React, so `npm test` covers them). */
import { t } from './i18n/index.ts'

export type Team = { author: string[]; reviewer: string[] }
export type Theme = 'system' | 'dark' | 'light'
export type WorkWaitSetting = { enabled: boolean; deadline_hours?: number; max_attempts?: number }
export type WorkWaitChoice = { wait_on_quota: true; wait_deadline_hours: number; wait_max_attempts: number }
export type Settings = { version: 1; team: Team | null; accepted_digest: string | null; theme: Theme; github: { clone_dir: string | null }
  preview?: { sandbox: boolean }; pinned_works?: string[]; work_teams?: Record<string, Team>; work_waits?: Record<string, WorkWaitSetting> }   // TB-22/TB-37/TB-41; absent in files written before them
export type SettingsView = { settings: Settings; error: string | null; problems: unknown[]; current_digest: string | null }
export type SettingsPatch = { team?: Team | null; theme?: Theme; github?: { clone_dir: string | null }; preview?: { sandbox: boolean }; pinned_works?: string[]
  work_teams?: Record<string, Team>; work_waits?: Record<string, WorkWaitSetting> }

export const PINNED_WORKS_LIMIT = 20
export const WORK_SETTINGS_LIMIT = 50

/** The saved default team while the providers.yaml ceiling still allows it. */
export const defaultTeam = (v: SettingsView | null): Team | null => v?.settings.team && !v.problems.length ? v.settings.team : null

/** TB-41: Per-work saved team from settings.work_teams[workId], if present. */
export const workSavedTeam = (v: SettingsView | null | undefined, workId: string): Team | null => {
  const t = v?.settings.work_teams?.[workId]
  return t && t.author?.length && t.reviewer?.length ? t : null
}

/** TB-41: Initial team for a work's composer: settings.work_teams[workId] -> work.latest_team -> defaultTeam(settings) -> fallback. */
export function initialWorkTeam(
  workId: string,
  settings: SettingsView | null | undefined,
  latestTeam: Team | null | undefined,
  fallbackTeam: Team,
  rejectedFirstTeam?: Team | null,
): Team {
  const saved = workSavedTeam(settings, workId)
  if (saved) return saved
  if (latestTeam) return latestTeam
  if (rejectedFirstTeam) return rejectedFirstTeam
  return defaultTeam(settings ?? null) ?? fallbackTeam
}

/** TB-41: Convert stored work_waits[workId] entry to composer WaitChoice (or null when disabled/absent). */
export function workWaitToChoice(w: WorkWaitSetting | null | undefined): WorkWaitChoice | null {
  if (!w || !w.enabled) return null
  return { wait_on_quota: true, wait_deadline_hours: w.deadline_hours ?? 4, wait_max_attempts: w.max_attempts ?? 3 }
}

/** TB-41: Convert composer WaitChoice to stored work_waits[workId] entry. */
export function choiceToWorkWait(c: WorkWaitChoice | null | undefined): WorkWaitSetting {
  if (!c) return { enabled: false }
  return { enabled: true, deadline_hours: c.wait_deadline_hours, max_attempts: c.wait_max_attempts }
}

/** TB-41: Initial quota wait for a work's composer from settings.work_waits[workId] (unless a rejected first message overrides it). */
export function initialWorkWait(
  workId: string,
  settings: SettingsView | null | undefined,
  rejectedFirstWait?: WorkWaitChoice | null,
): WorkWaitChoice | null {
  if (rejectedFirstWait) return rejectedFirstWait
  return workWaitToChoice(settings?.settings.work_waits?.[workId])
}

/** TB-41: Update or delete workId in work_teams map, bounded by WORK_SETTINGS_LIMIT. */
export function putWorkTeam(
  current: Record<string, Team> | undefined,
  workId: string,
  team: Team | null,
  limit = WORK_SETTINGS_LIMIT,
): Record<string, Team> {
  const next: Record<string, Team> = { ...(current ?? {}) }
  delete next[workId]
  if (!team) return next
  next[workId] = team
  const keys = Object.keys(next)
  while (keys.length > limit) {
    const oldest = keys.shift()
    if (oldest) delete next[oldest]
  }
  return next
}

/** TB-41: Update workId in work_waits map, bounded by WORK_SETTINGS_LIMIT. */
export function putWorkWait(
  current: Record<string, WorkWaitSetting> | undefined,
  workId: string,
  wait: WorkWaitChoice | null,
  limit = WORK_SETTINGS_LIMIT,
): Record<string, WorkWaitSetting> {
  const next: Record<string, WorkWaitSetting> = { ...(current ?? {}) }
  delete next[workId]
  next[workId] = choiceToWorkWait(wait)
  const keys = Object.keys(next)
  while (keys.length > limit) {
    const oldest = keys.shift()
    if (oldest) delete next[oldest]
  }
  return next
}

/** K1: a message whose team digest equals this one was accepted by saving it in Settings. Only while the saved
 *  digest is still the one the current policy gives (a changed providers.yaml asks again). */
export const acceptedDigest = (v: SettingsView | null): string | null =>
  v?.current_digest && v.current_digest === v.settings.accepted_digest ? v.current_digest : null

/** `system` follows the OS; the panel only knows light and dark. */
export const resolveTheme = (t: Theme, prefersDark: boolean): 'light' | 'dark' => t === 'system' ? (prefersDark ? 'dark' : 'light') : t

/** TB-37: Pinned works appear at the top of the sidebar while keeping relative list order within each group. */
export function sortPinnedWorks<T extends { id: string }>(works: readonly T[], pinnedIds: readonly string[]): T[] {
  if (!pinnedIds.length || !works.length) return [...works]
  const pinned = new Set(pinnedIds)
  return [...works.filter(w => pinned.has(w.id)), ...works.filter(w => !pinned.has(w.id))]
}

/** TB-37: Toggle a work ID in `pinned_works` (deduplicated, bounded by PINNED_WORKS_LIMIT when adding). */
export function togglePinnedWork(pinnedIds: readonly string[], workId: string, limit = PINNED_WORKS_LIMIT): string[] {
  const unique = pinnedIds.filter((id, idx) => id && pinnedIds.indexOf(id) === idx)
  if (unique.includes(workId)) return unique.filter(id => id !== workId)
  if (unique.length >= limit) return unique
  return [...unique, workId]
}

export type WorkMenuAction = 'pin' | 'unpin' | 'rename' | 'archive' | 'unarchive' | 'remove_isolation' | 'delete'

/** TB-37: Menu items for a sidebar work row. Phone only shows Pin and Rename; no command scope hides the menu. */
export function workMenuItems(
  work: { archived_at: string | null; isolation?: { removed: boolean } | null },
  opts: { pinned: boolean; mobile: boolean; canCommand: boolean },
): WorkMenuAction[] {
  if (!opts.canCommand) return []
  const items: WorkMenuAction[] = [opts.pinned ? 'unpin' : 'pin', 'rename']
  if (opts.mobile) return items
  items.push(work.archived_at ? 'unarchive' : 'archive')
  if (work.isolation && !work.isolation.removed && work.archived_at) items.push('remove_isolation')
  items.push('delete')
  return items
}

/** TB-37: Turkish warning for 409 work_active / revision_conflict and nested detail extraction on work actions. */
export function deleteWorkErrorMessage(e: unknown): string {
  const err = e as { status?: number; code?: string; message?: string; body?: { code?: string; detail?: { code?: string; detail?: string } | string } } | null
  const code = err?.code ?? err?.body?.code ?? (typeof err?.body?.detail === 'object' ? err?.body?.detail?.code : undefined)
  const nestedDetail = typeof err?.body?.detail === 'object' && typeof err?.body?.detail?.detail === 'string'
    ? err.body.detail.detail
    : typeof err?.body?.detail === 'string' ? err.body.detail : undefined
  if (err?.status === 409 && (code === 'work_active' || !code)) {
    return t('settings.err.work_active')
  }
  if (err?.status === 409 && code === 'revision_conflict') {
    return t('settings.err.revision_conflict')
  }
  if (err?.status === 404 || code === 'work_not_found') {
    return t('err.work_not_found')
  }
  if (err?.status === 403 && code === 'mobile_field') {
    return t('settings.err.mobile_field')
  }
  return nestedDetail || err?.message || String(e)
}
