import { useState } from 'react'

interface Props {
  onApprove: (actor: string) => Promise<void>
  onReject: (actor: string, reason: string) => Promise<void>
  error: string | null
}

export function ActionBar({ onApprove, onReject, error }: Props) {
  const [actor, setActor] = useState('human')
  const [reason, setReason] = useState('')
  const [showReject, setShowReject] = useState(false)
  const [busy, setBusy] = useState(false)

  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    try { await fn() } finally { setBusy(false) }
  }

  return (
    <div className="action-bar">
      <div className="actor-row">
        <label className="actor-label">
          Aktör:
          <input
            className="actor-input"
            value={actor}
            onChange={e => setActor(e.target.value)}
            disabled={busy}
          />
        </label>
      </div>

      {error && <p className="action-error">⚠ {error}</p>}

      <div className="action-buttons">
        <button
          className="btn-approve"
          disabled={busy || !actor}
          onClick={() => run(() => onApprove(actor))}
        >
          {busy ? '…' : '✓ Onayla ve İlet'}
        </button>

        {!showReject
          ? <button className="btn-reject-ghost" onClick={() => setShowReject(true)}>
              ✕ Reddet
            </button>
          : <div className="reject-form">
              <textarea
                className="reject-reason"
                placeholder="Red sebebi (opsiyonel)…"
                value={reason}
                onChange={e => setReason(e.target.value)}
                rows={2}
                disabled={busy}
              />
              <div className="reject-actions">
                <button
                  className="btn-reject"
                  disabled={busy || !actor}
                  onClick={() => run(() => onReject(actor, reason))}
                >
                  Reddet
                </button>
                <button className="btn-ghost" onClick={() => setShowReject(false)}>
                  İptal
                </button>
              </div>
            </div>}
      </div>
    </div>
  )
}
