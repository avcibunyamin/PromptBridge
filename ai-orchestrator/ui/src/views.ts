/** K-040: the shell's pages and their addresses, so a reload and back/forward land on the same page. Pure. */
export type View = 'overview' | 'work' | 'bridge' | 'settings'
export const HASHES: Record<Exclude<View, 'work'>, string> = { overview: '#/overview', bridge: '#/approvals', settings: '#/settings' }

export const viewOf = (hash: string): View | null => hash === HASHES.overview ? 'overview' : hash === HASHES.bridge ? 'bridge'
  : hash === HASHES.settings ? 'settings' : hash === '#/' || /^#\/(works|sessions|new)/.test(hash) ? 'work' : null   // `#/`: Çalışmalar

/** A page's own address wins; otherwise the Mac opens on the overview (like ZeroCost) and a phone on its work list. */
export const initialView = (hash: string, phone: boolean): View => viewOf(hash) ?? (phone ? 'work' : 'overview')

/** Back to the address the panel was opened with (none): its landing page again. */
export const viewAfterHashChange = (hash: string, phone: boolean): View | null =>
  viewOf(hash) ?? (hash === '' || hash === '#' ? initialView(hash, phone) : null)
