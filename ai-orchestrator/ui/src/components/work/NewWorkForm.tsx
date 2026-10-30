import { useEffect, useState } from 'react'
import { ArrowUp, FolderOpen, LoaderCircle } from 'lucide-react'
import { createWork, addMessage, getWork, ApiError } from '../../api'
import { missingProvider, newId, api, loopLine, teamOf, WaitOptions, type Policy, type WaitChoice } from './shared'
import { TeamPicker, type TeamSelection } from './TeamPicker'
import { click, openSelect, useSlash, type SlashCommand } from './SlashMenu'
import { RepoPicker } from './RepoPicker'
import { pendingCreate, rejectedFirst, type PendingCreate } from './pending'
import { startWork, resumeCreated, discardCreate, deriveTitle } from './flows'
import { hasNativeFolderPicker, installFolderSelectedListener, requestNativeFolder } from './nativeBridge'
import { mobile } from '../../mobile'
import { acceptedDigest, defaultTeam, loadSettings, type SettingsView } from '../../settings'
import { say, t } from '../../i18n'

/** New work = create the work (idempotent id), then its first message (idempotent id).
 *  If the message fails after the work exists, the work is kept and opened; its view retries the same message id. */
/** `folders`: on a phone (K-033/3) the work starts only in a folder an existing work uses; no path field, no GitHub.
 *  `initial`: K-055 “new branch” from a project in the sidebar (its folder, separate copy on). */
export function NewWorkForm({onCreated,folders,initial}:{onCreated:(workId:string)=>void;folders?:string[];initial?:{workspace:string;isolate:boolean}}){
  const [workspace,setWorkspace]=useState(initial?.workspace??''), [task,setTask]=useState('')
  const [canSelectFolder] = useState(()=>hasNativeFolderPicker(window))
  const [fromGitHub,setFromGitHub]=useState(false)   // TB-21: clone a repository and use its folder
  const [choosing,setChoosing]=useState(false)
  // K-041: Finder on the Mac — the app's own panel, or (in a browser) the server opens the same dialog on this Mac
  async function chooseFolder(){
    if(canSelectFolder){requestNativeFolder(window);return}
    setChoosing(true);setError('');setInnerRepos(null)
    try{const r=await api<{path:string|null;cancelled:boolean;error:string|null}>('/api/system/choose-folder',{method:'POST'})
      if(r.path)setWorkspace(r.path);else if(r.error)setError(t('work.new.folderDialogFailed',{error:r.error}))}
    catch(e){setError(String(e))}finally{setChoosing(false)}
  }
  const [readOnly,setReadOnly]=useState(false)   // K-057: review/question only; no agent may change files
  const [isolate,setIsolate]=useState(initial?.isolate??false)   // TB-26 (K-034): run in a git worktree; the folder itself stays untouched
  // F3-03: acceptance is bound to the previewed digest of the selected team; a new selection drops it.
  const [policy,setPolicy]=useState<Policy|null>(null), [sel,setSel]=useState<TeamSelection|null>(null), [acceptedFor,setAcceptedFor]=useState<string|null>(null)
  const [wait,setWait]=useState<WaitChoice|null>(null)   // F3-04: opt-in quota wait for the first message
  const [error,setError]=useState(''), [busy,setBusy]=useState(false)
  const [innerRepos,setInnerRepos]=useState<string[]|null>(null)   // K-055: not a repo → the repos inside it
  useEffect(()=>setInnerRepos(null),[workspace,isolate])   // the help is about the folder and choice it was shown for
  const [pending,setPending]=useState<PendingCreate|null>(()=>pendingCreate.get())
  const [settings,setSettings]=useState<SettingsView|null|undefined>(undefined)   // TB-19: undefined while loading
  const loadPolicy=()=>api<Policy>('/api/sessions/providers').then(p=>{setPolicy(p);setError('')}).catch(e=>{setPolicy(null);setError(String(e));setInnerRepos(null)})
  useEffect(()=>{
    const cleanupFolderSelected = installFolderSelectedListener(window, setWorkspace)
    const created=resumeCreated()   // a reload after the work was created: open it, never an empty form
    if(created){
      onCreated(created)
    } else {
      void loadPolicy()
      void loadSettings().then(setSettings)
    }
    return cleanupFolderSelected
  // eslint-disable-next-line react-hooks/exhaustive-deps
  },[])

  async function run(p:PendingCreate){
    setBusy(true);setError('');setInnerRepos(null);setPending(p)
    const r=await startWork(p,{createWork,addMessage})
    setBusy(false)
    if(r.kind==='opened'){
      const e=r.rejected   // the first message was definitely refused: the work view shows its text and the reason
      if(e)rejectedFirst.set(r.workId,{text:p.message.text,message:String(e),code:e instanceof ApiError?e.code:undefined,team:p.message.team,wait,read_only:p.message.read_only})
      onCreated(r.workId);return
    }
    if(r.kind==='create_rejected'){setPending(null);setError(say(r.error))
      const repos=!folders&&r.error instanceof ApiError&&r.error.code==='not_a_repo'?r.error.body?.repos:null
      setInnerRepos(Array.isArray(repos)?repos.filter((x):x is string=>typeof x==='string'):null)
      if(r.error instanceof ApiError&&r.error.status===409){setAcceptedFor(null);void loadPolicy()}}
    else setError(t('work.new.createUncertain'))
  }
  async function discard(p:PendingCreate){
    setBusy(true);setError('')
    const r=await discardCreate(p,getWork)
    setBusy(false)
    if(r==='discarded'){setPending(null);return}
    if(r==='kept'){setError(t('work.new.discardKept'));return}
    // It was created after all; its first message was never sent. Open it with the text in the composer.
    rejectedFirst.set(p.client_work_id,{text:p.message.text,message:t('work.new.createdNotSent'),team:p.message.team,read_only:p.message.read_only})
    onCreated(p.client_work_id)
  }
  // TB-19 K1: the default team saved in Settings was accepted there; another selection needs the box.
  const preview=sel?.preview??null, byDefault=!!preview&&preview.digest===acceptedDigest(settings??null)
  const accepted=!!preview&&(byDefault||acceptedFor===preview.digest)
  function submit(){
    if(!policy||!preview||!sel||!accepted||busy)return
    void run({client_work_id:newId(),title:deriveTitle(task),workspace_path:workspace.trim(),...(isolate?{isolate:true}:{}),
      message:{client_message_id:newId(),text:task,policy_digest:preview.digest,team:sel.team,...wait,...(readOnly?{read_only:true}:{})}})
  }

  // K-041: `/` shortcuts in the first message (none starts the work); a hook, so before the early return (React #310)
  const commands:SlashCommand[]=[
    ...(!mobile.active?[{name:'klasor',hint:t('work.cmd.folder'),run:()=>void chooseFolder()},
      {name:'github',hint:t('work.cmd.clone'),run:()=>setFromGitHub(true)}]:[]),
    {name:'model',hint:t('work.cmd.model'),run:()=>openSelect(t('session.team.field.modelAria',{name:t('session.role.author')}),t('session.team.field.providerAria',{name:t('session.role.author')}))},
    {name:'efor',hint:t('work.cmd.effort'),run:()=>openSelect(t('session.team.field.effortAria',{name:t('session.role.author')}),t('session.team.field.modelAria',{name:t('session.role.author')}),t('session.team.field.providerAria',{name:t('session.role.author')}))},
    {name:'kota',hint:t('work.cmd.quotaContext'),run:click('.composer .usage-ring')},
    {name:'takim',hint:t('work.cmd.team'),run:click('.composer .composer-tools')},
  ]
  const slash=useSlash(task,setTask,commands)
  if(pending)return <div className="work-form">
    <div className="run-head"><div><span className="eyebrow">{t('work.new.eyebrow')}</span><h1>{pending.title}</h1><p className="path">{pending.workspace_path}</p></div></div>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    <section className="card"><div className="card-b form-fields">
      <p className="notice">{t('work.new.pendingNotice')}</p>
      <p className="message-text">{pending.message.text}</p>
      <div className="form-footer"><button className="btn" disabled={busy} onClick={()=>void discard(pending)}>{t('work.discardLocalRecord')}</button>
        <button className="btn primary" disabled={busy} onClick={()=>void run(pending)}>{busy?t('work.new.retrying'):t('work.new.retrySameRequest')}</button></div>
    </div></section>
  </div>

  const chain=sel?t('work.new.chainLine',{author:sel.team.author.join(' → '),reviewer:sel.team.reviewer.join(' → ')}):''
  // TB-20: a chat-like start: folder, first message, the composer's model picker; the title comes from the message (K1).
  // K-039: a round start button at the bar's right end, like Claude's send
  const startButton=<button className="btn primary send round" disabled={busy||!workspace.trim()||!task.trim()||!policy||!preview||!accepted||missingProvider(policy,preview.policy)}
    aria-label={busy?t('work.new.starting'):t('work.new.start')} title={t('work.new.start')}>{busy?<LoaderCircle className="spin" aria-hidden/>:<ArrowUp aria-hidden/>}</button>
  return <form className="work-form new-chat" onSubmit={e=>{e.preventDefault();submit()}}>
    <div className="new-chat-head"><h1>{t('work.new.heading')}</h1>
      <p className="intro">{t('work.new.intro')}</p></div>
    {error&&<div className="error-banner"><span role="alert">{error}</span>
      {innerRepos&&<div className="not-repo-help"><p>{t(innerRepos.length?'work.new.notRepoHint':'work.new.notRepoHintNone')}</p>
        {innerRepos.length>0&&<p className="small">{t('work.new.innerRepos')}:</p>}
        <div className="not-repo-actions">{innerRepos.map(p=><button key={p} type="button" className="btn small mono" title={p}
          onClick={()=>{setWorkspace(p);setError('');document.getElementById('nw-task')?.focus()}}>{p.split('/').pop()}</button>)}
          <button type="button" className="btn small" onClick={()=>{setIsolate(false);setError('');document.getElementById('nw-task')?.focus()}}>{t('work.new.isolateOff')}</button></div>
      </div>}</div>}
    <div className="composer new-composer">
      <div className="folder-row">
        <label htmlFor="nw-workspace" className="sr-only">{t('work.new.folderLabel')}</label>
        {folders?<select id="nw-workspace" className="mono" required value={workspace} onChange={e=>setWorkspace(e.target.value)}>
          <option value="">{folders.length?t('work.new.pickFolder'):t('work.new.openOnMacFirst')}</option>
          {folders.map(f=><option key={f} value={f}>{f.split('/').pop()||f}</option>)}</select>:<>
        {!mobile.active&&<button type="button" className="btn small folder-pick" disabled={choosing} aria-busy={choosing} onClick={()=>void chooseFolder()}>
          {choosing?<LoaderCircle className="spin" aria-hidden/>:<FolderOpen aria-hidden/>}{workspace?t('work.new.change'):t('work.new.pickFromFinder')}</button>}
        <input id="nw-workspace" className="mono" required value={workspace} onChange={e=>setWorkspace(e.target.value)} placeholder={mobile.active?t('work.new.folderPlaceholderMobile'):t('work.new.folderPlaceholder')}/>
        <button type="button" className="btn small" aria-expanded={fromGitHub} onClick={()=>setFromGitHub(v=>!v)}>{t('work.new.fromGitHub')}</button></>}
      </div>
      {fromGitHub&&<RepoPicker onClose={()=>setFromGitHub(false)} onPicked={path=>{setWorkspace(path);setFromGitHub(false)}}/>}
      <label htmlFor="nw-task" className="sr-only">{t('work.firstTask')}</label>
      <div className="slash-anchor"><textarea id="nw-task" required rows={5} maxLength={100000} value={task} onChange={e=>setTask(e.target.value)}
        onKeyDown={e=>{slash.onKeyDown(e)}} placeholder={t('work.new.taskPlaceholder')}/>{slash.menu}</div>
      {policy&&settings!==undefined?<TeamPicker compact policy={policy} initial={defaultTeam(settings)??teamOf(policy.policy)} onReloadPolicy={loadPolicy}
        onChange={s=>{setSel(s);if(!s.preview)setAcceptedFor(null)}} trailing={startButton}/>:<p className="muted small">{policy||!error?t('work.new.loading'):t('work.new.policyLoadFailed')}</p>}
      {preview&&!byDefault&&<label className="accept-row confirm-line"><input type="checkbox" checked={accepted} onChange={e=>setAcceptedFor(e.target.checked?preview.digest:null)}/>
        <span>{t('work.new.acceptLead')} <span className="reaccept-chain">{chain}</span>{t('work.new.acceptTail',{loop:preview.policy.loop?t('work.new.acceptLoopPart'):''})}</span></label>}
      <label className="accept-row"><input type="checkbox" checked={isolate} onChange={e=>setIsolate(e.target.checked)}/>
        <span><strong>{t('work.new.isolateTitle')}</strong> {t('work.new.isolateBody')}</span></label>
      <label className="accept-row"><input type="checkbox" checked={readOnly} onChange={e=>setReadOnly(e.target.checked)}/>
        <span><strong>{t('work.readOnlyTitle')}</strong> {t('work.readOnlyBody')}</span></label>
      <details className="composer-more"><summary>{t('work.new.options')}</summary>
        <WaitOptions id="nw-wait" value={wait} onChange={setWait}/>
        <p className="chain-note">{t('work.new.chainNote',{count:(preview?.policy??policy?.policy)?.max_context_continuations??0})}</p>
        {loopLine(preview?.policy.loop)&&<p className="chain-note"><strong>{loopLine(preview?.policy.loop)}</strong></p>}
        <p className="hint">{t('work.new.folderLockHint')}</p>
      </details>
      <div className="composer-foot"><span className="muted small" title={t('work.new.titleHint')}>{task.trim()?t('work.new.titlePreview',{title:deriveTitle(task)}):''}</span>
        {!(policy&&settings!==undefined)&&startButton}</div>
    </div>
  </form>
}
