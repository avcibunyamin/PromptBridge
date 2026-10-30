import { useEffect, useRef, useState, useCallback } from 'react'
import { listAccounts, listEnvelopes, listWorks, reconcile, type Envelope } from './api'
import { WorkPanel } from './components/WorkPanel'
import { OverviewPage } from './components/OverviewPage'
import { HASHES, initialView, viewAfterHashChange, type View } from './views.ts'
import { ChevronRight, RefreshCw } from 'lucide-react'
import { ProfileMenu } from './components/ProfileMenu'
import { EnvelopeList } from './components/EnvelopeList'
import { EnvelopeDetail } from './components/EnvelopeDetail'
import { SettingsPage } from './components/SettingsPage'
import { SetupWizard } from './components/SetupWizard'
import { loadSettings, resolveTheme, saveSettings, type Theme } from './settings'
import { PairScreen } from './components/PairScreen'
import { canCommand, detectMode, events, loadToken, mobile, onUnauthorized, withAuth, type Mode } from './mobile'
import { t } from './i18n'
import { allDone, isSetupHidden, setupSteps, type SetupCatalog } from './setupRules'
import { api } from './components/work/shared'
import type { Catalog } from './components/work/team'

type StateFilter = 'ALL' | 'PENDING_APPROVAL' | 'DELIVERED' | 'REJECTED' | 'FAILED'
/** F3-06: what the server runs and serves (GET /health). */
type Version = { commit: string; schema: string | null; ui_bundle: string | null }

export default function App() {
  // TB-12: on the Mac (8765) the panel is unchanged; through the mobile gateway it is read-mostly or asks to pair.
  const [mode, setMode] = useState<Mode | null>(null)
  useEffect(() => { loadToken(); void detectMode().then(m => {
    // The offline shell only on the phone (gateway, HTTPS); never on the Mac panel, where it would pin old bundles.
    if (m.kind !== 'desktop' && 'serviceWorker' in navigator) void navigator.serviceWorker.register('/ui/sw.js', { scope: '/ui/' }).catch(() => {})
    setMode(m)
  }) }, [])
  useEffect(() => onUnauthorized(() => { mobile.active = false; setMode({ kind: 'pair' }) }), [])
  if (!mode) return <p className="loading">{t('shell.loading')}</p>
  if (mode.kind === 'pair') return <PairScreen />
  return <Shell />
}

const PAGES: Record<View, string> = { overview: t('shell.nav.overview'), work: t('shell.nav.work'), bridge: t('shell.nav.bridge'), settings: t('shell.nav.settings') }
const settingsSubpath = (h: string) => h.startsWith('#/settings/') ? h.slice('#/settings/'.length) : ''
const routeHash = (h: string) => h.startsWith('#/settings/') ? HASHES.settings : h
const firstView = () => initialView(routeHash(location.hash), matchMedia('(max-width: 600px)').matches)

function Shell() {
  const [view, setView] = useState<View>(firstView)
  const [setup, setSetup] = useState(() => location.hash === '#/setup')
  const [setupNeeded, setSetupNeeded] = useState(false)
  const [settingsSub, setSettingsSub] = useState(() => settingsSubpath(location.hash))
  useEffect(() => { const on = () => {
    if (location.hash === '#/setup') { setSetup(true); return }
    setSetup(false); setSettingsSub(settingsSubpath(location.hash))
    const v = viewAfterHashChange(routeHash(location.hash), matchMedia('(max-width: 600px)').matches); if (v) setView(v)
  }; addEventListener('hashchange', on); return () => removeEventListener('hashchange', on) }, [])
  useEffect(() => {
    if (mobile.active) return
    let cancelled = false
    const checkSetup = async () => {
      const [accounts, catalog, settings, works] = await Promise.allSettled([
        listAccounts(), api<Catalog>('/api/sessions/catalog'), loadSettings(), listWorks(false),
      ])
      if (cancelled) return
      const steps = setupSteps({
        accounts: accounts.status === 'fulfilled' ? accounts.value.accounts : null,
        catalog: catalog.status === 'fulfilled' ? catalog.value as SetupCatalog : null,
        settings: settings.status === 'fulfilled' ? settings.value : null,
        workCount: works.status === 'fulfilled' ? works.value.items.length : 0,
        errors: { cli: accounts.status === 'rejected', auth: catalog.status === 'rejected',
          team: settings.status === 'rejected', work: works.status === 'rejected' },
      })
      // Same steps as the wizard: a saved default team is optional (providers.yaml's chain applies without one).
      const required = steps.filter(s => s.key !== 'team')
      setSetupNeeded(!allDone(required))
      if (!location.hash && !isSetupHidden() && works.status === 'fulfilled' && works.value.items.length === 0 && !allDone(required)) {
        try {
          if (localStorage.getItem('pb.setupWizardSeen') !== '1') {
            localStorage.setItem('pb.setupWizardSeen', '1')
            location.hash = '#/setup'
          }
        } catch { /* Storage may be unavailable; the current launch still opens the wizard. */ location.hash = '#/setup' }
      }
    }
    void checkSetup()
    return () => { cancelled = true }
  }, [])
  const [version, setVersion] = useState<Version | null>(null)
  useEffect(() => {
    fetch('/health', withAuth()).then(r => r.json()).then(h => setVersion(h.version ?? null)).catch(() => setVersion(null))
  }, [])
  // TB-19: the theme is a setting on the Mac (system / dark / light); localStorage only makes the first paint right.
  const [theme, setTheme] = useState<Theme>(() => {
    try { const saved = localStorage.getItem('conductor-theme'); if (saved === 'light' || saved === 'dark' || saved === 'system') return saved } catch { /* Storage may be unavailable. */ }
    return 'system'
  })
  useEffect(() => { if (canCommand()) void loadSettings().then(v => { if (v) setTheme(v.settings.theme) }) }, [])
  const shown = resolveTheme(theme, matchMedia('(prefers-color-scheme: dark)').matches)
  useEffect(() => {
    document.documentElement.dataset.theme = shown
    try { localStorage.setItem('conductor-theme', theme) } catch { /* Theme still works in memory. */ }
  }, [theme, shown])
  const chooseTheme = (next: Theme) => {
    setTheme(next)
    if (canCommand()) void saveSettings({ theme: next }).catch(() => {})
  }
  const toggleTheme = () => chooseTheme(shown === 'dark' ? 'light' : 'dark')
  // TB-20: no top bar; brand on top of the sidebar, approvals / settings / theme / version at its foot (like Claude).
  const [waiting, setWaiting] = useState(0)
  useEffect(() => {
    const load = () => void listEnvelopes('PENDING_APPROVAL').then(e => setWaiting(e.length)).catch(() => {})
    load(); const t = setInterval(load, 15000); return () => clearInterval(t)
  }, [])
  const open = (v: View, hash: string = v === 'work' ? '#/' : HASHES[v]) => { setView(v); setSettingsSub(settingsSubpath(hash)); if (location.hash !== hash) location.hash = hash }
  const openSetup = () => { setSetup(true); if (location.hash !== '#/setup') location.hash = '#/setup' }
  // K-059: the brand on top; the control centre moved into the profile menu at the foot (like Claude)
  const head = <div className="brand side-brand"><img className="mark" src="/ui/logo.svg" alt="" width={24} height={24} />PromptBridge</div>
  const top = <header className="topbar d-only">
    <div className="crumbs"><span>{t('shell.nav.controlCenter')}</span><ChevronRight aria-hidden /><strong>{setup ? t('shell.wizard.open') : PAGES[view]}</strong></div>
    <div className="topbar-right">
      <span className={'status-dot' + (version ? ' online' : '')} aria-hidden /><span role="status">{version ? t('shell.host.connected') : t('shell.host.waiting')}</span>
      <span className="top-divider" aria-hidden />
      <button className="icon-btn plain" aria-label={t('shell.reload.label')} title={t('shell.reload.title')} onClick={() => location.reload()}><RefreshCw aria-hidden /></button>
    </div>
  </header>
  const foot = <nav className="side-nav" aria-label={t('shell.nav.sectionsLabel')}>
    {/* ≤600px: a bottom tab bar; this tab opens the work list (hidden on wide screens, where the list is always there) */}
    <button className="m-only" aria-current={view === 'work' ? 'page' : undefined} onClick={() => open('work')}>{t('shell.nav.work')}</button>
    <button aria-current={view === 'bridge' ? 'page' : undefined} onClick={() => open(view === 'bridge' ? 'work' : 'bridge')}>{t('shell.nav.bridge')}{waiting ? <span className="count">{waiting}</span> : null}</button>
    <button aria-current={view === 'settings' ? 'page' : undefined} onClick={() => open(view === 'settings' ? 'work' : 'settings')}>{t('shell.nav.settings')}</button>
    <ProfileMenu view={view} setup={setup} waiting={waiting} setupNeeded={setupNeeded} theme={theme} version={version}
      onOpen={v => { setSetup(false); open(v) }} onSetup={openSetup} onTheme={chooseTheme} />
    <div className="side-meta m-only">
      <button className="icon-btn" aria-label={shown === 'dark' ? t('shell.theme.toLight') : t('shell.theme.toDark')} title={t('shell.theme.toggleTitle')} onClick={toggleTheme}>{shown === 'dark' ? '☀' : '☾'}</button>
      {version && <span className="host mono" title={t('shell.version.title', { host: location.host })}>{t('shell.version.label', { commit: version.commit, schema: version.schema ?? '?' })}</span>}
    </div>
  </nav>
  return <div className="product-shell">
    <WorkPanel head={head} foot={foot} top={top} onOpen={() => setView('work')}
      override={setup ? <SetupWizard onOverview={() => { setSetup(false); open('overview') }} onSettings={() => { setSetup(false); open('settings') }} onNewWork={() => { setSetup(false); open('work', '#/new') }} />
        : view === 'overview' ? <OverviewPage onOpenWork={id => open('work', `#/works/${encodeURIComponent(id)}`)}
          onNewWork={() => open('work', '#/new')} onApprovals={() => open('bridge')} onSettings={() => open('settings')} />
        : view === 'bridge' ? <BridgePanel /> : view === 'settings' ? <SettingsPage subpath={settingsSub} onTheme={setTheme} /> : null} />
  </div>
}

function BridgePanel() {
  const [envelopes, setEnvelopes] = useState<Envelope[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<StateFilter>('ALL')
  const [live, setLive] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const esRef = useRef<ReturnType<typeof events> | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setEnvelopes(await listEnvelopes())
    } catch (e) {
      setError(String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Load one complete snapshot for global counts and local filtering.
  useEffect(() => { refresh() }, [refresh])

  // SSE live updates
  useEffect(() => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }
    const es = events('/api/events')
    esRef.current = es
    es.addEventListener('envelope_update', () => { refresh() })
    es.onopen = () => setLive(true)
    es.onerror = () => setLive(false)
    return () => { es.close(); setLive(false) }
  }, [refresh])

  const handleReconcile = async () => {
    try {
      await reconcile()
      refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const counts: Record<string, number> = {}
  for (const env of envelopes) {
    counts[env.state] = (counts[env.state] ?? 0) + 1
  }

  return (
    <div className="app">
      <header className="app-header">
        <span className="app-title">{t('shell.bridge.title')}</span>
        <div className="app-actions">
          {!mobile.active && <button className="btn-ghost" onClick={handleReconcile} title={t('shell.bridge.reconcileTitle')}>
            {t('shell.bridge.reconcile')}
          </button>}
          <span className={`live-indicator ${live ? 'live' : 'offline'}`} role="status">
            {live ? t('shell.bridge.live') : t('shell.bridge.offline')}
          </span>
        </div>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          ⚠ {error}
          <button type="button" aria-label={t('shell.bridge.dismissError')} onClick={() => setError(null)}>✕</button>
        </div>
      )}

      <div className="app-body">
        <aside className="sidebar">
          <div className="filter-bar" role="group" aria-label={t('shell.bridge.filterLabel')}>
            {(['ALL', 'PENDING_APPROVAL', 'DELIVERED', 'REJECTED', 'FAILED'] as StateFilter[]).map(s => (
              <button
                key={s}
                className={`filter-btn ${filter === s ? 'active' : ''}`}
                aria-pressed={filter === s}
                onClick={() => setFilter(s)}
              >
                {({ALL:t('shell.bridge.filterAll'),PENDING_APPROVAL:t('shell.bridge.filterPending'),DELIVERED:t('shell.bridge.filterDelivered'),REJECTED:t('shell.bridge.filterRejected'),FAILED:t('shell.bridge.filterFailed')})[s]}
                {s !== 'ALL' && counts[s] ? ` (${counts[s]})` : ''}
              </button>
            ))}
          </div>
          {loading
            ? <p className="loading">{t('shell.loading')}</p>
            : <EnvelopeList
                envelopes={filter === 'ALL' ? envelopes : envelopes.filter(env => env.state === filter)}
                selectedId={selectedId}
                onSelect={setSelectedId}
              />}
        </aside>

        <main className="detail-pane">
          {selectedId
            ? <EnvelopeDetail
                key={selectedId}
                eid={selectedId}
                onAction={() => { refresh(); }}
              />
            : <div className="empty-state">
                <p>{t('shell.bridge.selectEnvelope')}</p>
              </div>}
        </main>
      </div>
    </div>
  )
}
