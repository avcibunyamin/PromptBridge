import { useEffect, useRef, useState } from 'react'
import { LoaderCircle, LogIn, RefreshCw } from 'lucide-react'
import { checkAccountLogin, openAccountLogin } from '../api'
import { formatAccountError } from '../accountRules'
import { t } from '../i18n'

type Phase = 'idle' | 'known' | 'opening' | 'waiting' | 'checking' | 'signed_in' | 'signed_out' | 'unknown' | 'error'
const POLL_MS = 6000, POLL_MAX = 50   // ~5 minutes of waiting for the person to finish in Terminal

/** K-060: sign an agy/codex account in from the panel. "Giriş yap" opens its sign-in in Terminal, then the
 *  panel asks every few seconds until the account's models come back; nothing is ever typed for the person. */
export function SignIn({ base, autoStart = false, compact = false, signedIn = null, onSignedIn }: { base: string; autoStart?: boolean; compact?: boolean; signedIn?: boolean | null; onSignedIn?: () => void }) {
  // K-064: start from what the server's model catalog already knows, so a signed-in account is not asked again.
  const [phase, setPhase] = useState<Phase>(signedIn ? 'known' : signedIn === false ? 'signed_out' : 'idle'), [error, setError] = useState('')
  const run = useRef(0), started = useRef(false)
  useEffect(() => () => { run.current++ }, [])   // unmount stops any poll
  async function poll(id: number) {
    let last: boolean | null = null   // only an explicit "signed out" from the provider ends as signed_out
    for (let i = 0; i < POLL_MAX && id === run.current; i++) {
      await new Promise(r => setTimeout(r, POLL_MS))
      if (id !== run.current) return
      try {
        const r = await checkAccountLogin(base)
        if (id !== run.current) return
        if (r.signed_in) { setPhase('signed_in'); onSignedIn?.(); return }
        last = r.signed_in === false ? false : null
      } catch { last = null /* keep waiting; the explicit check shows errors */ }
    }
    if (id === run.current) setPhase(last === false ? 'signed_out' : 'unknown')
  }
  async function start() {
    const id = ++run.current
    setError(''); setPhase('opening')
    try { await openAccountLogin(base) } catch (e) { if (id === run.current) { setError(formatAccountError(e)); setPhase('error') } return }
    if (id !== run.current) return
    setPhase('waiting'); void poll(id)
  }
  async function check() {
    const id = ++run.current
    setError(''); setPhase('checking')
    try {
      const r = await checkAccountLogin(base)
      if (id !== run.current) return
      setPhase(r.signed_in ? 'signed_in' : r.signed_in === false ? 'signed_out' : 'unknown')
      if (r.signed_in) onSignedIn?.()
    } catch (e) { if (id === run.current) { setError(formatAccountError(e)); setPhase('error') } }
  }
  useEffect(() => { if (autoStart && !started.current) { started.current = true; void start() } })   // once, on mount
  const busy = phase === 'opening' || phase === 'checking'
  const text = { idle: '', known: t('signin.known'), opening: t('signin.opening'), waiting: t('signin.waiting'), checking: t('signin.checking'),
    signed_in: t('signin.signedIn'), signed_out: t('signin.signedOut'), unknown: t('signin.unknown'), error }[phase]
  return <div className={'signin' + (compact ? ' compact' : '')}>
    {phase !== 'signed_in' && phase !== 'known' && <button type="button" className="btn small primary" disabled={busy} onClick={() => void start()}>
      {phase === 'opening' ? <LoaderCircle className="spin" aria-hidden /> : <LogIn aria-hidden />}
      {phase === 'waiting' ? t('signin.reopen') : t('signin.open')}</button>}
    <button type="button" className="btn small" disabled={busy} onClick={() => void check()}>
      {phase === 'checking' ? <LoaderCircle className="spin" aria-hidden /> : <RefreshCw aria-hidden />}{t('signin.check')}</button>
    {text && <span className={'signin-status ' + phase} role={phase === 'error' ? 'alert' : 'status'}>
      {phase === 'waiting' && <LoaderCircle className="spin" aria-hidden />}{text}</span>}
  </div>
}
