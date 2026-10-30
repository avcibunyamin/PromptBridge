import { useEffect, useState } from 'react'
import { Play } from 'lucide-react'
import { ContentPreview } from '../ContentPreview'
import { api } from './shared'
import { t } from '../../i18n'
import { finalText, type FinalStep } from './workStatus'

/** K-065: the answer of a finished run in the chat itself — the last author's report (what was done, how to run it),
 *  like the final message of a chat assistant. The full step-by-step record stays under "Details". */
export function FinalAnswer({ sessionId, onPreview }: { sessionId: string; onPreview?: () => void }) {
  const [text, setText] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    api<{ steps: FinalStep[] }>(`/api/sessions/${sessionId}`).then(d => { if (alive) setText(finalText(d.steps)) }).catch(() => { if (alive) setText('') })
    return () => { alive = false }
  }, [sessionId])
  if (!text) return null
  return <div className="final-answer">
    <div className="final-head"><strong>{t('work.final.title')}</strong><span className="spacer" />
      {onPreview && <button type="button" className="btn small primary" onClick={onPreview}><Play aria-hidden />{t('work.final.preview')}</button>}</div>
    <ContentPreview content={text} noImages />
  </div>
}
