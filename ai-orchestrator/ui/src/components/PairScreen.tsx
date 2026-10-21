import { useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { saveToken } from '../mobile'
import { t } from '../i18n'

const reasons: Record<string, string> = {
  wrong_code: t('shell.pair.wrongCode'),
  no_active_code: t('shell.pair.noActiveCode'),
  tailscale_identity_missing: t('shell.pair.tailscaleMissing'),
  rate_limited: t('shell.pair.rateLimited'),
}

/** TB-12: shown through the mobile gateway when this phone has no (valid) device token. */
export function PairScreen() {
  const fromLink = /[?&]code=(\d{6})/.exec(location.hash)?.[1] ?? ''
  const guess = /iPhone|iPad/.test(navigator.userAgent) ? 'iPhone' : /Android/.test(navigator.userAgent) ? 'Android' : t('shell.pair.defaultDevice')
  const [code, setCode] = useState(fromLink), [name, setName] = useState(guess)
  const [busy, setBusy] = useState(false), [error, setError] = useState('')

  async function submit() {
    setBusy(true); setError('')
    try {
      const r = await fetch('/api/mobile/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code.replace(/\D/g, ''), device_name: name.trim() || guess }) })
      const body = await r.json().catch(() => ({}))
      if (!r.ok) { setError(reasons[body?.code] ?? t('shell.pair.rejected', { status: r.status })); return }
      saveToken(body.token)
      location.hash = '#/'
      location.reload()
    } catch { setError(t('shell.pair.unreachable')) } finally { setBusy(false) }
  }

  return <div className="pair-screen"><form className="card pair-card" onSubmit={e => { e.preventDefault(); void submit() }}><div className="card-b form-fields">
    <div className="pair-title"><img src="/ui/logo.svg" alt="" width={32} height={32} /><h1>{t('shell.pair.heading')}</h1></div>
    <p className="muted">{t('shell.pair.introBefore')} <strong>{t('shell.pair.introPath')}</strong> {t('shell.pair.introAfter')}</p>
    <label htmlFor="pair-code">{t('shell.pair.codeLabel')}<input id="pair-code" className="mono pair-input" inputMode="numeric" autoComplete="one-time-code" maxLength={7}
      value={code} onChange={e => setCode(e.target.value)} placeholder={t('shell.pair.codePlaceholder')} /></label>
    <label htmlFor="pair-name">{t('shell.pair.deviceNameLabel')}<input id="pair-name" value={name} maxLength={60} onChange={e => setName(e.target.value)} /></label>
    {error && <p className="error-banner" role="alert">{error}</p>}
    <button className="btn primary" disabled={busy || code.replace(/\D/g, '').length !== 6}>{busy ? <LoaderCircle className="spin" aria-hidden /> : null}{t('shell.pair.submit')}</button>
  </div></form></div>
}
