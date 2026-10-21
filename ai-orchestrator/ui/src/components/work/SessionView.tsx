import { canInterrupt, mobile } from '../../mobile'
import { events } from '../../mobile'
import { useEffect, useState } from 'react'
import { ApiError } from '../../api'
import { t } from '../../i18n'
import { LoopView, type Loop, type Turn } from './LoopView'
import { Pill, Provider, Chains, RECOVERED, active, labels, contextLabels, reasonLabels, resumeReasons, promptFormats, promptRejections, api, Modal, when, type Policy } from './shared'

type Session = {id:string; status:string; workspace_path:string; created_at:string}
type Step = {id:string; step_index:number; agent_name:string; role?:string; status:string; result_text:string|null; terminal_reason:string|null}
type Attempt = {id:string;step_id:string;attempt_no:number;provider:string;profile:string;status:string;error_code:string|null;continuation_no:number;provider_session_id:string|null;terminal_reason?:string|null;handover_reason?:string|null}
type ProjectInstructions = {status:'absent'|'included'|'rejected';reason:string|null;path:string|null;source_path:string|null;line:number|null;combined_digest:string|null;files:{path:string;sha256:string;bytes:number;depth:number}[]}
// F3-04: the session's newest automatic-retry plan (resets_at stays null until verified, K-012).
type WaitPlan = {id:string;status:'PENDING'|'FIRED'|'CANCELLED'|'EXPIRED';next_retry_at:string;deadline:string;attempts_so_far:number;max_attempts:number;closed_reason:string|null;resets_at:string|null}
type Detail = {session:Session; steps:Step[]; attempts:Attempt[]; policy:Policy['policy']|null; project_instructions?:ProjectInstructions|null; agents_instructions?:ProjectInstructions|null; loop?:Loop; turns?:Turn[]; wait_plan?:WaitPlan|null}
const WAIT_EVENTS = ['wait_planned','wait_fired','wait_expired','wait_cancelled','wait_rescheduled','wait_workspace_changed']
const waitEnds:Record<string,string> = {max_retries_exceeded:t('session.waitEnd.maxRetries'),retry_deadline_exceeded:t('session.waitEnd.deadline'),
  human_retry:t('session.waitEnd.humanRetry'),workspace_changed:t('session.waitEnd.workspaceChanged'),cancelled_by_user:t('session.waitEnd.cancelled'),
  recovery_required:t('session.waitEnd.recoveryRequired'),no_failed_step:t('session.waitEnd.noFailedStep'),
  session_not_waiting:t('session.waitEnd.notWaiting'),session_not_retryable:t('session.waitEnd.notRetryable')}
// F3-02 loop events: the loop card re-reads the backend detail when any of these arrive.
const LOOP_EVENTS = ['turn_started','review_parsed','review_invalid','loop_stopped','pause_requested','paused','resumed','resume_rejected','pause_superseded']
type Handoff = {id:string;step_id:string;from:string;to:string;reason:string}
type Continuation = {id:string;step_id:string;to_attempt_id:string;step_continuations_used:number;max_context_continuations:number;provider:string}
type Owner = {attempt_id:string;pid:number;pgid:number;leader_alive:boolean;group_alive:boolean}
type Recovery = {status:string;lock_retained:boolean;owners:Owner[]}
type FeedItem = {id:string;attempt_id?:string;kind:string;text:string;exitCode?:number;output?:string}
type Resume = {decision:'RESUME'|'FRESH';reason:string}
type PromptMeta = {format?:string;history?:{truncated?:boolean}}
type Rejected = {reason?:string;bytes?:number;limit?:number;mandatory_bytes?:number}
// TB-02: why a human retry was refused (backend RetryNotAllowed codes).
const retryRefusals:Record<string,string> = {session_not_retryable:t('session.retryRefusal.notRetryable'),
  workspace_busy:t('session.retryRefusal.workspaceBusy'),
  recovery_required:t('session.retryRefusal.recoveryRequired'),no_failed_step:t('session.retryRefusal.noFailedStep'),
  context_budget_exhausted:t('session.retryRefusal.contextBudgetExhausted')}
const instructionReasons:Record<string,string> = {import_invalid_path:t('session.instructionReason.invalidPath'),import_absolute:t('session.instructionReason.absolute'),import_home:t('session.instructionReason.home'),
  import_outside_workspace:t('session.instructionReason.outsideWorkspace'),symlink:t('session.instructionReason.symlink'),not_regular_file:t('session.instructionReason.notRegularFile'),
  import_not_found:t('session.instructionReason.notFound'),unreadable:t('session.instructionReason.unreadable'),not_utf8:t('session.instructionReason.notUtf8'),depth_exceeded:t('session.instructionReason.depthExceeded'),
  import_cycle:t('session.instructionReason.cycle'),too_large:t('session.instructionReason.tooLarge')}

/** One execution (session) from real backend data: steps, attempts, handoffs, continuations, live feed, recovery.
 *  `embedded` renders it under a work message (no page title); controls and rules are the same as standalone. */
export function SessionView({sessionId,embedded=false,onChange,status:hint}:{sessionId:string;embedded?:boolean;onChange?:()=>void;status?:string}){
  const [detail,setDetail]=useState<Detail|null>(null), [error,setError]=useState(''), [busy,setBusy]=useState(false)
  const [handoffs,setHandoffs]=useState<Handoff[]>([])
  const [continuations,setContinuations]=useState<Continuation[]>([])
  const [startedAttempts,setStartedAttempts]=useState<Set<string>>(new Set())
  const [feed,setFeed]=useState<FeedItem[]>([])
  const [resume,setResume]=useState<Resume|null>(null), [rejected,setRejected]=useState<Rejected|null>(null)
  const [noChanges,setNoChanges]=useState<Set<string>>(new Set())   // TB-14: local author steps that changed no file
  const [promptMeta,setPromptMeta]=useState<Record<string,PromptMeta>>({})
  const [connected,setConnected]=useState(false), [stopping,setStopping]=useState(false)
  const [recovery,setRecovery]=useState<Recovery|null>(null)
  const [recoverResult,setRecoverResult]=useState<{status:string;blocking?:number[];detail?:string}|null>(null)
  const [confirmRecover,setConfirmRecover]=useState(false), [nonce,setNonce]=useState(0)
  const [changedPaths,setChangedPaths]=useState<string[]>([])
  useEffect(()=>{
    setDetail(null);setFeed([]);setHandoffs([]);setContinuations([]);setStartedAttempts(new Set());setConnected(false);setStopping(false)
    setResume(null);setRejected(null);setPromptMeta({});setChangedPaths([]);setNoChanges(new Set())
    setRecovery(null);setRecoverResult(null);setConfirmRecover(false)
    let mounted=true, revision=0
    const update=async()=>{const current=++revision;try{const d=await api<Detail>(`/api/sessions/${sessionId}`);if(mounted&&current===revision)setDetail(d)}catch(e){if(mounted)setError(String(e))}}
    void update()
    // Session events carry their own seq ids; the browser resumes with Last-Event-ID and `seen` drops repeats.
    const es=events(`/api/sessions/${sessionId}/events`), seen=new Set<string>()
    es.onopen=()=>{if(mounted)setConnected(true)};es.onerror=()=>{if(mounted)setConnected(false)}
    es.addEventListener('work_event',e=>{
      const m=e as MessageEvent;if(!mounted||seen.has(m.lastEventId))return;seen.add(m.lastEventId)
      const d=JSON.parse(m.data)
      if(d.kind==='handoff')setHandoffs(h=>[...h,{...d.payload,id:m.lastEventId,step_id:d.step_id}])
      if(d.kind==='context_continuation')setContinuations(h=>[...h,{...d.payload,id:m.lastEventId,step_id:d.step_id}])
      if(d.kind==='started'&&d.payload.provider_session_id)setStartedAttempts(ids=>new Set([...ids,d.payload.attempt_id]))
      if(d.kind==='resume_decision')setResume({decision:d.payload.decision,reason:d.payload.reason})
      if(d.kind==='prompt_rejected')setRejected(d.payload)
      if(d.kind==='no_changes'&&d.step_id)setNoChanges(n=>new Set([...n,d.step_id]))
      if(LOOP_EVENTS.includes(d.kind)||WAIT_EVENTS.includes(d.kind))void update()
      if(d.kind==='wait_workspace_changed')setChangedPaths(d.payload.paths||[])
      if(d.kind==='attempt_started'&&d.payload.prompt_meta)setPromptMeta(p=>({...p,[d.payload.attempt_id]:d.payload.prompt_meta}))
      if(d.kind==='output_delta' || d.kind==='command_result' || d.kind==='handoff')setFeed(items=>{
        const last=items[items.length-1], p=d.payload
        if(d.kind==='output_delta' && last?.kind==='output_delta' && last.attempt_id===p.attempt_id)
          return [...items.slice(0,-1),{...last,text:(last.text+p.text).slice(-100000)}]
        const text=d.kind==='output_delta'?p.text:d.kind==='command_result'?p.command:`${p.from} → ${p.to} · ${p.reason}`
        return [...items,{id:m.lastEventId,attempt_id:p.attempt_id,kind:d.kind,text,exitCode:p.exit_code,output:p.output}].slice(-200)
      })
    })
    let lastStatus=''  // work_status repeats every poll; the parent only hears real transitions
    es.addEventListener('work_status',e=>{if(!mounted)return;void update();const st=JSON.parse((e as MessageEvent).data).status
      if(st!==lastStatus){if(lastStatus)onChange?.();lastStatus=st}
      if(!active(st)){es.close();setConnected(false)}})
    return()=>{mounted=false;es.close()}
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[sessionId,nonce])
  // F3-04: the scheduler can start a waiting run; the parent sees the new status, the closed stream does not.
  useEffect(()=>{if(hint&&detail&&hint!==detail.session.status&&!active(detail.session.status))setNonce(n=>n+1)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[hint])
  useEffect(()=>{
    if(detail?.session.status==='RECOVERY_REQUIRED')
      api<Recovery>(`/api/sessions/${sessionId}/recovery`).then(setRecovery).catch(e=>setError(String(e)))
  },[sessionId,detail?.session.status])
  async function cancel(){setBusy(true);setError('');try{await api(`/api/sessions/${sessionId}/cancel`,{method:'POST'});setStopping(true)}catch(e){setError(String(e))}finally{setBusy(false)}}
  async function recover(){setBusy(true);setError('');try{
    const r=await api<{status:string;blocking?:number[];detail?:string}>(`/api/sessions/${sessionId}/recover`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})})
    setConfirmRecover(false);setRecoverResult(r)
    const d=await api<Detail>(`/api/sessions/${sessionId}`);setDetail(d);onChange?.()
    if(d.session.status==='RECOVERY_REQUIRED')setRecovery(await api<Recovery>(`/api/sessions/${sessionId}/recovery`))
  }catch(e){setError(String(e))}finally{setBusy(false)}}
  async function retry(){setBusy(true);setError('');try{
    await api(`/api/sessions/${sessionId}/retry`,{method:'POST'});setNonce(n=>n+1);onChange?.()
  }catch(e){setError(e instanceof ApiError&&e.code&&retryRefusals[e.code]?retryRefusals[e.code]:String(e))}finally{setBusy(false)}}
  async function cancelWait(){setBusy(true);setError('');try{
    await api(`/api/sessions/${sessionId}/wait`,{method:'DELETE'});setDetail(await api<Detail>(`/api/sessions/${sessionId}`));onChange?.()
  }catch(e){setError(e instanceof ApiError&&e.code==='wait_already_fired'?t('session.wait.alreadyFired'):String(e))}finally{setBusy(false)}}
  if(!detail)return error?<div className="error-banner" role="alert">{error}</div>:<p className="loading">{t('session.loading')}</p>
  const status=detail.session.status, plan=detail.wait_plan
  const badInstructions=[detail.project_instructions,detail.agents_instructions].find(i=>i?.status==='rejected')
  const turnOf=(id:string)=>detail.turns?.find(t=>t.author_step_id===id||t.reviewer_step_id===id)?.turn_no
  const stepTitle=(s:Step)=>{const role=s.role==='author'?t('session.role.author'):s.role==='reviewer'?t('session.role.reviewer'):s.agent_name,turn=turnOf(s.id)
    return turn?t('session.step.turnTitle',{turn,role}):t('session.step.indexTitle',{index:s.step_index+1,role})}
  return <div className={embedded?'run-view embedded':'run-view'}>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    <div className="run-head">
      <div>{!embedded&&<h1>{detail.session.workspace_path.split('/').pop()||t('session.defaultTitle')}</h1>}
        <div className="sub"><Pill status={status} recovered={detail.steps.some(s=>s.terminal_reason===RECOVERED)}/>{connected&&<span className="live" role="status"><span className="dot"/>{t('session.live')}</span>}{embedded&&<span className="muted mono small">{detail.session.id}</span>}</div>
        {!embedded&&<p className="path">{detail.session.workspace_path}</p>}</div><span className="spacer"/>
      {canInterrupt()&&active(status)&&<button className="btn danger" disabled={busy||stopping} onClick={()=>void cancel()}>{stopping?t('session.stopping'):t('session.stop')}</button>}
    </div>
    {resume&&<p className={'resume-note '+(resume.decision==='RESUME'?'resume':'fresh')} role="status">{resume.decision==='RESUME'
      ?t('session.resume.continued')
      :t('session.resume.fresh',{reason:resumeReasons[resume.reason]||resume.reason})}</p>}
    {detail.loop&&<LoopView sessionId={sessionId} status={status} loop={detail.loop} turns={detail.turns||[]}
      onChanged={()=>{void api<Detail>(`/api/sessions/${sessionId}`).then(setDetail).catch(e=>setError(String(e)));onChange?.()}}
      onResumed={()=>{setNonce(n=>n+1);onChange?.()}}/>}
    {rejected&&<p className="notice" role="alert"><strong>{t('session.rejected.title')}</strong> {promptRejections[rejected.reason||'']||rejected.reason}{rejected.bytes&&rejected.limit?t('session.rejected.bytes',{bytes:rejected.bytes,limit:rejected.limit}):''}</p>}
    {badInstructions&&<p className="notice" role="alert"><strong>{t('session.badInstructions.title')}</strong>{' '}
      {instructionReasons[badInstructions.reason||'']||badInstructions.reason}
      {badInstructions.path?` · ${badInstructions.path}`:''}
      {badInstructions.source_path?t('session.badInstructions.fromSource',{source:`${badInstructions.source_path}${badInstructions.line?`:${badInstructions.line}`:''}`}):''}.
      {' '}{t('session.badInstructions.fixHint',{file:badInstructions===detail.agents_instructions?'AGENTS.md':'CLAUDE.md'})}</p>}
    {status==='RECOVERY_REQUIRED'&&<section className="card"><div className="card-h"><h2>{t('session.recovery.title')}</h2><span className="spacer"/>{recovery?.lock_retained&&<span className="pill warn">{t('session.recovery.lockRetained')}</span>}</div><div className="card-b">
      <p className="notice" role="status">{t('session.recovery.notice')}</p>
      {recovery?.owners.map(o=><div key={o.attempt_id} className="attempt"><span className="no mono">PID {o.pid}</span><span className="muted mono">PGID {o.pgid}</span><span className="spacer"/><span className={'pill '+(o.group_alive?'warn':'ok')}>{o.group_alive?t('session.recovery.processAlive'):t('session.recovery.processGone')}</span></div>)}
      {recovery&&!recovery.owners.length&&<p className="chain-note">{t('session.recovery.noOwners')}</p>}
      {!!recoverResult?.blocking?.length&&<p className="notice" role="alert">{t('session.recovery.blocking',{pids:recoverResult.blocking.join(', ')})}</p>}
      {!mobile.active&&<button className="btn danger" disabled={busy} onClick={()=>setConfirmRecover(true)}>{t('session.recovery.action')}</button>}
    </div></section>}
    {confirmRecover&&<Modal label={t('session.recovery.confirmTitle')} busy={busy} onClose={()=>setConfirmRecover(false)}>
      <h3>{t('session.recovery.confirmTitle')}</h3>
      <p><strong>{t('session.recovery.confirmLead')}</strong> {t('session.recovery.confirmBody')}</p>
      {!!recovery?.owners.length&&<p className="chain-note mono">{t('session.recovery.affectedPids',{pids:recovery.owners.map(o=>o.pid).join(', ')})}</p>}
      <div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setConfirmRecover(false)}>{t('session.recovery.cancel')}</button><button className="btn danger" disabled={busy} onClick={()=>void recover()}>{busy?t('session.recovery.recovering'):t('session.recovery.confirmAction')}</button></div>
    </Modal>}
    {status==='WAITING_CONTEXT'&&<div className="notice" role="status">{t('session.waitingContext.notice')} {detail.steps.map(s=>contextLabels[s.terminal_reason||'']).filter(Boolean).join(' ')} {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':t('session.retry')}</button>} {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>{t('session.stop')}</button>}</div>}
    {status==='WAITING_CAPACITY'&&plan?.status==='PENDING'?<div className="notice" role="status"><strong>{t('session.wait.plannedTitle')}</strong>{' '}
      {t('session.wait.plannedDetail',{used:plan.attempts_so_far,max:plan.max_attempts,next:when(plan.next_retry_at),deadline:when(plan.deadline)})}{' '}
      {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':t('session.wait.tryNow')}</button>}{' '}
      {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancelWait()}>{t('session.wait.cancel')}</button>}</div>
    :['WAITING_CAPACITY','WAITING_AUTH'].includes(status)&&<div className="notice" role="status">{labels[status]}. {t('session.wait.manualNotice')}{plan&&plan.closed_reason&&status==='WAITING_CAPACITY'?t('session.wait.endedInline',{reason:waitEnds[plan.closed_reason]||plan.closed_reason}):''} {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':t('session.retry')}</button>} {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>{t('session.stop')}</button>}</div>}
    {status==='WAITING_HUMAN'&&<div className="notice" role="alert"><strong>{t('session.waitingHuman.title')}</strong>{' '}
      {changedPaths.length?t('session.waitingHuman.changed',{paths:changedPaths.slice(0,5).join(', '),more:changedPaths.length>5?t('session.waitingHuman.moreFiles',{count:changedPaths.length-5}):''}):''}{t('session.waitingHuman.body')}{' '}
      {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':t('session.retry')}</button>}{' '}
      {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>{t('session.stop')}</button>}</div>}
    {status==='FAILED'&&plan?.status==='EXPIRED'&&<p className="notice" role="status">{t('session.wait.expiredNotice',{reason:waitEnds[plan.closed_reason||'']||plan.closed_reason||'',used:plan.attempts_so_far,max:plan.max_attempts})}</p>}
    {status==='WAITING_APPROVAL'&&<div className="notice" role="status">{labels.WAITING_APPROVAL}. {t('session.waitingApproval.notice')} {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>{t('session.stop')}</button>}</div>}
    {!embedded&&detail.policy&&<section className="card"><div className="card-h"><h2>{t('session.policy.title')}</h2><span className="spacer"/><span className="accepted">{t('session.policy.frozen')}</span></div><div className="card-b"><Chains policy={detail.policy}/></div></section>}
    <section className="card"><div className="card-h"><h2>{t('session.workflow.title')}</h2><span className="spacer"/><span className="chain-note" role="status">{t('session.workflow.progress',{done:detail.steps.filter(s=>s.status==='COMPLETED').length,total:detail.steps.length})}</span></div>
    <div className="card-b timeline">{!detail.steps.length&&<p className="muted">{t('session.workflow.empty')}</p>}{detail.steps.map(s=><section className={`step ${s.status==='COMPLETED'?'done':s.status==='RUNNING'?'active':''}`} key={s.id}>
      <span className="node" aria-hidden="true">{s.status==='COMPLETED'?'✓':s.step_index+1}</span>
      <div className="step-head"><h3 className="t">{stepTitle(s)}</h3>{s.role&&<span className={'chip '+s.role}>{s.role==='author'?t('session.profile.write'):t('session.profile.readOnly')}</span>}<span className="spacer"/><Pill status={s.status} recovered={s.terminal_reason===RECOVERED}/></div>
      <div className="attempts">{detail.attempts.filter(a=>a.step_id===s.id).map(a=>{const meta=promptMeta[a.id];return <div className="attempt" key={a.id}><span className="no">{t('session.attempt.no',{no:a.attempt_no})}</span><Provider name={a.provider}/><span className="muted">{a.profile==='WRITE'?t('session.profile.write'):t('session.profile.readOnly')}</span>
        {meta?.format&&<span className="muted">· {promptFormats[meta.format]||meta.format}{meta.history?.truncated?t('session.attempt.historyTruncated'):''}</span>}
        <span className="spacer"/><Pill status={a.status} recovered={a.terminal_reason===RECOVERED}/>{a.error_code&&<code className="attempt-code">{a.error_code}</code>}</div>})}</div>
      {noChanges.has(s.id)&&<div className="handover"><strong>{t('session.noChanges.title')}</strong><span>{t('session.noChanges.body')}</span></div>}
      {handoffs.filter(h=>h.step_id===s.id).map(h=><div key={h.id} className="handover"><strong>{t('session.handoff.title')}</strong><span>{h.from} → {h.to}</span><span>· {h.reason==='QUOTA_EXCEEDED'?t('session.handoff.quotaExceeded'):h.reason}</span></div>)}
      <p className="chain-note">{t('session.continuation.summary',{used:detail.attempts.filter(a=>a.step_id===s.id&&a.continuation_no>0&&a.handover_reason!=='command_denied_followup').length,max:detail.policy?.max_context_continuations??0})}</p>
      {continuations.filter(c=>c.step_id===s.id).map(c=><div key={c.id} className="handover"><strong>{startedAttempts.has(c.to_attempt_id)?t('session.continuation.started'):t('session.continuation.prepared')}</strong><span>{c.provider} · {c.step_continuations_used}/{c.max_context_continuations}</span></div>)}
      {s.result_text&&<details className="step-out" open={s.status==='FAILED'}><summary>{t('session.step.result')}</summary><pre>{s.result_text}</pre></details>}
      {s.terminal_reason&&!['completed','cancelled_by_user'].includes(s.terminal_reason)&&<p className="step-out" role="status">{reasonLabels[s.terminal_reason]||s.terminal_reason}</p>}
    </section>)}</div></section>
    <section className="card"><div className="card-h"><h2>{t('session.feed.title')}</h2><span className="spacer"/><span className="chain-note">{connected?t('session.feed.live'):t('session.feed.recorded')}</span></div>
    <div className="feed" role="log" aria-live="polite">{!feed.length&&<p className="card-b muted">{t('session.feed.empty')}</p>}{feed.map(e=>{
      const provider=detail.attempts.find(a=>a.id===e.attempt_id)?.provider
      return <article className={'evt '+(e.kind==='handoff'?'feed-handover':'')} key={e.id}>
        <span className={'av '+(e.kind==='handoff'?'sys':provider||'sys')} aria-hidden="true">{e.kind==='handoff'?'↗':provider?.slice(0,1).toUpperCase()||'·'}</span>
        <div className="event-body"><div className="who">{e.kind==='handoff'?t('session.feed.handoff'):provider||t('session.feed.provider')}<span>{e.kind==='command_result'?t('session.feed.commandResult'):e.kind==='output_delta'?t('session.feed.output'):''}</span></div>
        {e.kind==='command_result'?<><pre className="tool" tabIndex={0}>{e.text}</pre><span className="chain-note">{t('session.feed.exitCode',{code:e.exitCode??t('session.feed.unknownExit')})}</span>{e.output&&<details><summary>{t('session.feed.commandOutput')}</summary><pre className="tool" tabIndex={0}>{e.output}</pre></details>}</>:<p className="msg">{e.text}</p>}</div>
      </article>
    })}</div><p className="feed-note">{t('session.feed.note')}</p></section>
  </div>
}
