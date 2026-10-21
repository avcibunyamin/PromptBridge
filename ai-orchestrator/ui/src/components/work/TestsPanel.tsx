import { useEffect, useState } from 'react'
import { FlaskConical, LoaderCircle, ShieldCheck } from 'lucide-react'
import { testsApprove, testsRevoke, testsStatus, type Check, type TestRun, type TestsStatus } from '../../api'
import { Modal } from './shared'

const CHECKS: [Check, string, string][] = [
  ['tests', 'Testler', 'package.json test betiği, pytest ya da tests/'],
  ['lint', 'Lint / tip denetimi', 'package.json lint ya da typecheck betiği, tsconfig.json (tsc), .venv içinde ruff / flake8 / mypy'],
]

/** TB-27/28 (K-035), Mac only: approve the test and the lint command Conductor may run (sandboxed) before each review. */
export function TestsPanel({ workId, onClose }: { workId: string; onClose: () => void }) {
  const [pending, setPending] = useState(0), busy = pending > 0   // two sections load at once: count, not a flag
  const onBusy = (b: boolean) => setPending(n => n + (b ? 1 : -1))
  return <Modal label="Testler ve lint" busy={busy} onClose={onClose}>
    <h3><FlaskConical aria-hidden /> İncelemeden önce denetimler</h3>
    <p className="muted small">Onaylarsan PromptBridge bu komutları her inceleme adımından önce <strong>korumalı alanda</strong> çalıştırır
      (ağ yok, yalnız proje klasörüne yazar, ev klasörüne yazamaz, gizli dosyalar kapalı, en çok 5 dakika) ve sonucu inceleyiciye verir.
      İnceleyici komut çalıştırmaz. <strong>Onay, yazarın yazdığı kodun da bu korumalı alanda çalışmasına izin verir</strong>;
      güvenlik sınırı korumalı alandır. Komutu ya da hangi aracın çalışacağını belirleyen dosyalar değişirse onay düşer.</p>
    {CHECKS.map(([check, title, hint]) => <CheckSection key={check} workId={workId} check={check} title={title} hint={hint} onBusy={onBusy} />)}
    <div className="modal-actions"><button className="btn" disabled={busy} onClick={onClose}>Kapat</button></div>
  </Modal>
}

function CheckSection({ workId, check, title, hint, onBusy }: { workId: string; check: Check; title: string; hint: string; onBusy: (b: boolean) => void }) {
  const [st, setSt] = useState<TestsStatus | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  async function step(fn: () => Promise<TestsStatus>) {
    setBusy(true); onBusy(true); setError('')
    try { setSt(await fn()) } catch (e) { setError(String(e instanceof Error ? e.message : e)) } finally { setBusy(false); onBusy(false) }
  }
  useEffect(() => { void step(() => testsStatus(workId, check)) }, [workId, check])   // eslint-disable-line react-hooks/exhaustive-deps
  const s = st?.suggestion
  return <section aria-label={title} className="check-section"><h4>{title}</h4>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {!st ? <p className="muted"><LoaderCircle className="spin" aria-hidden /> Okunuyor…</p>
      : !s ? <p className="muted small">Bu klasörde bulunamadı ({hint}).</p>
      : <>
        <p>Komut: <span className="mono">{s.label}</span>{s.cwd && s.cwd !== '.' && <span className="muted small"> · {s.cwd}/ klasöründe</span>}{s.script_path && <span className="muted small"> · tanım: <span className="mono">{s.script_path}</span></span>}</p>
        {st.approved ? <p className="pill ok"><ShieldCheck aria-hidden /> Onaylı</p>
          : st.stale ? <p className="notice">Önceki onay geçersiz: komut ya da tanım dosyası değişti.</p> : null}
        <div className="modal-actions" style={{ justifyContent: 'flex-start' }}>
          {!st.approved && <button className="btn primary" disabled={busy} onClick={() => void step(() => testsApprove(workId, s.command_digest, check))}>Bu komutu onayla</button>}
          {(st.approved || st.stale) && <button className="btn" disabled={busy} onClick={() => void step(() => testsRevoke(workId, check))}>Onayı kaldır</button>}
        </div>
      </>}
  </section>
}

const words: Record<Check, Record<TestRun['status'], [string, string]>> = {
  tests: { passed: ['ok', 'Testler geçti'], failed: ['bad', 'Testler kaldı'], timeout: ['warn', 'Testler zaman aşımına uğradı'], not_run: ['idle', 'Testler çalışmadı'] },
  lint: { passed: ['ok', 'Lint temiz'], failed: ['bad', 'Lint hatası'], timeout: ['warn', 'Lint zaman aşımına uğradı'], not_run: ['idle', 'Lint çalışmadı'] },
}
const reasons: Record<string, string> = {
  approval_stale: 'onay geçersiz', no_command: 'komut yok', deps_missing: 'bağımlılıklar kurulu değil',
}

/** The run card's one-line summary; the output itself is in the run's details (and went to the reviewer). */
export function TestPill({ run, check = 'tests' }: { run: TestRun; check?: Check }) {
  const [tone, label] = words[check][run.status] ?? ['idle', run.status]
  const extra = run.status === 'not_run' ? (reasons[run.reason ?? ''] ?? run.reason) : run.duration_s != null ? `${run.duration_s} sn` : null
  return <span className={`pill ${tone}`} title={run.command ?? undefined}><span className="d" aria-hidden="true" />{label}{extra ? ` · ${extra}` : ''}</span>
}
