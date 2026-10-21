import { useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { ApiError, githubClone, githubRepos } from '../../api'
import { filterRepos, type GhRepo } from '../../githubRules'

/** TB-21: pick one of the account's repositories; it is cloned into the Settings clone folder (or reopened there). */
export function RepoPicker({ onPicked, onClose }: { onPicked: (path: string) => void; onClose: () => void }) {
  const [items, setItems] = useState<GhRepo[] | null>(null), [q, setQ] = useState(''), [busy, setBusy] = useState(''), [error, setError] = useState('')
  const say = (e: unknown) => e instanceof ApiError && e.code === 'clone_dir_missing' ? 'Önce Ayarlar → GitHub bölümünde klon klasörünü seçin.'
    : e instanceof ApiError && e.code === 'gh_missing' ? 'GitHub CLI (gh) bu Mac\'te kurulu değil.' : String(e instanceof Error ? e.message : e)
  useEffect(() => { githubRepos().then(r => setItems(r.items), e => { setItems([]); setError(say(e)) }) }, [])
  async function pick(repo: string) {
    setBusy(repo); setError('')
    try { onPicked((await githubClone(repo)).path) } catch (e) { setError(say(e)) } finally { setBusy('') }
  }
  const shown = filterRepos(items ?? [], q).slice(0, 50)
  return <div className="repo-picker" role="group" aria-label="GitHub deposu seç">
    <div className="folder-row">
      <label htmlFor="nw-repo-q" className="sr-only">Depo ara</label>
      <input id="nw-repo-q" value={q} autoFocus onChange={e => setQ(e.target.value)} placeholder="Depo ara — sahip/ad" />
      <button type="button" className="btn small" onClick={onClose}>Vazgeç</button>
    </div>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!items ? <p className="muted small"><LoaderCircle className="spin" aria-hidden /> Depolar alınıyor…</p>
      : <ul className="repo-list">{shown.map(r => <li key={r.nameWithOwner}>
        <button type="button" className="btn small ghost" disabled={!!busy} onClick={() => void pick(r.nameWithOwner)}>
          {busy === r.nameWithOwner ? <LoaderCircle className="spin" aria-hidden /> : null}<span className="mono">{r.nameWithOwner}</span>
          {r.isPrivate && <span className="muted small">özel</span>}{r.isArchived && <span className="muted small">arşiv</span>}</button></li>)}
        {!shown.length && <li className="muted small">Eşleşen depo yok.</li>}</ul>}
  </div>
}
