import { useCallback, useEffect, useState } from 'react'
import { Copy, ExternalLink, Play, RefreshCw, Square } from 'lucide-react'
import { ApiError, approvePreview, previewStatus, restartPreview, startPreview, stopPreview, type PreviewStatus } from '../../api'
import { PREVIEW_IFRAME_SANDBOX } from './previewRules'

const codes: Record<string, string> = {
  not_detected: 'Bu klasör için desteklenen bir çalıştırma komutu algılanamadı.',
  approval_required: 'Komutu çalıştırmadan önce açık onay gerekiyor.',
  digest_changed: 'Komut veya betik değişti; yeniden inceleyip onaylayın.',
  already_running: 'Bu proje için önizleme zaten çalışıyor.',
  port_in_use: 'Önizleme portu başka bir süreç tarafından kullanılıyor; o süreci durdurup yeniden deneyin.',
  process_exited: 'Önizleme süreci başladıktan hemen sonra kapandı; port başka bir süreçte olabilir.',
  readiness_timeout: 'Önizleme süresi içinde hazır olmadı.',
  untrusted_origin: 'Güvenilmeyen kaynaktan gelen işlem reddedildi.',
}

const say = (e: unknown) => e instanceof ApiError && e.code && codes[e.code] ? codes[e.code] : String(e)
const basename = (p: string) => p.split(/[\\/]/).pop() || p

export function PreviewPanel({ workspacePath }: { workspacePath: string }) {
  const [state, setState] = useState<PreviewStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setError('')
      setState(await previewStatus(workspacePath))
    } catch (e) {
      setError(say(e))
      setState(null)
    }
  }, [workspacePath])

  useEffect(() => { void refresh() }, [refresh])

  async function run(action: () => Promise<unknown>) {
    setBusy(true); setError('')
    try {
      await action()
      await refresh()
    } catch (e) {
      setError(say(e))
      await refresh().catch(() => {})
    } finally {
      setBusy(false)
    }
  }

  const suggestion = state?.suggestion
  const server = state?.server
  const running = server?.status === 'running'
  const url = server?.url || suggestion?.url || ''
  const argv = suggestion?.argv ?? []
  const commandDigest = suggestion?.command_digest ?? ''

  async function copyUrl() {
    if (!url) return
    await navigator.clipboard?.writeText(url)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }

  return <section className="card preview-panel" aria-label="Önizleme">
    <div className="card-h preview-head">
      <h2>Önizleme</h2>
      <span className="spacer" />
      {server && <span className={'pill ' + (running ? 'ok' : server.status === 'failed' ? 'bad' : server.status === 'starting' ? 'run' : 'idle')}>
        <span className="d" aria-hidden="true" />{server.status === 'running' ? 'Çalışıyor' : server.status === 'failed' ? 'Hata' : server.status === 'starting' ? 'Başlıyor' : 'Kapalı'}
      </span>}
      <button className="icon-btn" type="button" title="Yenile" aria-label="Önizlemeyi yenile" disabled={busy} onClick={() => void refresh()}><RefreshCw aria-hidden /></button>
    </div>
    <div className="card-b preview-body">
      {error && <div className="error-banner" role="alert">{error}</div>}
      {!suggestion && !error && <p className="muted">Bu klasörde çalıştırılabilir bir önizleme komutu bulunamadı.</p>}
      {suggestion && <div className="preview-command">
        <div>
          <strong>{suggestion.label}</strong>
          <p className="muted small">{basename(suggestion.script_path)} · port {suggestion.port}</p>
        </div>
        <code>{argv.join(' ')}</code>
      </div>}
      {suggestion && !state?.approved && <div className="reaccept">
        <p><strong>Bu komut proje kodunu çalıştırır.</strong> Komutu, klasörü ve betik digest’ini bu proje için onaylaman gerekiyor.</p>
        <label className="accept-row">
          <input type="checkbox" onChange={e => { if (e.target.checked) void run(() => approvePreview(workspacePath, commandDigest, suggestion.port)) }} disabled={busy} />
          Bu çalıştırma komutunu onaylıyorum.
        </label>
      </div>}
      <div className="preview-actions">
        <button className="btn primary" type="button" disabled={busy || !suggestion || !state?.approved || running}
          onClick={() => void run(() => startPreview(workspacePath, commandDigest))}><Play aria-hidden />Çalıştır</button>
        <button className="btn" type="button" disabled={busy || !suggestion || !state?.approved}
          onClick={() => void run(() => restartPreview(workspacePath, commandDigest))}><RefreshCw aria-hidden />Yeniden başlat</button>
        <button className="btn" type="button" disabled={busy || !server || !['running', 'starting'].includes(server.status)}
          onClick={() => void run(() => stopPreview(workspacePath))}><Square aria-hidden />Durdur</button>
        <button className="btn" type="button" disabled={!url} onClick={() => void copyUrl()}><Copy aria-hidden />{copied ? 'Kopyalandı' : 'URL kopyala'}</button>
        <button className="btn" type="button" disabled={!url} onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}><ExternalLink aria-hidden />Tarayıcıda aç</button>
      </div>
      {server?.error && <p className="notice">{server.error}</p>}
      {running && server?.url && <iframe className="preview-frame" title="Proje önizlemesi" src={server.url} sandbox={PREVIEW_IFRAME_SANDBOX} />}
      {server?.log_tail && <details className="preview-log"><summary>Günlük</summary><pre>{server.log_tail}</pre></details>}
    </div>
  </section>
}
