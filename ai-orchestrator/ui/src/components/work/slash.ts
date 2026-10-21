/** K-041: Claude-like `/` shortcuts in the composer. Pure (no React), so `npm test` covers the matching. */
export type Slash = { name: string; hint: string }

/** The query while the whole text is one `/word` (no space, no new line); otherwise null (the menu is closed). */
export const slashQuery = (text: string): string | null => {
  const m = /^\/([\p{L}\p{N}-]*)$/u.exec(text)
  return m ? fold(m[1]) : null
}

/** Turkish letters typed or not (`/önizleme`, `/takım`, `/TAKIM`) find the same ASCII command name. */
export const fold = (s: string) => s.replace(/[ıIİ]/g, 'i').toLocaleLowerCase('tr-TR')
  .replace(/ö/g, 'o').replace(/ü/g, 'u').replace(/ş/g, 's').replace(/ç/g, 'c').replace(/ğ/g, 'g')

/** Names starting with the query first, then names containing it; list order otherwise. */
export function matchSlash<T extends Slash>(commands: T[], query: string): T[] {
  const q = fold(query), starts = commands.filter(c => fold(c.name).startsWith(q))
  return [...starts, ...commands.filter(c => !starts.includes(c) && fold(c.name).includes(q))]
}
