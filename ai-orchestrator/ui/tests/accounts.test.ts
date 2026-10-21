import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  formatAccountError,
  groupAccountsByProvider,
  isValidAccountName,
  providerRequiresAccount,
  providerSupportsAccount,
  validateAccountInput,
  type AccountItem,
} from '../src/accountRules.ts'

test('TB-39: account name validation matches ACCOUNT_RE (lowercase, digits, hyphen, max 16)', () => {
  assert.equal(isValidAccountName('a'), true)
  assert.equal(isValidAccountName('hesap1'), true)
  assert.equal(isValidAccountName('is-2'), true)
  assert.equal(isValidAccountName('1234567890123456'), true)
  assert.equal(isValidAccountName('12345678901234567'), false)
  assert.equal(isValidAccountName(''), false)
  assert.equal(isValidAccountName('-hesap'), false)
  assert.equal(isValidAccountName('Hesap1'), false)
  assert.equal(isValidAccountName('hesap_1'), false)
  assert.equal(isValidAccountName('hesap@1'), false)
})

test('TB-39: validateAccountInput enforces provider-specific account and role rules', () => {
  assert.equal(providerRequiresAccount('agy'), true)
  assert.equal(providerSupportsAccount('agy'), true)
  assert.equal(providerRequiresAccount('codex'), false)
  assert.equal(providerSupportsAccount('codex'), true)
  assert.equal(providerSupportsAccount('claude'), false)
  assert.equal(providerSupportsAccount('ollama'), false)

  const agyOk = validateAccountInput('agy', '  hesap-2 ', ['reviewer', 'author'])
  assert.deepEqual(agyOk, {
    ok: true,
    value: { provider: 'agy', account: 'hesap-2', roles: ['author', 'reviewer'] },
    base: 'agy@hesap-2',
  })

  const agyMissing = validateAccountInput('agy', '   ', ['author'])
  assert.equal(agyMissing.ok, false)

  const agyBad = validateAccountInput('agy', 'Hesap!', ['author'])
  assert.equal(agyBad.ok, false)

  const codexDefault = validateAccountInput('codex', '', ['author'])
  assert.deepEqual(codexDefault, {
    ok: true,
    value: { provider: 'codex', account: null, roles: ['author'] },
    base: 'codex',
  })

  const codexNamed = validateAccountInput('codex', 'yedek1', ['reviewer'])
  assert.deepEqual(codexNamed, {
    ok: true,
    value: { provider: 'codex', account: 'yedek1', roles: ['reviewer'] },
    base: 'codex@yedek1',
  })

  const claudeNamed = validateAccountInput('claude', 'hesap1', ['author'])
  assert.equal(claudeNamed.ok, false)

  const claudeOk = validateAccountInput('claude', null, ['author'])
  assert.deepEqual(claudeOk, {
    ok: true,
    value: { provider: 'claude', account: null, roles: ['author'] },
    base: 'claude',
  })

  const noRoles = validateAccountInput('agy', 'hesap1', [])
  assert.equal(noRoles.ok, false)
})

test('TB-39: groupAccountsByProvider groups in canonical provider order and skips empty providers', () => {
  const item = (base: string, provider: AccountItem['provider'], account: string | null): AccountItem => ({
    base,
    provider,
    account,
    roles: ['author'],
    in_default_team: false,
    installed: true,
    login: { command: null, note: null },
  })
  const items: AccountItem[] = [
    item('claude', 'claude', null),
    item('agy@hesap1', 'agy', 'hesap1'),
    item('codex', 'codex', null),
    item('agy@hesap2', 'agy', 'hesap2'),
  ]
  const groups = groupAccountsByProvider(items)
  assert.deepEqual(
    groups.map(g => ({ provider: g.provider, label: g.label, bases: g.items.map(i => i.base) })),
    [
      { provider: 'agy', label: 'Antigravity (agy)', bases: ['agy@hesap1', 'agy@hesap2'] },
      { provider: 'codex', label: 'Codex', bases: ['codex'] },
      { provider: 'claude', label: 'Claude', bases: ['claude'] },
    ],
  )
})

test('TB-39: formatAccountError translates exists, in_default_team, and validation_error to Turkish', () => {
  const existsErr = { status: 409, body: { detail: { code: 'exists', detail: 'agy@hesap1 zaten tanımlı' } } }
  assert.match(formatAccountError(existsErr), /zaten kayıtlı|zaten tanımlı/i)

  const defaultTeamErr = { status: 409, code: 'in_default_team', body: { detail: { code: 'in_default_team', detail: 'agy@hesap1 varsayılan takımda kullanılıyor' } } }
  assert.match(formatAccountError(defaultTeamErr), /varsayılan takımda kullanıldığı için kaldırılamaz/i)

  const invalidErr = { status: 422, code: 'validation_error', body: { detail: { code: 'validation_error', detail: 'agy için geçerli bir hesap adı zorunludur' } } }
  assert.match(formatAccountError(invalidErr), /Geçersiz hesap bilgisi/)
})

test('loginNote shows known providers in the UI language and passes others through', async () => {
  const { loginNote } = await import('../src/accountRules.ts')
  assert.equal(loginNote('agy', 'Ayrı HOME ve anahtar zinciri ile giriş'), 'Ayrı HOME ve anahtar zinciri ile giriş')
  assert.equal(loginNote('codex', 'x'), 'Ayrı CODEX_HOME ile giriş')
  assert.equal(loginNote('ollama', 'ollama serve'), 'ollama serve')
  assert.equal(loginNote('agy', null), null)
})
