import { useEffect, useState } from 'react'
import { ExternalLink, GitBranch, LoaderCircle, Send, TriangleAlert } from 'lucide-react'
import { githubCommit, githubPr, githubPrView, githubPush, githubPushView, githubState } from '../../api'
import { defaultSelection, needsSecretAck, statusLabel, type GhState, type PrView, type PushView } from '../../githubRules'
import { Modal } from './shared'

/** TB-21: save → push → draft PR, each a separate human step (K-026/3). What a step shows is what the server
 *  checks: push and PR send back the digest of their preview and are refused if anything changed meanwhile. */
export function GitHubPanel({ workId, title, onClose }: { workId: string; title: string; onClose: () => void }) {
  const [st, setSt] = useState<GhState | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [picked, setPicked] = useState<string[]>([]), [message, setMessage] = useState(title), [secretsAck, setSecretsAck] = useState(false)
  const [push, setPush] = useState<PushView | null>(null), [workflowsAck, setWorkflowsAck] = useState(false), [pushed, setPushed] = useState(false)
  const [pr, setPr] = useState<PrView | null>(null), [prTitle, setPrTitle] = useState(''), [prBody, setPrBody] = useState(''), [url, setUrl] = useState('')

  async function step<T>(fn: () => Promise<T>, then: (v: T) => void) {
    setBusy(true); setError('')
    try { then(await fn()) } catch (e) { setError(String(e instanceof Error ? e.message : e)) } finally { setBusy(false) }
  }
  const load = () => step(() => githubState(workId), s => { setSt(s); setPicked(defaultSelection(s.files ?? [])); setSecretsAck(false); setPush(null); setPr(null) })
  useEffect(() => { void load() }, [workId])   // eslint-disable-line react-hooks/exhaustive-deps

  const files = st?.files ?? []
  const toggle = (p: string) => setPicked(s => s.includes(p) ? s.filter(x => x !== p) : [...s, p])
  const secretPicked = needsSecretAck(files, picked)

  return <Modal label="GitHub'a gönder" busy={busy} onClose={onClose}>
    <h3>GitHub'a gönder</h3>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!st ? <p className="muted"><LoaderCircle className="spin" aria-hidden /> Depo okunuyor…</p>
      : !st.git ? <p className="notice">Bu çalışmanın klasörü bir git deposunun kökü değil.</p>
      : st.unsafe_config?.length ? <p className="notice"><TriangleAlert aria-hidden /> Deponun kendi ayarında izin verilmeyen anahtarlar var:
          <span className="mono"> {st.unsafe_config.join(', ')}</span>. Bunlar git komutlarını başka bir şey çalıştırmaya yönlendirebilir; kaldırmadan hiçbir adım yapılmaz.</p>
      : !st.repo ? <p className="notice">origin bir GitHub deposu değil.</p>
      : <div className="gh-steps">
        <p className="muted small"><GitBranch aria-hidden /> <span className="mono">{st.repo}</span> · dal <span className="mono">{st.branch ?? '(bağlı değil)'}</span></p>

        <section aria-label="1. Değişiklikleri kaydet"><h4>1. Değişiklikleri kaydet</h4>
          {files.length === 0 ? <p className="muted small">Kaydedilecek değişiklik yok.</p> : <>
            <ul className="gh-files">{files.map(f => <li key={f.path}><label>
              <input type="checkbox" checked={picked.includes(f.path)} onChange={() => toggle(f.path)} />
              <span className="mono">{f.path}</span> <span className="muted small">{statusLabel(f.status)}</span>
              {f.secret && <span className="pill warn">gizli olabilir</span>}{f.workflow && <span className="pill warn">iş akışı</span>}</label></li>)}</ul>
            <label htmlFor="gh-msg">Commit mesajı<textarea id="gh-msg" rows={3} maxLength={65536} value={message} onChange={e => setMessage(e.target.value)} /></label>
            {secretPicked && <label className="accept-row"><input type="checkbox" checked={secretsAck} onChange={e => setSecretsAck(e.target.checked)} />
              <span>Seçili dosyalardan bazıları gizli bilgi içerebilir (<code>.env</code>, anahtar…). Yine de kaydedilsin.</span></label>}
            <p className="muted small">Ana daldaysanız yeni bir <code>conductor/…</code> dalı açılır; ana dala yazılmaz.</p>
            <button className="btn" disabled={busy || !picked.length || !message.trim() || (secretPicked && !secretsAck)}
              onClick={() => void step(() => githubCommit(workId, { files: picked, message, expected_head: st.head ?? null, secrets_ack: secretsAck }), () => void load())}>Kaydet (commit)</button>
          </>}
        </section>

        <section aria-label="2. GitHub'a gönder"><h4>2. GitHub'a gönder</h4>
          {!push ? <button className="btn" disabled={busy} onClick={() => void step(() => githubPushView(workId), v => { setPush(v); setWorkflowsAck(false) })}>Gönderilecekleri göster</button>
            : <>
              <p className="small">Depo <span className="mono">{push.repo}</span>, dal <span className="mono">{push.branch}</span> (taban {push.base}); {push.commits.length} commit, {push.files} dosya.</p>
              <ul className="gh-commits">{push.commits.map(c => <li key={c.sha}><span className="mono">{c.sha.slice(0, 7)}</span> {c.subject}</li>)}</ul>
              {push.workflow_files.length > 0 && <label className="accept-row"><input type="checkbox" checked={workflowsAck} onChange={e => setWorkflowsAck(e.target.checked)} />
                <span><strong>İş akışı dosyası var:</strong> <span className="mono">{push.workflow_files.join(', ')}</span>. Bu push GitHub Actions'ta kod çalıştırabilir; ayrıca onaylıyorum.</span></label>}
              {pushed ? <p className="notice">Gönderildi.</p>
                : <button className="btn primary" disabled={busy || (push.workflow_files.length > 0 && !workflowsAck)}
                  onClick={() => void step(() => githubPush(workId, push.digest, workflowsAck), () => setPushed(true))}><Send aria-hidden />Gönder (push)</button>}
            </>}
        </section>

        <section aria-label="3. Taslak PR"><h4>3. Taslak PR</h4>
          {url ? <p><a href={url} target="_blank" rel="noreferrer">{url} <ExternalLink aria-hidden /></a></p>
            : !pr ? <button className="btn" disabled={busy || !pushed} onClick={() => void step(() => githubPrView(workId), v => { setPr(v); setPrTitle(v.title); setPrBody(v.body) })}>PR'ı hazırla</button>
            : <>
              <label htmlFor="gh-pr-title">Başlık<input id="gh-pr-title" maxLength={200} value={prTitle} onChange={e => setPrTitle(e.target.value)} /></label>
              <label htmlFor="gh-pr-body">Açıklama<textarea id="gh-pr-body" rows={5} maxLength={65536} value={prBody} onChange={e => setPrBody(e.target.value)} /></label>
              <p className="muted small"><span className="mono">{pr.head}</span> → <span className="mono">{pr.base}</span>; PR taslak açılır, GitHub'da “Ready for review” dersiniz.</p>
              <button className="btn primary" disabled={busy || !prTitle.trim()} onClick={() => void step(() => githubPr(workId, pr.digest, prTitle, prBody), r => setUrl(r.url))}>Taslak PR aç</button>
            </>}
        </section>
      </div>}
    <div className="modal-actions"><button className="btn" disabled={busy} onClick={onClose}>Kapat</button></div>
  </Modal>
}
