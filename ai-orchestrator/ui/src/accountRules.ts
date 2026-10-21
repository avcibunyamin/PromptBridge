/** TB-39: pure rules and types for the Mac-only Accounts card in Settings (node-testable, no fetch). */

export type AccountProvider = 'agy' | 'codex' | 'claude' | 'ollama'
export type AccountRole = 'author' | 'reviewer'

export interface AccountLogin {
  command: string | null
  note: string | null
}

export interface AccountItem {
  base: string
  provider: AccountProvider
  account: string | null
  roles: AccountRole[]
  in_default_team: boolean
  installed: boolean
  login: AccountLogin
}

export interface AddAccountInput {
  provider: AccountProvider
  account: string | null
  roles: AccountRole[]
}

export interface AccountGroup {
  provider: AccountProvider
  label: string
  items: AccountItem[]
}

export const ACCOUNT_NAME_RE = /^[a-z0-9][a-z0-9-]{0,15}$/

export const PROVIDER_ORDER: readonly AccountProvider[] = ['agy', 'codex', 'claude', 'ollama']

export const PROVIDER_LABELS: Record<AccountProvider, string> = {
  agy: 'Antigravity (agy)',
  codex: 'Codex',
  claude: 'Claude',
  ollama: 'Ollama',
}

export const ROLE_LABELS: Record<AccountRole, string> = {
  author: 'Yazar',
  reviewer: 'İnceleyici',
}

export function isValidAccountName(value: string): boolean {
  return ACCOUNT_NAME_RE.test(value)
}

export function providerSupportsAccount(provider: AccountProvider): boolean {
  return provider === 'agy' || provider === 'codex'
}

export function providerRequiresAccount(provider: AccountProvider): boolean {
  return provider === 'agy'
}

export type ValidatedAccountInput =
  | { ok: true; value: AddAccountInput; base: string }
  | { ok: false; error: string }

export function validateAccountInput(
  provider: AccountProvider,
  rawAccount: string | null | undefined,
  rawRoles: readonly AccountRole[],
): ValidatedAccountInput {
  const trimmed = (rawAccount ?? '').trim()

  if (provider === 'agy') {
    if (!trimmed) {
      return { ok: false, error: 'Antigravity (agy) için hesap adı zorunludur.' }
    }
    if (!isValidAccountName(trimmed)) {
      return {
        ok: false,
        error: 'Geçersiz hesap adı: küçük harf veya rakamla başlamalı, yalnız küçük harf, rakam ve tire içermeli (en çok 16 karakter).',
      }
    }
  } else if (provider === 'codex') {
    if (trimmed && !isValidAccountName(trimmed)) {
      return {
        ok: false,
        error: 'Geçersiz hesap adı: küçük harf veya rakamla başlamalı, yalnız küçük harf, rakam ve tire içermeli (en çok 16 karakter).',
      }
    }
  } else if (trimmed) {
    return { ok: false, error: `${PROVIDER_LABELS[provider]} için hesap adı kullanılmaz.` }
  }

  const roles: AccountRole[] = (['author', 'reviewer'] as const).filter(r => rawRoles.includes(r))
  if (roles.length === 0) {
    return { ok: false, error: 'En az bir rol (Yazar veya İnceleyici) seçilmelidir.' }
  }

  const account = providerSupportsAccount(provider) && trimmed ? trimmed : null
  const base = account ? `${provider}@${account}` : provider
  return {
    ok: true,
    value: { provider, account, roles },
    base,
  }
}

export function groupAccountsByProvider(items: readonly AccountItem[]): AccountGroup[] {
  const byProvider = new Map<AccountProvider, AccountItem[]>()
  for (const item of items) {
    const list = byProvider.get(item.provider) ?? []
    list.push(item)
    byProvider.set(item.provider, list)
  }

  const orderedProviders: AccountProvider[] = [...PROVIDER_ORDER]
  for (const p of byProvider.keys()) {
    if (!orderedProviders.includes(p)) orderedProviders.push(p)
  }

  const groups: AccountGroup[] = []
  for (const provider of orderedProviders) {
    const groupItems = byProvider.get(provider)
    if (groupItems && groupItems.length > 0) {
      groups.push({
        provider,
        label: PROVIDER_LABELS[provider] ?? provider,
        items: groupItems,
      })
    }
  }
  return groups
}

/** Extracts error code/detail from FastAPI HTTPException bodies (`{detail: {code, detail}}` or `{code, detail}`)
 *  and returns a clear Turkish message. */
export function formatAccountError(err: unknown): string {
  const obj = (typeof err === 'object' && err !== null ? err : {}) as Record<string, unknown>
  const body = (typeof obj.body === 'object' && obj.body !== null ? obj.body : {}) as Record<string, unknown>
  const nested = (typeof body.detail === 'object' && body.detail !== null && !Array.isArray(body.detail)
    ? body.detail
    : {}) as Record<string, unknown>

  const code =
    (typeof obj.code === 'string' && obj.code) ||
    (typeof body.code === 'string' && body.code) ||
    (typeof nested.code === 'string' && nested.code) ||
    ''

  const detail =
    (typeof nested.detail === 'string' && nested.detail) ||
    (typeof body.detail === 'string' && body.detail) ||
    (typeof obj.message === 'string' && obj.message) ||
    ''

  if (code === 'exists') {
    return detail ? `Bu hesap zaten kayıtlı (${detail}).` : 'Bu hesap zaten izinli listesinde mevcut.'
  }
  if (code === 'in_default_team') {
    return 'Bu hesap varsayılan takımda kullanıldığı için kaldırılamaz. Önce Varsayılan takım kartından çıkarın.'
  }
  if (code === 'validation_error') {
    if (/en çok|max|allowed|fazla/i.test(detail)) {
      return `İzinli hesap sınırı aşıldı: ${detail}`
    }
    return detail
      ? `Geçersiz hesap bilgisi: ${detail}`
      : 'Geçersiz hesap adı veya rol seçimi (küçük harf, rakam ve tire; en çok 16 karakter).'
  }
  if (code === 'not_found') {
    return 'Hesap bulunamadı.'
  }
  if (code === 'policy_invalid') {
    return detail ? `Sağlayıcı yapılandırması geçersiz: ${detail}` : 'providers.yaml dosyası okunamadı.'
  }

  const status = typeof obj.status === 'number' ? obj.status : 0
  if (status === 409) {
    return detail || 'Bu işlem mevcut hesap veya varsayılan takım durumuyla çakışıyor.'
  }
  if (status === 422) {
    return 'Geçersiz hesap adı veya rol seçimi (küçük harf, rakam ve tire; en çok 16 karakter).'
  }
  if (status === 403) {
    return 'Yetkisiz istek: hesap yönetimi yalnız bu Mac üzerindeki yerel panelden yapılabilir.'
  }

  return detail || String(err)
}
