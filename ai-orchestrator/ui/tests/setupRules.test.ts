import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  SETUP_HIDDEN_KEY,
  allDone,
  hideSetup,
  isSetupHidden,
  doctorStatus,
  doctorWizardStatuses,
  setupSteps,
  type SetupAccount,
  type SetupCatalog,
  type SetupSettings,
} from '../src/setupRules.ts'

const makeAccount = (over: Partial<SetupAccount> = {}): SetupAccount => ({
  base: 'agy@h1',
  provider: 'agy',
  account: 'h1',
  installed: true,
  login: { command: 'HOME=/tmp/agy-h1 agy login', note: null },
  ...over,
})

const validSettings: SetupSettings = {
  settings: {
    team: { author: ['claude/claude-sonnet-4-5'], reviewer: ['codex/gpt-5'] },
    accepted_digest: 'sha256:abc123',
  },
}

test('hiçbir şey yok → 4 adım bekliyor', () => {
  const steps = setupSteps({
    accounts: [],
    catalog: { entries: [], errors: {} },
    settings: null,
    workCount: 0,
  })
  assert.equal(steps.length, 4)
  assert.deepEqual(
    steps.map(s => [s.key, s.done]),
    [
      ['cli', false],
      ['auth', false],
      ['team', false],
      ['work', false],
    ],
  )
  assert.ok(steps[0].note?.includes('docs/KULLANIM-KILAVUZU.md'))
  assert.equal(steps[2].note, "Ayarlar'da takımı seç ve kaydet")
  assert.equal(allDone(steps), false)
  assert.equal(allDone({ accounts: [], catalog: { entries: [], errors: {} }, settings: null, workCount: 0 }), false)
})

test('claude kurulu ama katalogda liste yok → adım 2 tamam (Doğrulanmadı)', () => {
  const accounts: SetupAccount[] = [
    makeAccount({
      base: 'claude',
      provider: 'claude',
      account: null,
      installed: true,
      login: { command: 'claude login', note: null },
    }),
  ]
  const catalog: SetupCatalog = { entries: [], errors: {} }
  const steps = setupSteps({ accounts, catalog, settings: null, workCount: 0 })

  assert.equal(steps[0].done, true)
  assert.equal(steps[1].key, 'auth')
  assert.equal(steps[1].done, true)
  assert.equal(steps[1].unverified, true)
  assert.equal(steps[1].note, 'Doğrulanmadı')
  assert.equal(steps[1].loginCommand, null)
})

test('agy unauthenticated → adım 2 bekliyor ve login komutu o hesabınki', () => {
  const agyCmd = 'HOME=/Users/test/.system/agy-h1 agy login'
  const accounts: SetupAccount[] = [
    makeAccount({
      base: 'agy@h1',
      provider: 'agy',
      account: 'h1',
      installed: true,
      login: { command: agyCmd, note: null },
    }),
  ]
  const catalog: SetupCatalog = {
    entries: [
      {
        identity: 'agy@h1/gemini-3.1-pro-high',
        provider: 'agy',
        account: 'h1',
        model: 'gemini-3.1-pro-high',
        status: 'available',
        source: 'live',
      },
    ],
    errors: { 'agy@h1': 'unauthenticated' },
  }
  const steps = setupSteps({ accounts, catalog, settings: null, workCount: 0 })

  assert.equal(steps[0].done, true)
  assert.equal(steps[1].done, false)
  assert.equal(steps[1].loginAccount, 'agy@h1')
  assert.equal(steps[1].loginCommand, agyCmd)
  assert.equal(allDone(steps), false)
})

test('hepsi tamam → allDone true', () => {
  const accounts: SetupAccount[] = [
    makeAccount({
      base: 'codex',
      provider: 'codex',
      account: null,
      installed: true,
      login: { command: 'codex login', note: null },
    }),
  ]
  const catalog: SetupCatalog = {
    entries: [
      {
        identity: 'codex/gpt-5.3-codex',
        provider: 'codex',
        account: null,
        model: 'gpt-5.3-codex',
        status: 'available',
        source: 'live',
      },
    ],
    errors: {},
  }
  const input = {
    accounts,
    catalog,
    settings: validSettings,
    workCount: 2,
  }
  const steps = setupSteps(input)

  assert.ok(steps.every(s => s.done))
  assert.equal(steps[1].unverified, false)
  assert.equal(allDone(steps), true)
  assert.equal(allDone(input), true)
})

test('yükleme hatası olursa ilgili adım okunamadı olarak işaretlenir ve çökmez', () => {
  const steps = setupSteps({
    accounts: null,
    catalog: null,
    settings: null,
    workCount: 0,
    errors: { cli: true, auth: true, team: true },
  })
  assert.equal(steps[0].error, true)
  assert.equal(steps[0].note, 'okunamadı')
  assert.equal(steps[1].error, true)
  assert.equal(steps[1].note, 'okunamadı')
  assert.equal(steps[2].error, true)
  assert.equal(steps[2].note, 'okunamadı')
  assert.equal(allDone(steps), false)
})

test('pb.setupHidden localStorage yardımcıları hata fırlatmadan çalışır', () => {
  const store = new Map<string, string>()
  const fakeStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v) },
  }
  assert.equal(isSetupHidden(fakeStorage), false)
  hideSetup(fakeStorage)
  assert.equal(store.get(SETUP_HIDDEN_KEY), '1')
  assert.equal(isSetupHidden(fakeStorage), true)

  const throwingStorage = {
    getItem: (): string | null => { throw new Error('denied') },
    setItem: (): void => { throw new Error('denied') },
  }
  assert.equal(isSetupHidden(throwingStorage), false)
  assert.doesNotThrow(() => hideSetup(throwingStorage))
})

test('doctor denetimleri kurulum sihirbazı durumlarına eşlenir', () => {
  const checks = [
    { key: 'python', level: 'OK' as const, detail: '3.12' },
    { key: 'providers', level: 'FAIL' as const, detail: 'missing' },
    { key: 'cli:claude', level: 'OK' as const, detail: 'installed' },
    { key: 'cli:codex', level: 'FAIL' as const, detail: 'missing' },
    { key: 'panel', level: 'OK' as const, detail: 'built' },
    { key: 'node', level: 'WARN' as const, detail: 'old' },
  ]
  assert.equal(doctorStatus(checks, 'python'), 'complete')
  assert.equal(doctorStatus(checks, 'providers'), 'missing')
  assert.equal(doctorStatus(checks, 'server'), 'checking')
  assert.deepEqual(doctorWizardStatuses(checks), {
    python: 'complete', providers: 'missing', clis: 'missing', panel: 'complete', node: 'warning',
  })
})
