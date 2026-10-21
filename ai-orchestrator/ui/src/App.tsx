import { useEffect, useRef, useState, useCallback } from 'react'
import { listEnvelopes, reconcile, type Envelope } from './api'
import { WorkPanel } from './components/WorkPanel'
import { OverviewPage } from './components/OverviewPage'
import { HASHES, initialView, viewAfterHashChange, type View } from './views.ts'
import { ChevronRight, FileCheck2, LayoutDashboard, MessagesSquare, RefreshCw, Settings2 } from 'lucide-react'
import { EnvelopeList } from './components/EnvelopeList'
import { EnvelopeDetail } from './components/EnvelopeDetail'
import { SettingsPage } from './components/SettingsPage'
import { loadSettings, resolveTheme, saveSettings, type Theme } from './settings'
import { PairScreen } from './components/PairScreen'
import { canCommand, detectMode, events, loadToken, mobile, onUnauthorized, withAuth, type Mode } from './mobile'

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
  if (!mode) return <p className="loading">Yükleniyor…</p>
  if (mode.kind === 'pair') return <PairScreen />
  return <Shell />
}

const PAGES: Record<View, string> = { overview: 'Genel bakış', work: 'Çalışmalar', bridge: 'Onaylar', settings: 'Ayarlar' }
const firstView = () => initialView(location.hash, matchMedia('(max-width: 600px)').matches)

function Shell() {
  const [view, setView] = useState<View>(firstView)
  useEffect(() => { const on = () => { const v = viewAfterHashChange(location.hash, matchMedia('(max-width: 600px)').matches); if (v) setView(v) }; addEventListener('hashchange', on); return () => removeEventListener('hashchange', on) }, [])
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
  const toggleTheme = () => {
    const next: Theme = shown === 'dark' ? 'light' : 'dark'
    setTheme(next)
    if (canCommand()) void saveSettings({ theme: next }).catch(() => {})
  }
  // TB-20: no top bar; brand on top of the sidebar, approvals / settings / theme / version at its foot (like Claude).
  const [waiting, setWaiting] = useState(0)
  useEffect(() => {
    const load = () => void listEnvelopes('PENDING_APPROVAL').then(e => setWaiting(e.length)).catch(() => {})
    load(); const t = setInterval(load, 15000); return () => clearInterval(t)
  }, [])
  const open = (v: View, hash: string = v === 'work' ? '#/' : HASHES[v]) => { setView(v); if (location.hash !== hash) location.hash = hash }
  const item = (v: View, icon: JSX.Element, extra?: JSX.Element) =>
    <button className="nav-item" aria-current={view === v ? 'page' : undefined} onClick={() => open(v)}>{icon}{PAGES[v]}{extra}</button>
  const head = <>
    <div className="brand side-brand"><img className="mark" src="/ui/logo.svg" alt="" width={24} height={24} />PromptBridge</div>
    {/* K-040: ZeroCost's desktop sidebar — where it runs, then the control centre's sections, then recent works */}
    <div className="host-card d-only" title={location.host}>
      <span className="host-mono">P</span>
      <span><strong>Bu Mac</strong><span role="status">{version ? 'Yerel sunucu bağlı' : 'Bağlantı bekleniyor'}</span></span>
      <span className={'status-dot' + (version ? ' online' : '')} aria-hidden />
    </div>
    <p className="nav-label d-only">KONTROL MERKEZİ</p>
    <nav className="nav-main d-only" aria-label="Kontrol merkezi">
      {item('overview', <LayoutDashboard aria-hidden />)}
      {item('work', <MessagesSquare aria-hidden />)}
      {item('bridge', <FileCheck2 aria-hidden />, waiting ? <span className="nav-count">{waiting}</span> : undefined)}
      {item('settings', <Settings2 aria-hidden />)}
    </nav>
  </>
  const top = <header className="topbar d-only">
    <div className="crumbs"><span>Kontrol merkezi</span><ChevronRight aria-hidden /><strong>{PAGES[view]}</strong></div>
    <div className="topbar-right">
      <span className={'status-dot' + (version ? ' online' : '')} aria-hidden /><span role="status">{version ? 'Yerel sunucu bağlı' : 'Bağlantı bekleniyor'}</span>
      <span className="top-divider" aria-hidden />
      <button className="icon-btn plain" aria-label="Yeniden yükle" title="Paneli yeniden yükle" onClick={() => location.reload()}><RefreshCw aria-hidden /></button>
    </div>
  </header>
  const foot = <nav className="side-nav" aria-label="Uygulama bölümleri">
    {/* ≤600px: a bottom tab bar; this tab opens the work list (hidden on wide screens, where the list is always there) */}
    <button className="m-only" aria-current={view === 'work' ? 'page' : undefined} onClick={() => open('work')}>Çalışmalar</button>
    <button aria-current={view === 'bridge' ? 'page' : undefined} onClick={() => open(view === 'bridge' ? 'work' : 'bridge')}>Onaylar{waiting ? <span className="count">{waiting}</span> : null}</button>
    <button aria-current={view === 'settings' ? 'page' : undefined} onClick={() => open(view === 'settings' ? 'work' : 'settings')}>Ayarlar</button>
    <div className="side-meta">
      <button className="icon-btn" aria-label={shown === 'dark' ? 'Açık temaya geç' : 'Koyu temaya geç'} title="Tema değiştir" onClick={toggleTheme}>{shown === 'dark' ? '☀' : '☾'}</button>
      {version && <span className="host mono" title={`Çalışan kod · veritabanı şeması · sunulan panel · ${location.host}`}>{version.commit} · şema {version.schema ?? '?'}</span>}
    </div>
  </nav>
  return <div className="product-shell">
    <WorkPanel head={head} foot={foot} top={top} onOpen={() => setView('work')}
      override={view === 'overview' ? <OverviewPage onOpenWork={id => open('work', `#/works/${encodeURIComponent(id)}`)}
          onNewWork={() => open('work', '#/new')} onApprovals={() => open('bridge')} onSettings={() => open('settings')} />
        : view === 'bridge' ? <BridgePanel /> : view === 'settings' ? <SettingsPage onTheme={setTheme} /> : null} />
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
        <span className="app-title">Dosya onayları</span>
        <div className="app-actions">
          {!mobile.active && <button className="btn-ghost" onClick={handleReconcile} title="İletiliyor durumunda kalan kayıtları uzlaştır">
            Uzlaştır
          </button>}
          <span className={`live-indicator ${live ? 'live' : 'offline'}`} role="status">
            {live ? '● Canlı' : '○ Bağlantı yok'}
          </span>
        </div>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          ⚠ {error}
          <button type="button" aria-label="Hatayı kapat" onClick={() => setError(null)}>✕</button>
        </div>
      )}

      <div className="app-body">
        <aside className="sidebar">
          <div className="filter-bar" role="group" aria-label="Zarf durumu filtresi">
            {(['ALL', 'PENDING_APPROVAL', 'DELIVERED', 'REJECTED', 'FAILED'] as StateFilter[]).map(s => (
              <button
                key={s}
                className={`filter-btn ${filter === s ? 'active' : ''}`}
                aria-pressed={filter === s}
                onClick={() => setFilter(s)}
              >
                {({ALL:'Tümü',PENDING_APPROVAL:'Onay bekliyor',DELIVERED:'İletildi',REJECTED:'Reddedildi',FAILED:'Hata'})[s]}
                {s !== 'ALL' && counts[s] ? ` (${counts[s]})` : ''}
              </button>
            ))}
          </div>
          {loading
            ? <p className="loading">Yükleniyor…</p>
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
                <p>Listeden bir zarf seçin.</p>
              </div>}
        </main>
      </div>
    </div>
  )
}
