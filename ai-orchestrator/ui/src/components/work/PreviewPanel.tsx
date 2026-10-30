import { useCallback, useEffect, useId, useState } from 'react'
import { Copy, ExternalLink, Play, RefreshCw, Square, X } from 'lucide-react'
import { approvePreview, previewStatus, restartPreview, startPreview, stopPreview, type PreviewStatus } from '../../api'
import { say, t } from '../../i18n'
import { PREVIEW_IFRAME_SANDBOX } from './previewRules'

const basename = (p: string) => p.split(/[\\/]/).pop() || p
// K-065: the address bar only moves within the preview's own server; '//other.host' or a full URL falls back to it.
const samePage = (base: string, path: string) => {
  try { const u = new URL(path.startsWith('/') ? path : '/' + path, base); return u.origin === new URL(base).origin ? u.href : base } catch { return base }
}

export function PreviewPanel({ workspacePath, onClose }: { workspacePath: string; onClose?: () => void }) {
  // K-065: an address bar over the running app; the path stays relative to the preview's own server.
  const [path, setPath] = useState('/'), [frameKey, setFrameKey] = useState(0), pathId = useId()
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
  const pageUrl = url && samePage(url, path)
  const argv = suggestion?.argv ?? []
  const commandDigest = suggestion?.command_digest ?? ''

  async function copyUrl() {
    if (!url) return
    await navigator.clipboard?.writeText(url)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }

  return <section className="card preview-panel" aria-label={t('tools.preview.title')}>
    <div className="card-h preview-head">
      <h2>{t('tools.preview.title')}</h2>
      <span className="spacer" />
      {server && <span className={'pill ' + (running ? 'ok' : server.status === 'failed' ? 'bad' : server.status === 'starting' ? 'run' : 'idle')}>
        <span className="d" aria-hidden="true" />{server.status === 'running' ? t('tools.preview.status.running') : server.status === 'failed' ? t('tools.preview.status.failed') : server.status === 'starting' ? t('tools.preview.status.starting') : t('tools.preview.status.stopped')}
      </span>}
      <button className="icon-btn" type="button" title={t('tools.preview.refresh')} aria-label={t('tools.preview.refreshAria')} disabled={busy} onClick={() => void refresh()}><RefreshCw aria-hidden /></button>
      {onClose && <button className="icon-btn" type="button" title={t('work.previewClose')} aria-label={t('work.previewClose')} onClick={onClose}><X aria-hidden /></button>}
    </div>
    <div className="card-b preview-body">
      {error && <div className="error-banner" role="alert">{error}</div>}
      {!suggestion && !error && <p className="muted">{t('tools.preview.empty')}</p>}
      {suggestion && running && <details className="preview-log"><summary>{suggestion.label}</summary><code className="mono small">{argv.join(' ')}</code></details>}
      {suggestion && !running && <div className="preview-command">
        <div>
          <strong>{suggestion.label}</strong>
          <p className="muted small">{basename(suggestion.script_path)} · port {suggestion.port}</p>
        </div>
        <code>{argv.join(' ')}</code>
        {suggestion.scripts && suggestion.scripts.length > 0 && <div className="preview-scripts">
          <p className="muted small">{t('tools.preview.scripts')}</p>
          <ul className="small">
            {suggestion.scripts.map(([name, text], i) => <li key={`${name}-${i}`}>{name}: <code>{text}</code></li>)}
          </ul>
        </div>}
      </div>}
      {suggestion && !state?.approved && <div className="reaccept">
        <p><strong>{t('tools.preview.approveLead')}</strong> {t('tools.preview.approveBody')}</p>
        <label className="accept-row">
          <input type="checkbox" onChange={e => { if (e.target.checked) void run(() => approvePreview(workspacePath, commandDigest, suggestion.port)) }} disabled={busy} />
          {t('tools.preview.approveCheck')}
        </label>
      </div>}
      <div className="preview-actions">
        <button className="btn primary" type="button" disabled={busy || !suggestion || !state?.approved || running}
          onClick={() => void run(() => startPreview(workspacePath, commandDigest))}><Play aria-hidden />{t('tools.preview.start')}</button>
        <button className="btn" type="button" disabled={busy || !suggestion || !state?.approved}
          onClick={() => void run(() => restartPreview(workspacePath, commandDigest))}><RefreshCw aria-hidden />{t('tools.preview.restart')}</button>
        <button className="btn" type="button" disabled={busy || !server || !['running', 'starting'].includes(server.status)}
          onClick={() => void run(() => stopPreview(workspacePath))}><Square aria-hidden />{t('tools.preview.stop')}</button>
        <button className="btn" type="button" disabled={!url} onClick={() => void copyUrl()}><Copy aria-hidden />{copied ? t('tools.preview.copied') : t('tools.preview.copyUrl')}</button>
        <button className="btn" type="button" disabled={!url} onClick={() => window.open(pageUrl || url, '_blank', 'noopener,noreferrer')}><ExternalLink aria-hidden />{t('tools.preview.openBrowser')}</button>
      </div>
      {server?.error && <p className="notice">{server.error}</p>}
      {running && server?.url && <form className="preview-address" onSubmit={e => { e.preventDefault(); setFrameKey(k => k + 1) }}>
        <span className="mono small muted">{new URL(server.url).host}</span>
        <label className="sr-only" htmlFor={pathId}>{t('tools.preview.path')}</label>
        <input id={pathId} className="mono" value={path} onChange={e => setPath(e.target.value)} spellCheck={false} />
        <button className="icon-btn" type="submit" title={t('tools.preview.reloadPage')} aria-label={t('tools.preview.reloadPage')}><RefreshCw aria-hidden /></button>
      </form>}
      {running && server?.url && <iframe key={frameKey} className="preview-frame" title={t('tools.preview.iframeTitle')} src={pageUrl} sandbox={PREVIEW_IFRAME_SANDBOX} />}
      {server?.log_tail && <details className="preview-log"><summary>{t('tools.preview.log')}</summary><pre>{server.log_tail}</pre></details>}
    </div>
  </section>
}
