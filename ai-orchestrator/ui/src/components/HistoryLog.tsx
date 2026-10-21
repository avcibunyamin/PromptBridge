import type { HistoryEntry } from '../api'

const EVENT_LABELS: Record<string, string> = {
  capture: 'Yakalandı',
  enqueue: 'Kuyruğa alındı',
  human_approve: 'Onaylandı',
  human_reject: 'Reddedildi',
  route: 'Yönlendirildi',
  deliver_ok: 'İletildi',
  deliver_err: 'Teslim hatası',
  retry: 'Yeniden denendi',
  escalate: 'Yükseltildi',
  timeout: 'Zaman aşımı',
}

export function HistoryLog({ entries }: { entries: HistoryEntry[] }) {
  if (!entries.length) return <p className="muted">Geçmiş yok.</p>
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
