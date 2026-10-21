/** Pure setup-guide rules for the Mac overview page (node-testable, no DOM or fetch). */

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
    title: "Sağlayıcı CLI'si kurulu",
    done: cliDone,
    unverified: false,
    loginCommand: null,
    loginAccount: null,
    note: cliError
      ? 'okunamadı'
      : cliDone
        ? null
        : "claude, codex, agy ya da ollama'dan birini kurun (docs/KULLANIM-KILAVUZU.md)",
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
    title: 'Bir hesapta oturum açık',
    done: authDone,
    unverified: authUnverified,
    loginCommand,
    loginAccount,
    note: authError ? 'okunamadı' : authUnverified ? 'Doğrulanmadı' : null,
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
    title: 'Varsayılan takım kayıtlı',
    done: teamDone,
    unverified: false,
    loginCommand: null,
    loginAccount: null,
    note: teamError ? 'okunamadı' : teamDone ? null : "Ayarlar'da takımı seç ve kaydet",
    error: teamError,
  }

  // 4. İlk çalışma
  const workError = Boolean(errors.work)
  const workDone = !workError && (input.workCount ?? 0) > 0
  const workStep: SetupStep = {
    key: 'work',
    title: 'İlk çalışma',
    done: workDone,
    unverified: false,
    loginCommand: null,
    loginAccount: null,
    note: workError ? 'okunamadı' : null,
    error: workError,
  }

  return [cliStep, authStep, teamStep, workStep]
}

export function allDone(inputOrSteps?: readonly SetupStep[] | SetupInput | null): boolean {
  if (!inputOrSteps) return false
  const steps = Array.isArray(inputOrSteps) ? inputOrSteps : setupSteps(inputOrSteps as SetupInput)
  return steps.length > 0 && steps.every(s => s.done)
}
