import { useState, type KeyboardEvent } from 'react'
import { matchSlash, slashQuery, type Slash } from './slash'
import { t } from '../../i18n'

export type SlashCommand = Slash & { run: () => void }

/** Open the select behind a composer control (model / effort): focus it and, where the browser can, unfold it. */
export function openSelect(...labels: string[]) {
  // the first that exists: e.g. no effort select before a model is chosen, so the model select opens instead
  const el = labels.map(l => document.querySelector<HTMLSelectElement>(`.composer select[aria-label="${l}"]`)).find(Boolean)
  if (!el) return
  el.focus()
  try { (el as HTMLSelectElement & { showPicker?: () => void }).showPicker?.() } catch { /* focus is enough */ }
}
export const click = (selector: string) => () => document.querySelector<HTMLElement>(selector)?.click()

/** K-041: `/` at the start of the composer lists shortcuts; ↑/↓ choose, Enter or Tab run, Esc closes. A command
 *  only opens or fills something on this page — none sends a message or starts a run by itself. */
export function useSlash(text: string, setText: (t: string) => void, commands: SlashCommand[]) {
  const [index, setIndex] = useState(0), [closedFor, setClosedFor] = useState<string | null>(null)
  const query = slashQuery(text)
  const list = query === null || closedFor === text ? [] : matchSlash(commands, query)
  const at = Math.min(index, Math.max(0, list.length - 1))
  const run = (c: SlashCommand) => { setText(''); setIndex(0); c.run() }
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!list.length || e.nativeEvent.isComposing) return false   // an IME is composing: its keys are not ours
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); return true }   // never send a bare /command
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); setIndex((at + (e.key === 'ArrowDown' ? 1 : list.length - 1)) % list.length); return true }
    if ((e.key === 'Enter' && !e.shiftKey && !e.altKey) || (e.key === 'Tab' && !e.shiftKey && !e.altKey)) { e.preventDefault(); run(list[at]); return true }
    if (e.key === 'Escape') { e.preventDefault(); setClosedFor(text); return true }
    return false
  }
  const menu = list.length ? <ul className="slash-menu" role="listbox" aria-label={t('work.slash.label')}>
    {list.map((c, i) => <li key={c.name} role="option" aria-selected={i === at}>
      <button type="button" onMouseDown={e => e.preventDefault()} onClick={() => run(c)} onMouseEnter={() => setIndex(i)}>
        <span className="mono">/{c.name}</span><span className="muted small">{c.hint}</span></button></li>)}
  </ul> : null
  return { menu, onKeyDown }
}
