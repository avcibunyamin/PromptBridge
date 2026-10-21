import { t } from '../i18n'

const STATE_LABELS: Record<string, string> = {
  CAPTURED: t('shared.badge.captured'),
  PENDING_APPROVAL: t('shared.badge.pending_approval'),
  APPROVED: t('shared.badge.approved'),
  DELIVERING: t('shared.badge.delivering'),
  DELIVERED: t('shared.badge.delivered'),
  REJECTED: t('shared.badge.rejected'),
  FAILED: t('shared.badge.failed'),
  EXPIRED: t('shared.badge.expired'),
}

export function StatusBadge({ state }: { state: string }) {
  return (
    <span className={`badge badge-${state.toLowerCase()}`}>
      {STATE_LABELS[state] ?? state}
    </span>
  )
}
