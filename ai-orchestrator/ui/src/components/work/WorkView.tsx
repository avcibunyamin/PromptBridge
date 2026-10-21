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

const blockers:Record<string,string> = {
  run_active:'Bir yürütme sürüyor. Yeni mesajlar sıraya alınır ve o bitince başlar; çalışan ajana iletilmez.',
  run_waiting:'Son yürütme bekliyor (kota, hesap girişi, bağlam ya da beklerken değişen klasör). Sıradaki mesajlar bekler; ilerlemek için aşağıdaki yürütmedeki kontrolleri kullanın (otomatik bekleme varsa kendisi yeniden dener).',
  recovery_required:'Son yürütme kurtarma bekliyor. Sıradaki mesajlar bekler; aşağıdaki yürütmedeki “Kurtar” kontrolünü kullanın.',
  paused:'Son yürütme duraklatıldı. Sıradaki mesajlar ve arşivleme bekler; aşağıdaki yürütmeden devam edin ya da durdurun.',
}
const codes:Record<string,string> = {
  revision_conflict:'Çalışma başka bir yerde değişti; güncel hâli yüklendi, tekrar deneyin.',
  work_active:'Aktif, bekleyen veya kurtarma gerektiren yürütme varken arşivlenemez.',
  work_archived:'Çalışma arşivde; yeni mesaj için önce arşivden çıkarın.',
  message_linked:'Mesaj zaten başlatıldı; iptal için yürütmenin “Durdur” kontrolünü kullanın.',
  stale_session:'Durum değişti; güncel hâli yüklendi.',
  stale_outcome:'Yürütmenin sonucu değişti; güncel hâli yüklendi.',
  outcome_missing:'Döngünün sonucu kayıtlı değil; sıra bu yürütmeden sonra açılamaz. Durumu yeniden yükleyin.',
  already_continued:'Bu yürütmeden sonra sıra zaten açılmış; güncel hâli yüklendi.',
  paused:'Yürütme duraklatılmış; önce devam edin ya da durdurun.',
  conflict:'Sağlayıcı politikası değişti. Güncel zinciri inceleyip yeniden kabul edin.',
  team_invalid:'Seçilen takım artık izinli değil. Takım kartındaki uyarılara bakıp yeniden seçin.',
  recipe_unknown:'Seçilen tarif tanınmıyor.',
  mobile_field:'Bu işlem telefondan yapılamaz.',
}
const UNCERTAIN = 'Gönderimin sonucu bilinmiyor (bağlantı veya sunucu hatası). Aynı kimlikle yeniden deneyin; mesaj iki kez oluşmaz.'
const say = (e:unknown) => e instanceof ApiError && e.code && codes[e.code] ? codes[e.code] : String(e)

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
    setWork(null);setMessages([]);setError('');setEditing(false);setAcceptedFor(null);setSel(null);setConfirmContinue(false);setText('')
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
    if(r==='kept'){setError('Sunucuya ulaşılamadı; kayıt korunuyor. Bağlantı gelince aynı mesajı yeniden deneyin ya da tekrar silmeyi deneyin.');return}
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
      setWork(updated);setModalTitle(updated.title);setSettingsNotice('Çalışma adı kaydedildi.')
      await refresh();onChange()
    }catch(e){setSettingsError(say(e));await refresh()}finally{setSettingsBusy(false)}
  }
  async function saveWorkTeam(){
    const chosen=modalTeam?.team??(policy&&work?initialWorkTeam(workId,settings,work.latest_team,teamOf(policy.policy),firstTeam):null)
    if(!chosen)return
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await saveSettings({work_teams:putWorkTeam(settings?.settings.work_teams,workId,chosen)})
      setSettings(updated);setSettingsNotice('Bu çalışmanın takımı kaydedildi.')
    }catch(e){setSettingsError(say(e))}finally{setSettingsBusy(false)}
  }
  async function resetWorkTeam(){
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await saveSettings({work_teams:putWorkTeam(settings?.settings.work_teams,workId,null)})
      setSettings(updated);setModalTeam(null);setModalTeamKey(k=>k+1);setSettingsNotice('Varsayılan takıma dönüldü.')
    }catch(e){setSettingsError(say(e))}finally{setSettingsBusy(false)}
  }
  async function saveWorkWait(){
    setSettingsBusy(true);setSettingsError('');setSettingsNotice('')
    try{
      const updated=await saveSettings({work_waits:putWorkWait(settings?.settings.work_waits,workId,modalWait)})
      setSettings(updated);setWait(modalWait);setSettingsNotice('Kota bekleme ayarı kaydedildi.')
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
    {name:'model',hint:'Yazarın modelini seç',run:()=>openSelect('Yazar model','Yazar sağlayıcı')},
    {name:'efor',hint:'Yazarın eforunu seç',run:()=>openSelect('Yazar efor','Yazar model','Yazar sağlayıcı')},
    {name:'kota',hint:'Context ve kota penceresi',run:click('.composer .usage-ring')},
    {name:'takim',hint:'Gelişmiş: yedekler ve inceleyici',run:click('.composer .composer-tools')},
    ...RECIPES.map(r=>({name:'tarif-'+r.id,hint:'Tarif: '+r.title,run:()=>setRecipe(r.id)})),
    ...(!mobile.active&&work&&!work.archived_at?[
      {name:'onizleme',hint:'Projenin önizlemesi',run:()=>setShowPreview(true)},
      {name:'testler',hint:'İncelemeden önceki test ve lint',run:()=>setShowTests(true)},
      ...(between?[{name:'github',hint:'Kaydet, gönder, taslak PR',run:()=>setShowGitHub(true)}]:[])]:[]),
    {name:'yeni',hint:'Yeni çalışma',run:()=>{location.hash='#/new'}},
  ]
  const slash=useSlash(text,setText,commands)
  if(!work)return error?<div className="error-banner" role="alert">{error}</div>:<p className="loading">Çalışma yükleniyor…</p>
  const running=work.blocked_reason==='run_active'
  const preview=sel?.preview??null
  const cannotSend=!policy||!preview||missingProvider(policy,preview.policy)
  const mustAccept=!!preview&&chainNeedsAcceptance(preview.digest,work.latest_policy_digest,acceptedDigest(settings??null))
  const accepted=!!preview&&acceptedFor===preview.digest
  const chainLine=(t:Team)=>`Yazar ${t.author.join(' → ')} · İnceleyici ${t.reviewer.join(' → ')}`
  const reloadPolicy=async()=>{setPolicy(await api<Policy>('/api/sessions/providers'))}
  const lastSeq=messages.length?messages[messages.length-1].seq:null
  const savedWorkTeam=workSavedTeam(settings,workId)
  const selectedRecipe=recipe?RECIPES.find(r=>r.id===recipe)??null:null
  // K-039: a round send button at the bar's right end, like Claude's
  const sendButton=<button className="btn primary send round" aria-busy={busy} disabled={busy||!!pending||!text.trim()||cannotSend||(mustAccept&&!accepted)}
    aria-label={busy?'Gönderiliyor':running?'Sıraya ekle':'Gönder'} title={running?'Sıraya ekle (Ctrl/⌘+Enter)':'Gönder (Ctrl/⌘+Enter)'}>
    {busy?<LoaderCircle className="spin" aria-hidden/>:<ArrowUp aria-hidden/>}</button>
  return <div className="work-view">
    <div className="run-head"><div>
      {editing?<form className="title-edit" onSubmit={e=>{e.preventDefault();void saveTitle()}}>
        <label htmlFor="work-title" className="sr-only">Başlık</label>
        <input id="work-title" value={title} maxLength={200} autoFocus onChange={e=>setTitle(e.target.value)}/>
        <button className="btn primary" disabled={busy||!title.trim()}>Kaydet</button><button type="button" className="btn" onClick={()=>setEditing(false)}>Vazgeç</button>
      </form>:<h1>{work.title} {!mobile.active&&<button className="icon-btn inline" aria-label="Başlığı düzenle" title="Başlığı düzenle" onClick={()=>{setTitle(work.title);setEditRevision(work.revision);setEditing(true)}}><Pencil size={14} aria-hidden/></button>}</h1>}
      <div className="sub">{work.latest_session?<Pill status={work.latest_session.status}/>:<span className="pill idle"><span className="d" aria-hidden="true"/>Henüz yürütme yok</span>}
        {work.account_wait&&(work.account_wait.holder_work_id
          ?<a className="pill warn" href={`#/works/${encodeURIComponent(work.account_wait.holder_work_id)}`} onClick={()=>{location.hash=`#/works/${encodeURIComponent(work.account_wait!.holder_work_id!)}`}}><span className="d" aria-hidden="true"/>{work.account_wait.account} bekliyor · {work.account_wait.holder_work_title||'başka bir çalışma'} kullanıyor</a>
          :<span className="pill warn"><span className="d" aria-hidden="true"/>{work.account_wait.account} bekliyor · {work.account_wait.holder_work_title||'başka bir çalışma'} kullanıyor</span>)}
        {work.blocked_reason==='needs_review_decision'&&<span className="pill warn"><span className="d" aria-hidden="true"/>Hedef doğrulanmadı</span>}
        {work.pending_messages>0&&<span className="pill idle">{work.pending_messages} mesaj sırada</span>}
        {work.archived_at&&<span className="pill idle">Arşivde</span>}
        {live?<span className="live"><span className="dot"/>Canlı</span>:<span className="muted small">Bağlantı yok — yeniden deneniyor</span>}</div>
      <p className="path">{work.workspace_path}</p>
      {work.isolation&&<p className="muted small">Ayrı kopya{work.isolation.removed?' (kaldırıldı)':''} · dal <span className="mono">{work.isolation.branch}</span> · kaynak <span className="mono">{work.isolation.source}</span></p>}</div><span className="spacer"/>
      {!mobile.active&&work.isolation&&<button className="btn" disabled={!!work.blocked_reason&&['run_active','run_waiting','recovery_required','paused'].includes(work.blocked_reason)}
        title="Kopyadaki değişiklikleri ana klasöre birleştir ya da kopyayı kaldır (yürütmeler arasında)" onClick={()=>setShowCopy(true)}>Ana klasöre getir</button>}
      {!mobile.active&&!work.archived_at&&<button className="btn" aria-expanded={showPreview} onClick={()=>setShowPreview(v=>!v)}>{showPreview?'Önizlemeyi kapat':'Önizleme'}</button>}
      {!mobile.active&&!work.archived_at&&<button className="btn" disabled={!!work.blocked_reason&&['run_active','run_waiting','recovery_required','paused'].includes(work.blocked_reason)}
        title="Değişiklikleri kaydet, GitHub'a gönder, taslak PR aç (yürütmeler arasında)" onClick={()=>setShowGitHub(true)}>GitHub</button>}
      {!mobile.active&&!work.archived_at&&<button className="btn" title="İncelemeden önce korumalı alanda çalışacak test ve lint komutları" onClick={()=>setShowTests(true)}>Testler</button>}
      {!mobile.active&&<button className="btn" disabled={busy} onClick={()=>void toggleArchive()}>{work.archived_at?'Arşivden çıkar':'Arşivle'}</button>}
      {canCommand()&&<button type="button" className="btn" onClick={openWorkSettings}><Settings2 size={14} aria-hidden/> Ayarlar</button>}
    </div>
    {/* With a composer the error is shown next to it (sticky, so a failed send is visible where the text is kept). */}
    {error&&work.archived_at&&<div className="error-banner" role="alert">{error}</div>}
    {!mobile.active&&!work.archived_at&&showPreview&&<PreviewPanel workspacePath={work.workspace_path}/>}
    {!mobile.active&&showCopy&&work.isolation&&<IsolationPanel work={work} onClose={()=>{setShowCopy(false);void refresh()}}/>}
    {!mobile.active&&showTests&&<TestsPanel workId={workId} onClose={()=>setShowTests(false)}/>}
    {!mobile.active&&showGitHub&&<GitHubPanel workId={workId} title={work.title} onClose={()=>{setShowGitHub(false);void refresh()}}/>}
    {showWorkSettings&&canCommand()&&<Modal label="Çalışma ayarları" busy={busy||settingsBusy} onClose={closeWorkSettings}>
      <h3>Çalışma ayarları</h3>
      {settingsError&&<div className="error-banner" role="alert">{settingsError}</div>}
      {settingsNotice&&<p className="info-note" role="status">{settingsNotice}</p>}
      <section className="settings-section">
        <h4>Ad</h4>
        <form className="title-edit" onSubmit={e=>{e.preventDefault();void saveModalTitle()}}>
          <label htmlFor="work-settings-title" className="sr-only">Çalışma adı</label>
          <input id="work-settings-title" value={modalTitle} maxLength={200} onChange={e=>setModalTitle(e.target.value)}/>
          <button type="submit" className="btn primary" disabled={busy||settingsBusy||!modalTitle.trim()}>Kaydet</button>
        </form>
      </section>
      <section className="settings-section">
        <h4>Bu çalışmanın takımı</h4>
        <p className="muted small">{savedWorkTeam?'Bu çalışma için özel takım kayıtlı.':'Bu çalışma için özel takım kayıtlı değil; son mesajın takımı veya genel varsayılan kullanılır.'}</p>
        {policy&&<TeamPicker key={`modal-${workId}-${modalTeamKey}`} policy={policy}
          initial={modalTeam?.team??initialWorkTeam(workId,settings,work.latest_team,teamOf(policy.policy),firstTeam)}
          onReloadPolicy={reloadPolicy} onChange={setModalTeam}/>}
        <div className="modal-actions">
          <button type="button" className="btn primary" disabled={busy||settingsBusy||!policy||!!modalTeam?.invalid} onClick={()=>void saveWorkTeam()}>Bu çalışma için kaydet</button>
          <button type="button" className="btn" disabled={busy||settingsBusy||!savedWorkTeam} onClick={()=>void resetWorkTeam()}>Varsayılana dön</button>
        </div>
      </section>
      {!mobile.active&&<section className="settings-section">
        <h4>Kota bekleme</h4>
        <WaitOptions id="work-settings-wait" value={modalWait} onChange={setModalWait}/>
        <div className="modal-actions">
          {!mobile.active&&<button type="button" className="btn primary" disabled={busy||settingsBusy} onClick={()=>void saveWorkWait()}>Bu çalışma için kaydet</button>}
        </div>
      </section>}
      {!mobile.active&&work.isolation&&<section className="settings-section">
        <h4>Ayrı kopya</h4>
        <p className="muted small">Dal: <span className="mono">{work.isolation.branch}</span> · Kaynak: <span className="mono">{work.isolation.source}</span> · Durum: {work.isolation.removed?'Kaldırıldı':'Etkin'}</p>
        <div className="modal-actions">
          {!mobile.active&&<button type="button" className="btn" disabled={!between} onClick={()=>{closeWorkSettings();setShowCopy(true)}}>Ana klasöre getir</button>}
        </div>
      </section>}
      {!mobile.active&&<section className="settings-section">
        <h4>Kısayollar</h4>
        <div className="modal-actions">
          {!mobile.active&&!work.archived_at&&<button type="button" className="btn" onClick={()=>{closeWorkSettings();setShowTests(true)}}>Testler</button>}
          {!mobile.active&&!work.archived_at&&<button type="button" className="btn" aria-expanded={showPreview} onClick={()=>{closeWorkSettings();setShowPreview(v=>!v)}}>{showPreview?'Önizlemeyi kapat':'Önizleme'}</button>}
          {!mobile.active&&!work.archived_at&&<button type="button" className="btn" disabled={!between} onClick={()=>{closeWorkSettings();setShowGitHub(true)}}>GitHub</button>}
        </div>
      </section>}
      {!mobile.active&&<section className="settings-section">
        <h4>Tehlikeli bölge</h4>
        <div className="modal-actions">
          {!mobile.active&&<button type="button" className="btn" disabled={busy||settingsBusy} onClick={()=>void toggleArchive()}>{work.archived_at?'Arşivden çıkar':'Arşivle'}</button>}
          {!mobile.active&&!confirmDelete&&<button type="button" className="btn danger" disabled={busy||settingsBusy||!between} onClick={()=>setConfirmDelete(true)}>Sil</button>}
        </div>
        {!mobile.active&&confirmDelete&&<div className="confirm-delete">
          <p className="notice"><strong>{work.title}</strong> çalışmasını silmek istediğinize emin misiniz?</p>
          <div className="modal-actions">
            {!mobile.active&&<button type="button" className="btn" disabled={busy||settingsBusy} onClick={()=>setConfirmDelete(false)}>Vazgeç</button>}
            {!mobile.active&&<button type="button" className="btn danger" disabled={busy||settingsBusy} onClick={()=>void doDeleteWork()}>Silmeyi onayla</button>}
          </div>
        </div>}
      </section>}
      <div className="modal-actions">
        <button type="button" className="btn" disabled={busy||settingsBusy} onClick={closeWorkSettings}>Kapat</button>
      </div>
    </Modal>}
    {work.blocked_reason==='needs_continue'&&<section className="card blocked"><div className="card-h"><h2>Sıra durdu</h2></div><div className="card-b">
      <p>Son yürütme <strong>{labels[work.latest_session?.status||'']||work.latest_session?.status}</strong> ile bitti. Sıradaki mesajlar, siz bu sonucu inceleyip onaylamadan başlamaz.</p>
      {!mobile.active&&<button className="btn" disabled={busy} onClick={()=>setConfirmContinue(true)}>Sonucu inceledim, sıradakine geç</button>}
    </div></section>}
    {work.blocked_reason==='needs_review_decision'&&<section className="card blocked"><div className="card-h"><h2>Sıra durdu — hedef doğrulanmadı</h2></div><div className="card-b">
      <p>Son yürütme temiz bitti ama inceleyici hedefi doğrulamadı: <strong>{outcome?outcomeLabels[outcome]||outcome:'sonuç yükleniyor…'}</strong>. Sıradaki mesajlar, siz sonucu inceleyip onaylamadan başlamaz.</p>
      {!mobile.active&&<button className="btn" disabled={busy||!outcome} onClick={()=>setConfirmContinue(true)}>Sonucu inceledim, sıradakine geç</button>}
    </div></section>}
    {work.blocked_reason&&blockers[work.blocked_reason]&&<p className={running?'info-note':'notice'} role="status">{blockers[work.blocked_reason]}</p>}
    {confirmContinue&&<Modal label="Sıradakine geç" busy={busy} onClose={()=>setConfirmContinue(false)}>
      <h3>Sıradaki mesaja geç</h3>
      <p>Başarısız/durdurulmuş yürütme olduğu gibi kalır; yeniden çalıştırılmaz. Sıradaki mesaj ayrı ve taze bir yürütme olarak başlar.</p>
      <div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setConfirmContinue(false)}>Vazgeç</button><button className="btn primary" disabled={busy} onClick={()=>void doContinue()}>Onayla</button></div>
    </Modal>}

    <ol className="messages" aria-label="Mesajlar">
      {truncated&&<li className="muted small">En eski 2000 mesaj gösteriliyor.</li>}
      {!messages.length&&!pending&&<li className="empty-list">Henüz mesaj yok.{canCommand()?' İlk görevi aşağıdan gönderin.':''}</li>}
      {messages.map(m=><li key={m.id} className="message chat">
        <div className="bubble">
          <p className="message-text">{m.text}</p>
          <div className="bubble-meta"><span className="seq mono">#{m.seq}</span><span>{new Date(m.created_at).toLocaleString('tr-TR')}</span>
            {m.recipe&&<span className="pill idle">Tarif: {m.recipe.title} v{m.recipe.version}</span>}
            {m.status==='PENDING'&&<><span className="pill idle"><span className="d" aria-hidden="true"/>Sonraki yürütme için sırada</span>{canInterrupt()&&<button className="btn small" disabled={busy} onClick={()=>void cancelQueued(m)}>Sıradan çıkar</button>}</>}
            {m.status==='CANCELLED'&&<span className="pill idle">İptal edildi — hiç çalıştırılmadı</span>}</div>
        </div>
        {m.session&&<div className="run-card">
          <div className="run-card-head"><span className="who">PromptBridge</span><Pill status={m.session.status}/>
            <span className="run-line">{runLine(m.team,short)}</span>{m.session.tests&&<TestPill run={m.session.tests}/>}{m.session.lint&&<TestPill run={m.session.lint} check="lint"/>}<span className="spacer"/>
            <button className="btn small ghost" aria-expanded={isOpen(m.id)} onClick={()=>toggle(m.id)}>{isOpen(m.id)?'Ayrıntıları gizle':'Ayrıntılar'}</button></div>
          {!!m.session.git_changed?.length&&<p className="notice" role="alert"><strong>Bu yürütme deponun git kancalarına/ayarına dokundu:</strong> <span className="mono">{m.session.git_changed.join(', ')}</span>. Bu klasörde kendi terminalinden git komutu çalıştırmadan önce bu dosyaları kontrol et; git bunları senin kimliğinle çalıştırır. (PromptBridge'in GitHub adımları kancaları çalıştırmaz.)</p>}
          {isOpen(m.id)&&<SessionView sessionId={m.session.id} status={m.session.status} embedded onChange={()=>{void refresh();onChange()}}/>}
        </div>}
      </li>)}
      {pending&&<li className="message pending">
        <div className="message-head"><span className="pill warn"><span className="d" aria-hidden="true"/>{busy?'Gönderiliyor…':'Gönderim sonucu bilinmiyor'}</span></div>
        <p className="message-text">{pending.text}</p>
        {uncertain&&!busy&&<div className="pending-actions">
          <p className="hint">Bu mesaj, sunucu onaylayana kadar bu tarayıcıda yerel olarak saklanır ve aynı kimlikle yeniden denenir; metni değiştirilemez.</p>
          <button className="btn primary" onClick={()=>void send(pending)}>Aynı mesajı yeniden dene</button>
          <button className="btn" onClick={()=>void discardPending(pending)}>Yerel kaydı sil</button>
        </div>}
      </li>}
    </ol>

    {!canCommand()?<p className="muted small">Bu telefon yalnız izler ve dosya onaylar. Görev vermek için Mac'te Ayarlar → Mobil erişim → Cihazlar → “Görev verebilir”.</p>:work.archived_at?<p className="notice">Bu çalışma arşivde. Yeni mesaj göndermek için arşivden çıkarın.</p>:
    <form className="composer" aria-label={messages.length?'Takip mesajı':'İlk görev'} onSubmit={e=>{e.preventDefault();submit()}}>
      {(lastSeq!==null||work.pending_messages>0)&&<div className="composer-context">
        {lastSeq!==null&&<span className="muted small">#{lastSeq} sonrası</span>}
        {work.pending_messages>0&&<span className="pill idle">{work.pending_messages} sırada</span>}
      </div>}
      {error&&<div className="error-banner" role="alert">{error}</div>}
      {mustAccept&&policy&&preview&&<div className="reaccept"><p><strong>{work.latest_policy_digest?'Takım veya sağlayıcı politikası bu çalışmanın son mesajından farklı.':'Bu mesaj için takım:'}</strong> <span className="reaccept-chain">{sel&&chainLine(sel.team)}</span></p>
        {loopLine(preview.policy.loop)&&<p className="chain-note">{loopLine(preview.policy.loop)}</p>}
        <label className="accept-row"><input type="checkbox" checked={accepted} onChange={e=>setAcceptedFor(e.target.checked?preview.digest:null)}/> Bu mesaj bu takımla gönderilsin; kabul ediyorum.</label></div>}
      {policy&&preview&&missingProvider(policy,preview.policy)&&<p className="notice">Takımdaki bir sağlayıcı kurulu değil; yeni mesaj gönderilemez.</p>}
      {selectedRecipe&&<div className="composer-context">
        <span className="pill idle">Tarif: {selectedRecipe.title} <button type="button" className="icon-btn inline" disabled={!!pending} aria-label="Tarifi kaldır" title="Tarifi kaldır" onClick={()=>setRecipe(null)}>×</button></span>
      </div>}
      <div className="composer-input">
        <label htmlFor="composer" className="sr-only">{messages.length?'Takip mesajı':'İlk görev'}</label>
        <textarea id="composer" rows={3} value={text} disabled={!!pending} aria-describedby="composer-hint" onChange={e=>setText(e.target.value)}
          onKeyDown={e=>{if(slash.onKeyDown(e))return;if(e.key==='Enter'&&(e.metaKey||e.ctrlKey)){e.preventDefault();submit()}}}
          placeholder={running?'Sıraya eklenecek sonraki istek… (/ ile kısayollar)':messages.length?'Sonraki istek… (/ ile kısayollar)':'Neyi tamamlamamızı istiyorsun?'}/>
        {slash.menu}
        {!(policy&&settings!==undefined)&&sendButton}
      </div>
      {policy&&settings!==undefined&&<TeamPicker key={`${workId}:${JSON.stringify(savedWorkTeam)}`} compact policy={policy}
        initial={initialWorkTeam(workId,settings,work.latest_team,teamOf(policy.policy),firstTeam)}
        startOpen={!work.latest_team&&!savedWorkTeam&&!!firstTeam} onReloadPolicy={reloadPolicy} onChange={s=>{setSel(s);if(!s.preview)setAcceptedFor(null)}}
        sessionId={work.latest_session?.id} sessionStatus={work.latest_session?.status} trailing={sendButton}/>}
      <div className="composer-foot">
        <details className="composer-more"><summary>Kota bekleme: {wait?'açık':'kapalı'}</summary><WaitOptions id="composer-wait" value={wait} onChange={setWait}/></details>
        <p className="hint" id="composer-hint">{running?'Mesaj sıraya girer; aktif yürütme bitince ayrı bir yürütme olarak başlar.':(preview?.policy??policy?.policy)?.loop?'Mesaj yazar ↔ inceleyici döngüsü başlatır.':'Mesaj yazar → inceleyici → yazar yürütmesi başlatır.'} {loopLine((preview?.policy??policy?.policy)?.loop)} Ctrl/⌘+Enter ile gönderin.</p>
      </div>
    </form>}
  </div>
}
