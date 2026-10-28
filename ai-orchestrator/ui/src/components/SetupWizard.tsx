import { useCallback, useEffect, useMemo, useState } from 'react'
import { listAccounts, listWorks, setupChecks, type AccountItem, type SetupCheck, type Work } from '../api'
import { mobile } from '../mobile'
import { loadSettings } from '../settings'
import { doctorWizardStatuses, setupSteps, type SetupCatalog, type SetupSettings, type WizardStatus } from '../setupRules'
import { api } from './work/shared'
import type { Catalog } from './work/team'
import { t } from '../i18n'

type Preset = 'example' | 'claude-only' | 'codex-only' | 'agy-only' | 'ollama-local'
const PRESETS: Preset[] = ['example', 'claude-only', 'codex-only', 'agy-only', 'ollama-local']
const PRESET_FILES: Record<Preset, string> = {
  example: 'providers.example.yaml',
  'claude-only': 'providers.claude-only.yaml',
  'codex-only': 'providers.codex-only.yaml',
  'agy-only': 'providers.agy-only.yaml',
  'ollama-local': 'providers.ollama-local.yaml',
}

export function SetupWizard({ onOverview, onSettings, onNewWork }: {
  onOverview: () => void
  onSettings: () => void
  onNewWork: () => void
}) {
  const [checks, setChecks] = useState<SetupCheck[] | null>(null)
  const [accounts, setAccounts] = useState<AccountItem[] | null>(null)
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [settings, setSettings] = useState<SetupSettings | null>(null)
  const [works, setWorks] = useState<Work[] | null>(null)
  const [errors, setErrors] = useState<Record<string, boolean>>({})
  const [loading, setLoading] = useState(false)
  const [copied, setCopied] = useState<string | null>(null)
  const [preset, setPreset] = useState<Preset>('example')

  const refresh = useCallback(async () => {
    if (mobile.active) return
    setLoading(true)
    const results = await Promise.allSettled([
      setupChecks(), listAccounts(), api<Catalog>('/api/sessions/catalog'), loadSettings(), listWorks(false),
    ])
    const failures: Record<string, boolean> = {}
    if (results[0].status === 'fulfilled') setChecks(results[0].value.checks); else failures.checks = true
    if (results[1].status === 'fulfilled') setAccounts(results[1].value.accounts); else failures.accounts = true
    if (results[2].status === 'fulfilled') setCatalog(results[2].value); else failures.catalog = true
    if (results[3].status === 'fulfilled') setSettings(results[3].value); else failures.settings = true
    if (results[4].status === 'fulfilled') setWorks(results[4].value.items); else failures.works = true
    setErrors(failures)
    setLoading(false)
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const statuses = doctorWizardStatuses(checks)
  if (errors.checks) {
    statuses.python = 'warning'
    statuses.providers = 'warning'
    statuses.clis = 'warning'
    statuses.panel = 'warning'
    statuses.node = 'warning'
  }
  const guide = setupSteps({ accounts, catalog: catalog as SetupCatalog | null, settings,
    workCount: works?.length ?? 0,
    errors: { cli: errors.accounts, auth: errors.catalog, team: errors.settings, work: errors.works } })
  const auth = guide[1]
  const workDone = (works?.length ?? 0) > 0 && !errors.works
  const steps: { id: string; title: string; description: string; status: WizardStatus; command?: string }[] = [
    { id: 'python', title: t('shell.wizard.pythonTitle'), description: checks?.find(c => c.key === 'python')?.detail ?? t('shell.wizard.pythonNote'), status: statuses.python,
      command: statuses.python === 'missing' ? 'python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt' : undefined },
    { id: 'providers', title: t('shell.wizard.providersTitle'), description: checks?.find(c => c.key === 'providers')?.detail ?? t('shell.wizard.providersNote'), status: statuses.providers,
      command: statuses.providers === 'missing' ? `mkdir -p .system && cp ${PRESET_FILES[preset]} .system/providers.yaml` : undefined },
    { id: 'clis', title: t('shell.wizard.cliTitle'), description: checks?.filter(c => c.key.startsWith('cli:')).map(c => `${c.key.slice(4)}: ${c.detail}`).join(' · ') || t('shell.wizard.cliNote'),
      status: statuses.clis === 'checking' && statuses.providers !== 'complete' ? 'warning' : statuses.clis },
    { id: 'auth', title: t('shell.wizard.authTitle'), description: auth.note ?? (auth.unverified ? t('shell.setup.unverified') : t('shell.wizard.authNote')),
      status: errors.catalog || errors.accounts ? 'warning' : auth.done ? 'complete' : accounts === null || catalog === null ? 'checking' : 'missing',
      command: auth.loginCommand ?? undefined },
    { id: 'panel', title: t('shell.wizard.panelTitle'), description: [checks?.find(c => c.key === 'panel')?.detail, checks?.find(c => c.key === 'node')?.detail].filter(Boolean).join(' · ') || t('shell.wizard.panelNote'),
      status: errors.checks ? 'warning' : statuses.panel === 'missing' || statuses.node === 'missing' ? 'missing'
        : statuses.panel === 'checking' || statuses.node === 'checking' ? 'checking'
          : statuses.panel === 'warning' || statuses.node === 'warning' ? 'warning' : 'complete',
      command: statuses.panel === 'missing' ? 'cd ui && npm install && npm run build && cd ..' : undefined },
    { id: 'work', title: t('shell.wizard.workTitle'), description: errors.works ? t('shell.unreadable') : workDone ? t('shell.wizard.workDone') : t('shell.wizard.workNote'), status: errors.works ? 'warning' : works === null ? 'checking' : workDone ? 'complete' : 'missing' },
  ]
  const complete = steps.every(step => step.status === 'complete')
  const completedCount = steps.filter(step => step.status === 'complete').length
  const percent = Math.round((completedCount / steps.length) * 100)
  const copy = async (id: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(id)
      window.setTimeout(() => setCopied(current => current === id ? null : current), 1800)
    } catch { /* Clipboard may be unavailable in an insecure context. */ }
  }
  const signInHint = useMemo(() => auth.loginAccount ? t('shell.wizard.loginFor', { account: auth.loginAccount }) : '', [auth.loginAccount])

  return <main className="setup-wizard">
    <header className="setup-wizard-head">
      <div><p className="eyebrow">{t('shell.wizard.eyebrow')}</p><h1>{t('shell.wizard.heading')}</h1><p>{t('shell.wizard.intro')}</p></div>
      <button className="btn" type="button" onClick={() => void refresh()} disabled={loading} aria-busy={loading}>{loading ? t('shell.wizard.checking') : t('shell.wizard.recheck')}</button>
    </header>
    <div className="setup-progress" aria-label={t('shell.wizard.progress', { done: completedCount, total: steps.length })}>
      <div className="setup-progress-copy"><strong>{t('shell.wizard.progress', { done: completedCount, total: steps.length })}</strong><span>{percent}%</span></div>
      <div className="setup-progress-track"><span style={{ width: `${percent}%` }} /></div>
    </div>
    {complete && <div className="setup-celebrate" role="status">{t('shell.wizard.celebrate')} <button className="btn small primary" onClick={onOverview}>{t('shell.wizard.goPanel')}</button></div>}
    <ol className="setup-timeline">
      {steps.map((step, index) => <li className={`setup-wizard-step state-${step.status}`} key={step.id}>
        <span className="setup-number" aria-hidden="true">{step.status === 'complete' ? '✓' : index + 1}</span>
        <section className="setup-wizard-card">
          <div className="setup-wizard-title"><h2>{step.title}</h2><span className={`pill ${step.status === 'complete' ? 'ok' : step.status === 'warning' ? 'warn' : step.status === 'missing' ? 'bad' : 'idle'}`}>{t(`shell.wizard.status.${step.status}`)}</span></div>
          <p>{step.description}</p>
          {step.id === 'providers' && step.status !== 'complete' && <label className="setup-preset">{t('shell.wizard.preset')}
            <select value={preset} onChange={event => setPreset(event.target.value as Preset)}>{PRESETS.map(value => <option key={value} value={value}>{t(`shell.wizard.preset.${value}`)}</option>)}</select>
          </label>}
          {step.command && <div className="setup-command-row"><code>{step.command}</code><button className="btn small" onClick={() => void copy(step.id, step.command!)}>{copied === step.id ? t('shell.setup.copied') : t('shell.setup.copy')}</button></div>}
          {step.id === 'auth' && <div className="setup-wizard-actions"><span className="small muted">{signInHint}</span>
            {auth.loginCommand && <button className="btn small" onClick={() => void copy('login', auth.loginCommand!)}>{copied === 'login' ? t('shell.setup.copied') : t('shell.setup.copy')}</button>}
            <button className="btn small" onClick={() => void refresh()} disabled={loading}>{t('shell.wizard.checkSessions')}</button>
            <button className="btn small" onClick={onSettings}>{t('shell.setup.settingsAccounts')}</button>
          </div>}
          {step.id === 'work' && !workDone && <button className="btn small primary" onClick={onNewWork}>{t('shell.overview.newWork')}</button>}
        </section>
      </li>)}
    </ol>
  </main>
}
