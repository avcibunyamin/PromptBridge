import { canCommand, canInterrupt, mobile } from '../../mobile'
import { events } from '../../mobile'
import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowUp, LoaderCircle, Pencil, Settings2 } from 'lucide-react'
import { getWork, listAllMessages, addMessage, cancelMessage, patchWork, deleteWork, continueWork, ApiError, type Work, type WorkMessage } from '../../api'
import { Pill, labels, missingProvider, newId, api, loopLine, outcomeLabels, Modal, teamOf, WaitOptions, type Policy, type Team, type WaitChoice } from './shared'
import { TeamPicker, type TeamSelection } from './TeamPicker'
import { click, openSelect, useSlash, type SlashCommand } from './SlashMenu'
import { SessionView } from './SessionView'
import { PreviewPanel } from './PreviewPanel'
import { GitHubPanel } from './GitHubPanel'
import { TestPill, TestsPanel } from './TestsPanel'
import { IsolationPanel } from './IsolationPanel'
import { pendingMessage, answered, rejectedFirst, type PendingMessage } from './pending'
import { chainNeedsAcceptance, discardMessage, opensByItself, runLine } from './flows'
import { baseOf, modelLabel, splitId } from './team'
import { acceptedDigest, deleteWorkErrorMessage, initialWorkTeam, initialWorkWait, loadSettings, putWorkTeam, putWorkWait, saveSettings, workSavedTeam, type SettingsView } from '../../settings'
import { RECIPES } from '../../recipes'
import { locale, say, t } from '../../i18n'

const blockers:Record<string,string> = {
  run_active:t('work.blocker.run_active'),
  run_waiting:t('work.blocker.run_waiting'),
  recovery_required:t('work.blocker.recovery_required'),
  paused:t('work.blocker.paused'),
}
const codes:Record<string,string> = {
  revision_conflict:t('err.revision_conflict'),
  work_active:t('err.work_active'),
  work_archived:t('err.work_archived'),
  message_linked:t('err.message_linked'),
  stale_session:t('err.stale_session'),
  stale_outcome:t('err.stale_outcome'),
  outcome_missing:t('err.outcome_missing'),
  already_continued:t('err.already_continued'),
  paused:t('err.paused'),
  conflict:t('err.conflict'),
  team_invalid:t('err.team_invalid'),
  recipe_unknown:t('err.recipe_unknown'),
  mobile_field:t('err.mobile_field'),
}
const UNCERTAIN = t('work.uncertainSend')

export function WorkView({workId,onChange}:{workId:string;onChange:()=>void}){
  const [work,setWork]=useState<Work|null>(null), [messages,setMessages]=useState<WorkMessage[]>([]), [truncated,setTruncated]=useState(false)
  const [error,setError]=useState(''), [busy,setBusy]=useState(false), [live,setLive]=useState(false)
  // The chain shown and accepted is bound to its digest: a changed policy (or a reload) never inherits an acceptance.
  // F3-03: what is sent is the selected team and its previewed digest (default: the newest message's team).
  const [policy,setPolicy]=useState<Policy|null>(null), [acceptedFor,setAcceptedFor]=useState<string|null>(null), [sel,setSel]=useState<TeamSelection|null>(null)
  const [firstTeam,setFirstTeam]=useState<Team|null>(null)   // a rejected first message's team (no message stored yet)
  const [wait,setWait]=useState<WaitChoice|null>(null)   // F3-04: opt-in quota wait for the next message
  const [recipe,setRecipe]=useState<string|null>(null)   // TB-45: role checklist recipe for the next message
  const [text,setText]=useState(''), [pending,setPending]=useState<PendingMessage|null>(null), [uncertain,setUncertain]=useState(false)
  const [editing,setEditing]=useState(false), [title,setTitle]=useState(''), [editRevision,setEditRevision]=useState(0)  // CAS base = what the user started editing
  const [open,setOpen]=useState<Set<string>>(new Set()), [confirmContinue,setConfirmContinue]=useState(false)
  const [outcome,setOutcome]=useState<string|null>(null)   // F3-02: the unverified outcome continue must name
  const [showPreview,setShowPreview]=useState(false)   // TB-20: the preview opens from the header
  const [showGitHub,setShowGitHub]=useState(false)   // TB-21: save / push / draft PR, only between runs
  const [showTests,setShowTests]=useState(false)   // TB-27: the test command Conductor runs before each review
  const [showCopy,setShowCopy]=useState(false)   // TB-26: an isolated work's copy (bring back / remove), Mac only
  const [settings,setSettings]=useState<SettingsView|null|undefined>(undefined)   // TB-19: undefined while loading
  const [showWorkSettings,setShowWorkSettings]=useState(false)   // TB-41: per-work settings modal
  const [modalTitle,setModalTitle]=useState('')
  const [modalTeam,setModalTeam]=useState<TeamSelection|null>(null)
  const [modalTeamKey,setModalTeamKey]=useState(0)
  const [modalWait,setModalWait]=useState<WaitChoice|null>(null)
  const [confirmDelete,setConfirmDelete]=useState(false)
  const [settingsBusy,setSettingsBusy]=useState(false)
  const [settingsError,setSettingsError]=useState('')
  const [settingsNotice,setSettingsNotice]=useState('')
  const seq=useRef(0)
  const editBtnRef=useRef<HTMLButtonElement>(null)
  const wasEditing=useRef(false)

  const refresh=useCallback(async()=>{
    const mine=++seq.current
    try{
      const [w,m]=await Promise.all([getWork(workId),listAllMessages(workId)])
      if(mine!==seq.current)return
      setWork(w);setMessages(m.items);setTruncated(m.truncated)
      // Reconcile a stored uncertain send with what the server really has.
      const p=pendingMessage.get(workId)
      if(p&&m.items.some(x=>x.client_message_id===p.client_message_id)){pendingMessage.clear(workId);setPending(null);setUncertain(false);setText(t=>t===p.text?'':t);setRecipe(r=>r===(p.recipe??null)?null:r);setError(x=>x===UNCERTAIN?'':x)}
    }catch(e){if(mine===seq.current)setError(say(e))}
  },[workId])

  useEffect(()=>{
    setWork(null);setMessages([]);setError('');setEditing(false);wasEditing.current=false;setAcceptedFor(null);setSel(null);setConfirmContinue(false);setText('')
    setShowWorkSettings(false);setConfirmDelete(false);setSettingsError('');setSettingsNotice('');setModalTeam(null)
    const p=pendingMessage.get(workId);setPending(p);setUncertain(!!p);setRecipe(p?.recipe??null)
    const first=rejectedFirst.get(workId);rejectedFirst.delete(workId)
    setFirstTeam(first?.team??null);setWait(first?.wait??null)
    if(first){setText(first.text);setError(first.code&&codes[first.code]?codes[first.code]:first.message)}
    api<Policy>('/api/sessions/providers').then(setPolicy).catch(e=>setError(String(e)))
    if(canCommand())void loadSettings().then(s=>{setSettings(s);if(!first?.wait)setWait(initialWorkWait(workId,s))});else setSettings(null)
    void refresh()
    // Work events (own seq ids): one refresh per burst; reconnect resumes with Last-Event-ID.
    const es=events(`/api/works/${encodeURIComponent(workId)}/events`), seen=new Set<string>()
    let timer:number|undefined
    es.onopen=()=>setLive(true);es.onerror=()=>setLive(false)
    es.addEventListener('work_event',e=>{
      const m=e as MessageEvent;if(seen.has(m.lastEventId))return;seen.add(m.lastEventId)
      clearTimeout(timer);timer=window.setTimeout(()=>{void refresh();onChange()},150)
    })
    return()=>{clearTimeout(timer);es.close();setLive(false)}
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[workId])

  useEffect(()=>{
    const sid=work?.blocked_reason==='needs_review_decision'?work.latest_session?.id:undefined
    if(!sid){setOutcome(null);return}
    api<{loop?:{outcome:string|null}}>(`/api/sessions/${sid}`).then(d=>setOutcome(d.loop?.outcome??null)).catch(e=>setError(String(e)))
  },[work?.blocked_reason,work?.latest_session?.id])

  useEffect(()=>{
    if(wasEditing.current&&!editing)editBtnRef.current?.focus()
    wasEditing.current=editing
  },[editing])

  // TB-20 K2: a run shows its summary; details open on demand (each open one holds an SSE). A run that needs a
  // person (waiting, failed, recovery, paused) opens by itself so its controls stay visible.
  const auto=(id:string)=>opensByItself(messages.find(m=>m.id===id)?.session,work?.latest_session?.id)
  const isOpen=(id:string)=>open.has(id)||(auto(id)&&!open.has('!'+id))
  const toggle=(id:string)=>setOpen(s=>{const n=new Set(s);if(isOpen(id)){n.delete(id);if(auto(id))n.add('!'+id)}else{n.add(id);n.delete('!'+id)}return n})
  const short=(id:string)=>{const p=splitId(id);return p?.model?modelLabel(null,baseOf(id),p.model):baseOf(id)}

  async function send(p:PendingMessage){
    setBusy(true);setError('')
    pendingMessage.set(workId,p);setPending(p);setUncertain(false)   // frozen before the request leaves
    try{
      await addMessage(workId,p)
      pendingMessage.clear(workId);setPending(null);setText('');setRecipe(null);setAcceptedFor(null)
      await refresh();onChange()
    }catch(e){
      if(answered(e)){
        // Definitely not stored: release the id, give the text back for editing.
        pendingMessage.clear(workId);setPending(null);setText(p.text);setRecipe(p.recipe??null);setError(say(e))
        if(e instanceof ApiError&&(e.code==='conflict'||e.code==='team_invalid')){   // ceiling or policy changed: re-preview, re-accept
          setAcceptedFor(null)
          try{setPolicy(await api<Policy>('/api/sessions/providers'))}catch(x){setPolicy(null);setError(String(x))}
        }
      }else{setUncertain(true);setError(UNCERTAIN);void refresh()}   // the server may already have it
    }finally{setBusy(false)}
  }
  function submit(){
    const preview=sel?.preview
    if(!policy||!sel||!preview||!text.trim()||busy||pending)return
    if(mustAccept&&!accepted)return
    void send({client_message_id:newId(),text,policy_digest:preview.digest,team:sel.team,...wait,...(recipe?{recipe}:{})})
  }
  /** Drop an uncertain send only after asking the server about the same id; offline keeps the record. */
  async function discardPending(p:PendingMessage){
    setBusy(true);setError('')
    const r=await discardMessage(workId,p,()=>listAllMessages(workId))
    setBusy(false)
    if(r==='kept'){setError(t('work.discardKept'));return}
    setPending(null);setUncertain(false)
    if(r==='sent'){setText(t=>t===p.text?'':t);setRecipe(r0=>r0===(p.recipe??null)?null:r0)}   // the server has it: nothing to resend
    else{setText(p.text);setRecipe(p.recipe??null)}                                           // definitely absent: the text returns for editing
    void refresh()
  }

  async function saveTitle(){if(!work||!title.trim())return;setBusy(true);setError('');try{
    await patchWork(workId,{expected_revision:editRevision,title:title.trim()});setEditing(false);await refresh();onChange()
  }catch(e){setError(say(e));if(e instanceof ApiError&&e.code==='revision_conflict')setEditing(false);await refresh()}finally{setBusy(false)}}
  async function toggleArchive(){if(!work)return;setBusy(true);setError('');setSettingsError('');try{
    await patchWork(workId,{expected_revision:work.revision,archived:!work.archived_at});await refresh();onChange()
  }catch(e){const msg=say(e);setError(msg);setSettingsError(msg);await refresh()}finally{setBusy(false)}}
  async function cancelQueued(m:WorkMessage){setBusy(true);setError('');try{await cancelMessage(workId,m.id);await refresh();onChange()}
    catch(e){setError(say(e));await refresh()}finally{setBusy(false)}}
  async function doContinue(){if(!work?.latest_session)return;setBusy(true);setError('');try{
    await continueWork(workId,work.latest_session.id,work.blocked_reason==='needs_review_decision'?outcome||undefined:undefined);setConfirmContinue(false);await refresh();onChange()
  }catch(e){setError(say(e));setConfirmContinue(false);await refresh()}finally{setBusy(false)}}

  function openWorkSettings(){
    if(!work)return
    setModalTitle(work.title)
    setModalTeam(null)
    setModalTeamKey(k=>k+1)
    setModalWait(wait??initialWorkWait(workId,settings))
    setConfirmDelete(false)
    setSettingsError('')
    setSettingsNotice('')
    setShowWorkSettings(true)
  }
  function closeWorkSettings(){
    setShowWorkSettings(false)
    setConfirmDelete(false)
    setSettingsError('')
    setSettingsNotice('')
  }
  async function saveModalTitle(){
    if(!work||!modalTitle.trim())return
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await patchWork(workId,{expected_revision:work.revision,title:modalTitle.trim()})
      setWork(updated);setModalTitle(updated.title);setSettingsNotice(t('work.notice.titleSaved'))
      await refresh();onChange()
    }catch(e){setSettingsError(say(e));await refresh()}finally{setSettingsBusy(false)}
  }
  async function saveWorkTeam(){
    const chosen=modalTeam?.team??(policy&&work?initialWorkTeam(workId,settings,work.latest_team,teamOf(policy.policy),firstTeam):null)
    if(!chosen)return
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await saveSettings({work_teams:putWorkTeam(settings?.settings.work_teams,workId,chosen)})
      setSettings(updated);setSettingsNotice(t('work.notice.teamSaved'))
    }catch(e){setSettingsError(say(e))}finally{setSettingsBusy(false)}
  }
  async function resetWorkTeam(){
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await saveSettings({work_teams:putWorkTeam(settings?.settings.work_teams,workId,null)})
      setSettings(updated);setModalTeam(null);setModalTeamKey(k=>k+1);setSettingsNotice(t('work.notice.teamReset'))
    }catch(e){setSettingsError(say(e))}finally{setSettingsBusy(false)}
  }
  async function saveWorkWait(){
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await saveSettings({work_waits:putWorkWait(settings?.settings.work_waits,workId,modalWait)})
      setSettings(updated);setWait(modalWait);setSettingsNotice(t('work.notice.waitSaved'))
    }catch(e){setSettingsError(say(e))}finally{setSettingsBusy(false)}
  }
  async function doDeleteWork(){
    if(!work)return
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      await deleteWork(workId,work.revision)
      setShowWorkSettings(false);setConfirmDelete(false)
      location.hash='#/'
      onChange()
    }catch(e){setSettingsError(deleteWorkErrorMessage(e));await refresh()}finally{setSettingsBusy(false)}
  }

  // K-041: `/` shortcuts — each only opens something on this page (Mac-only panels only on the Mac, as their buttons).
  // A hook: called before every early return, so each render runs the same hooks (React #310).
  const between=!(work?.blocked_reason&&['run_active','run_waiting','recovery_required','paused'].includes(work.blocked_reason))
  const commands:SlashCommand[]=[
    {name:'model',hint:t('work.cmd.model'),run:()=>openSelect(t('session.team.field.modelAria',{name:t('session.role.author')}),t('session.team.field.providerAria',{name:t('session.role.author')}))},
    {name:'efor',hint:t('work.cmd.effort'),run:()=>openSelect(t('session.team.field.effortAria',{name:t('session.role.author')}),t('session.team.field.modelAria',{name:t('session.role.author')}),t('session.team.field.providerAria',{name:t('session.role.author')}))},
    {name:'kota',hint:t('work.cmd.quota'),run:click('.composer .usage-ring')},
    {name:'takim',hint:t('work.cmd.team'),run:click('.composer .composer-tools')},
    ...RECIPES.map(r=>({name:'tarif-'+r.id,hint:t('work.cmd.recipe',{title:r.title}),run:()=>setRecipe(r.id)})),
    ...(!mobile.active&&work&&!work.archived_at?[
      {name:'onizleme',hint:t('work.cmd.preview'),run:()=>setShowPreview(true)},
      {name:'testler',hint:t('work.cmd.tests'),run:()=>setShowTests(true)},
      ...(between?[{name:'github',hint:t('work.cmd.github'),run:()=>setShowGitHub(true)}]:[])]:[]),
    {name:'yeni',hint:t('work.cmd.new'),run:()=>{location.hash='#/new'}},
  ]
  const slash=useSlash(text,setText,commands)
  if(!work)return error?<div className="error-banner" role="alert">{error}</div>:<p className="loading">{t('work.loading')}</p>
  const running=work.blocked_reason==='run_active'
  const preview=sel?.preview??null
  const cannotSend=!policy||!preview||missingProvider(policy,preview.policy)
  const mustAccept=!!preview&&chainNeedsAcceptance(preview.digest,work.latest_policy_digest,acceptedDigest(settings??null))
  const accepted=!!preview&&acceptedFor===preview.digest
  const chainLine=(tm:Team)=>t('work.chainLine',{author:tm.author.join(' → '),reviewer:tm.reviewer.join(' → ')})
  const reloadPolicy=async()=>{setPolicy(await api<Policy>('/api/sessions/providers'))}
  const lastSeq=messages.length?messages[messages.length-1].seq:null
  const savedWorkTeam=workSavedTeam(settings,workId)
  const selectedRecipe=recipe?RECIPES.find(r=>r.id===recipe)??null:null
  // K-039: a round send button at the bar's right end, like Claude's
  const sendButton=<button className="btn primary send round" aria-busy={busy} disabled={busy||!!pending||!text.trim()||cannotSend||(mustAccept&&!accepted)}
    aria-label={busy?t('work.sending'):running?t('work.queueAdd'):t('work.send')} title={running?t('work.queueAddShortcut'):t('work.sendShortcut')}>
    {busy?<LoaderCircle className="spin" aria-hidden/>:<ArrowUp aria-hidden/>}</button>
  return <div className="work-view">
    <div className="run-head"><div>
      {editing?<form className="title-edit" onSubmit={e=>{e.preventDefault();void saveTitle()}}>
        <label htmlFor="work-title" className="sr-only">{t('work.titleLabel')}</label>
        <input id="work-title" value={title} maxLength={200} autoFocus onChange={e=>setTitle(e.target.value)} onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();setEditing(false)}}}/>
        <button className="btn primary" disabled={busy||!title.trim()}>{t('work.save')}</button><button type="button" className="btn" onClick={()=>setEditing(false)}>{t('work.cancel')}</button>
      </form>:<h1>{work.title} {!mobile.active&&<button ref={editBtnRef} className="icon-btn inline" aria-label={t('work.editTitle')} title={t('work.editTitle')} onClick={()=>{setTitle(work.title);setEditRevision(work.revision);setEditing(true)}}><Pencil size={14} aria-hidden/></button>}</h1>}
      <div className="sub">{work.latest_session?<Pill status={work.latest_session.status}/>:<span className="pill idle"><span className="d" aria-hidden="true"/>{t('work.noRunsYet')}</span>}
        {work.account_wait&&(work.account_wait.holder_work_id
          ?<a className="pill warn" href={`#/works/${encodeURIComponent(work.account_wait.holder_work_id)}`} onClick={()=>{location.hash=`#/works/${encodeURIComponent(work.account_wait!.holder_work_id!)}`}}><span className="d" aria-hidden="true"/>{t('work.accountWait',{account:work.account_wait.account,holder:work.account_wait.holder_work_title||t('work.otherWork')})}</a>
          :<span className="pill warn"><span className="d" aria-hidden="true"/>{t('work.accountWait',{account:work.account_wait.account,holder:work.account_wait.holder_work_title||t('work.otherWork')})}</span>)}
        {work.blocked_reason==='needs_review_decision'&&<span className="pill warn"><span className="d" aria-hidden="true"/>{t('work.goalUnverified')}</span>}
        {work.pending_messages>0&&<span className="pill idle">{t('work.messagesQueued',{count:work.pending_messages})}</span>}
        {work.archived_at&&<span className="pill idle">{t('work.archived')}</span>}
        {live?<span className="live" role="status"><span className="dot"/>{t('work.live')}</span>:<span className="muted small" role="status">{t('work.reconnecting')}</span>}</div>
      <p className="path">{work.workspace_path}</p>
      {work.isolation&&<p className="muted small">{t('work.isolationCopy')}{work.isolation.removed?t('work.isolationRemovedSuffix'):''} · {t('work.isolationBranch')} <span className="mono">{work.isolation.branch}</span> · {t('work.isolationSource')} <span className="mono">{work.isolation.source}</span></p>}</div><span className="spacer"/>
      {!mobile.active&&work.isolation&&<button className="btn" disabled={!!work.blocked_reason&&['run_active','run_waiting','recovery_required','paused'].includes(work.blocked_reason)}
        title={t('work.bringToMainTitle')} onClick={()=>setShowCopy(true)}>{t('work.bringToMain')}</button>}
      {!mobile.active&&!work.archived_at&&<button className="btn" aria-expanded={showPreview} onClick={()=>setShowPreview(v=>!v)}>{showPreview?t('work.previewClose'):t('work.preview')}</button>}
      {!mobile.active&&!work.archived_at&&<button className="btn" disabled={!!work.blocked_reason&&['run_active','run_waiting','recovery_required','paused'].includes(work.blocked_reason)}
        title={t('work.githubTitle')} onClick={()=>setShowGitHub(true)}>GitHub</button>}
      {!mobile.active&&!work.archived_at&&<button className="btn" title={t('work.testsTitle')} onClick={()=>setShowTests(true)}>{t('work.tests')}</button>}
      {!mobile.active&&<button className="btn" disabled={busy} onClick={()=>void toggleArchive()}>{work.archived_at?t('work.unarchive'):t('work.archive')}</button>}
      {canCommand()&&<button type="button" className="btn" onClick={openWorkSettings}><Settings2 size={14} aria-hidden/> {t('work.settings')}</button>}
    </div>
    {/* With a composer the error is shown next to it (sticky, so a failed send is visible where the text is kept). */}
    {error&&work.archived_at&&<div className="error-banner" role="alert">{error}</div>}
    {!mobile.active&&!work.archived_at&&showPreview&&<PreviewPanel workspacePath={work.workspace_path}/>}
    {!mobile.active&&showCopy&&work.isolation&&<IsolationPanel work={work} onClose={()=>{setShowCopy(false);void refresh()}}/>}
    {!mobile.active&&showTests&&<TestsPanel workId={workId} onClose={()=>setShowTests(false)}/>}
    {!mobile.active&&showGitHub&&<GitHubPanel workId={workId} title={work.title} onClose={()=>{setShowGitHub(false);void refresh()}}/>}
    {showWorkSettings&&canCommand()&&<Modal label={t('work.settingsModal.title')} busy={busy||settingsBusy} onClose={closeWorkSettings}>
      <h3>{t('work.settingsModal.title')}</h3>
      {settingsError&&<div className="error-banner" role="alert">{settingsError}</div>}
      {settingsNotice&&<p className="info-note" role="status">{settingsNotice}</p>}
      <section className="settings-section">
        <h4>{t('work.settingsModal.name')}</h4>
        <form className="title-edit" onSubmit={e=>{e.preventDefault();void saveModalTitle()}}>
          <label htmlFor="work-settings-title" className="sr-only">{t('work.settingsModal.workName')}</label>
          <input id="work-settings-title" value={modalTitle} maxLength={200} onChange={e=>setModalTitle(e.target.value)}/>
          <button type="submit" className="btn primary" disabled={busy||settingsBusy||!modalTitle.trim()}>{t('work.save')}</button>
        </form>
      </section>
      <section className="settings-section">
        <h4>{t('work.settingsModal.teamHeading')}</h4>
        <p className="muted small">{savedWorkTeam?t('work.settingsModal.teamSavedNote'):t('work.settingsModal.teamDefaultNote')}</p>
        {policy&&<TeamPicker key={`modal-${workId}-${modalTeamKey}`} policy={policy}
          initial={modalTeam?.team??initialWorkTeam(workId,settings,work.latest_team,teamOf(policy.policy),firstTeam)}
          onReloadPolicy={reloadPolicy} onChange={setModalTeam}/>}
        <div className="modal-actions">
          <button type="button" className="btn primary" disabled={busy||settingsBusy||!policy||!!modalTeam?.invalid} onClick={()=>void saveWorkTeam()}>{t('work.settingsModal.saveForWork')}</button>
          <button type="button" className="btn" disabled={busy||settingsBusy||!savedWorkTeam} onClick={()=>void resetWorkTeam()}>{t('work.settingsModal.resetDefault')}</button>
        </div>
      </section>
      {!mobile.active&&<section className="settings-section">
        <h4>{t('work.settingsModal.quotaWait')}</h4>
        <WaitOptions id="work-settings-wait" value={modalWait} onChange={setModalWait}/>
        <div className="modal-actions">
          {!mobile.active&&<button type="button" className="btn primary" disabled={busy||settingsBusy} onClick={()=>void saveWorkWait()}>{t('work.settingsModal.saveForWork')}</button>}
        </div>
      </section>}
      {!mobile.active&&work.isolation&&<section className="settings-section">
        <h4>{t('work.isolationCopy')}</h4>
        <p className="muted small">{t('work.isolation.branchLabel')} <span className="mono">{work.isolation.branch}</span> · {t('work.isolation.sourceLabel')} <span className="mono">{work.isolation.source}</span> · {t('work.isolation.statusLabel')} {work.isolation.removed?t('work.isolation.removed'):t('work.isolation.active')}</p>
        <div className="modal-actions">
          {!mobile.active&&<button type="button" className="btn" disabled={!between} onClick={()=>{closeWorkSettings();setShowCopy(true)}}>{t('work.bringToMain')}</button>}
        </div>
      </section>}
      {!mobile.active&&<section className="settings-section">
        <h4>{t('work.settingsModal.shortcuts')}</h4>
        <div className="modal-actions">
          {!mobile.active&&!work.archived_at&&<button type="button" className="btn" onClick={()=>{closeWorkSettings();setShowTests(true)}}>{t('work.tests')}</button>}
          {!mobile.active&&!work.archived_at&&<button type="button" className="btn" aria-expanded={showPreview} onClick={()=>{closeWorkSettings();setShowPreview(v=>!v)}}>{showPreview?t('work.previewClose'):t('work.preview')}</button>}
          {!mobile.active&&!work.archived_at&&<button type="button" className="btn" disabled={!between} onClick={()=>{closeWorkSettings();setShowGitHub(true)}}>GitHub</button>}
        </div>
      </section>}
      {!mobile.active&&<section className="settings-section">
        <h4>{t('work.settingsModal.dangerZone')}</h4>
        <div className="modal-actions">
          {!mobile.active&&<button type="button" className="btn" disabled={busy||settingsBusy} onClick={()=>void toggleArchive()}>{work.archived_at?t('work.unarchive'):t('work.archive')}</button>}
          {!mobile.active&&!confirmDelete&&<button type="button" className="btn danger" disabled={busy||settingsBusy||!between} onClick={()=>setConfirmDelete(true)}>{t('work.delete')}</button>}
        </div>
        {!mobile.active&&confirmDelete&&<div className="confirm-delete">
          <p className="notice"><strong>{work.title}</strong> {t('work.confirmDeleteAsk')}</p>
          <div className="modal-actions">
            {!mobile.active&&<button type="button" className="btn" disabled={busy||settingsBusy} onClick={()=>setConfirmDelete(false)}>{t('work.cancel')}</button>}
            {!mobile.active&&<button type="button" className="btn danger" disabled={busy||settingsBusy} onClick={()=>void doDeleteWork()}>{t('work.confirmDeleteBtn')}</button>}
          </div>
        </div>}
      </section>}
      <div className="modal-actions">
        <button type="button" className="btn" disabled={busy||settingsBusy} onClick={closeWorkSettings}>{t('work.close')}</button>
      </div>
    </Modal>}
    {work.blocked_reason==='needs_continue'&&<section className="card blocked"><div className="card-h"><h2>{t('work.queueStopped')}</h2></div><div className="card-b">
      <p>{t('work.needsContinueLead')} <strong>{labels[work.latest_session?.status||'']||work.latest_session?.status}</strong> {t('work.needsContinueTail')}</p>
      {!mobile.active&&<button className="btn" disabled={busy} onClick={()=>setConfirmContinue(true)}>{t('work.reviewedContinue')}</button>}
    </div></section>}
    {work.blocked_reason==='needs_review_decision'&&<section className="card blocked"><div className="card-h"><h2>{t('work.queueStoppedUnverified')}</h2></div><div className="card-b">
      <p>{t('work.needsReviewLead')} <strong>{outcome?outcomeLabels[outcome]||outcome:t('work.outcomeLoading')}</strong>. {t('work.needsReviewTail')}</p>
      {!mobile.active&&<button className="btn" disabled={busy||!outcome} onClick={()=>setConfirmContinue(true)}>{t('work.reviewedContinue')}</button>}
    </div></section>}
    {work.blocked_reason&&blockers[work.blocked_reason]&&<p className={running?'info-note':'notice'} role="status">{blockers[work.blocked_reason]}</p>}
    {confirmContinue&&<Modal label={t('work.continueModal.label')} busy={busy} onClose={()=>setConfirmContinue(false)}>
      <h3>{t('work.continueModal.title')}</h3>
      <p>{t('work.continueModal.body')}</p>
      <div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setConfirmContinue(false)}>{t('work.cancel')}</button><button className="btn primary" disabled={busy} onClick={()=>void doContinue()}>{t('work.confirm')}</button></div>
    </Modal>}

    <ol className="messages" aria-label={t('work.messagesLabel')}>
      {truncated&&<li className="muted small">{t('work.truncatedMessages')}</li>}
      {!messages.length&&!pending&&<li className="empty-list">{t('work.noMessages')}{canCommand()?t('work.sendFirstHint'):''}</li>}
      {messages.map(m=><li key={m.id} className="message chat">
        <div className="bubble">
          <p className="message-text">{m.text}</p>
          <div className="bubble-meta"><span className="seq mono">#{m.seq}</span><span>{new Date(m.created_at).toLocaleString(locale)}</span>
            {m.recipe&&<span className="pill idle">{t('work.recipePill',{title:m.recipe.title,version:m.recipe.version})}</span>}
            {m.status==='PENDING'&&<><span className="pill idle"><span className="d" aria-hidden="true"/>{t('work.queuedForNextRun')}</span>{canInterrupt()&&<button className="btn small" disabled={busy} onClick={()=>void cancelQueued(m)}>{t('work.removeFromQueue')}</button>}</>}
            {m.status==='CANCELLED'&&<span className="pill idle">{t('work.cancelledNeverRan')}</span>}</div>
        </div>
        {m.session&&<div className="run-card">
          <div className="run-card-head"><span className="who">PromptBridge</span><Pill status={m.session.status}/>
            <span className="run-line">{runLine(m.team,short)}</span>{m.session.tests&&<TestPill run={m.session.tests}/>}{m.session.lint&&<TestPill run={m.session.lint} check="lint"/>}<span className="spacer"/>
            <button className="btn small ghost" aria-expanded={isOpen(m.id)} onClick={()=>toggle(m.id)}>{isOpen(m.id)?t('work.hideDetails'):t('work.showDetails')}</button></div>
          {!!m.session.git_changed?.length&&<p className="notice" role="alert"><strong>{t('work.gitChangedLead')}</strong> <span className="mono">{m.session.git_changed.join(', ')}</span>{t('work.gitChangedTail')}</p>}
          {isOpen(m.id)&&<SessionView sessionId={m.session.id} status={m.session.status} embedded onChange={()=>{void refresh();onChange()}}/>}
        </div>}
      </li>)}
      {pending&&<li className="message pending">
        <div className="message-head"><span className="pill warn"><span className="d" aria-hidden="true"/>{busy?t('work.sendingEllipsis'):t('work.sendOutcomeUnknown')}</span></div>
        <p className="message-text">{pending.text}</p>
        {uncertain&&!busy&&<div className="pending-actions">
          <p className="hint">{t('work.pendingHint')}</p>
          <button className="btn primary" onClick={()=>void send(pending)}>{t('work.retrySameMessage')}</button>
          <button className="btn" onClick={()=>void discardPending(pending)}>{t('work.discardLocalRecord')}</button>
        </div>}
      </li>}
    </ol>

    {!canCommand()?<p className="muted small">{t('work.phoneReadOnly')}</p>:work.archived_at?<p className="notice">{t('work.archivedNotice')}</p>:
    <form className="composer" aria-label={messages.length?t('work.followUpMessage'):t('work.firstTask')} onSubmit={e=>{e.preventDefault();submit()}}>
      {(lastSeq!==null||work.pending_messages>0)&&<div className="composer-context">
        {lastSeq!==null&&<span className="muted small">{t('work.afterSeq',{seq:lastSeq})}</span>}
        {work.pending_messages>0&&<span className="pill idle">{t('work.queuedCount',{count:work.pending_messages})}</span>}
      </div>}
      {error&&<div className="error-banner" role="alert">{error}</div>}
      {mustAccept&&policy&&preview&&<div className="reaccept"><p><strong>{work.latest_policy_digest?t('work.reacceptChanged'):t('work.reacceptFirst')}</strong> <span className="reaccept-chain">{sel&&chainLine(sel.team)}</span></p>
        {loopLine(preview.policy.loop)&&<p className="chain-note">{loopLine(preview.policy.loop)}</p>}
        <label className="accept-row"><input type="checkbox" checked={accepted} onChange={e=>setAcceptedFor(e.target.checked?preview.digest:null)}/> {t('work.acceptTeam')}</label></div>}
      {policy&&preview&&missingProvider(policy,preview.policy)&&<p className="notice">{t('work.missingProviderNotice')}</p>}
      {selectedRecipe&&<div className="composer-context">
        <span className="pill idle">{t('work.cmd.recipe',{title:selectedRecipe.title})} <button type="button" className="icon-btn inline" disabled={!!pending} aria-label={t('work.removeRecipe')} title={t('work.removeRecipe')} onClick={()=>setRecipe(null)}>×</button></span>
      </div>}
      <div className="composer-input">
        <label htmlFor="composer" className="sr-only">{messages.length?t('work.followUpMessage'):t('work.firstTask')}</label>
        <textarea id="composer" rows={3} value={text} disabled={!!pending} aria-describedby="composer-hint" onChange={e=>setText(e.target.value)}
          onKeyDown={e=>{if(slash.onKeyDown(e))return;if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();submit()}}}
          placeholder={running?t('work.placeholderQueued'):messages.length?t('work.placeholderNext'):t('work.placeholderFirst')}/>
        {slash.menu}
        {!(policy&&settings!==undefined)&&sendButton}
      </div>
      {policy&&settings!==undefined&&<TeamPicker key={`${workId}:${JSON.stringify(savedWorkTeam)}`} compact policy={policy}
        initial={initialWorkTeam(workId,settings,work.latest_team,teamOf(policy.policy),firstTeam)}
        startOpen={!work.latest_team&&!savedWorkTeam&&!!firstTeam} onReloadPolicy={reloadPolicy} onChange={s=>{setSel(s);if(!s.preview)setAcceptedFor(null)}}
        sessionId={work.latest_session?.id} sessionStatus={work.latest_session?.status} trailing={sendButton}/>}
      <div className="composer-foot">
        <details className="composer-more"><summary>{t('work.quotaWaitSummary',{state:wait?t('work.quotaWaitOn'):t('work.quotaWaitOff')})}</summary><WaitOptions id="composer-wait" value={wait} onChange={setWait}/></details>
        <p className="hint" id="composer-hint">{running?t('work.hintQueued'):(preview?.policy??policy?.policy)?.loop?t('work.hintLoop'):t('work.hintSingle')} {loopLine((preview?.policy??policy?.policy)?.loop)} {t('work.hintShortcut')}</p>
      </div>
    </form>}
  </div>
}
