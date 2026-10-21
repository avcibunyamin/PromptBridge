const STATE_LABELS: Record<string, string> = {
  CAPTURED: 'Yakalandı',
  PENDING_APPROVAL: 'Onay Bekliyor',
  APPROVED: 'Onaylandı',
  DELIVERING: 'İletiliyor',
  DELIVERED: 'İletildi',
  REJECTED: 'Reddedildi',
  FAILED: 'Başarısız',
  EXPIRED: 'Süresi Doldu',
}

export function StatusBadge({ state }: { state: string }) {
  return (
    <span className={`badge badge-${state.toLowerCase()}`}>
      {STATE_LABELS[state] ?? state}
    </span>
  )
}
