import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function ContentPreview({ content }: { content: string }) {
  if (!content) return <p className="muted">İçerik yok.</p>
  return (
    <div className="content-preview markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}
