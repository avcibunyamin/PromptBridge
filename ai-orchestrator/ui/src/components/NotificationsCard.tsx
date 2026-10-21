import { useEffect, useState } from 'react'
import { Bell, BellOff, LoaderCircle } from 'lucide-react'
import { t } from '../i18n'
import { disablePush, enablePush, pushState, testPush, type PushState } from '../push'

const says: Record<PushState, string> = {
  on: t('settings.notify.state.on'),
  off: t('settings.notify.state.off'),
  denied: t('settings.notify.state.denied'),
  unsupported: t('settings.notify.state.unsupported'),
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
  return <section className="card"><div className="card-h"><h2>{t('settings.notify.title')}</h2></div><div className="card-b form-fields">
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!st ? <p className="muted"><LoaderCircle className="spin" aria-hidden /> {t('settings.notify.reading')}</p> : <p className={st === 'on' ? '' : 'muted'}>{says[st]}</p>}
    {note && <p className="muted small" role="status">{note}</p>}
    <div className="form-footer">
      {st === 'off' && <button className="btn primary" disabled={busy} onClick={() => void step(enablePush)}><Bell aria-hidden />{t('settings.notify.enable')}</button>}
      {st === 'on' && <>
        <button className="btn" disabled={busy} onClick={() => void step(testPush, t('settings.notify.testSent'))}>{t('settings.notify.test')}</button>
        <button className="btn" disabled={busy} onClick={() => void step(disablePush)}><BellOff aria-hidden />{t('settings.notify.disable')}</button></>}
    </div>
  </div></section>
}
