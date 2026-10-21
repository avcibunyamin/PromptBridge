import { mobile } from '../../mobile'
import { useState } from 'react'
import { ApiError } from '../../api'
import { t } from '../../i18n'
import { api, active, duration, outcomeLabels, verdictLabels, invalidCodes, Modal } from './shared'

export type Loop = {max_turns:number;max_active_ms:number;current_turn:number;phase:'author'|'review'|'done';pause_requested_at:string|null;
  pause_revision:number;outcome:string|null;outcome_reason:string|null;last_author_unreviewed:number;used_ms:number;time_estimated:boolean}
export type Finding = {id:string;severity:'high'|'medium'|'low';summary:string;evidence:string[]}
export type Turn = {turn_no:number;author_step_id:string;reviewer_step_id:string;verdict:string|null;invalid_code:string|null;ended_reason:string|null;findings:Finding[]|null}
type Change = {path:string;certainty:string}
type Changed = {digest:string;changes:Change[];comparable:boolean;unknown:string[]}

const severity:Record<string,string> = {high:t('session.loop.severity.high'),medium:t('session.loop.severity.medium'),low:t('session.loop.severity.low')}

/** F3-02 turn loop: counters, outcome, pause/resume (digest-bound), and the per-turn verdicts.
 *  Everything shown comes from GET /api/sessions/{id} (loop, turns). */
export function LoopView({sessionId,status,loop,turns,onChanged,onResumed}:{sessionId:string;status:string;loop:Loop;turns:Turn[];onChanged:()=>void;onResumed:()=>void}){
  const [busy,setBusy]=useState(false), [error,setError]=useState(''), [changed,setChanged]=useState<Changed|null>(null)
  const running=active(status)&&loop.phase!=='done'
  async function pause(){setBusy(true);setError('');try{await api(`/api/sessions/${sessionId}/pause`,{method:'POST'});onChanged()}
    catch(e){setError(String(e))}finally{setBusy(false)}}
  async function resume(digest?:string){setBusy(true);setError('');try{
    await api(`/api/sessions/${sessionId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm:true,pause_revision:loop.pause_revision,...(digest?{diff_digest:digest}:{})})})
    setChanged(null);onResumed()
  }catch(e){
    const body=e instanceof ApiError?e.body:undefined
    if(e instanceof ApiError&&e.code==='workspace_changed'&&body){
      // Show exactly what changed; the next confirmation is bound to this digest.
      setChanged({digest:String(body.diff_digest),changes:(body.changes as Change[])||[],comparable:!!body.comparable,unknown:(body.unknown as string[])||[]})
    }else{setChanged(null);setError(e instanceof ApiError&&e.code==='workspace_busy'?t('session.loop.workspaceBusy'):
      e instanceof ApiError&&e.code==='stale_pause'?t('session.loop.stalePause'):String(e));onChanged()}
  }finally{setBusy(false)}}

  return <section className="card loop"><div className="card-h"><h2>{t('session.loop.title')}</h2><span className="spacer"/>
      <span className="chain-note">{t('session.loop.summary',{turn:loop.current_turn,maxTurns:loop.max_turns,used:duration(loop.used_ms),maxTime:duration(loop.max_active_ms)})}{loop.time_estimated?t('session.loop.estimatedSuffix'):''}</span></div>
    <div className="card-b loop-body">
      {error&&<div className="error-banner" role="alert">{error}</div>}
      {loop.outcome&&<p className={'loop-outcome '+(loop.outcome==='verified'?'ok':'warn')} role="status"><strong>{outcomeLabels[loop.outcome]||loop.outcome}</strong>
        {loop.outcome==='review_invalid'&&loop.outcome_reason?` — ${invalidCodes[loop.outcome_reason]||loop.outcome_reason}`:''}</p>}
      {!!loop.last_author_unreviewed&&<p className="notice" role="status">{t('session.loop.unreviewedNotice')}</p>}
      {running&&loop.pause_requested_at&&<p className="info-note" role="status">{t('session.loop.pauseRequested')}</p>}
      {!mobile.active&&running&&!loop.pause_requested_at&&<button className="btn" disabled={busy} onClick={()=>void pause()}>{t('session.loop.pauseAfterStep')}</button>}
      {status==='PAUSED'&&<div className="paused">
        <p>{t('session.loop.pausedNotice',{turn:loop.current_turn,step:loop.phase==='author'?t('session.loop.phase.author'):t('session.loop.phase.reviewer')})}</p>
        {!mobile.active&&<button className="btn primary" disabled={busy} onClick={()=>void resume()}>{busy?'…':t('session.loop.resume')}</button>}
      </div>}
      {changed&&<Modal label={t('session.loop.changed.modalLabel')} busy={busy} onClose={()=>setChanged(null)}>
        <h3>{t('session.loop.changed.title')}</h3>
        {!changed.comparable&&<p className="notice">{t('session.loop.changed.notComparable')}</p>}
        {!!changed.changes.length&&<ul className="change-list">{changed.changes.map(c=><li key={c.path} className="mono">{c.path}{c.certainty!=='hash'?t('session.loop.changed.uncertain'):''}</li>)}</ul>}
        {!!changed.unknown.length&&<p className="chain-note">{t('session.loop.changed.unscanned',{items:changed.unknown.join(', ')})}</p>}
        <p>{t('session.loop.changed.body')}</p>
        <div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setChanged(null)}>{t('session.loop.changed.cancel')}</button>
          <button className="btn primary" disabled={busy} onClick={()=>void resume(changed.digest)}>{busy?'…':t('session.loop.changed.confirm')}</button></div>
      </Modal>}
      <ol className="turns">{turns.map(turn=><li key={turn.turn_no} className="turn">
        <div className="turn-head"><strong>{t('session.loop.turnTitle',{turn:turn.turn_no})}</strong><span className="spacer"/>
          {turn.verdict?<span className={'pill '+(turn.verdict==='pass'?'ok':turn.verdict==='changes_requested'?'idle':'warn')}><span className="d" aria-hidden="true"/>{verdictLabels[turn.verdict]||turn.verdict}</span>
            :<span className="pill idle"><span className="d" aria-hidden="true"/>{t('session.loop.awaitingReview')}</span>}</div>
        {turn.verdict==='invalid'&&<p className="chain-note">{t('session.loop.invalidReview',{reason:invalidCodes[turn.invalid_code||'']||turn.invalid_code||''})}</p>}
        {!!turn.findings?.length&&<><p className="chain-note">{t('session.loop.reviewerStatement')}</p>
          <ul className="findings">{turn.findings.map(f=><li key={f.id}><span className={'sev '+f.severity}>{severity[f.severity]}</span><span className="mono muted">{f.id}</span> <span className="finding-text">{f.summary}</span>
            {!!f.evidence.length&&<div className="evidence mono">{f.evidence.join(' · ')}</div>}</li>)}</ul></>}
      </li>)}</ol>
    </div></section>
}
