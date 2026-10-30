import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { t } from '../i18n'

// K-065: noImages keeps a model's report from making the browser fetch outside images (IP and timing leak).
export function ContentPreview({ content, noImages = false }: { content: string; noImages?: boolean }) {
  if (!content) return <p className="muted">{t('bridge.preview.empty')}</p>
  return (
    <div className="content-preview markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} disallowedElements={noImages ? ['img'] : undefined} unwrapDisallowed>{content}</ReactMarkdown>
    </div>
  )
}
