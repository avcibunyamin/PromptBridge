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

/** New work = create the work (idempotent id), then its first message (idempotent id).
 *  If the message fails after the work exists, the work is kept and opened; its view retries the same message id. */
/** `folders`: on a phone (K-033/3) the work starts only in a folder an existing work uses; no path field, no GitHub. */
export function NewWorkForm({onCreated,folders}:{onCreated:(workId:string)=>void;folders?:string[]}){
  const [workspace,setWorkspace]=useState(''), [task,setTask]=useState('')
  const [canSelectFolder] = useState(()=>hasNativeFolderPicker(window))
  const [fromGitHub,setFromGitHub]=useState(false)   // TB-21: clone a repository and use its folder
  const [choosing,setChoosing]=useState(false)
  // K-041: Finder on the Mac — the app's own panel, or (in a browser) the server opens the same dialog on this Mac
  async function chooseFolder(){
    if(canSelectFolder){requestNativeFolder(window);return}
    setChoosing(true);setError('')
    try{const r=await api<{path:string|null;cancelled:boolean;error:string|null}>('/api/system/choose-folder',{method:'POST'})
      if(r.path)setWorkspace(r.path);else if(r.error)setError(`Klasör penceresi açılamadı (${r.error}); yolu elle yazabilirsiniz.`)}
    catch(e){setError(String(e))}finally{setChoosing(false)}
  }
  const [isolate,setIsolate]=useState(false)   // TB-26 (K-034): run in a git worktree; the folder itself stays untouched
  // F3-03: acceptance is bound to the previewed digest of the selected team; a new selection drops it.
  const [policy,setPolicy]=useState<Policy|null>(null), [sel,setSel]=useState<TeamSelection|null>(null), [acceptedFor,setAcceptedFor]=useState<string|null>(null)
  const [wait,setWait]=useState<WaitChoice|null>(null)   // F3-04: opt-in quota wait for the first message
  const [error,setError]=useState(''), [busy,setBusy]=useState(false)
  const [pending,setPending]=useState<PendingCreate|null>(()=>pendingCreate.get())
  const [settings,setSettings]=useState<SettingsView|null|undefined>(undefined)   // TB-19: undefined while loading
  const loadPolicy=()=>api<Policy>('/api/sessions/providers').then(p=>{setPolicy(p);setError('')}).catch(e=>{setPolicy(null);setError(String(e))})
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
    setBusy(true);setError('');setPending(p)
    const r=await startWork(p,{createWork,addMessage})
    setBusy(false)
    if(r.kind==='opened'){
      const e=r.rejected   // the first message was definitely refused: the work view shows its text and the reason
      if(e)rejectedFirst.set(r.workId,{text:p.message.text,message:String(e),code:e instanceof ApiError?e.code:undefined,team:p.message.team,wait})
      onCreated(r.workId);return
    }
    if(r.kind==='create_rejected'){setPending(null);setError(String(r.error))
      if(r.error instanceof ApiError&&r.error.status===409){setAcceptedFor(null);void loadPolicy()}}
    else setError('Çalışmanın oluşturulup oluşturulmadığı bilinmiyor. Aynı kimlikle yeniden deneyin; iki kez oluşmaz.')
  }
  async function discard(p:PendingCreate){
    setBusy(true);setError('')
    const r=await discardCreate(p,getWork)
    setBusy(false)
    if(r==='discarded'){setPending(null);return}
    if(r==='kept'){setError('Sunucuya ulaşılamadı; kayıt korunuyor. Bağlantı gelince yeniden deneyin ya da tekrar silmeyi deneyin.');return}
    // It was created after all; its first message was never sent. Open it with the text in the composer.
    rejectedFirst.set(p.client_work_id,{text:p.message.text,message:'Çalışma oluşturulmuştu; ilk görev gönderilmedi. Metni kontrol edip gönderin.',team:p.message.team})
    onCreated(p.client_work_id)
  }
  // TB-19 K1: the default team saved in Settings was accepted there; another selection needs the box.
  const preview=sel?.preview??null, byDefault=!!preview&&preview.digest===acceptedDigest(settings??null)
  const accepted=!!preview&&(byDefault||acceptedFor===preview.digest)
  function submit(){
    if(!policy||!preview||!sel||!accepted||busy)return
    void run({client_work_id:newId(),title:deriveTitle(task),workspace_path:workspace.trim(),...(isolate?{isolate:true}:{}),
      message:{client_message_id:newId(),text:task,policy_digest:preview.digest,team:sel.team,...wait}})
  }

  // K-041: `/` shortcuts in the first message (none starts the work); a hook, so before the early return (React #310)
  const commands:SlashCommand[]=[
    ...(!mobile.active?[{name:'klasor',hint:'Finder\'dan proje klasörü',run:()=>void chooseFolder()},
      {name:'github',hint:'GitHub\'dan klonla',run:()=>setFromGitHub(true)}]:[]),
    {name:'model',hint:'Yazarın modelini seç',run:()=>openSelect('Yazar model','Yazar sağlayıcı')},
    {name:'efor',hint:'Yazarın eforunu seç',run:()=>openSelect('Yazar efor','Yazar model','Yazar sağlayıcı')},
    {name:'kota',hint:'Context ve kota penceresi',run:click('.composer .usage-ring')},
    {name:'takim',hint:'Gelişmiş: yedekler ve inceleyici',run:click('.composer .composer-tools')},
  ]
  const slash=useSlash(task,setTask,commands)
  if(pending)return <div className="work-form">
    <div className="run-head"><div><span className="eyebrow">YENİ ÇALIŞMA</span><h1>{pending.title}</h1><p className="path">{pending.workspace_path}</p></div></div>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    <section className="card"><div className="card-b form-fields">
      <p className="notice">Önceki oluşturma isteğinin sonucu bilinmiyor. İstek bu tarayıcıda yerel olarak saklandı; aynı kimlikle yeniden denendiğinde iki çalışma oluşmaz. İçerik değiştirilemez.</p>
      <p className="message-text">{pending.message.text}</p>
      <div className="form-footer"><button className="btn" disabled={busy} onClick={()=>void discard(pending)}>Yerel kaydı sil</button>
        <button className="btn primary" disabled={busy} onClick={()=>void run(pending)}>{busy?'Deneniyor…':'Aynı isteği yeniden dene'}</button></div>
    </div></section>
  </div>

  const chain=sel?`Yazar ${sel.team.author.join(' → ')} · İnceleme ${sel.team.reviewer.join(' → ')}`:''
  // TB-20: a chat-like start: folder, first message, the composer's model picker; the title comes from the message (K1).
  // K-039: a round start button at the bar's right end, like Claude's send
  const startButton=<button className="btn primary send round" disabled={busy||!workspace.trim()||!task.trim()||!policy||!preview||!accepted||missingProvider(policy,preview.policy)}
    aria-label={busy?'Başlatılıyor':'Başlat'} title="Başlat">{busy?<LoaderCircle className="spin" aria-hidden/>:<ArrowUp aria-hidden/>}</button>
  return <form className="work-form new-chat" onSubmit={e=>{e.preventDefault();submit()}}>
    <div className="new-chat-head"><h1>Ne yapmak istiyorsun?</h1>
      <p className="intro">Bir klasör seç ve yaz. Yazar değişikliği yapar, inceleyici kontrol eder; sonuç burada sohbet gibi akar.</p></div>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    <div className="composer new-composer">
      <div className="folder-row">
        <label htmlFor="nw-workspace" className="sr-only">Çalışma klasörü</label>
        {folders?<select id="nw-workspace" className="mono" required value={workspace} onChange={e=>setWorkspace(e.target.value)}>
          <option value="">{folders.length?'Proje klasörü seç':'Önce Mac\'ten bir çalışma açın'}</option>
          {folders.map(f=><option key={f} value={f}>{f.split('/').pop()||f}</option>)}</select>:<>
        {!mobile.active&&<button type="button" className="btn small folder-pick" disabled={choosing} aria-busy={choosing} onClick={()=>void chooseFolder()}>
          {choosing?<LoaderCircle className="spin" aria-hidden/>:<FolderOpen aria-hidden/>}{workspace?'Değiştir':'Finder\'dan seç'}</button>}
        <input id="nw-workspace" className="mono" required value={workspace} onChange={e=>setWorkspace(e.target.value)} placeholder={mobile.active?'Klasör — /Users/…/Projem':'Klasör seçin ya da yolu yazın'}/>
        <button type="button" className="btn small" aria-expanded={fromGitHub} onClick={()=>setFromGitHub(v=>!v)}>GitHub'dan</button></>}
      </div>
      {fromGitHub&&<RepoPicker onClose={()=>setFromGitHub(false)} onPicked={path=>{setWorkspace(path);setFromGitHub(false)}}/>}
      <label htmlFor="nw-task" className="sr-only">İlk görev</label>
      <div className="slash-anchor"><textarea id="nw-task" required rows={5} maxLength={100000} value={task} onChange={e=>setTask(e.target.value)}
        onKeyDown={e=>{slash.onKeyDown(e)}} placeholder="Neyi tamamlamamızı istiyorsun? (/ ile kısayollar)"/>{slash.menu}</div>
      {policy&&settings!==undefined?<TeamPicker compact policy={policy} initial={defaultTeam(settings)??teamOf(policy.policy)} onReloadPolicy={loadPolicy}
        onChange={s=>{setSel(s);if(!s.preview)setAcceptedFor(null)}} trailing={startButton}/>:<p className="muted small">{policy||!error?'Yükleniyor…':'Politika yüklenemedi.'}</p>}
      {preview&&!byDefault&&<label className="accept-row confirm-line"><input type="checkbox" checked={accepted} onChange={e=>setAcceptedFor(e.target.checked?preview.digest:null)}/>
        <span>Bu çalışma şu takımla başlayacak: <span className="reaccept-chain">{chain}</span>. Takımı, otomatik kota devrini{preview.policy.loop?', döngü bütçesini':''} ve bağlam devam sınırını kabul ediyorum.</span></label>}
      <label className="accept-row"><input type="checkbox" checked={isolate} onChange={e=>setIsolate(e.target.checked)}/>
        <span><strong>Ayrı kopyada çalış</strong> (git deposu): ajanlar klasörün bir kopyasında, kendi dalında çalışır; klasörün kendisine dokunulmaz. Sonucu sonra “Ana klasöre getir” ya da GitHub ile alırsınız. Kaydedilmemiş değişiklikler kopyaya geçmez.</span></label>
      <details className="composer-more"><summary>Seçenekler</summary>
        <WaitOptions id="nw-wait" value={wait} onChange={setWait}/>
        <p className="chain-note">Yalnız doğrulanmış kota hatasında sıradaki sağlayıcı devralır. Her sağlayıcının kök denemesi bir kez açılır. Codex bağlamı tükenirse aynı adımda ortak en fazla {(preview?.policy??policy?.policy)?.max_context_continuations??0} ek oturum açılır; kota devri bu sayacı sıfırlamaz.</p>
        {loopLine(preview?.policy.loop)&&<p className="chain-note"><strong>{loopLine(preview?.policy.loop)}</strong></p>}
        <p className="hint">Klasör kilidi yalnız PromptBridge çalışmalarını kapsar; dış editörleri veya ajanları engellemez.</p>
      </details>
      <div className="composer-foot"><span className="muted small" title="Başlık ilk mesajdan; sonra değiştirilebilir">{task.trim()?`Başlık: ${deriveTitle(task)}`:''}</span>
        {!(policy&&settings!==undefined)&&startButton}</div>
    </div>
  </form>
}
