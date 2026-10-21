import { canInterrupt, mobile } from '../../mobile'
import { events } from '../../mobile'
import { useEffect, useState } from 'react'
import { ApiError } from '../../api'
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
const waitEnds:Record<string,string> = {max_retries_exceeded:'en fazla deneme sayısına ulaşıldı',retry_deadline_exceeded:'bekleme son süresi doldu',
  human_retry:'yeniden denemeyi siz başlattınız',workspace_changed:'klasör beklerken değişti',cancelled_by_user:'bekleme iptal edildi',
  recovery_required:'önceki sürecin temizliği doğrulanamadı; önce kurtarın',no_failed_step:'yeniden denenecek adım yok',
  session_not_waiting:'yürütme artık beklemiyordu',session_not_retryable:'yürütme yeniden denenebilir durumda değildi'}
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
const retryRefusals:Record<string,string> = {session_not_retryable:'Bu yürütme yeniden denenebilir durumda değil (durum değişmiş olabilir).',
  workspace_busy:'Klasör başka bir yürütmede kullanılıyor ya da kilitli; o bitince yeniden deneyin.',
  recovery_required:'Önceki sürecin temizliği doğrulanamadı; önce kurtarın.',no_failed_step:'Yeniden denenecek başarısız adım yok.',
  context_budget_exhausted:'Bu yürütme için kabul edilen bağlam devam hakkı bitti; yeniden deneme yeni hak açmaz.'}
const instructionReasons:Record<string,string> = {import_invalid_path:'import yolunda kontrol karakteri var',import_absolute:'mutlak import yolu kullanılamaz',import_home:'~ ile başlayan import kullanılamaz',
  import_outside_workspace:'import çalışma klasörünün dışına çıkıyor',symlink:'sembolik bağlantı kullanılamaz',not_regular_file:'hedef normal bir dosya değil',
  import_not_found:'import edilen dosya bulunamadı',unreadable:'dosya güvenle okunamadı',not_utf8:'dosya UTF-8 değil',depth_exceeded:'import derinliği 5 sınırını aşıyor',
  import_cycle:'import döngüsü bulundu',too_large:'proje talimatı 16 KiB sınırını aşıyor'}

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
  }catch(e){setError(e instanceof ApiError&&e.code==='wait_already_fired'?'Otomatik deneme az önce başladı; iptal için yürütmeyi durdurun.':String(e))}finally{setBusy(false)}}
  if(!detail)return error?<div className="error-banner" role="alert">{error}</div>:<p className="loading">Yürütme yükleniyor…</p>
  const status=detail.session.status, plan=detail.wait_plan
  const badInstructions=[detail.project_instructions,detail.agents_instructions].find(i=>i?.status==='rejected')
  const turnOf=(id:string)=>detail.turns?.find(t=>t.author_step_id===id||t.reviewer_step_id===id)?.turn_no
  const stepTitle=(s:Step)=>{const role=s.role==='author'?'Yazar':s.role==='reviewer'?'İnceleyici':s.agent_name,turn=turnOf(s.id)
    return turn?`Tur ${turn} · ${role}`:`${s.step_index+1}. ${role}`}
  return <div className={embedded?'run-view embedded':'run-view'}>
    {error&&<div className="error-banner" role="alert">{error}</div>}
    <div className="run-head">
      <div>{!embedded&&<h1>{detail.session.workspace_path.split('/').pop()||'Çalışma'}</h1>}
        <div className="sub"><Pill status={status} recovered={detail.steps.some(s=>s.terminal_reason===RECOVERED)}/>{connected&&<span className="live" role="status"><span className="dot"/>Canlı</span>}{embedded&&<span className="muted mono small">{detail.session.id}</span>}</div>
        {!embedded&&<p className="path">{detail.session.workspace_path}</p>}</div><span className="spacer"/>
      {canInterrupt()&&active(status)&&<button className="btn danger" disabled={busy||stopping} onClick={()=>void cancel()}>{stopping?'Durduruluyor…':'Durdur'}</button>}
    </div>
    {resume&&<p className={'resume-note '+(resume.decision==='RESUME'?'resume':'fresh')} role="status">{resume.decision==='RESUME'
      ?'Yazar, önceki başarılı yürütmenin Codex konuşmasına devam isteğiyle başlatıldı.'
      :`Yazar taze istemle başlatıldı — ${resumeReasons[resume.reason]||resume.reason}.`}</p>}
    {detail.loop&&<LoopView sessionId={sessionId} status={status} loop={detail.loop} turns={detail.turns||[]}
      onChanged={()=>{void api<Detail>(`/api/sessions/${sessionId}`).then(setDetail).catch(e=>setError(String(e)));onChange?.()}}
      onResumed={()=>{setNonce(n=>n+1);onChange?.()}}/>}
    {rejected&&<p className="notice" role="alert"><strong>İstem çok büyük — model çağrılmadı.</strong> {promptRejections[rejected.reason||'']||rejected.reason}{rejected.bytes&&rejected.limit?` (${rejected.bytes} / ${rejected.limit} bayt)`:''}</p>}
    {badInstructions&&<p className="notice" role="alert"><strong>Proje talimatı reddedildi — model çağrılmadı.</strong>{' '}
      {instructionReasons[badInstructions.reason||'']||badInstructions.reason}
      {badInstructions.path?` · ${badInstructions.path}`:''}
      {badInstructions.source_path?` (${badInstructions.source_path}${badInstructions.line?`:${badInstructions.line}`:''} kaynağından)`:''}.
      {' '}{badInstructions===detail.agents_instructions?'AGENTS.md':'CLAUDE.md'} dosyasını düzeltin, yeni bir mesaj gönderin ve bu başarısız yürütmeyi inceledikten sonra sırayı devam ettirin.</p>}
    {status==='RECOVERY_REQUIRED'&&<section className="card"><div className="card-h"><h2>Kurtarma gerekiyor</h2><span className="spacer"/>{recovery?.lock_retained&&<span className="pill warn">Kilit tutuluyor</span>}</div><div className="card-b">
      <p className="notice" role="status">İşlem otomatik yeniden çalıştırılmadı. Kurtarma, sahipli süreç grubunu sonlandırıp ölümü doğruladıktan sonra klasör kilidini bırakır.</p>
      {recovery?.owners.map(o=><div key={o.attempt_id} className="attempt"><span className="no mono">PID {o.pid}</span><span className="muted mono">PGID {o.pgid}</span><span className="spacer"/><span className={'pill '+(o.group_alive?'warn':'ok')}>{o.group_alive?'Süreç yaşıyor':'Süreç yok'}</span></div>)}
      {recovery&&!recovery.owners.length&&<p className="chain-note">Kayıtlı canlı süreç yok; kurtarma yalnız kilidi bırakır.</p>}
      {!!recoverResult?.blocking?.length&&<p className="notice" role="alert">Şu süreçler otomatik doğrulanamadı: {recoverResult.blocking.join(', ')}. Kilit korundu; OS düzeyinde manuel inceleme/temizleme gerekebilir.</p>}
      {!mobile.active&&<button className="btn danger" disabled={busy} onClick={()=>setConfirmRecover(true)}>Kurtar</button>}
    </div></section>}
    {confirmRecover&&<Modal label="Kurtarmayı onayla" busy={busy} onClose={()=>setConfirmRecover(false)}>
      <h3>Kurtarmayı onayla</h3>
      <p><strong>Bu çalışma klasöründeki tüm sahipli süreçler sonlandırılacak.</strong> Süreç grubu ölümü doğrulandıktan sonra klasör kilidi bırakılır ve çalışma başarısız olarak kapanır.</p>
      {!!recovery?.owners.length&&<p className="chain-note mono">Etkilenecek PID: {recovery.owners.map(o=>o.pid).join(', ')}</p>}
      <div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setConfirmRecover(false)}>Vazgeç</button><button className="btn danger" disabled={busy} onClick={()=>void recover()}>{busy?'Kurtarılıyor…':'Süreçleri sonlandır ve kurtar'}</button></div>
    </Modal>}
    {status==='WAITING_CONTEXT'&&<div className="notice" role="status">Otomatik devam durdu; inceleme gerekli. {detail.steps.map(s=>contextLabels[s.terminal_reason||'']).filter(Boolean).join(' ')} {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':'Yeniden dene'}</button>} {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>Durdur</button>}</div>}
    {status==='WAITING_CAPACITY'&&plan?.status==='PENDING'?<div className="notice" role="status"><strong>Kota bekleniyor — otomatik yeniden deneme planlı.</strong>{' '}
      Deneme {plan.attempts_so_far} / {plan.max_attempts} · sonraki deneme {when(plan.next_retry_at)} · son süre {when(plan.deadline)}.{' '}
      {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':'Şimdi dene'}</button>}{' '}
      {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancelWait()}>Beklemeyi iptal et</button>}</div>
    :['WAITING_CAPACITY','WAITING_AUTH'].includes(status)&&<div className="notice" role="status">{labels[status]}. Otomatik devam yok; yeniden denemeyi siz başlatırsınız.{plan&&plan.closed_reason&&status==='WAITING_CAPACITY'?` Otomatik bekleme bitti: ${waitEnds[plan.closed_reason]||plan.closed_reason}.`:''} {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':'Yeniden dene'}</button>} {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>Durdur</button>}</div>}
    {status==='WAITING_HUMAN'&&<div className="notice" role="alert"><strong>Klasör kota beklenirken değişti — otomatik devam durdu.</strong>{' '}
      {changedPaths.length?`Değişen: ${changedPaths.slice(0,5).join(', ')}${changedPaths.length>5?` ve ${changedPaths.length-5} dosya daha`:''}. `:''}Değişiklikleri inceleyin; devam etmek için yeniden deneyin ya da yürütmeyi durdurun.{' '}
      {!mobile.active&&<button className="btn" disabled={busy} aria-busy={busy} onClick={()=>void retry()}>{busy?'…':'Yeniden dene'}</button>}{' '}
      {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>Durdur</button>}</div>}
    {status==='FAILED'&&plan?.status==='EXPIRED'&&<p className="notice" role="status">Otomatik bekleme bitti: {waitEnds[plan.closed_reason||'']||plan.closed_reason} ({plan.attempts_so_far} / {plan.max_attempts} deneme).</p>}
    {status==='WAITING_APPROVAL'&&<div className="notice" role="status">{labels.WAITING_APPROVAL}. Sağlayıcı bir aracı reddetti; bu yürütme kendiliğinden devam etmez. Durdurup yeni mesajla yeniden başlatın. {canInterrupt()&&<button className="btn danger" disabled={busy} onClick={()=>void cancel()}>Durdur</button>}</div>}
    {!embedded&&detail.policy&&<section className="card"><div className="card-h"><h2>Sağlayıcı zinciri</h2><span className="spacer"/><span className="accepted">Bu çalışma için donduruldu</span></div><div className="card-b"><Chains policy={detail.policy}/></div></section>}
    <section className="card"><div className="card-h"><h2>Çalışma akışı</h2><span className="spacer"/><span className="chain-note" role="status">{detail.steps.filter(s=>s.status==='COMPLETED').length} / {detail.steps.length} adım tamamlandı</span></div>
    <div className="card-b timeline">{!detail.steps.length&&<p className="muted">Adımlar başlatılmadı.</p>}{detail.steps.map(s=><section className={`step ${s.status==='COMPLETED'?'done':s.status==='RUNNING'?'active':''}`} key={s.id}>
      <span className="node" aria-hidden="true">{s.status==='COMPLETED'?'✓':s.step_index+1}</span>
      <div className="step-head"><h3 className="t">{stepTitle(s)}</h3>{s.role&&<span className={'chip '+s.role}>{s.role==='author'?'Yazma':'Salt okunur'}</span>}<span className="spacer"/><Pill status={s.status} recovered={s.terminal_reason===RECOVERED}/></div>
      <div className="attempts">{detail.attempts.filter(a=>a.step_id===s.id).map(a=>{const meta=promptMeta[a.id];return <div className="attempt" key={a.id}><span className="no">Deneme {a.attempt_no}</span><Provider name={a.provider}/><span className="muted">{a.profile==='WRITE'?'Yazma':'Salt okunur'}</span>
        {meta?.format&&<span className="muted">· {promptFormats[meta.format]||meta.format}{meta.history?.truncated?' · geçmiş kısaltıldı':''}</span>}
        <span className="spacer"/><Pill status={a.status} recovered={a.terminal_reason===RECOVERED}/>{a.error_code&&<code className="attempt-code">{a.error_code}</code>}</div>})}</div>
      {noChanges.has(s.id)&&<div className="handover"><strong>Değişiklik yok</strong><span>Yerel model bu adımda klasörde hiçbir dosyayı değiştirmedi; çıktıyı kontrol edin.</span></div>}
      {handoffs.filter(h=>h.step_id===s.id).map(h=><div key={h.id} className="handover"><strong>Devir</strong><span>{h.from} → {h.to}</span><span>· {h.reason==='QUOTA_EXCEEDED'?'Doğrulanmış kota sınırı':h.reason}</span></div>)}
      <p className="chain-note">Bağlam devamı: {detail.attempts.filter(a=>a.step_id===s.id&&a.continuation_no>0&&a.handover_reason!=='command_denied_followup').length} / {detail.policy?.max_context_continuations??0} · adım toplamı</p>
      {continuations.filter(c=>c.step_id===s.id).map(c=><div key={c.id} className="handover"><strong>{startedAttempts.has(c.to_attempt_id)?'Yeni oturumda devam':'Bağlam devamı hazırlandı'}</strong><span>{c.provider} · {c.step_continuations_used}/{c.max_context_continuations}</span></div>)}
      {s.result_text&&<details className="step-out" open={s.status==='FAILED'}><summary>Adım sonucu</summary><pre>{s.result_text}</pre></details>}
      {s.terminal_reason&&!['completed','cancelled_by_user'].includes(s.terminal_reason)&&<p className="step-out" role="status">{reasonLabels[s.terminal_reason]||s.terminal_reason}</p>}
    </section>)}</div></section>
    <section className="card"><div className="card-h"><h2>Olay akışı</h2><span className="spacer"/><span className="chain-note">{connected?'Canlı güncelleniyor':'Kaydedilen olaylar'}</span></div>
    <div className="feed">{!feed.length&&<p className="card-b muted">Henüz çıktı veya komut olayı yok.</p>}{feed.map(e=>{
      const provider=detail.attempts.find(a=>a.id===e.attempt_id)?.provider
      return <article className={'evt '+(e.kind==='handoff'?'feed-handover':'')} key={e.id}>
        <span className={'av '+(e.kind==='handoff'?'sys':provider||'sys')} aria-hidden="true">{e.kind==='handoff'?'↗':provider?.slice(0,1).toUpperCase()||'·'}</span>
        <div className="event-body"><div className="who">{e.kind==='handoff'?'Sağlayıcı devri':provider||'Sağlayıcı'}<span>{e.kind==='command_result'?'Komut sonucu':e.kind==='output_delta'?'Çıktı':''}</span></div>
        {e.kind==='command_result'?<><pre className="tool">{e.text}</pre><span className="chain-note">Çıkış kodu: {e.exitCode??'Bilinmiyor'}</span>{e.output&&<details><summary>Komut çıktısı</summary><pre className="tool">{e.output}</pre></details>}</>:<p className="msg">{e.text}</p>}</div>
      </article>
    })}</div><p className="feed-note">Son 200 çıktı/komut bloğu gösterilir. Komutlar yalnız sağlayıcının kaydettiği olaylarda görünür.</p></section>
  </div>
}
