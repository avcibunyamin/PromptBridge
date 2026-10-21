import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { t } from '../i18n'

export function ContentPreview({ content }: { content: string }) {
  if (!content) return <p className="muted">{t('bridge.preview.empty')}</p>
  return (
    <div className="content-preview markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}
