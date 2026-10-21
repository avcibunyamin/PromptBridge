import { useEffect, useState } from 'react'
import { GitMerge, LoaderCircle, Trash2, TriangleAlert } from 'lucide-react'
import { isolationApply, isolationPreview, isolationRemove, type ApplyView, type Work } from '../../api'
import { Modal } from './shared'

/** TB-26 (K-034), Mac only: what the copy holds, then one confirmed step to merge it into the source folder.
 *  The server checks the preview's digest, a clean source and aborts a conflicting merge. */
export function IsolationPanel({ work, onClose }: { work: Work; onClose: () => void }) {
  const iso = work.isolation!
  const [view, setView] = useState<ApplyView | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [done, setDone] = useState('')
  async function step<T>(fn: () => Promise<T>, then: (v: T) => void) {
    setBusy(true); setError('')
    try { then(await fn()) } catch (e) { setError(String(e instanceof Error ? e.message : e)) } finally { setBusy(false) }
  }
  const load = () => step(() => isolationPreview(work.id), setView)
  useEffect(() => { if (!iso.removed) void load() }, [work.id])   // eslint-disable-line react-hooks/exhaustive-deps

  const nothing = !!view && !view.uncommitted.length && !view.commits.length
  return <Modal label="Ayrı kopya" busy={busy} onClose={onClose}>
    <h3>Ayrı kopya</h3>
    <p className="muted small">Dal <span className="mono">{iso.branch}</span> · kaynak <span className="mono">{iso.source}</span></p>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {done && <p className="notice" role="status">{done}</p>}
    {iso.removed ? <p className="muted">Kopya kaldırıldı.</p>
      : !view ? <p className="muted"><LoaderCircle className="spin" aria-hidden /> Okunuyor…</p>
      : <div className="gh-steps">
        <section aria-label="Ana klasöre getir"><h4>Ana klasöre getir</h4>
          {nothing ? <p className="muted small">Kopyada getirilecek değişiklik yok.</p> : <>
            {view.uncommitted.length > 0 && <><p className="small">Kaydedilmemiş {view.uncommitted.length} dosya kopyanın dalına commit'lenecek:</p>
              <ul className="gh-files">{view.uncommitted.map(f => <li key={f} className="mono small">{f}</li>)}</ul></>}
            {view.commits.length > 0 && <ul className="gh-commits">{view.commits.map(c => <li key={c.sha}><span className="mono">{c.sha.slice(0, 7)}</span> {c.subject}</li>)}</ul>}
            {view.attention.length > 0 && <p className="notice"><TriangleAlert aria-hidden /> Dikkat: <span className="mono">{view.attention.join(', ')}</span> değişti.
              Bunlar git'in sonraki davranışını (alt modüller, öznitelikler) ya da GitHub Actions'ı etkiler; getirmeden önce içeriğine bakın.</p>}
            <p className="small">Sonra ana klasörün <span className="mono">{view.target ?? '(dal yok)'}</span> dalına birleştirilir (merge). Çakışırsa hiçbir şey değişmez.</p>
            {view.source_dirty.length > 0 && <p className="notice"><TriangleAlert aria-hidden /> Ana klasörde kaydedilmemiş değişiklik var
              (<span className="mono">{view.source_dirty.slice(0, 5).join(', ')}</span>). Önce onları kaydedin ya da geri alın.</p>}
            <button className="btn primary" disabled={busy || !!view.source_dirty.length || !view.target}
              onClick={() => void step(() => isolationApply(work.id, view.digest), r => { setDone(`${r.target} dalına birleştirildi (${r.commit.slice(0, 7)}).`); void load() })}>
              <GitMerge aria-hidden />Ana klasöre getir</button>
          </>}
        </section>
        <section aria-label="Kopyayı kaldır"><h4>Kopyayı kaldır</h4>
          <p className="muted small">{work.archived_at ? 'Kopya klasörü silinir; dal ana klasöre tamamen birleşmişse o da silinir, yoksa kalır. Kaydedilmemiş değişiklik varsa reddedilir.' : 'Önce çalışmayı arşivleyin.'}</p>
          <button className="btn" disabled={busy || !work.archived_at}
            onClick={() => void step(() => isolationRemove(work.id), () => onClose())}><Trash2 aria-hidden />Kopyayı kaldır</button>
        </section>
      </div>}
    <div className="modal-actions"><button className="btn" disabled={busy} onClick={onClose}>Kapat</button></div>
  </Modal>
}
