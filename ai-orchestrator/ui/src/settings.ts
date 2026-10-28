/** TB-19 (K-026) / K-033 / TB-37: panel preferences in .system/settings.json (phone command scope reads/writes theme, default team and pinned_works). */
import type { SettingsPatch, SettingsView } from './settingsRules.ts'
export * from './settingsRules.ts'

export const SETTINGS_TABS = ['team', 'accounts', 'models', 'appearance', 'integrations', 'phone'] as const
export type SettingsTab = (typeof SETTINGS_TABS)[number]
export const SETTINGS_TAB_STORAGE_KEY = 'conductor-settings-tab'

/** Visible settings tabs for the current device/scope; tabs that would have no cards are omitted. */
export function availableSettingsTabs(opts: { mobile: boolean; canCommand: boolean }): SettingsTab[] {
  return SETTINGS_TABS.filter(tab => {
    if (tab === 'team' || tab === 'models') return opts.canCommand
    if (tab === 'accounts') return !opts.mobile && opts.canCommand
    if (tab === 'integrations') return !opts.mobile
    return true
  })
}

/** Pure URL/storage resolver for #/settings/<tab>. Falls back to stored tab, then 'team' (or first available). */
export function settingsTab(
  hash: string | null | undefined,
  stored?: string | null,
  available: readonly SettingsTab[] = SETTINGS_TABS,
): SettingsTab {
  const raw = extractSettingsTabKey(hash)
  if (raw && (available as readonly string[]).includes(raw)) return raw as SettingsTab
  if (stored && (available as readonly string[]).includes(stored)) return stored as SettingsTab
  if ((available as readonly string[]).includes('team')) return 'team'
  return available[0] ?? 'team'
}

function extractSettingsTabKey(hash: string | null | undefined): string {
  if (!hash) return ''
  const clean = hash.split('?')[0].replace(/\/+$/, '')
  if (clean.startsWith('#/settings/')) return clean.slice('#/settings/'.length)
  if (clean.startsWith('/settings/')) return clean.slice('/settings/'.length)
  if (clean.startsWith('#') || clean.startsWith('/')) return ''
  return clean
}

export function readStoredSettingsTab(): string | null {
  try { return localStorage.getItem(SETTINGS_TAB_STORAGE_KEY) } catch { return null }
}

export function writeStoredSettingsTab(tab: SettingsTab): void {
  try { localStorage.setItem(SETTINGS_TAB_STORAGE_KEY, tab) } catch { /* Storage may be unavailable. */ }
}

export async function loadSettings(): Promise<SettingsView | null> {
  try {
    const { checkedFetch } = await import('./api.ts')
    return await (await checkedFetch('/api/settings')).json() as SettingsView
  } catch { return null }
}

export async function saveSettings(patch: SettingsPatch): Promise<SettingsView> {
  const { checkedFetch } = await import('./api.ts')
  const r = await checkedFetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
  return await r.json() as SettingsView
}
