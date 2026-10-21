// TB-20: title from the first message (K1) and the run card summary (K2).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { deriveTitle, needsAttention, opensByItself, runLine } from '../src/components/work/flows.ts'

test('title: first non-empty line, collapsed, at most 60 characters', () => {
  assert.equal(deriveTitle('\n\n  Hesap   makinesine çıkarma ekle \nve testleri'), 'Hesap makinesine çıkarma ekle')
  assert.equal(deriveTitle('   \n  '), 'Yeni çalışma')
  const long = 'Bu klasördeki uygulamaya kullanıcı girişi, şifre sıfırlama ve e-posta doğrulama akışlarını ekle'
  const t = deriveTitle(long)
  assert.ok(t.length <= 60, t)
  assert.ok(t.endsWith('…'))
  assert.ok(!t.includes('  '))
  assert.equal(deriveTitle('x'.repeat(80)).length, 60)          // no space to cut at: hard cut
})

test('run card summary names the primary author and reviewer', () => {
  const label = (id: string) => ({ 'claude/claude-sonnet-5-5~low': 'Claude Sonnet 5.5', 'agy@h2/gemini-3.1-pro-high': 'Gemini 3.1 Pro High' } as Record<string, string>)[id] ?? id
  assert.equal(runLine({ author: ['claude/claude-sonnet-5-5~low', 'codex'], reviewer: ['agy@h2/gemini-3.1-pro-high'] }, label),
    'Yazar Claude Sonnet 5.5 · İnceleme Gemini 3.1 Pro High')
  assert.equal(runLine({ author: ['claude'], reviewer: [] }, label), 'Yazar claude')
})

test('K2: a run that needs a person opens by itself', () => {
  for (const s of ['WAITING_CAPACITY', 'WAITING_AUTH', 'WAITING_HUMAN', 'WAITING_CONTEXT', 'RECOVERY_REQUIRED', 'FAILED', 'PAUSED'])
    assert.equal(needsAttention(s), true, s)
  for (const s of ['QUEUED', 'RUNNING', 'SUCCEEDED', 'CANCELLED'])
    assert.equal(needsAttention(s), false, s)
})

test('K2: only the latest run opens by itself (Codex TB-20 P3)', () => {
  assert.equal(opensByItself({ id: 's2', status: 'FAILED' }, 's2'), true)
  assert.equal(opensByItself({ id: 's1', status: 'FAILED' }, 's2'), false)
  assert.equal(opensByItself({ id: 's2', status: 'SUCCEEDED' }, 's2'), false)
  assert.equal(opensByItself(null, 's2'), false)
})
