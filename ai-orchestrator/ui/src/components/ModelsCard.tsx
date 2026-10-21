import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { mobile } from '../mobile'
import { locale, t } from '../i18n'
import { api } from './work/shared'
import { agyFamilyLabel, agyGroups, baseOf, effortLabel, modelLabel, type Catalog, type CatalogEntry } from './work/team'

const errors: Record<string, string> = { unauthenticated: t('settings.models.err.unauthenticated'), timeout: t('settings.models.err.timeout'), unavailable: t('settings.models.err.unavailable'), cleanup_unverified: t('settings.models.err.unavailable') }

/** K-041: the current models per provider and account (agy as family + efforts), as the pickers offer them. */
export function ModelsCard({ efforts, bases: allowed }: { efforts: Record<string, string[]>; bases: string[] }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState('')
  // right after a restart the server is still listing models: read again until it has (or ~30 s pass)
  useEffect(() => { let live = true, tries = 0, timer: ReturnType<typeof setTimeout> | undefined
    const read = () => api<Catalog>('/api/sessions/catalog').then(c => { if (!live) return; setCatalog(prev => prev && c.checked_at === null ? prev : c)
      if (c.checked_at === null && ++tries < 8) timer = setTimeout(read, 4000) }, e => { if (live) setError(String(e)) })
    void read(); return () => { live = false; clearTimeout(timer) } }, [])
  async function refresh() {
    setBusy(true); setError('')
    try { setCatalog(await api<Catalog>('/api/sessions/catalog/refresh', { method: 'POST' })) } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }
  const bases = new Map<string, CatalogEntry[]>(allowed.map(b => [b, []]))   // every allowed account, even before its list
  for (const e of catalog?.entries ?? []) bases.set(baseOf(e.identity), [...(bases.get(baseOf(e.identity)) ?? []), e])
  for (const b of Object.keys(catalog?.errors ?? {})) if (!bases.has(b)) bases.set(b, [])
  const rows = (base: string, list: CatalogEntry[]) => {
    if (base.startsWith('agy')) return [...agyGroups(list.map(e => e.model))].map(([fam, list2]) =>
      ({ key: fam, label: agyFamilyLabel(catalog, base, fam, list2), efforts: list2, status: 'available' }))
    return list.map(e => ({ key: e.model, label: modelLabel(catalog, base, e.model), status: e.status,
      efforts: e.source === 'live' || e.efforts.length ? e.efforts : (efforts[e.provider] ?? []) }))   // a live list is the authority
  }
  return <section className="card"><div className="card-h"><h2>{t('settings.models.title')}</h2><span className="spacer" />
    {catalog?.checked_at && <span className="muted small">{new Date(catalog.checked_at * 1000).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}</span>}
    {!mobile.active && <button className="btn small" disabled={busy} aria-busy={busy} onClick={() => void refresh()} title={t('settings.models.refreshTitle')}>
      <RefreshCw className={busy ? 'spin' : undefined} aria-hidden />{busy ? t('settings.models.refreshing') : t('settings.models.refresh')}</button>}</div>
    <div className="card-b">
      <p className="muted small">{t('settings.models.intro')}</p>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {!catalog ? <p className="loading">{t('settings.loading')}</p> : <div className="models-grid">
        {[...bases].sort(([a], [b]) => a.localeCompare(b)).map(([base, list]) => <div className="models-base" key={base}>
          <p className="models-title mono">{base}{catalog.errors[base] && <span className="pill warn">{errors[catalog.errors[base]] ?? catalog.errors[base]}</span>}</p>
          <ul>{rows(base, list).map(r => <li key={r.key}>
            <span className={r.status === 'unsupported' ? 'muted' : undefined}>{r.label}</span>
            <span className="models-efforts">{r.status === 'unsupported' ? <span className="pill bad">{t('settings.models.unsupported')}</span>
              : r.efforts.map(ef => <span key={ef} className="chip-efor">{effortLabel(ef)}</span>)}</span>
          </li>)}</ul>
          {!list.length && !catalog.errors[base] && <p className="muted small">{t('settings.models.empty')}</p>}
        </div>)}
      </div>}
    </div></section>
}
