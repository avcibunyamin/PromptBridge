import type { HistoryEntry } from '../api'
import { t } from '../i18n'

const EVENT_LABELS: Record<string, string> = {
  capture: t('bridge.event.capture'),
  enqueue: t('bridge.event.enqueue'),
  human_approve: t('bridge.event.human_approve'),
  human_reject: t('bridge.event.human_reject'),
  route: t('bridge.event.route'),
  deliver_ok: t('bridge.event.deliver_ok'),
  deliver_err: t('bridge.event.deliver_err'),
  retry: t('bridge.event.retry'),
  escalate: t('bridge.event.escalate'),
  timeout: t('bridge.event.timeout'),
}

export function HistoryLog({ entries }: { entries: HistoryEntry[] }) {
  if (!entries.length) return <p className="muted">{t('bridge.history.empty')}</p>
  return (
    <ul className="history-list">
      {entries.map((h, i) => (
        <li key={i} className="history-entry">
          <span className="history-dot" />
          <div className="history-body">
            <span className="history-event">{EVENT_LABELS[h.event ?? ''] ?? h.event}</span>
            {h.from_state && (
              <span className="history-transition muted">
                {h.from_state} → {h.to_state}
              </span>
            )}
            <span className="history-meta muted">
              {h.actor ?? '—'} · {h.at?.slice(0, 19).replace('T', ' ')}
            </span>
          </div>
        </li>
      ))}
    </ul>
  )
}
