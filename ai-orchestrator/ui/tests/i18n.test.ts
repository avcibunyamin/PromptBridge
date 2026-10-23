// UI i18n dictionary invariants, Node default language, and placeholder filling.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { EN, TR, fill, lang, locale, say, sayIn, t, withDetail, type Key } from '../src/i18n/index.ts'
import { ApiError } from '../src/api.ts'
import * as bridge from '../src/i18n/bridge.ts'
import * as errors from '../src/i18n/errors.ts'
import * as session from '../src/i18n/session.ts'
import * as settings from '../src/i18n/settings.ts'
import * as shared from '../src/i18n/shared.ts'
import * as shell from '../src/i18n/shell.ts'
import * as tools from '../src/i18n/tools.ts'
import * as work from '../src/i18n/work.ts'

const placeholders = (s: string) => [...s.matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort()

test('TR and EN key sets, placeholder sets, and non-empty values match', () => {
  const trKeys = Object.keys(TR).sort() as Key[]
  const enKeys = Object.keys(EN).sort() as Key[]
  assert.deepEqual(trKeys, enKeys)
  for (const k of trKeys) {
    assert.ok(TR[k].trim().length > 0, `empty TR value for ${k}`)
    assert.ok(EN[k].trim().length > 0, `empty EN value for ${k}`)
    assert.deepEqual(placeholders(TR[k]), placeholders(EN[k]), `placeholder mismatch for ${k}`)
  }
})

test('Node defaults to Turkish and translates settings.lang', () => {
  assert.equal(lang, 'tr')
  assert.equal(locale, 'tr-TR')
  assert.equal(t('settings.lang'), 'Dil')
})

test('fill replaces placeholders and say falls back to String(e) for unknown codes', () => {
  assert.equal(fill('{n} öğe, {ad}', { n: 3, ad: 'dosya' }), '3 öğe, dosya')
  assert.equal(fill('Sabit metin'), 'Sabit metin')
  assert.equal(fill('{eksik} kaldı', {}), '{eksik} kaldı')
  assert.equal(say(new ApiError('Bad request', 400, 'UNKNOWN_CODE')), 'Error: Bad request')
  assert.equal(sayIn('settings.', new ApiError('Conflict', 409, 'revision_conflict')), t('settings.err.revision_conflict'))
  assert.equal(sayIn('settings.', new ApiError('Not found', 404, 'not_found')), t('err.not_found'))
  assert.equal(sayIn('settings.', new ApiError('Bad request', 400, 'UNKNOWN_CODE')), 'Error: Bad request')
})

test('withDetail appends message detail only to translations ending with a colon', () => {
  const e = new ApiError('Ana klasörde kaydedilmemiş değişiklik var: a.py, b.py', 409, 'X')
  assert.equal(withDetail('Kaydedilmemiş değişiklik var:', e.message), 'Kaydedilmemiş değişiklik var: a.py, b.py')
  assert.equal(withDetail('Kaydedilmemiş değişiklik var', e.message), 'Kaydedilmemiş değişiklik var')
  assert.equal(withDetail('Değişiklik var:', 'Detay yok'), 'Değişiklik var:')
  assert.equal(withDetail('Değişiklik var:', 'Boş detay:   '), 'Değişiklik var:')
  assert.equal(say(new ApiError('Nothing: x', 409, 'nothing')), t('err.nothing'))
})

test('no translation key is shared across the 8 domain modules', () => {
  const modules: [string, Record<string, string>][] = [
    ['shell', shell.tr],
    ['shared', shared.tr],
    ['work', work.tr],
    ['session', session.tr],
    ['tools', tools.tr],
    ['bridge', bridge.tr],
    ['settings', settings.tr],
    ['errors', errors.tr],
  ]
  const seen = new Map<string, string>()
  const duplicates: string[] = []
  for (const [name, dict] of modules) {
    for (const k of Object.keys(dict)) {
      const prev = seen.get(k)
      if (prev) {
        duplicates.push(`${k} (in ${prev} and ${name})`)
      } else {
        seen.set(k, name)
      }
    }
  }
  assert.deepEqual(duplicates, [], `Duplicate translation keys found across domain modules:\n${duplicates.join('\n')}`)
})

test('no unexpected Turkish characters in ui/src outside src/i18n/', () => {
  const srcDir = fileURLToPath(new URL('../src', import.meta.url))
  const i18nDir = path.join(srcDir, 'i18n')

  function collectTsFiles(dir: string): string[] {
    const files: string[] = []
    const entries = fs.readdirSync(dir, { withFileTypes: true })
    for (const entry of entries) {
      const fullPath = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        if (fullPath === i18nDir) continue
        files.push(...collectTsFiles(fullPath))
      } else if (entry.isFile() && (entry.name.endsWith('.ts') || entry.name.endsWith('.tsx'))) {
        files.push(fullPath)
      }
    }
    return files
  }

  const allowlist = [
    ".replace(/[ıIİ]/g, 'i')",
    ".replace(/ö/g, 'o').replace(/ü/g, 'u').replace(/ş/g, 's').replace(/ç/g, 'c').replace(/ğ/g, 'g')",
    '/en çok|max|allowed|fazla/',
  ]

  const turkishCharRegex = /[çğıöşüÇĞİÖŞÜ]/
  const offending: string[] = []

  const files = collectTsFiles(srcDir).sort()
  for (const file of files) {
    const content = fs.readFileSync(file, 'utf-8')
    // Blok yorumları (/* … */) satır numaralarını koruyacak şekilde boşlukla değiştir
    const noBlock = content.replace(/\/\*[\s\S]*?\*\//g, match => match.replace(/[^\r\n]/g, ' '))
    const lines = noBlock.split(/\r?\n/)

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i]
      // Satır yorumlarını (// sonrası; ama :// içeren satırlarda yalnız ' //' ile başlayan kısmı) çıkar
      let code = line
      if (code.trimStart().startsWith('//')) {
        code = ''
      } else if (code.includes('://')) {
        const idx = code.indexOf(' //')
        if (idx !== -1) code = code.slice(0, idx)
      } else {
        const idx = code.indexOf('//')
        if (idx !== -1) code = code.slice(0, idx)
      }

      if (turkishCharRegex.test(code)) {
        if (!allowlist.some(allowed => code.includes(allowed))) {
          const rel = path.relative(path.join(srcDir, '..'), file)
          offending.push(`${rel}:${i + 1}`)
        }
      }
    }
  }

  assert.deepEqual(offending, [], `Found Turkish characters outside src/i18n/ not on allowlist:\n${offending.join('\n')}`)
})

test('English values contain no Turkish letters (except the language name itself)', () => {
  const allowed = new Set<Key>(['settings.lang.tr'])
  const bad = (Object.keys(EN) as Key[]).filter(k => !allowed.has(k) && /[çğıöşüÇĞİÖŞÜ]/.test(EN[k]))
  assert.deepEqual(bad, [], `untranslated English values: ${bad.join(', ')}`)
})
