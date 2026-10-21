import { useEffect, useState } from 'react'
import { Bell, BellOff, LoaderCircle } from 'lucide-react'
import { disablePush, enablePush, pushState, testPush, type PushState } from '../push'

const says: Record<PushState, string> = {
  on: 'Açık. Bir iş bittiğinde, takıldığında ya da senden onay beklediğinde bu telefona bildirim gelir (Tailscale kapalıyken de).',
  off: 'Kapalı.',
  denied: 'Bu tarayıcıda PromptBridge bildirimleri engellenmiş: Android Ayarlar → Uygulamalar → Chrome (ya da PromptBridge) → Bildirimler.',
  unsupported: 'Bu tarayıcı bildirim desteklemiyor. PromptBridge\'i Chrome\'da “Uygulamayı yükle” ile kurup oradan aç.',
}

/** TB-29 (K-036): this phone's notifications (Web Push through the browser's push service). */
export function NotificationsCard() {
  const [st, setSt] = useState<PushState | null>(null), [busy, setBusy] = useState(false)
  const [error, setError] = useState(''), [note, setNote] = useState('')
  async function step(fn: () => Promise<PushState | void>, done?: string) {
    setBusy(true); setError(''); setNote('')
    try { const r = await fn(); if (r) setSt(r); if (done) setNote(done) } catch (e) { setError(String(e instanceof Error ? e.message : e)) } finally { setBusy(false) }
  }
  useEffect(() => { void step(pushState) }, [])
  return <section className="card"><div className="card-h"><h2>Bildirimler</h2></div><div className="card-b form-fields">
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!st ? <p className="muted"><LoaderCircle className="spin" aria-hidden /> Okunuyor…</p> : <p className={st === 'on' ? '' : 'muted'}>{says[st]}</p>}
    {note && <p className="muted small" role="status">{note}</p>}
    <div className="form-footer">
      {st === 'off' && <button className="btn primary" disabled={busy} onClick={() => void step(enablePush)}><Bell aria-hidden />Bildirimleri aç</button>}
      {st === 'on' && <>
        <button className="btn" disabled={busy} onClick={() => void step(testPush, 'Deneme bildirimi gönderildi; birkaç saniye içinde gelir.')}>Deneme bildirimi</button>
        <button className="btn" disabled={busy} onClick={() => void step(disablePush)}><BellOff aria-hidden />Kapat</button></>}
    </div>
  </div></section>
}
