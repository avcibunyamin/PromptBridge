/** TB-19 (K-026) / K-033 / TB-37: panel preferences in .system/settings.json (phone command scope reads/writes theme, default team and pinned_works). */
import { checkedFetch } from './api'
import type { SettingsPatch, SettingsView } from './settingsRules'
export * from './settingsRules'

export async function loadSettings(): Promise<SettingsView | null> {
  try { return await (await checkedFetch('/api/settings')).json() as SettingsView } catch { return null }
}

export async function saveSettings(patch: SettingsPatch): Promise<SettingsView> {
  const r = await checkedFetch('/api/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) })
  return await r.json() as SettingsView
}
