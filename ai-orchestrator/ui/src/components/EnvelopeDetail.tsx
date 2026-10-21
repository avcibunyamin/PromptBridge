import { canApprove, mobile } from '../mobile'
import { useEffect, useState } from 'react'
import { getEnvelope, getContent, approveEnvelope, rejectEnvelope, type EnvelopeDetail as Detail } from '../api'
import { StatusBadge } from './StatusBadge'
import { ContentPreview } from './ContentPreview'
import { ContentEditor } from './ContentEditor'
import { ActionBar } from './ActionBar'
import { HistoryLog } from './HistoryLog'

type Tab = 'preview' | 'edit' | 'history'

interface Props {
  eid: string
  onAction: () => void
}

export function EnvelopeDetail({ eid, onAction }: Props) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [content, setContent] = useState<string>('')
  const [selectedVersionId, setSelectedVersionId] = useState<string | undefined>(undefined)
  const [tab, setTab] = useState<Tab>('preview')
  const [loading, setLoading] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = async (versionId = selectedVersionId) => {
    setLoading(true)
    setActionError(null)
    try {
      const [d, c] = await Promise.all([getEnvelope(eid), getContent(eid, versionId)])
      setDetail(d)
      setContent(c)
      if (!versionId) {
        setSelectedVersionId(d.envelope.approved_version_id ?? d.envelope.captured_version_id)
      }
    } catch (e) {
      setActionError(String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setSelectedVersionId(undefined)
    load(undefined)
  }, [eid])

  const handleApprove = async (actor: string) => {
    setActionError(null)
    try {
      await approveEnvelope(eid, actor, selectedVersionId)
      await load()
      onAction()
    } catch (e) {
      setActionError(String(e))
    }
  }

  const handleReject = async (actor: string, reason: string) => {
    setActionError(null)
    try {
      await rejectEnvelope(eid, actor, reason)
      await load()
      onAction()
    } catch (e) {
      setActionError(String(e))
    }
  }

  const handleEdited = async (versionId: string) => {
    setSelectedVersionId(versionId)
    await load(versionId)
    setTab('preview')
  }

  if (loading && !detail) return <p className="loading">Yükleniyor…</p>
  if (!detail) return actionError ? <p className="action-error" role="alert">{actionError}</p> : null

  const { envelope, versions, history } = detail
  const isPending = envelope.state === 'PENDING_APPROVAL'

  return (
    <div className="detail">
      <div className="detail-header">
        <div className="detail-title-row">
          <h2 className="detail-title">{envelope.title ?? '(başlıksız)'}</h2>
          <StatusBadge state={envelope.state} />
        </div>
        <div className="detail-meta">
          <span>{envelope.source_agent} → {envelope.target_agent}</span>
          {envelope.type && <span className="chip">{envelope.type}</span>}
          <span className="muted">{envelope.id}</span>
        </div>
        {envelope.delivered_path && (
          <div className="delivered-path muted">
            İletildi: {envelope.delivered_path}
          </div>
        )}
      </div>

      <div className="tab-bar">
        {((mobile.active ? ['preview', 'history'] : ['preview', 'edit', 'history']) as Tab[]).map(t => (
          <button
            key={t}
            className={`tab-btn ${tab === t ? 'active' : ''}`}
            onClick={() => setTab(t)}
          >
            {t === 'preview' ? 'Önizleme' : t === 'edit' ? 'Düzenle' : 'Geçmiş'}
          </button>
        ))}
        <span className="tab-meta muted">{versions.length} sürüm</span>
      </div>

      <div className="tab-content">
        {tab === 'preview' && <ContentPreview content={content} />}
        {tab === 'edit' && (
          <ContentEditor
            eid={eid}
            initialContent={content}
            disabled={!isPending}
            onSaved={handleEdited}
          />
        )}
        {tab === 'history' && <HistoryLog entries={history} />}
      </div>

      {isPending && canApprove() && (
        <ActionBar
          onApprove={handleApprove}
          onReject={handleReject}
          error={actionError}
        />
      )}
      {!isPending && actionError && (
        <p className="action-error">⚠ {actionError}</p>
      )}
    </div>
  )
}
