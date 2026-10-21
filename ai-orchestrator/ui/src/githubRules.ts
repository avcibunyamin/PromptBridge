/** TB-21: pure rules of the GitHub screens (node-testable, no fetch). */
export type GhStatus = { installed: boolean; logged_in: boolean; account: string | null }
export type GhRepo = { nameWithOwner: string; isPrivate: boolean; isArchived: boolean; updatedAt: string }
export type GhFile = { path: string; status: string; secret: boolean; workflow: boolean }
export type GhState = { git: boolean; reason?: string; repo?: string | null; branch?: string | null; head?: string | null;
  unsafe_config?: string[]; files?: GhFile[] }
export type PushView = { repo: string; branch: string; base: string; head: string; url: string; files: number;
  commits: { sha: string; subject: string }[]; workflow_files: string[]; digest: string }
export type PrView = { repo: string; base: string; head: string; sha: string; title: string; body: string; digest: string }

/** Search by name (case-insensitive); archived repositories last. */
export function filterRepos(items: GhRepo[], query: string): GhRepo[] {
  const q = query.trim().toLowerCase()
  return items.filter(r => r.nameWithOwner.toLowerCase().includes(q))
    .sort((a, b) => Number(a.isArchived) - Number(b.isArchived))
}

/** K3: files that may hold secrets are listed but never preselected. */
export const defaultSelection = (files: GhFile[]): string[] => files.filter(f => !f.secret).map(f => f.path)

const STATUS: Record<string, string> = { '??': 'yeni', A: 'eklendi', M: 'değişti', D: 'silindi', R: 'yeniden adlandırıldı', T: 'tür değişti' }
/** `git status --porcelain` code → a word (the first letter that says something). */
export const statusLabel = (code: string): string => STATUS[code] ?? STATUS[code.replace(/[ .]/g, '')[0] ?? ''] ?? code

export const needsSecretAck = (files: GhFile[], selected: string[]): boolean =>
  files.some(f => f.secret && selected.includes(f.path))
