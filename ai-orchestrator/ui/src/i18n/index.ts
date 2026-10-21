/** Minimal two-language (tr/en) dictionary and lookup helpers for the UI. */
import { ApiError } from '../api.ts'
import * as bridge from './bridge.ts'
import * as session from './session.ts'
import * as settings from './settings.ts'
import * as shared from './shared.ts'
import * as shell from './shell.ts'
import * as tools from './tools.ts'
import * as work from './work.ts'

export const TR = {
  ...shell.tr,
  ...shared.tr,
  ...work.tr,
  ...session.tr,
  ...tools.tr,
  ...bridge.tr,
  ...settings.tr,
}

export type Key = keyof typeof TR

export const EN: Record<Key, string> = {
  ...shell.en,
  ...shared.en,
  ...work.en,
  ...session.en,
  ...tools.en,
  ...bridge.en,
  ...settings.en,
}

export type Lang = 'tr' | 'en'

function detectLang(): Lang {
  if (typeof window === 'undefined') return 'tr'
  try {
    const saved = localStorage.getItem('pb.lang')
    if (saved === 'tr' || saved === 'en') return saved
  } catch { /* ignore storage errors */ }
  return typeof navigator !== 'undefined' && navigator.language?.toLowerCase().startsWith('tr') ? 'tr' : 'en'
}

/** Active UI language ('tr' outside browser so Node tests stay deterministic). */
export const lang: Lang = detectLang()

export const locale = lang === 'en' ? 'en-US' : 'tr-TR'

/** Replace `{name}` placeholders in `raw` with values from `vars`. */
export function fill(raw: string, vars?: Record<string, string | number>): string {
  if (!vars) return raw
  return raw.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m))
}

export function t(key: Key, vars?: Record<string, string | number>): string {
  const raw = (lang === 'en' ? EN[key] : TR[key]) ?? TR[key] ?? key
  return fill(raw, vars)
}

export function setLang(l: Lang): void {
  try { localStorage.setItem('pb.lang', l) } catch { /* ignore storage errors */ }
  location.reload()
}

/** Translate an ApiError code (`err.<code>`) when present in TR, otherwise fall back to `String(e)`. */
export function say(e: unknown): string {
  if (e instanceof ApiError && e.code) {
    const k = 'err.' + e.code
    if (k in TR) return t(k as Key)
  }
  return String(e)
}

/** Translate an ApiError code prefixed with `prefix` (`<prefix>err.<code>`) when present in TR, otherwise fall back to `say(e)`. */
export function sayIn(prefix: string, e: unknown): string {
  if (e instanceof ApiError && e.code) {
    const k = prefix + 'err.' + e.code
    if (k in TR) return t(k as Key)
  }
  return say(e)
}
