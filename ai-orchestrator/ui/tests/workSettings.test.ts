// TB-41: per-work team priority order, quota wait mapping and 50-entry map bounds.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  WORK_SETTINGS_LIMIT,
  choiceToWorkWait,
  initialWorkTeam,
  initialWorkWait,
  putWorkTeam,
  putWorkWait,
  workSavedTeam,
  workWaitToChoice,
  type SettingsView,
  type Team,
} from '../src/settingsRules.ts'

const workTeam: Team = { author: ['codex@work'], reviewer: ['claude@work'] }
const latestTeam: Team = { author: ['codex@latest'], reviewer: ['claude@latest'] }
const globalDefault: Team = { author: ['codex@default'], reviewer: ['claude@default'] }
const policyFallback: Team = { author: ['codex'], reviewer: ['claude'] }

const makeView = (over: Partial<SettingsView['settings']> = {}): SettingsView => ({
  settings: {
    version: 1,
    team: globalDefault,
    accepted_digest: 'd1',
    theme: 'system',
    github: { clone_dir: null },
    work_teams: {},
    work_waits: {},
    ...over,
  },
  error: null,
  problems: [],
  current_digest: 'd1',
})

test('TB-41: initial work team priority is settings.work_teams[workId] -> work.latest_team -> default', () => {
  const viewWithWorkTeam = makeView({ work_teams: { w1: workTeam } })
  // 1. settings.work_teams[workId] wins over work.latest_team and defaultTeam
  assert.deepEqual(initialWorkTeam('w1', viewWithWorkTeam, latestTeam, policyFallback), workTeam)
  assert.deepEqual(workSavedTeam(viewWithWorkTeam, 'w1'), workTeam)

  // 2. When work_teams[workId] is absent, work.latest_team wins over defaultTeam
  assert.deepEqual(initialWorkTeam('w2', viewWithWorkTeam, latestTeam, policyFallback), latestTeam)

  // 3. When neither work_teams[workId] nor work.latest_team is set, defaultTeam(settings) wins over policyFallback
  assert.deepEqual(initialWorkTeam('w2', viewWithWorkTeam, null, policyFallback), globalDefault)

  // 4. When no settings default team exists, policyFallback is used
  const viewWithoutDefault = makeView({ team: null })
  assert.deepEqual(initialWorkTeam('w2', viewWithoutDefault, null, policyFallback), policyFallback)
  assert.deepEqual(initialWorkTeam('w2', null, null, policyFallback), policyFallback)
})

test('TB-41: putWorkTeam saves, resets and caps at 50 entries', () => {
  let map = putWorkTeam({}, 'w1', workTeam)
  assert.deepEqual(map, { w1: workTeam })

  // Resetting deletes the key
  map = putWorkTeam(map, 'w1', null)
  assert.deepEqual(map, {})

  // Capped at WORK_SETTINGS_LIMIT (50)
  let many: Record<string, Team> = {}
  for (let i = 0; i < WORK_SETTINGS_LIMIT + 5; i++) {
    many = putWorkTeam(many, `w_${i}`, workTeam)
  }
  assert.equal(Object.keys(many).length, WORK_SETTINGS_LIMIT)
  assert.equal('w_0' in many, false)
  assert.deepEqual(many[`w_${WORK_SETTINGS_LIMIT + 4}`], workTeam)
})

test('TB-41: work_waits mapping and initialWorkWait resolution', () => {
  const view = makeView({
    work_waits: {
      w_on: { enabled: true, deadline_hours: 8, max_attempts: 4 },
      w_defaults: { enabled: true },
      w_off: { enabled: false },
    },
  })

  assert.deepEqual(initialWorkWait('w_on', view), {
    wait_on_quota: true,
    wait_deadline_hours: 8,
    wait_max_attempts: 4,
  })
  assert.deepEqual(initialWorkWait('w_defaults', view), {
    wait_on_quota: true,
    wait_deadline_hours: 4,
    wait_max_attempts: 3,
  })
  assert.equal(initialWorkWait('w_off', view), null)
  assert.equal(initialWorkWait('w_missing', view), null)

  const waits = putWorkWait({}, 'w1', { wait_on_quota: true, wait_deadline_hours: 6, wait_max_attempts: 2 })
  assert.deepEqual(waits, { w1: { enabled: true, deadline_hours: 6, max_attempts: 2 } })
  assert.deepEqual(workWaitToChoice(waits.w1), { wait_on_quota: true, wait_deadline_hours: 6, wait_max_attempts: 2 })
  assert.deepEqual(choiceToWorkWait(null), { enabled: false })
})
