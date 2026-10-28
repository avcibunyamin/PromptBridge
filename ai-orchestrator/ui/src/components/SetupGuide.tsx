import { useEffect, useState } from 'react'
import { listAccounts, type AccountItem, type Work } from '../api'
import { mobile } from '../mobile'
import { loadSettings, type SettingsView } from '../settings'
import { allDone, hideSetup, isSetupHidden, setupSteps, type SetupErrors } from '../setupRules'
import { api } from './work/shared'
import type { Catalog } from './work/team'
import { t } from '../i18n'

export function SetupGuide({
  works,
  onNewWork,
  onSettings,
}: {
  works: Work[] | null
  onNewWork: () => void
  onSettings: () => void
}) {
  const [hidden, setHidden] = useState(() => isSetupHidden())
  const [accounts, setAccounts] = useState<AccountItem[] | null>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [settings, setSettings] = useState<SettingsView | null>(null)
  const [errors, setErrors] = useState<SetupErrors>({})
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (mobile.active || hidden) return
    let cancelled = false
    void listAccounts()
      .then(r => {
        if (!cancelled) {
          setAccounts(r.accounts)
          setErrors(prev => ({ ...prev, cli: false }))
        }
      })
      .catch(() => {
        if (!cancelled) setErrors(prev => ({ ...prev, cli: true }))
      })
    void api<Catalog>('/api/sessions/catalog')
      .then(c => {
        if (!cancelled) {
          setCatalog(c)
          setErrors(prev => ({ ...prev, auth: false }))
        }
      })
      .catch(() => {
        if (!cancelled) setErrors(prev => ({ ...prev, auth: true }))
      })
    void loadSettings()
      .then(s => {
        if (!cancelled) {
          setSettings(s)
          setErrors(prev => ({ ...prev, team: !s || Boolean(s.error) }))
        }
      })
      .catch(() => {
        if (!cancelled) setErrors(prev => ({ ...prev, team: true }))
      })
    return () => {
      cancelled = true
    }
  }, [hidden])

  const steps = setupSteps({
    accounts,
    catalog,
    settings,
    workCount: works?.length ?? 0,
    errors,
  })

  // wait for every source (or its error): a finished setup must not flash as "Bekliyor"
  const loading = (accounts === null && !errors.cli) || (catalog === null && !errors.auth)
    || (settings === null && errors.team === undefined) || works === null
  if (mobile.active || hidden || loading || allDone(steps)) return null

  function handleHide() {
    hideSetup()
    setHidden(true)
  }

  async function copyLoginCommand(cmd: string) {
    try {
      await navigator.clipboard?.writeText(cmd)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard access denied or unavailable */
    }
  }

  return (
    <section className="panel setup-guide" aria-label={t('shell.setup.heading')}>
      <div className="panel-heading">
        <div>
          <p className="eyebrow">{t('shell.setup.eyebrow')}</p>
          <h2>{t('shell.setup.heading')}</h2>
        </div>
        <div className="setup-guide-actions">
          {!mobile.active&&<button type="button" className="btn small ghost" onClick={() => { location.hash = '#/setup' }}>{t('shell.wizard.open')}</button>}
          {!mobile.active&&<button type="button" className="btn small ghost" onClick={handleHide}>{t('shell.setup.hideForNow')}</button>}
        </div>
      </div>
      <ol className="setup-list">
        {steps.map(step => (
          <li key={step.key} className={'setup-step' + (step.done ? ' done' : '')}>
            <div className="setup-step-main">
              <span className={'setup-mark' + (step.done ? ' ok' : '')} aria-hidden="true">
                {step.done ? '✓' : '·'}
              </span>
              <span className="setup-title">{step.title}</span>
              {step.error ? (
                <span className="pill warn">{t('shell.unreadable')}</span>
              ) : step.unverified ? (
                <span className="pill warn">{t('shell.setup.unverified')}</span>
              ) : step.done ? (
                <span className="pill ok">{t('shell.setup.done')}</span>
              ) : (
                <span className="pill idle">{t('shell.setup.pending')}</span>
              )}
            </div>
            {!step.error && !step.done && step.key === 'cli' && (
              <p className="small muted setup-note">
                {t('shell.setup.cliInstallPrefix')} (<code className="mono">docs/KULLANIM-KILAVUZU.md</code>)
              </p>
            )}
            {!step.error && !step.done && step.key === 'auth' && (
              <div className="setup-actions">
                {step.loginCommand && <code className="mono setup-cmd">{step.loginCommand}</code>}
                {!mobile.active&&step.loginCommand&&<button type="button" className="btn small" onClick={() => void copyLoginCommand(step.loginCommand!)}>{copied ? t('shell.setup.copied') : t('shell.setup.copy')}</button>}
                {!mobile.active&&<button type="button" className="btn small" onClick={onSettings}>{t('shell.setup.settingsAccounts')}</button>}
              </div>
            )}
            {!step.error && !step.done && step.key === 'team' && (
              <div className="setup-actions">
                <span className="small muted">{t('shell.setup.teamNote')}</span>
                {!mobile.active&&<button type="button" className="btn small" onClick={onSettings}>{t('shell.nav.settings')}</button>}
              </div>
            )}
            {!step.error && !step.done && step.key === 'work' && (
              <div className="setup-actions">
                {!mobile.active&&<button type="button" className="btn small primary" onClick={onNewWork}>{t('shell.overview.newWork')}</button>}
              </div>
            )}
          </li>
        ))}
      </ol>
    </section>
  )
}
