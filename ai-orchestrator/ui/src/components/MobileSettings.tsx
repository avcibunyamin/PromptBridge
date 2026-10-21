import { useCallback, useEffect, useState } from 'react'
import { Copy, KeyRound, LoaderCircle, Smartphone, Trash2 } from 'lucide-react'
import { checkedFetch } from '../api'

type Device = { id: string; name: string; scopes: string; tailscale_login: string; created_at: string; last_seen_at: string | null; revoked_at: string | null; push?: boolean }
type Audit = { id: number; at: string; device_id: string | null; method: string; path: string; status: number; outcome: string }
type Status = { saved: { enabled: boolean; host: string; port: number } | null; active: { host: string; port: number } | null; devices: Device[]; audit: Audit[] }

const when = (iso: string | null) => iso ? new Date(iso).toLocaleString('tr-TR', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'
/** Extra scopes the Mac grants per device (K-027, K-033); `view` and `approve` come with pairing. */
const scopeBoxes: [string, string, string][] = [
  ['interrupt', 'Durdurabilir', 'Telefondan: çalışmayı durdur, kota beklemesini iptal et, sıradaki mesajı geri al (K-027)'],
  ['command', 'Görev verebilir', 'Telefondan: mevcut bir çalışmanın klasöründe yeni çalışma, mesaj, takım/model seçimi, tema ve varsayılan takım (K-033)'],
]
const json = (method: string, body?: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })

/** TB-12 (K-024), Mac only: gateway settings, pairing codes, devices and the mobile audit log. */
export function MobileSettings({ embedded = false }: { embedded?: boolean }) {
  const [status, setStatus] = useState<Status | null>(null)
  const [host, setHost] = useState(''), [enabled, setEnabled] = useState(false)
  const [code, setCode] = useState<{ code: string; expires_at: string } | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [copied, setCopied] = useState('')

  const refresh = useCallback(async () => {
    try {
      const s = await (await checkedFetch('/api/mobile/status')).json() as Status
      setStatus(s); setHost(h => h || s.saved?.host || ''); setEnabled(e => e || !!s.saved?.enabled); setError('')
    } catch (e) { setError(String(e)) }
  }, [])
  useEffect(() => { void refresh() }, [refresh])

  async function run(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try { await action(); await refresh() } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const copy = (text: string) => { void navigator.clipboard?.writeText(text); setCopied(text); setTimeout(() => setCopied(''), 1500) }

  const saved = status?.saved, active = status?.active
  const target = saved?.host || host
  const serveCmd = `tailscale serve --bg --https=443 http://127.0.0.1:${saved?.port ?? 8770}`
  const pairUrl = code && target ? `https://${target}/ui/#/pair?code=${code.code}` : ''
  const needsRestart = !!saved && (saved.enabled ? active?.host !== saved.host || active?.port !== saved.port : !!active)

  const content = <>
    <div className="run-head"><div>{embedded ? <h2>Mobil erişim</h2> : <h1>Mobil erişim</h1>}
      <p className="intro">Telefondan izleme ve dosya onayı. Bağlantıyı Tailscale açar; PromptBridge yalnız bu Mac'te (127.0.0.1) dinler. Telefonda yeni görev, durdurma ve ayar yok.</p></div></div>
    {error && <div className="error-banner" role="alert">{error}</div>}

    <section className="card"><div className="card-h"><h2>Geçit</h2><span className="spacer" />
      <span className={'pill ' + (active ? 'ok' : 'idle')}><span className="d" aria-hidden="true" />{active ? `Açık · ${active.host}` : 'Kapalı'}</span></div>
      <div className="card-b form-fields">
        <label htmlFor="mobile-host">Mac'in Tailscale adı (MagicDNS)
          <input id="mobile-host" className="mono" value={host} placeholder="mac-adi.tailXXXX.ts.net" onChange={e => setHost(e.target.value)} /></label>
        <label className="accept-row"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} /> Mobil geçidi aç</label>
        <div className="form-footer"><p className="hint"><span>Değişiklik PromptBridge yeniden başladığında etkin olur (<code>deploy/launchd.sh restart</code>).</span></p>
          <button className="btn primary" disabled={busy} onClick={() => void run(() => checkedFetch('/api/mobile/settings', json('PUT', { enabled, host })))}>
            {busy ? <LoaderCircle className="spin" aria-hidden /> : null}Kaydet</button></div>
        {needsRestart && <p className="notice">Kaydedilen ayar çalışan sunucudan farklı; PromptBridge'i yeniden başlatın.</p>}
        <div className="mobile-cmd"><span className="muted small">Tailscale'i açmak (bir kez, Mac'te):</span>
          <code className="mono">{serveCmd}</code>
          <button className="btn small" onClick={() => copy(serveCmd)}><Copy aria-hidden />{copied === serveCmd ? 'Kopyalandı' : 'Kopyala'}</button></div>
        <p className="muted small">Kapatmak: <code>tailscale serve reset</code>. <strong>Funnel kullanmayın</strong> (internete açar).</p>
      </div></section>

    <section className="card"><div className="card-h"><h2>Eşleştirme</h2></div><div className="card-b form-fields">
      <p className="muted">Telefonda Tailscale açıkken adresi açın, kodu girin. Kod 5 dakika geçerli, tek kullanımlık.</p>
      <div><button className="btn" disabled={busy || !active} title={active ? undefined : 'Önce geçidi açıp PromptBridge\'i yeniden başlatın'}
        onClick={() => void run(async () => setCode(await (await checkedFetch('/api/mobile/pairing', json('POST'))).json()))}>
        <KeyRound aria-hidden />Eşleştirme kodu oluştur</button></div>
      {code && <div className="pair-code">
        <span className="pair-digits mono" aria-label="Eşleştirme kodu">{code.code.slice(0, 3)} {code.code.slice(3)}</span>
        <span className="muted small">Son geçerlilik {when(code.expires_at)}</span>
        {pairUrl && <><code className="mono">{pairUrl}</code><button className="btn small" onClick={() => copy(pairUrl)}><Copy aria-hidden />{copied === pairUrl ? 'Kopyalandı' : 'Adresi kopyala'}</button></>}
      </div>}
    </div></section>

    <section className="card"><div className="card-h"><h2>Cihazlar</h2></div><div className="card-b">
      {!status?.devices.length ? <p className="muted">Eşleşmiş cihaz yok.</p> :
        <ul className="device-list">{status.devices.map(d => <li key={d.id} className={d.revoked_at ? 'revoked' : ''}>
          <Smartphone aria-hidden /><div><strong>{d.name}</strong><span className="muted small"> · {d.tailscale_login}</span>
            {d.push && !d.revoked_at && <span className="pill ok small">Bildirim açık</span>}
            <div className="muted small">Eklendi {when(d.created_at)} · son görülme {when(d.last_seen_at)}{d.revoked_at ? ` · iptal ${when(d.revoked_at)}` : ''}</div></div>
          <span className="spacer" />
          {!d.revoked_at && scopeBoxes.map(([scope, label, title]) => <label key={scope} className="accept-row small" title={title}>
            <input type="checkbox" checked={d.scopes.split(' ').includes(scope)} disabled={busy}
              onChange={e => void run(() => checkedFetch(`/api/mobile/devices/${d.id}/scopes`,
                json('PUT', { scopes: [...d.scopes.split(' ').filter(s => s !== scope), ...(e.target.checked ? [scope] : [])] })))} />
            <span>{label}</span></label>)}
          {!d.revoked_at && <button className="btn small" disabled={busy} onClick={() => void run(() => checkedFetch(`/api/mobile/devices/${d.id}`, json('DELETE')))}>
            <Trash2 aria-hidden />İptal et</button>}
        </li>)}</ul>}
    </div></section>

    <section className="card"><div className="card-h"><h2>Son mobil işlemler</h2></div><div className="card-b">
      {!status?.audit.length ? <p className="muted">Kayıt yok.</p> :
        <table className="audit"><tbody>{status.audit.map(a => <tr key={a.id}>
          <td className="mono small">{when(a.at)}</td><td className="mono small">{a.method} {a.path}</td>
          <td><span className={'pill ' + (a.status < 300 ? 'ok' : a.status < 500 ? 'warn' : 'bad')}>{a.status}</span></td>
          <td className="small">{a.outcome}</td><td className="mono small muted">{a.device_id ?? '—'}</td></tr>)}</tbody></table>}
    </div></section>
  </>
  // TB-19: inside the Settings page it is one section; alone it keeps its own page frame.
  return embedded ? <div className="mobile-settings">{content}</div>
    : <div className="body mobile-body"><main className="main"><div className="pane mobile-settings">{content}</div></main></div>
}
