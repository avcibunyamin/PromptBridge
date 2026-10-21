import type { Envelope } from '../api'
import { StatusBadge } from './StatusBadge'

interface Props {
  envelopes: Envelope[]
  selectedId: string | null
  onSelect: (eid: string) => void
}

export function EnvelopeList({ envelopes, selectedId, onSelect }: Props) {
  if (envelopes.length === 0) {
    return <p className="empty-list">Zarf yok.</p>
  }
  return (
    <ul className="envelope-list">
      {envelopes.map(env => (
        <li
          key={env.id}
        >
          <button type="button" aria-pressed={selectedId === env.id}
          className={`envelope-card ${selectedId === env.id ? 'selected' : ''}`}
          onClick={() => onSelect(env.id)}
        >
          <div className="card-top">
            <StatusBadge state={env.state} />
            <span className="card-date">{env.created_at.slice(0, 16).replace('T', ' ')}</span>
          </div>
          <div className="card-title">{env.title ?? '(başlıksız)'}</div>
          <div className="card-meta">{env.source_agent} → {env.target_agent}</div>
          </button>
        </li>
      ))}
    </ul>
  )
}
