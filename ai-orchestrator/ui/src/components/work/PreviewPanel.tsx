import { useCallback, useEffect, useState } from 'react'
import { Copy, ExternalLink, Play, RefreshCw, Square } from 'lucide-react'
import { approvePreview, previewStatus, restartPreview, startPreview, stopPreview, type PreviewStatus } from '../../api'
import { say, t } from '../../i18n'
import { PREVIEW_IFRAME_SANDBOX } from './previewRules'

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

  return <section className="card preview-panel" aria-label={t('tools.preview.title')}>
    <div className="card-h preview-head">
      <h2>{t('tools.preview.title')}</h2>
      <span className="spacer" />
      {server && <span className={'pill ' + (running ? 'ok' : server.status === 'failed' ? 'bad' : server.status === 'starting' ? 'run' : 'idle')}>
        <span className="d" aria-hidden="true" />{server.status === 'running' ? t('tools.preview.status.running') : server.status === 'failed' ? t('tools.preview.status.failed') : server.status === 'starting' ? t('tools.preview.status.starting') : t('tools.preview.status.stopped')}
      </span>}
      <button className="icon-btn" type="button" title={t('tools.preview.refresh')} aria-label={t('tools.preview.refreshAria')} disabled={busy} onClick={() => void refresh()}><RefreshCw aria-hidden /></button>
    </div>
    <div className="card-b preview-body">
      {error && <div className="error-banner" role="alert">{error}</div>}
      {!suggestion && !error && <p className="muted">{t('tools.preview.empty')}</p>}
      {suggestion && <div className="preview-command">
        <div>
          <strong>{suggestion.label}</strong>
          <p className="muted small">{basename(suggestion.script_path)} · port {suggestion.port}</p>
        </div>
        <code>{argv.join(' ')}</code>
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
        <button className="btn" type="button" disabled={!url} onClick={() => window.open(url, '_blank', 'noopener,noreferrer')}><ExternalLink aria-hidden />{t('tools.preview.openBrowser')}</button>
      </div>
      {server?.error && <p className="notice">{server.error}</p>}
      {running && server?.url && <iframe className="preview-frame" title={t('tools.preview.iframeTitle')} src={server.url} sandbox={PREVIEW_IFRAME_SANDBOX} />}
      {server?.log_tail && <details className="preview-log"><summary>{t('tools.preview.log')}</summary><pre>{server.log_tail}</pre></details>}
    </div>
  </section>
}
