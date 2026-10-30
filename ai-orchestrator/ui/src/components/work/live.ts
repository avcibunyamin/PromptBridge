/** K-059: one line about what a running execution is doing now, folded from its session events. Pure. */
export type LiveState = { role: string; provider: string; since: number; text: string; last: string; kind: 'output' | 'command' | 'activity' | null }
export type LiveEvent = { kind: string; at?: string; payload: Record<string, unknown> }

const MAX = 140

/** The newest non-empty line of streamed output, ending-trimmed to MAX characters. */
export function lastLine(text: string): string {
  const lines = text.split('\n').map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean)
  const line = lines[lines.length - 1] ?? ''
  return line.length > MAX ? '…' + line.slice(-(MAX - 1)) : line
}

export function foldLive(state: LiveState | null, e: LiveEvent): LiveState | null {
  const p = e.payload
  if (e.kind === 'attempt_started')
    return { role: String(p.role ?? ''), provider: String(p.provider ?? ''), since: Date.parse(e.at ?? '') || Date.now(), text: '', last: '', kind: null }
  if (!state) return state
  if (e.kind === 'output_delta') {
    const text = (state.text + String(p.text ?? '')).slice(-4000)
    const last = lastLine(text)
    return { ...state, text, last: last || state.last, kind: last ? 'output' : state.kind }
  }
  if (e.kind === 'command_result') return { ...state, last: String(p.command ?? '').slice(0, MAX), kind: 'command' }
  if (e.kind === 'activity')   // K-061: agy's tool steps (tool + command/path)
    return { ...state, last: [p.tool, p.detail].filter(Boolean).join(' · ').slice(0, MAX), kind: 'activity' }
  return state
}

/** `agy@hesap1/gemini-4-argon-high` → `agy@hesap1 · gemini-4-argon-high`. */
export const providerLabel = (provider: string) => provider.split('/').join(' · ')

export function elapsedMinutes(since: number, now: number): number {
  return Math.max(0, Math.floor((now - since) / 60000))
}
