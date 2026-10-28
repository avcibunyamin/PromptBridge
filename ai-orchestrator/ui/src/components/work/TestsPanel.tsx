import { useEffect, useState } from 'react'
import { FlaskConical, LoaderCircle, ShieldCheck } from 'lucide-react'
import { testsApprove, testsRevoke, testsStatus, type Check, type TestRun, type TestsStatus } from '../../api'
import { t } from '../../i18n'
import { Modal } from './shared'

const CHECKS: [Check, string, string][] = [
  ['tests', t('tools.tests.check.tests'), t('tools.tests.check.testsHint')],
  ['lint', t('tools.tests.check.lint'), t('tools.tests.check.lintHint')],
]

/** TB-27/28 (K-035), Mac only: approve the test and the lint command Conductor may run (sandboxed) before each review. */
export function TestsPanel({ workId, onClose }: { workId: string; onClose: () => void }) {
  const [pending, setPending] = useState(0), busy = pending > 0   // two sections load at once: count, not a flag
  const onBusy = (b: boolean) => setPending(n => n + (b ? 1 : -1))
  return <Modal label={t('tools.tests.modalLabel')} busy={busy} onClose={onClose}>
    <h3><FlaskConical aria-hidden /> {t('tools.tests.title')}</h3>
    <p className="muted small">{t('tools.tests.introBefore')}<strong>{t('tools.tests.introSandbox')}</strong>{t('tools.tests.introMiddle')}<strong>{t('tools.tests.introApproval')}</strong>{t('tools.tests.introAfter')}</p>
    {CHECKS.map(([check, title, hint]) => <CheckSection key={check} workId={workId} check={check} title={title} hint={hint} onBusy={onBusy} />)}
    <div className="modal-actions"><button className="btn" disabled={busy} onClick={onClose}>{t('tools.common.close')}</button></div>
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
    {!st ? <p className="muted"><LoaderCircle className="spin" aria-hidden /> {t('tools.common.reading')}</p>
      : !s ? <p className="muted small">{t('tools.tests.notFound', { hint })}</p>
      : <>
        <p>{t('tools.tests.commandLabel')} <span className="mono">{s.label}</span>{s.cwd && s.cwd !== '.' && <span className="muted small">{t('tools.tests.inFolder', { cwd: s.cwd })}</span>}{s.script_path && <span className="muted small">{t('tools.tests.definedIn')}<span className="mono">{s.script_path}</span></span>}</p>
        {s.scripts && s.scripts.length > 0 && <div className="small">
          <p className="muted">{t('tools.tests.scripts')}</p>
          <ul>{s.scripts.map(([name, cmd]) => <li key={name}><span className="mono">{name}</span>: <code>{cmd}</code></li>)}</ul>
        </div>}
        {st.approved ? <p className="pill ok"><ShieldCheck aria-hidden /> {t('tools.tests.approved')}</p>
          : st.stale ? <p className="notice">{t('tools.tests.stale')}</p> : null}
        <div className="modal-actions" style={{ justifyContent: 'flex-start' }}>
          {!st.approved && <button className="btn primary" disabled={busy} onClick={() => void step(() => testsApprove(workId, s.command_digest, check))}>{t('tools.tests.approve')}</button>}
          {(st.approved || st.stale) && <button className="btn" disabled={busy} onClick={() => void step(() => testsRevoke(workId, check))}>{t('tools.tests.revoke')}</button>}
        </div>
      </>}
  </section>
}

const words: Record<Check, Record<TestRun['status'], [string, string]>> = {
  tests: { passed: ['ok', t('tools.tests.run.tests.passed')], failed: ['bad', t('tools.tests.run.tests.failed')], timeout: ['warn', t('tools.tests.run.tests.timeout')], not_run: ['idle', t('tools.tests.run.tests.notRun')] },
  lint: { passed: ['ok', t('tools.tests.run.lint.passed')], failed: ['bad', t('tools.tests.run.lint.failed')], timeout: ['warn', t('tools.tests.run.lint.timeout')], not_run: ['idle', t('tools.tests.run.lint.notRun')] },
}
const reasons: Record<string, string> = {
  approval_stale: t('tools.tests.reason.approvalStale'), no_command: t('tools.tests.reason.noCommand'), deps_missing: t('tools.tests.reason.depsMissing'),
}

/** The run card's one-line summary; the output itself is in the run's details (and went to the reviewer). */
export function TestPill({ run, check = 'tests' }: { run: TestRun; check?: Check }) {
  const [tone, label] = words[check][run.status] ?? ['idle', run.status]
  const extra = run.status === 'not_run' ? (reasons[run.reason ?? ''] ?? run.reason) : run.duration_s != null ? t('tools.tests.seconds', { n: run.duration_s }) : null
  return <span className={`pill ${tone}`} title={run.command ?? undefined}><span className="d" aria-hidden="true" />{label}{extra ? ` · ${extra}` : ''}</span>
}
