import { useEffect, useState } from 'react'
import { LoaderCircle } from 'lucide-react'
import { githubClone, githubRepos } from '../../api'
import { filterRepos, type GhRepo } from '../../githubRules'
import { say, t } from '../../i18n'

/** TB-21: pick one of the account's repositories; it is cloned into the Settings clone folder (or reopened there). */
export function RepoPicker({ onPicked, onClose }: { onPicked: (path: string) => void; onClose: () => void }) {
  const [items, setItems] = useState<GhRepo[] | null>(null), [q, setQ] = useState(''), [busy, setBusy] = useState(''), [error, setError] = useState('')
  useEffect(() => { githubRepos().then(r => setItems(r.items), e => { setItems([]); setError(say(e)) }) }, [])
  async function pick(repo: string) {
    setBusy(repo); setError('')
    try { onPicked((await githubClone(repo)).path) } catch (e) { setError(say(e)) } finally { setBusy('') }
  }
  const shown = filterRepos(items ?? [], q).slice(0, 50)
  return <div className="repo-picker" role="group" aria-label={t('tools.repo.pickAria')}>
    <div className="folder-row">
      <label htmlFor="nw-repo-q" className="sr-only">{t('tools.repo.search')}</label>
      <input id="nw-repo-q" value={q} autoFocus onChange={e => setQ(e.target.value)} placeholder={t('tools.repo.searchPlaceholder')} />
      <button type="button" className="btn small" onClick={onClose}>{t('tools.repo.cancel')}</button>
    </div>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!items ? <p className="muted small"><LoaderCircle className="spin" aria-hidden /> {t('tools.repo.loading')}</p>
      : <ul className="repo-list">{shown.map(r => <li key={r.nameWithOwner}>
        <button type="button" className="btn small ghost" disabled={!!busy} onClick={() => void pick(r.nameWithOwner)}>
          {busy === r.nameWithOwner ? <LoaderCircle className="spin" aria-hidden /> : null}<span className="mono">{r.nameWithOwner}</span>
          {r.isPrivate && <span className="muted small">{t('tools.repo.private')}</span>}{r.isArchived && <span className="muted small">{t('tools.repo.archived')}</span>}</button></li>)}
        {!shown.length && <li className="muted small">{t('tools.repo.noMatch')}</li>}</ul>}
  </div>
}
