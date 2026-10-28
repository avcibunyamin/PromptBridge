import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { LoaderCircle, RotateCcw, Save } from 'lucide-react'
import { AccountsCard } from './AccountsCard'
import { MobileSettings } from './MobileSettings'
import { canCommand, mobile } from '../mobile'
import { NotificationsCard } from './NotificationsCard'
import { ModelsCard } from './ModelsCard'
import { githubStatus } from '../api'
import type { GhStatus } from '../githubRules'
import { TeamPicker, type TeamSelection } from './work/TeamPicker'
import { baseOf } from './work/team'
import { api, teamOf, type Policy } from './work/shared'
import {
  acceptedDigest,
  availableSettingsTabs,
  defaultTeam,
  loadSettings,
  readStoredSettingsTab,
  saveSettings,
  settingsTab,
  writeStoredSettingsTab,
  type SettingsPatch,
  type SettingsTab,
  type SettingsView,
  type Theme,
} from '../settings'
import { lang, setLang, sayIn, t, type Lang } from '../i18n'

const themes: [Theme, string][] = [['system', t('settings.theme.system')], ['dark', t('settings.theme.dark')], ['light', t('settings.theme.light')]]

/** TB-19 (K-026): preferences saved to .system/settings.json. The provider ceiling stays in providers.yaml.
 *  K-033: a phone allowed to give work sees the default team, the theme and the ceiling; the rest stays on the Mac. */
export function SettingsPage({ subpath, onTheme }: { subpath?: string; onTheme: (t: Theme) => void }) {
  const [view, setView] = useState<SettingsView | null>(null), [policy, setPolicy] = useState<Policy | null>(null)
  const [sel, setSel] = useState<TeamSelection | null>(null), [cloneDir, setCloneDir] = useState('')
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState('')
  const [gh, setGh] = useState<GhStatus | null>(null)   // TB-21: account name only
  const tabRefs = useRef<Partial<Record<SettingsTab, HTMLButtonElement | null>>>({})

  const isMobile = mobile.active
  const canCmd = canCommand()
  const available = useMemo(() => availableSettingsTabs({ mobile: isMobile, canCommand: canCmd }), [isMobile, canCmd])
  const routeHash = subpath !== undefined ? (subpath ? `#/settings/${subpath}` : '#/settings') : location.hash
  const [selectedTab, setSelectedTab] = useState<SettingsTab>(() => settingsTab(routeHash, readStoredSettingsTab(), available))
  const activeTab = available.includes(selectedTab) ? selectedTab : settingsTab(routeHash, readStoredSettingsTab(), available)

  useEffect(() => {
    const next = settingsTab(routeHash, readStoredSettingsTab(), available)
    setSelectedTab(prev => {
      if (prev !== next && prev === 'team') setSel(null)
      return next
    })
    writeStoredSettingsTab(next)
    const target = `#/settings/${next}`
    if (location.hash !== target && (location.hash === '#/settings' || location.hash.startsWith('#/settings/'))) {
      history.replaceState(null, '', target)
    }
  }, [routeHash, available])

  useEffect(() => {
    const sync = () => {
      if (location.hash === '#/settings' || location.hash === '#/settings/') {
        const next = settingsTab(location.hash, readStoredSettingsTab(), available)
        history.replaceState(null, '', `#/settings/${next}`)
      }
    }
    addEventListener('hashchange', sync)
    return () => removeEventListener('hashchange', sync)
  }, [available])

  useEffect(() => { if (!mobile.active) githubStatus().then(setGh, () => setGh({ installed: false, logged_in: false, account: null })) }, [])

  const loadPolicy = useCallback(async () => { setPolicy(await api<Policy>('/api/sessions/providers')) }, [])
  useEffect(() => {
    // a phone without the command scope has no settings to read (the gateway would refuse and audit it; agy P3)
    void Promise.all([mobile.active && !canCommand() ? Promise.resolve(null) : loadSettings(), loadPolicy()]).then(([v]) => { setView(v); setCloneDir(v?.settings.github?.clone_dir ?? '') })   // a phone gets no github field (K-033/4b)
      .catch(e => setError(sayIn('settings.', e)))
  }, [loadPolicy])

  async function save(patch: SettingsPatch, label: string) {
    setBusy(true); setError(''); setSaved('')
    try {
      const v = await saveSettings(patch); setView(v); setSaved(label)
      if (patch.theme) onTheme(patch.theme)
    } catch (e) { setError(sayIn('settings.', e)) } finally { setBusy(false) }
  }

  function selectTab(next: SettingsTab) {
    if (activeTab === 'team' && next !== 'team') setSel(null)
    setSelectedTab(next)
    writeStoredSettingsTab(next)
    const target = `#/settings/${next}`
    if (location.hash !== target) location.hash = target
  }

  function onTabKeyDown(e: KeyboardEvent<HTMLButtonElement>, current: SettingsTab) {
    const idx = available.indexOf(current)
    if (idx < 0 || !available.length) return
    let next: SettingsTab | null = null
    if (e.key === 'ArrowRight') next = available[(idx + 1) % available.length]
    else if (e.key === 'ArrowLeft') next = available[(idx - 1 + available.length) % available.length]
    else if (e.key === 'Home') next = available[0]
    else if (e.key === 'End') next = available[available.length - 1]
    if (!next) return
    e.preventDefault()
    selectTab(next)
    tabRefs.current[next]?.focus()
  }

  const tabLabels: Record<SettingsTab, string> = {
    team: t('settings.tabs.team'),
    accounts: t('settings.tabs.accounts'),
    models: t('settings.tabs.models'),
    appearance: t('settings.tabs.appearance'),
    integrations: t('settings.tabs.integrations'),
    phone: t('settings.tabs.phone'),
  }

  const accepted = !!view && !!acceptedDigest(view)
  const unchanged = !!sel?.preview && sel.preview.digest === acceptedDigest(view)
  const allowed = policy?.ceiling?.allowed
  return <div className="body mobile-body"><main className="main"><div className="pane settings-page">
    <div className="run-head"><div><h1>{t('settings.title')}</h1>
      <p className="intro">{mobile.active ? t('settings.intro.mobile') : t('settings.intro.mac')} {t('settings.intro.ceiling')}</p></div></div>

    <div className="settings-tabs" role="tablist" aria-label={t('settings.tabs.label')}>
      {available.map(tab => (
        <button
          type="button"
          key={tab}
          ref={el => { tabRefs.current[tab] = el }}
          id={`settings-tab-${tab}`}
          className="settings-tab"
          role="tab"
          aria-selected={activeTab === tab}
          aria-controls={`settings-panel-${tab}`}
          tabIndex={activeTab === tab ? 0 : -1}
          onClick={() => selectTab(tab)}
          onKeyDown={e => onTabKeyDown(e, tab)}
        >
          {tabLabels[tab]}
        </button>
      ))}
    </div>

    {error && <div className="error-banner" role="alert">{error}</div>}
    {view?.error && <p className="notice">{t('settings.fileError')}</p>}
    {saved && <p className="muted small" role="status">{saved}</p>}

    <div id={`settings-panel-${activeTab}`} className="settings-tabpanel" role="tabpanel" aria-labelledby={`settings-tab-${activeTab}`}>
      {activeTab === 'team' && canCmd && <>
        <section className="card"><div className="card-h"><h2>{t('settings.team.title')}</h2><span className="spacer" />
          {accepted && <span className="pill ok"><span className="d" aria-hidden="true" />{t('settings.team.accepted')}</span>}</div>
          <div className="card-b form-fields">
            <p className="muted">{t('settings.team.intro')} <strong>{t('settings.team.acceptLead')}</strong> {t('settings.team.acceptBody')}</p>
            {!!view?.problems.length && <p className="notice">{t('settings.team.disallowed')}</p>}
            {policy && view !== null ? <TeamPicker policy={policy} initial={defaultTeam(view) ?? teamOf(policy.policy)} onReloadPolicy={loadPolicy} onChange={setSel} />
              : policy ? null : <p className="loading">{t('settings.loading')}</p>}
            <div className="form-footer">
              {view?.settings.team && <button className="btn" disabled={busy} onClick={() => void save({ team: null }, t('settings.team.reverted'))}>
                <RotateCcw aria-hidden />{t('settings.team.revert')}</button>}
              <button className="btn primary" disabled={busy || !sel?.preview || unchanged} onClick={() => sel && void save({ team: sel.team }, t('settings.team.saved'))}>
                {busy ? <LoaderCircle className="spin" aria-hidden /> : <Save aria-hidden />}{t('settings.team.save')}</button>
            </div>
          </div></section>

        <section className="card"><div className="card-h"><h2>{t('settings.ceiling.title')}</h2></div><div className="card-b">
          {allowed ? <ul className="ceiling-list">{(['author', 'reviewer'] as const).map(role => <li key={role}>
            <strong>{role === 'author' ? t('settings.ceiling.author') : t('settings.ceiling.reviewer')}:</strong> <span className="mono small">{allowed[role].join(', ')}</span></li>)}</ul>
            : <p className="muted">{t('settings.ceiling.empty')}</p>}
          <p className="muted small">{t('settings.ceiling.note')}</p>
        </div></section>
      </>}

      {activeTab === 'accounts' && canCmd && !isMobile && <AccountsCard onChanged={loadPolicy} />}

      {activeTab === 'models' && canCmd && <ModelsCard
        efforts={Object.fromEntries(Object.entries(policy?.capabilities ?? {}).map(([k, c]) => [k, c.efforts ?? []]))}
        bases={[...new Set([...(allowed?.author ?? []), ...(allowed?.reviewer ?? [])].map(baseOf))]} />}

      {activeTab === 'appearance' && <section className="card"><div className="card-h"><h2>{t('settings.appearance.title')}</h2></div><div className="card-b form-fields">
        <label htmlFor="set-theme">{t('settings.theme.label')}<select id="set-theme" value={view?.settings.theme ?? 'system'} disabled={busy || !view}
          onChange={e => void save({ theme: e.target.value as Theme }, t('settings.theme.saved'))}>
          {themes.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
        <label htmlFor="set-lang">{t('settings.lang')}<select id="set-lang" value={lang}
          onChange={e => setLang(e.target.value as Lang)}>
          <option value="tr">{t('settings.lang.tr')}</option>
          <option value="en">{t('settings.lang.en')}</option></select></label>
        <p className="muted small">{t('settings.lang.note')}</p>
      </div></section>}

      {activeTab === 'integrations' && !isMobile && <>
        <section className="card"><div className="card-h"><h2>GitHub</h2></div><div className="card-b form-fields">
          <label htmlFor="set-clone">{t('settings.github.cloneLabel')}<input id="set-clone" className="mono" value={cloneDir} placeholder={t('settings.github.clonePlaceholder')}
            onChange={e => setCloneDir(e.target.value)} /></label>
          <p className="muted small">{t('settings.github.cloneHelp')}</p>
          <p className="small" role="status">{!gh ? t('settings.github.checking') : !gh.installed ? t('settings.github.notInstalled')
            : gh.logged_in ? t('settings.github.loggedIn', { account: gh.account ?? '' })
            : t('settings.github.loggedOut')}</p>
          <div className="form-footer"><button className="btn" disabled={busy || !view || cloneDir === (view.settings.github.clone_dir ?? '')}
            onClick={() => void save({ github: { clone_dir: cloneDir.trim() || null } }, t('settings.github.saved'))}><Save aria-hidden />{t('settings.save')}</button></div>
        </div></section>

        <section className="card"><div className="card-h"><h2>{t('settings.preview.title')}</h2></div><div className="card-b form-fields">
          <label className="accept-row"><input type="checkbox" checked={!!view?.settings.preview?.sandbox} disabled={busy || !view}
            onChange={e => void save({ preview: { sandbox: e.target.checked } }, e.target.checked ? t('settings.preview.sandboxOn') : t('settings.preview.sandboxOff'))} />
            <span><strong>{t('settings.preview.sandboxLead')}</strong> {t('settings.preview.sandboxBody')}</span></label>
        </div></section>
      </>}

      {activeTab === 'phone' && <>
        {!isMobile && <MobileSettings embedded />}
        {isMobile && <NotificationsCard />}
      </>}
    </div>
  </div></main></div>
}
