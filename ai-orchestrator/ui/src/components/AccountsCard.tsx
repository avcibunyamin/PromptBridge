import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { LoaderCircle, RefreshCw } from 'lucide-react'
import {
  ACCOUNT_NAME_RE,
  PROVIDER_LABELS,
  PROVIDER_ORDER,
  ROLE_LABELS,
  formatAccountError,
  groupAccountsByProvider,
  isValidAccountName,
  providerRequiresAccount,
  providerSupportsAccount,
  validateAccountInput,
  type AccountGroup,
  type AccountItem,
  type AccountLogin,
  type AccountProvider,
  type AccountRole,
  type AddAccountInput,
} from '../accountRules'
import { addAccount, listAccounts, removeAccount } from '../api'
import { mobile } from '../mobile'

export {
  ACCOUNT_NAME_RE,
  PROVIDER_LABELS,
  PROVIDER_ORDER,
  ROLE_LABELS,
  formatAccountError,
  groupAccountsByProvider,
  isValidAccountName,
  providerRequiresAccount,
  providerSupportsAccount,
  validateAccountInput,
  type AccountGroup,
  type AccountItem,
  type AccountLogin,
  type AccountProvider,
  type AccountRole,
  type AddAccountInput,
}

/** TB-39: Mac-only provider account management card in Settings.
 *  Never asks for or stores passwords/tokens; users log in directly in their own Terminal. */
export function AccountsCard({ onChanged }: { onChanged?: () => void | Promise<void> } = {}) {
  const [items, setItems] = useState<AccountItem[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [provider, setProvider] = useState<AccountProvider>('agy')
  const [account, setAccount] = useState('')
  const [roles, setRoles] = useState<AccountRole[]>(['author', 'reviewer'])
  const [justAdded, setJustAdded] = useState<AccountItem | null>(null)
  const [copiedKey, setCopiedKey] = useState<string | null>(null)
  const [confirmBase, setConfirmBase] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const res = await listAccounts()
      setItems(Array.isArray(res) ? res : (res?.accounts ?? []))
    } catch (e) {
      setError(formatAccountError(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (mobile.active) return
    void load()
  }, [load])

  const groups = useMemo(() => groupAccountsByProvider(items ?? []), [items])

  // Hook'lar koşulsuz olarak erken dönüşten ÖNCE çağrıldı (React #310 önlemi).
  if (mobile.active) return null

  function toggleRole(role: AccountRole, checked: boolean) {
    setRoles(prev => {
      const next = checked ? [...prev, role] : prev.filter(r => r !== role)
      return (['author', 'reviewer'] as const).filter(r => next.includes(r))
    })
  }

  async function copyCommand(key: string, command: string) {
    try {
      await navigator.clipboard?.writeText(command)
      setCopiedKey(key)
      setTimeout(() => setCopiedKey(prev => (prev === key ? null : prev)), 2000)
    } catch {
      setError('Komut panoya kopyalanamadı; satırı seçip elle kopyalayabilirsiniz.')
    }
  }

  async function handleAdd(e: FormEvent) {
    e.preventDefault()
    setError('')
    const validated = validateAccountInput(provider, account, roles)
    if (!validated.ok) {
      setError(validated.error)
      return
    }
    setBusy(true)
    try {
      const res = await addAccount(validated.value)
      const refreshed = await listAccounts().catch(() => ({ accounts: res.accounts ?? [] }))
      const nextItems = Array.isArray(refreshed) ? refreshed : (refreshed?.accounts ?? res.accounts ?? [])
      setItems(nextItems)
      const created = res.account ?? nextItems.find(item => item.base === validated.base) ?? null
      setJustAdded(created)
      setAccount('')
      void onChanged?.()
    } catch (err) {
      setError(formatAccountError(err))
    } finally {
      setBusy(false)
    }
  }

  async function handleRemove(base: string) {
    setError('')
    setBusy(true)
    try {
      const res = await removeAccount(base)
      const refreshed = await listAccounts().catch(() => ({ accounts: res.accounts ?? [] }))
      const nextItems = Array.isArray(refreshed) ? refreshed : (refreshed?.accounts ?? res.accounts ?? [])
      setItems(nextItems)
      setConfirmBase(null)
      if (justAdded?.base === base) setJustAdded(null)
      void onChanged?.()
    } catch (err) {
      setConfirmBase(null)
      setError(formatAccountError(err))
    } finally {
      setBusy(false)
    }
  }

  const supportsAccount = providerSupportsAccount(provider)
  const requiresAccount = providerRequiresAccount(provider)

  return (
    <section className="card accounts-card">
      <div className="card-h">
        <h2>Hesaplar</h2>
        <span className="spacer" />
        <button
          type="button"
          className="btn small"
          disabled={loading || busy}
          aria-busy={loading}
          onClick={() => void load()}
        >
          <RefreshCw className={loading ? 'spin' : undefined} aria-hidden />
          {loading ? 'Yenileniyor…' : 'Yenile'}
        </button>
      </div>
      <div className="card-b">
        <p className="muted small">
          İzinli sağlayıcı hesaplarını (<code>providers.yaml</code>) bu Mac üzerinden yönetin.{' '}
          <strong>Panel parolayı ya da jetonu asla istemez ve saklamaz;</strong> hesabı ekledikten sonra
          verilen giriş komutunu kendi Terminal&apos;inizde çalıştırarak doğrudan sağlayıcı CLI&apos;ı ile oturum açarsınız.
        </p>

        {error && (
          <div className="error-banner" role="alert">
            {error}
          </div>
        )}

        {justAdded && (
          <div className="notice account-added-banner" role="status">
            <p>
              <strong>{justAdded.base}</strong> eklendi.{' '}
              <strong>Şimdi bu komutu kendi Terminal&apos;inde çalıştırıp giriş yap:</strong>
            </p>
            {justAdded.login.command ? (
              <div className="mobile-cmd account-login">
                <code className="mono">{justAdded.login.command}</code>
                <button
                  type="button"
                  className="btn small"
                  onClick={() => void copyCommand(`added:${justAdded.base}`, justAdded.login.command!)}
                >
                  {copiedKey === `added:${justAdded.base}` ? 'Kopyalandı' : 'Kopyala'}
                </button>
              </div>
            ) : (
              justAdded.login.note && <p className="mono small">{justAdded.login.note}</p>
            )}
          </div>
        )}

        {!items ? (
          <p className="loading">Yükleniyor…</p>
        ) : groups.length === 0 ? (
          <p className="muted small">Kayıtlı hesap yok.</p>
        ) : (
          <div className="accounts-groups">
            {groups.map(group => (
              <div className="accounts-group" key={group.provider}>
                <h3 className="accounts-group-title">{group.label}</h3>
                <ul className="accounts-list">
                  {group.items.map(item => {
                    const isHighlighted = justAdded?.base === item.base
                    const isConfirming = confirmBase === item.base
                    return (
                      <li
                        key={item.base}
                        className={isHighlighted ? 'account-row highlighted' : 'account-row'}
                      >
                        <div className="account-head">
                          <span className="mono account-base">{item.base}</span>
                          <div className="account-badges">
                            {item.roles.includes('author') && <span className="pill">{ROLE_LABELS.author}</span>}
                            {item.roles.includes('reviewer') && <span className="pill">{ROLE_LABELS.reviewer}</span>}
                            {item.in_default_team && <span className="pill ok">Varsayılan takımda</span>}
                            {!item.installed && <span className="pill warn">Kurulu değil</span>}
                          </div>
                          <span className="spacer" />
                          <div className="account-actions">
                            {item.in_default_team && (
                              <span className="muted small">Varsayılan takımda olduğu için kaldırılamaz</span>
                            )}
                            <button
                              type="button"
                              className="btn small"
                              disabled={busy || item.in_default_team}
                              title={
                                item.in_default_team
                                  ? 'Varsayılan takımda kullanıldığı için kaldırılamaz'
                                  : `${item.base} hesabını kaldır`
                              }
                              onClick={() => {
                                setError('')
                                setConfirmBase(item.base)
                              }}
                            >
                              Kaldır
                            </button>
                          </div>
                        </div>

                        <div className="mobile-cmd account-login">
                          {item.login.command ? (
                            <>
                              <code className="mono">{item.login.command}</code>
                              <button
                                type="button"
                                className="btn small"
                                onClick={() => void copyCommand(item.base, item.login.command!)}
                              >
                                {copiedKey === item.base ? 'Kopyalandı' : 'Kopyala'}
                              </button>
                              {item.login.note && <span className="muted small">{item.login.note}</span>}
                            </>
                          ) : (
                            item.login.note && <span className="mono muted small">{item.login.note}</span>
                          )}
                        </div>

                        {isConfirming && (
                          <div
                            className="reaccept account-confirm"
                            role="dialog"
                            aria-modal="true"
                            aria-label={`${item.base} hesabını kaldır onayı`}
                          >
                            <p className="small">
                              <strong>{item.base}</strong> hesabını izinli sağlayıcı listesinden kaldırmak
                              istediğinize emin misiniz? Hesabın <code>HOME</code> klasörü silinmez.
                            </p>
                            <div className="team-actions">
                              <button
                                type="button"
                                className="btn small danger"
                                disabled={busy}
                                onClick={() => void handleRemove(item.base)}
                              >
                                {busy ? <LoaderCircle className="spin" aria-hidden /> : null}
                                Evet, kaldır
                              </button>
                              <button
                                type="button"
                                className="btn small"
                                disabled={busy}
                                onClick={() => setConfirmBase(null)}
                              >
                                Vazgeç
                              </button>
                            </div>
                          </div>
                        )}
                      </li>
                    )
                  })}
                </ul>
              </div>
            ))}
          </div>
        )}

        <form className="account-form form-fields" onSubmit={e => void handleAdd(e)}>
          <h3 className="accounts-group-title">Hesap ekle</h3>
          <div className="team-selects">
            <label htmlFor="acc-provider">
              Sağlayıcı
              <select
                id="acc-provider"
                value={provider}
                disabled={busy}
                onChange={e => {
                  const next = e.target.value as AccountProvider
                  setProvider(next)
                  if (!providerSupportsAccount(next)) setAccount('')
                }}
              >
                <option value="agy">Antigravity (agy)</option>
                <option value="codex">Codex</option>
                <option value="claude">Claude</option>
                <option value="ollama">Ollama</option>
              </select>
            </label>

            <label htmlFor="acc-name">
              Hesap adı {requiresAccount ? '(zorunlu)' : supportsAccount ? '(isteğe bağlı)' : '(kullanılmaz)'}
              <input
                id="acc-name"
                className="mono"
                value={supportsAccount ? account : ''}
                disabled={busy || !supportsAccount}
                maxLength={16}
                placeholder={
                  requiresAccount
                    ? 'hesap1 (küçük harf/rakam/tire, en çok 16)'
                    : supportsAccount
                      ? 'varsayılan için boş bırakın veya hesap2'
                      : 'Bu sağlayıcıda hesap adı yok'
                }
                onChange={e => setAccount(e.target.value)}
              />
            </label>
          </div>

          <div className="account-roles" role="group" aria-label="Roller">
            <span className="account-roles-label">Roller:</span>
            <label className="account-role-check">
              <input
                type="checkbox"
                checked={roles.includes('author')}
                disabled={busy}
                onChange={e => toggleRole('author', e.target.checked)}
              />
              <span>Yazar</span>
            </label>
            <label className="account-role-check">
              <input
                type="checkbox"
                checked={roles.includes('reviewer')}
                disabled={busy}
                onChange={e => toggleRole('reviewer', e.target.checked)}
              />
              <span>İnceleyici</span>
            </label>
          </div>

          <div className="form-footer">
            <span className="hint small">
              Antigravity (<code>agy</code>) için hesap adı zorunludur (küçük harf, rakam, tire; en çok 16 karakter)
              ve ayrı bir <code>HOME</code> + anahtar zinciri hazırlanır.
            </span>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? <LoaderCircle className="spin" aria-hidden /> : null}
              Hesap ekle
            </button>
          </div>
        </form>
      </div>
    </section>
  )
}
