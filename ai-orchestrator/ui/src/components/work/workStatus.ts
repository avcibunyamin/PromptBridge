/** K-062: one plain answer to “is anything running here, and if not, how did it end?” Pure, node-testable. */
export type WorkState = 'running' | 'queued' | 'waiting' | 'message_queued' | 'done' | 'failed' | 'stopped' | 'empty'
export type WorkStatusInput = { latestStatus: string | null; pendingMessages: number; reason?: string | null }

const WAITING = ['WAITING_AUTH', 'WAITING_CAPACITY', 'WAITING_CONTEXT', 'WAITING_HUMAN', 'WAITING_APPROVAL', 'PAUSED', 'RECOVERY_REQUIRED']

export function workState({ latestStatus, pendingMessages }: WorkStatusInput): WorkState {
  if (latestStatus === 'RUNNING') return 'running'
  if (latestStatus === 'QUEUED') return 'queued'
  if (latestStatus && WAITING.includes(latestStatus)) return 'waiting'
  if (pendingMessages > 0) return 'message_queued'
  return latestStatus === 'SUCCEEDED' ? 'done' : latestStatus === 'FAILED' ? 'failed' : latestStatus === 'CANCELLED' ? 'stopped' : 'empty'
}

/** Nothing runs and the last run did not finish: one click sends “continue where you left off”. */
export const canResume = (state: WorkState) => state === 'failed' || state === 'stopped'

/** The server sends a code (timeout | cancelled | auth | error); anything else counts as an error. */
export type ReasonKind = 'timeout' | 'cancelled' | 'auth' | 'error'
export function reasonKind(reason: string | null | undefined): ReasonKind | null {
  if (!reason) return null
  return reason === 'timeout' || reason === 'cancelled' || reason === 'auth' ? reason : 'error'
}

export type FinalStep = { step_index: number; role?: string; status: string; result_text: string | null }

/** K-065: a finished run's answer is its last completed author report (the reviewer's verdict stays in the details). */
export function finalText(steps: FinalStep[]): string {
  const done = steps.filter(s => s.role === 'author' && s.status === 'COMPLETED' && s.result_text?.trim())
  return done.length ? done.reduce((a, b) => (b.step_index > a.step_index ? b : a)).result_text!.trim() : ''
}
