import { useState } from 'react'
import { editEnvelope } from '../api'

interface Props {
  eid: string
  initialContent: string
  disabled: boolean
  onSaved: (versionId: string) => void
}

export function ContentEditor({ eid, initialContent, disabled, onSaved }: Props) {
  const [text, setText] = useState(initialContent)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      const result = await editEnvelope(eid, text)
      onSaved(result.version_id)
    } catch (e) {
      setError(String(e))
    } finally {
      setSaving(false)
    }
  }

  if (disabled) {
    return (
      <div className="editor-disabled">
        <p className="muted">Düzenleme yalnızca <strong>Onay Bekliyor</strong> durumunda mümkündür.</p>
      </div>
    )
  }

  return (
    <div className="editor">
      <textarea
        className="editor-textarea"
        value={text}
        onChange={e => setText(e.target.value)}
        rows={20}
        spellCheck={false}
      />
      {error && <p className="action-error">⚠ {error}</p>}
      <div className="editor-footer">
        <button
          className="btn-primary"
          onClick={handleSave}
          disabled={saving || text === initialContent}
        >
          {saving ? 'Kaydediliyor…' : 'Düzenlenmiş sürümü kaydet'}
        </button>
        <span className="muted">Kayıt sonrası bu sürüm onaylanacak.</span>
      </div>
    </div>
  )
}
