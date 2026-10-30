import { useEffect, useRef, useState } from 'react'
import { FileCheck2, LayoutDashboard, ListChecks, MessagesSquare, Settings2 } from 'lucide-react'
import { mobile } from '../mobile'
import { t } from '../i18n'
import { api } from './work/shared'
import type { Theme } from '../settings'
import type { View } from '../views'

type Host = { user: string; login: string; computer: string; system: string }
type Version = { commit: string; schema: string | null; ui_bundle: string | null }
const THEMES: [Theme, string][] = [['system', t('settings.theme.system')], ['light', t('settings.theme.light')], ['dark', t('settings.theme.dark')]]

export const initials = (name: string) => name.trim().split(/\s+/).slice(0, 2).map(w => w[0]?.toLocaleUpperCase() ?? '').join('') || 'P'

/** K-059: Claude-like profile button at the foot of the sidebar; it opens the control centre (pages, theme, version). */
export function ProfileMenu({ view, setup, waiting, setupNeeded, theme, version, onOpen, onSetup, onTheme }: {
  view: View; setup: boolean; waiting: number; setupNeeded: boolean; theme: Theme; version: Version | null
  onOpen: (v: View) => void; onSetup: () => void; onTheme: (th: Theme) => void
}) {
  const [host, setHost] = useState<Host | null>(null), [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null), button = useRef<HTMLButtonElement>(null)
  useEffect(() => { if (!mobile.active) void api<Host>('/api/host').then(setHost).catch(() => {}) }, [])
  useEffect(() => {
    if (!open) return
    root.current?.querySelector<HTMLButtonElement>('.profile-panel button')?.focus()   // keyboard users land in the menu
    const down = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false) }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { e.preventDefault(); setOpen(false); button.current?.focus() } }
    document.addEventListener('mousedown', down); document.addEventListener('keydown', key)
    return () => { document.removeEventListener('mousedown', down); document.removeEventListener('keydown', key) }
  }, [open])
  const name = host?.user ?? t('shell.host.thisMac')
  const go = (v: View) => { setOpen(false); onOpen(v) }
  const page = (v: View, icon: JSX.Element, label: string, extra?: JSX.Element) =>
    <button type="button" className="nav-item" aria-current={!setup && view === v ? 'page' : undefined} onClick={() => go(v)}>{icon}{label}{extra}</button>
  return <div className="profile d-only" ref={root}>
    <button ref={button} type="button" className="profile-btn" aria-expanded={open} aria-controls="profile-panel" onClick={() => setOpen(o => !o)}
      title={host ? `${host.login} · ${host.computer} · ${host.system}` : undefined}>
      <span className="profile-avatar" aria-hidden>{initials(name)}</span>
      <span className="profile-text"><strong>{name}</strong>
        <span><span className={'status-dot' + (version ? ' online' : '')} aria-hidden /> {host?.computer ?? (version ? t('shell.host.connected') : t('shell.host.waiting'))}</span></span>
      {(waiting > 0 || setupNeeded) && <span className="nav-count" aria-label={t('shell.profile.attention')}>{waiting || '!'}</span>}
    </button>
    {open && <div className="profile-panel" id="profile-panel">
      <p className="nav-label">{t('shell.nav.controlCenterUpper')}</p>
      <nav aria-label={t('shell.nav.controlCenter')} className="nav-main">
        {page('overview', <LayoutDashboard aria-hidden />, t('shell.nav.overview'))}
        {page('work', <MessagesSquare aria-hidden />, t('shell.nav.work'))}
        {page('bridge', <FileCheck2 aria-hidden />, t('shell.nav.bridge'), waiting ? <span className="nav-count">{waiting}</span> : undefined)}
        {page('settings', <Settings2 aria-hidden />, t('shell.nav.settings'))}
        <button type="button" className="nav-item" aria-current={setup ? 'page' : undefined} onClick={() => { setOpen(false); onSetup() }}>
          <ListChecks aria-hidden />{t('shell.wizard.open')}{setupNeeded && <span className="setup-nav-dot" aria-label={t('shell.wizard.incomplete')} />}</button>
      </nav>
      <div className="profile-theme" role="group" aria-label={t('settings.theme.label')}>
        <span>{t('settings.theme.label')}</span>
        <div className="seg">{THEMES.map(([v, l]) => <button key={v} type="button" className={theme === v ? 'on' : ''} aria-pressed={theme === v} onClick={() => onTheme(v)}>{l}</button>)}</div>
      </div>
      {version && <p className="profile-version mono" title={t('shell.version.title', { host: location.host })}>{t('shell.version.label', { commit: version.commit, schema: version.schema ?? '?' })}</p>}
    </div>}
  </div>
}
