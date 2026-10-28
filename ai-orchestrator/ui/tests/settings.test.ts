// TB-19 (K-026): default team, K1 acceptance and theme rules.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { acceptedDigest, defaultTeam, resolveTheme, type SettingsView } from '../src/settingsRules.ts'
import { SETTINGS_TABS, availableSettingsTabs, settingsTab } from '../src/settings.ts'
import { chainNeedsAcceptance } from '../src/components/work/flows.ts'

const team = { author: ['claude/claude-sonnet-5-5~low'], reviewer: ['agy@h2/gemini-3.1-pro-high'] }
const view = (over: Partial<SettingsView> = {}, saved = 'd1'): SettingsView => ({
  settings: { version: 1, team, accepted_digest: saved, theme: 'system', github: { clone_dir: null } },
  error: null, problems: [], current_digest: 'd1', ...over,
})

test('the saved team is the default only while the ceiling allows it', () => {
  assert.deepEqual(defaultTeam(view()), team)
  assert.equal(defaultTeam(view({ problems: [{ reason: 'provider_not_allowed' }] })), null)
  assert.equal(defaultTeam(null), null)                                        // the phone: no settings
})

test('K1: the saved digest skips the box only while it is still the current one', () => {
  assert.equal(acceptedDigest(view()), 'd1')
  assert.equal(acceptedDigest(view({ current_digest: 'd2' })), null)             // providers.yaml changed
  assert.equal(acceptedDigest(view({ current_digest: null })), null)             // the team is no longer allowed
  assert.equal(chainNeedsAcceptance('d1', null, acceptedDigest(view())), false)   // a new work with the default team
  assert.equal(chainNeedsAcceptance('d9', null, acceptedDigest(view())), true)    // another model chosen
  assert.equal(chainNeedsAcceptance('d1', null, null), true)                      // no saved default: as before
})

test('theme: system follows the OS', () => {
  assert.equal(resolveTheme('system', true), 'dark')
  assert.equal(resolveTheme('system', false), 'light')
  assert.equal(resolveTheme('light', true), 'light')
})

test('settingsTab: resolves valid #/settings/<tab> keys over stored preference', () => {
  for (const key of SETTINGS_TABS) {
    assert.equal(settingsTab(`#/settings/${key}`, 'team'), key)
  }
  assert.equal(settingsTab('#/settings/models/', 'accounts'), 'models')
  assert.equal(settingsTab('integrations', 'team'), 'integrations')
})

test('settingsTab: falls back to stored tab, then team, when hash key is missing or invalid', () => {
  assert.equal(settingsTab('#/settings', 'accounts'), 'accounts')
  assert.equal(settingsTab('#/settings/', 'models'), 'models')
  assert.equal(settingsTab('#/settings/unknown', 'appearance'), 'appearance')
  assert.equal(settingsTab('#/settings/team/extra', 'phone'), 'phone')
  assert.equal(settingsTab('#/team', 'models'), 'models')
  assert.equal(settingsTab('', 'integrations'), 'integrations')
  assert.equal(settingsTab('#/settings', null), 'team')
  assert.equal(settingsTab('#/settings/unknown', 'invalid'), 'team')
  assert.equal(settingsTab(null, undefined), 'team')
})

test('settingsTab & availableSettingsTabs: hides empty tabs on mobile and resolves within available tabs', () => {
  const mac = availableSettingsTabs({ mobile: false, canCommand: true })
  assert.deepEqual(mac, ['team', 'accounts', 'models', 'appearance', 'integrations', 'phone'])

  const commandPhone = availableSettingsTabs({ mobile: true, canCommand: true })
  assert.deepEqual(commandPhone, ['team', 'models', 'appearance', 'phone'])
  assert.equal(settingsTab('#/settings/accounts', 'models', commandPhone), 'models')
  assert.equal(settingsTab('#/settings/integrations', 'accounts', commandPhone), 'team')

  const readOnlyPhone = availableSettingsTabs({ mobile: true, canCommand: false })
  assert.deepEqual(readOnlyPhone, ['appearance', 'phone'])
  assert.equal(settingsTab('#/settings/phone', null, readOnlyPhone), 'phone')
  assert.equal(settingsTab('#/settings', 'phone', readOnlyPhone), 'phone')
  assert.equal(settingsTab('#/settings', null, readOnlyPhone), 'appearance')
  assert.equal(settingsTab('#/settings/team', 'models', readOnlyPhone), 'appearance')
})
