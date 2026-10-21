import { useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { saveToken } from '../mobile'

const reasons: Record<string, string> = {
  wrong_code: 'Kod yanlış. Mac panelindeki kodu kontrol edin.',
  no_active_code: 'Geçerli kod yok (süresi dolmuş, kullanılmış ya da çok fazla yanlış deneme). Mac panelinde yeni kod oluşturun.',
  tailscale_identity_missing: 'Tailscale kimliği görülmedi. Telefonda Tailscale açık mı, Mac\'teki adla mı bağlandınız?',
  rate_limited: 'Çok fazla deneme. Bir dakika bekleyin.',
}

/** TB-12: shown through the mobile gateway when this phone has no (valid) device token. */
export function PairScreen() {
  const fromLink = /[?&]code=(\d{6})/.exec(location.hash)?.[1] ?? ''
  const guess = /iPhone|iPad/.test(navigator.userAgent) ? 'iPhone' : /Android/.test(navigator.userAgent) ? 'Android' : 'Telefon'
  const [code, setCode] = useState(fromLink), [name, setName] = useState(guess)
  const [busy, setBusy] = useState(false), [error, setError] = useState('')

  async function submit() {
    setBusy(true); setError('')
    try {
      const r = await fetch('/api/mobile/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: code.replace(/\D/g, ''), device_name: name.trim() || guess }) })
      const body = await r.json().catch(() => ({}))
      if (!r.ok) { setError(reasons[body?.code] ?? `Eşleştirme reddedildi (${r.status}).`); return }
      saveToken(body.token)
      location.hash = '#/'
      location.reload()
    } catch { setError('Sunucuya ulaşılamadı. Tailscale bağlı mı?') } finally { setBusy(false) }
  }

  return <div className="pair-screen"><form className="card pair-card" onSubmit={e => { e.preventDefault(); void submit() }}><div className="card-b form-fields">
    <div className="pair-title"><img src="/ui/logo.svg" alt="" width={32} height={32} /><h1>PromptBridge'e bağlan</h1></div>
    <p className="muted">Mac'teki panelde <strong>Ayarlar → Mobil erişim → Eşleştirme kodu oluştur</strong> ile aldığınız 6 haneli kodu girin. Telefon çalışmaları izler ve dosya onaylarını yanıtlar; Mac izin verirse görev de verir.</p>
    <label htmlFor="pair-code">Kod<input id="pair-code" className="mono pair-input" inputMode="numeric" autoComplete="one-time-code" maxLength={7}
      value={code} onChange={e => setCode(e.target.value)} placeholder="6 haneli kod" /></label>
    <label htmlFor="pair-name">Cihaz adı<input id="pair-name" value={name} maxLength={60} onChange={e => setName(e.target.value)} /></label>
    {error && <p className="error-banner" role="alert">{error}</p>}
    <button className="btn primary" disabled={busy || code.replace(/\D/g, '').length !== 6}>{busy ? <LoaderCircle className="spin" aria-hidden /> : null}Eşleştir</button>
  </div></form></div>
}
