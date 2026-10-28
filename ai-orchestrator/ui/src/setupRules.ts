/** Pure setup-guide rules for the Mac overview page (node-testable, no DOM or fetch). */
import { t } from './i18n/index.ts'

export type SetupStepKey = 'cli' | 'auth' | 'team' | 'work'

export interface SetupAccount {
  base: string
  provider: string
  account?: string | null
  installed: boolean
  login?: { command: string | null; note?: string | null } | null
}

export interface SetupCatalogEntry {
  identity: string
  provider?: string
  account?: string | null
  model: string
  status?: string
  source?: string
}

export interface SetupCatalog {
  entries?: readonly SetupCatalogEntry[]
  errors?: Record<string, string>
}

export interface SetupTeam {
  author?: readonly string[]
  reviewer?: readonly string[]
}

export interface SetupSettings {
  settings?: {
    team?: SetupTeam | null
    accepted_digest?: string | null
  } | null
  team?: SetupTeam | null
  accepted_digest?: string | null
  error?: string | null
}

export interface SetupErrors {
  cli?: boolean
  auth?: boolean
  team?: boolean
  work?: boolean
}

export interface SetupInput {
  accounts?: readonly SetupAccount[] | null
  catalog?: SetupCatalog | null
  settings?: SetupSettings | null
  workCount?: number | null
  errors?: SetupErrors
}

export type DoctorCheck = { key: string; level: 'OK' | 'WARN' | 'FAIL'; detail: string }
export type WizardStatus = 'complete' | 'warning' | 'missing' | 'checking'
export type DoctorStatusKey = 'python' | 'providers' | 'clis' | 'panel' | 'node'

/** Map one doctor check to the setup wizard's display state. */
export function doctorStatus(checks: readonly DoctorCheck[] | null | undefined, key: string): WizardStatus {
  const found = checks?.find(check => check.key === key)
  if (!found) return 'checking'
  return found.level === 'OK' ? 'complete' : found.level === 'WARN' ? 'warning' : 'missing'
}

/** Group CLI checks while keeping missing results in the checking state. */
export function doctorWizardStatuses(checks: readonly DoctorCheck[] | null | undefined): Record<DoctorStatusKey, WizardStatus> {
  const cliChecks = checks?.filter(check => check.key.startsWith('cli:')) ?? []
  const cliStatus: WizardStatus = !cliChecks.length ? 'checking'
    : cliChecks.some(check => check.level === 'FAIL') ? 'missing'
      : cliChecks.some(check => check.level === 'WARN') ? 'warning' : 'complete'
  return {
    python: doctorStatus(checks, 'python'),
    providers: doctorStatus(checks, 'providers'),
    clis: cliStatus,
    panel: doctorStatus(checks, 'panel'),
    node: doctorStatus(checks, 'node'),
  }
}

export interface SetupStep {
  key: SetupStepKey
  title: string
  done: boolean
  unverified: boolean
  loginCommand: string | null
  loginAccount: string | null
  note: string | null
  error: boolean
}

export const SETUP_HIDDEN_KEY = 'pb.setupHidden'

export function isSetupHidden(storage?: Pick<Storage, 'getItem'> | null): boolean {
  try {
    const s = storage !== undefined ? storage : (typeof localStorage === 'undefined' ? null : localStorage)
    return s?.getItem(SETUP_HIDDEN_KEY) === '1'
  } catch {
    return false
  }
}

export function hideSetup(storage?: Pick<Storage, 'setItem'> | null): void {
  try {
    const s = storage !== undefined ? storage : (typeof localStorage === 'undefined' ? null : localStorage)
    s?.setItem(SETUP_HIDDEN_KEY, '1')
  } catch {
    /* localStorage unavailable: caller keeps hidden state in memory for this session */
  }
}

function entryBase(entry: SetupCatalogEntry): string {
  if (entry.identity) {
    const head = entry.identity.split(/[\/~]/)[0]
    if (head) return head
  }
  if (entry.provider) {
    return entry.account ? `${entry.provider}@${entry.account}` : entry.provider
  }
  return ''
}

function isClaudeId(baseOrProvider: string): boolean {
  return baseOrProvider === 'claude' || baseOrProvider.startsWith('claude@')
}

function isAccountAuthenticated(
  account: SetupAccount,
  catalog: SetupCatalog | null | undefined,
): boolean {
  if (!account.installed) return false
  const errors = catalog?.errors ?? {}
  if (errors[account.base] === 'unauthenticated') return false
  if (isClaudeId(account.provider) || isClaudeId(account.base)) {
    return errors.claude !== 'unauthenticated'
  }
  const entries = catalog?.entries ?? []
  return entries.some(e => entryBase(e) === account.base && Boolean(e.model && e.model.trim()))
}

export function setupSteps(input: SetupInput = {}): SetupStep[] {
  const accounts = input.accounts ?? []
  const catalog = input.catalog
  const errors = input.errors ?? {}

  // 1. Sağlayıcı CLI'si kurulu
  const cliError = Boolean(errors.cli)
  const cliDone = !cliError && accounts.some(a => a.installed)
  const cliStep: SetupStep = {
    key: 'cli',
    title: t('shell.setup.cliTitle'),
    done: cliDone,
    unverified: false,
    loginCommand: null,
    loginAccount: null,
    note: cliError
      ? t('shell.unreadable')
      : cliDone
        ? null
        : t('shell.setup.cliNote'),
    error: cliError,
  }

  // 2. Bir hesapta oturum açık
  const authError = Boolean(errors.auth)
  const catErrors = catalog?.errors ?? {}
  const catEntries = catalog?.entries ?? []

  const verifiedBaseHasSession = !authError && catEntries.some(e => {
    if (!e.model || !e.model.trim()) return false
    const base = entryBase(e)
    if (!base) return false
    if (catErrors[base] === 'unauthenticated') return false
    if (isClaudeId(base) || (e.provider && isClaudeId(e.provider))) {
      // Claude CLI does not list models; handled via the installed-claude rule below.
      return false
    }
    const matchingAcc = accounts.find(a => a.base === base)
    if (matchingAcc && !matchingAcc.installed) return false
    return true
  })

  const claudeInstalledAndNotUnauth = !authError && accounts.some(
    a =>
      a.installed &&
      (isClaudeId(a.provider) || isClaudeId(a.base)) &&
      catErrors[a.base] !== 'unauthenticated' &&
      catErrors.claude !== 'unauthenticated',
  )

  const authDone = verifiedBaseHasSession || claudeInstalledAndNotUnauth
  const authUnverified = !verifiedBaseHasSession && claudeInstalledAndNotUnauth

  let loginAccount: string | null = null
  let loginCommand: string | null = null
  if (!authDone && !authError) {
    const candidate =
      accounts.find(a => catErrors[a.base] === 'unauthenticated' && Boolean(a.login?.command)) ??
      accounts.find(a => a.installed && !isAccountAuthenticated(a, catalog) && Boolean(a.login?.command)) ??
      accounts.find(a => !isAccountAuthenticated(a, catalog) && Boolean(a.login?.command)) ??
      null
    if (candidate?.login?.command) {
      loginAccount = candidate.base
      loginCommand = candidate.login.command
    }
  }

  const authStep: SetupStep = {
    key: 'auth',
    title: t('shell.setup.authTitle'),
    done: authDone,
    unverified: authUnverified,
    loginCommand,
    loginAccount,
    note: authError ? t('shell.unreadable') : authUnverified ? t('shell.setup.unverified') : null,
    error: authError,
  }

  // 3. Varsayılan takım kayıtlı
  const rawSettings = input.settings
  const innerSettings =
    rawSettings && 'settings' in rawSettings && rawSettings.settings !== undefined
      ? rawSettings.settings
      : rawSettings
  const teamError = Boolean(errors.team || (rawSettings && 'error' in rawSettings && rawSettings.error))
  const teamObj = innerSettings?.team ?? null
  const hasTeam = Boolean(
    teamObj &&
      Array.isArray(teamObj.author) &&
      teamObj.author.length > 0 &&
      Array.isArray(teamObj.reviewer) &&
      teamObj.reviewer.length > 0,
  )
  const hasDigest = Boolean(innerSettings?.accepted_digest && innerSettings.accepted_digest.trim().length > 0)
  const teamDone = !teamError && hasTeam && hasDigest

  const teamStep: SetupStep = {
    key: 'team',
    title: t('shell.setup.teamTitle'),
    done: teamDone,
    unverified: false,
    loginCommand: null,
    loginAccount: null,
    note: teamError ? t('shell.unreadable') : teamDone ? null : t('shell.setup.teamNote'),
    error: teamError,
  }

  // 4. İlk çalışma
  const workError = Boolean(errors.work)
  const workDone = !workError && (input.workCount ?? 0) > 0
  const workStep: SetupStep = {
    key: 'work',
    title: t('shell.setup.workTitle'),
    done: workDone,
    unverified: false,
    loginCommand: null,
    loginAccount: null,
    note: workError ? t('shell.unreadable') : null,
    error: workError,
  }

  return [cliStep, authStep, teamStep, workStep]
}

export function allDone(inputOrSteps?: readonly SetupStep[] | SetupInput | null): boolean {
  if (!inputOrSteps) return false
  const steps = Array.isArray(inputOrSteps) ? inputOrSteps : setupSteps(inputOrSteps as SetupInput)
  return steps.length > 0 && steps.every(s => s.done)
}
