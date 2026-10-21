import { useEffect, useRef, type ReactNode } from 'react'
import { checkedFetch } from '../../api'
import { splitId, type Cap } from './team'

/** Confirmation dialog on the native <dialog>: showModal() keeps focus inside and makes the page inert,
 *  Escape closes (unless busy). React removes the element before close() runs, so the browser's own focus
 *  restore does not fire: the opener is refocused explicitly. */
export function Modal({label,onClose,busy,children}:{label:string;onClose:()=>void;busy?:boolean;children:ReactNode}){
  const ref=useRef<HTMLDialogElement>(null)
  useEffect(()=>{const d=ref.current,opener=document.activeElement as HTMLElement|null;d?.showModal()
    return()=>{d?.close();opener?.focus()}},[])
  return <dialog ref={ref} className="modal" aria-label={label} onCancel={e=>{e.preventDefault();if(!busy)onClose()}}>{children}</dialog>
}

export type PolicyBody = {roles:Record<string,{provider:string;profile:string}[]>;max_context_continuations?:number;loop?:{max_turns:number;max_active_minutes:number}}
export type Team = {author:string[];reviewer:string[]}
export type Availability = Record<string,{installed:boolean;auth:'authenticated'|'unauthenticated'|'unknown'|'model_unavailable';checked_at:number|null;
  health?:'ok'|'probe_cleanup_failed'}>
export type Policy = {policy:PolicyBody;digest:string;installed:Record<string,boolean>;
  ceiling?:{allowed:Team;max_context_continuations:number;loop:PolicyBody['loop']|null};availability?:Availability;
  capabilities?:Record<string,Cap&{profiles:string[];context_continuation:boolean}>}
/** Install state of an identity; identities the server did not list fall back to their tool. */
export const installedOf = (p:Policy, id:string) => p.installed[id] ?? p.installed[splitId(id)?.kind??id] ?? false
/** `catalog`: each link's status from the last catalog refresh (available | unverified | model_unavailable | unknown). */
export type TeamPreview = {policy:PolicyBody;digest:string;warnings:string[];catalog?:Record<string,string>}
export type TeamProblem = {role:'author'|'reviewer';provider:string;reason:string}
export const teamOf = (p:PolicyBody):Team => ({author:p.roles.author.map(i=>i.provider),reviewer:p.roles.reviewer.map(i=>i.provider)})
export const RECOVERED = 'recovered_by_human'
export const labels:Record<string,string> = {QUOTA:'Kota doldu',PENDING:'Bekliyor',COMPLETED:'Tamamlandı',QUEUED:'Sırada',RUNNING:'Çalışıyor',SUCCEEDED:'Tamamlandı',FAILED:'Hata',CANCELLED:'Durduruldu',RECOVERY_REQUIRED:'Kurtarma incelemesi gerekiyor',WAITING_APPROVAL:'Araç izni gerekiyor',WAITING_AUTH:'Hesap girişi gerekiyor',WAITING_CAPACITY:'Kota bekleniyor',WAITING_CONTEXT:'Bağlam incelemesi gerekiyor',WAITING_HUMAN:'Klasör değişti — karar gerekiyor',CONTEXT_EXHAUSTED:'Bağlam doldu',PAUSED:'Duraklatıldı'}
export const contextLabels:Record<string,string> = {context_continuation_disabled:'Bu çalışma için bağlam devamı kapalı.',context_continuation_exhausted:'Adımın ortak bağlam devam sınırına ulaşıldı.',context_prompt_too_large:'Hedef ve devam paketi boyut sınırına sığmıyor.'}
export const reasonLabels:Record<string,string> = {...contextLabels,[RECOVERED]:'İnsan onayıyla kurtarıldı — sahipli süreçler sonlandırıldı, klasör kilidi bırakıldı.',
  PROMPT_TOO_LARGE:'İstem 128 KiB sınırını aşıyor; hiçbir sağlayıcı çağrılmadı.',
  budget_exhausted:'Etkin süre bütçesi doldu; adım temiz biçimde durduruldu.'}
/** F3-01a resume gate reasons (backend resume_decision event). */
export const resumeReasons:Record<string,string> = {no_previous_run:'bu çalışmanın ilk yürütmesi',previous_run_not_succeeded:'önceki yürütme başarıyla bitmedi',
  first_provider_not_codex:'yazar zinciri Codex ile başlamıyor',policy_changed:'sağlayıcı politikası değişti',workspace_mismatch:'klasör farklı',
  no_completed_author_attempt:'önceki yürütmede tamamlanmış yazar denemesi yok',last_author_not_codex:'önceki yürütmenin son yazarı Codex değildi',
  no_provider_session:'önceki konuşma kimliği kayıtlı değil',codex_home_changed:'önceki konuşma PromptBridge’in ayrı Codex alanından (K-014) önce açıldı',author_attempt_unclean:'önceki yazar süreci temiz kapanmadı',
  other_writer:'arada bu klasörde başka bir PromptBridge çalışması yürüdü',baseline_missing:'önceki yürütmenin bitiş kaydı yok',
  snapshot_incomplete:'klasör tam taranamadı',snapshot_scope_mismatch:'klasör tarama kapsamı değişti',workspace_changed:'klasörde önceki yürütmeden sonra değişiklik var',
  retry_fresh:'insan yeniden denemesi her zaman taze başlar'}
export const promptFormats:Record<string,string> = {RESUME_AUTHOR:'önceki konuşmaya devam istemi',FRESH_AUTHOR:'taze yazar istemi',REVIEWER:'bağımsız inceleme istemi',
  CORRECTION:'aynı konuşmada düzeltme istemi',FRESH_CORRECTION:'taze düzeltme istemi'}
export const promptRejections:Record<string,string> = {mandatory_exceeds_limit:'Zorunlu bölümler (kurallar, hedef, güncel istek) sınırı aşıyor.',
  final_prompt_exceeds_limit:'Devir/devam eklendikten sonra son istem sınırı aşıyor.',history_metadata_exceeds_budget:'Geçmiş kayıtlarının özeti bile bütçeye sığmıyor.'}

export function Pill({status,recovered=false}:{status:string;recovered?:boolean}) {
  // K1: a human-recovered run is FAILED, but shown apart from ordinary failures.
  if(recovered&&status==='FAILED')return <span className="pill warn"><span className="d" aria-hidden="true"/>İnsan kurtardı — temizlendi</span>
  const tone = ['COMPLETED','SUCCEEDED'].includes(status)?'ok':status==='RUNNING'?'run':status==='FAILED'?'bad':['CONTEXT_EXHAUSTED','WAITING_CONTEXT','QUOTA','WAITING_CAPACITY','WAITING_APPROVAL','WAITING_AUTH','WAITING_HUMAN','RECOVERY_REQUIRED','PAUSED'].includes(status)?'warn':'idle'
  return <span className={'pill '+tone}><span className="d" aria-hidden="true"/>{labels[status]||status}</span>
}
// TB-06: `codex@is` keeps the codex badge colour; the full identity stays visible.
export function Provider({name}:{name:string}) {return <span className="prov"><span className={'g '+name.split(/[@/]/)[0]} aria-hidden="true">{name.slice(0,1).toUpperCase()}</span>{name}</span>}
export function Chains({policy,installed}:{policy:Policy['policy'];installed?:Policy['installed']}) {
  return <div className="chains">{Object.entries(policy.roles).map(([role,chain])=><div className="chain-row" key={role}>
    <span className={'chip '+role}>{role==='author'?'Yazar · yazma':'İnceleyici · salt okunur'}</span>
    <div className="flow">{chain.map((p,i)=><span className="flow" key={p.provider}>{i>0&&<span className="arr" aria-hidden="true">→</span>}<Provider name={p.provider}/>{installed&&!(installed[p.provider]??installed[splitId(p.provider)?.kind??''])&&<span className="pill bad">Kurulu değil</span>}</span>)}</div>
  </div>)}</div>
}
export const active = (s:string) => s === 'QUEUED' || s === 'RUNNING'
/** F3-04 (K-012): opt-in bounded quota wait, sent with a message and frozen with it (part of the payload hash). */
export type WaitChoice = {wait_on_quota:true;wait_deadline_hours:number;wait_max_attempts:number}
const WAIT_DEFAULT:WaitChoice = {wait_on_quota:true,wait_deadline_hours:4,wait_max_attempts:3}
const whole = (v:string,low:number,high:number) => Math.min(high,Math.max(low,Math.round(Number(v))||low))
export function WaitOptions({value,onChange,id}:{value:WaitChoice|null;onChange:(v:WaitChoice|null)=>void;id:string}){
  return <div className="wait-options">
    <label className="accept-row"><input type="checkbox" checked={!!value} onChange={e=>onChange(e.target.checked?WAIT_DEFAULT:null)}/> Kota dolarsa bekle ve otomatik yeniden dene</label>
    {value&&<div className="wait-limits">
      <label htmlFor={id+'-hours'}>Son süre (saat)<input id={id+'-hours'} type="number" min={1} max={24} value={value.wait_deadline_hours} onChange={e=>onChange({...value,wait_deadline_hours:whole(e.target.value,1,24)})}/></label>
      <label htmlFor={id+'-attempts'}>En fazla deneme<input id={id+'-attempts'} type="number" min={1} max={5} value={value.wait_max_attempts} onChange={e=>onChange({...value,wait_max_attempts:whole(e.target.value,1,5)})}/></label>
    </div>}
    <p className="chain-note">{value
      ?`İlk kota anından en fazla ${value.wait_deadline_hours} saat içinde en fazla ${value.wait_max_attempts} otomatik deneme (15 dk'dan başlayıp katlanan aralıklarla). Klasör beklerken değişirse otomatik devam etmez, size sorar. Kota sıfırlanması çalışmanın süreceğini garanti etmez.`
      :'Kapalı: kota dolunca yürütme bekler; yeniden denemeyi siz başlatırsınız.'}</p>
  </div>
}
/** Local, short date/time for plan moments (ISO UTC from the backend). */
export const when = (iso:string) => new Date(iso).toLocaleString('tr-TR',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})
export const missingProvider = (p:Policy, body:PolicyBody=p.policy) => Object.values(body.roles).some(chain=>chain.some(i=>!installedOf(p,i.provider)))
export async function api<T>(url:string, options?:RequestInit):Promise<T> {
  return (await checkedFetch(url,options)).json()
}
export const newId = () => crypto.randomUUID()

/** F3-02 loop outcomes (backend `session_loop_state.outcome`). */
export const outcomeLabels:Record<string,string> = {verified:'İnceleyici hedefi doğruladı',reviewer_blocked:'İnceleyici engel bildirdi',
  review_invalid:'İnceleme sonucu okunamadı',no_progress:'İlerleme yok: aynı bulgular, klasörde değişiklik görülmedi',
  turn_limit:'Tur sınırı doldu; inceleyici hâlâ değişiklik istiyor',budget_exhausted:'Etkin süre bütçesi doldu'}
export const verdictLabels:Record<string,string> = {pass:'Onay',changes_requested:'Değişiklik istendi',blocked:'Engel',invalid:'Okunamadı'}
export const invalidCodes:Record<string,string> = {no_block:'sonuç bloğu yok',multiple_blocks:'birden fazla blok',unclosed_block:'blok kapanmamış',
  trailing_text:'bloktan sonra metin var',too_large:'blok çok büyük',invalid_json:'JSON geçersiz',duplicate_key:'yinelenen alan',
  non_finite:'NaN/Infinity',schema:'şemaya uymuyor'}
export const loopLine = (loop?:{max_turns:number;max_active_minutes:number}) => loop
  ? `Döngü: en fazla ${loop.max_turns} tur · ${loop.max_active_minutes} dk etkin süre. İnceleyici onaylayınca biter; tamamlanabilen her turun son yazar çıktısı incelenir.`
  : null
export const duration = (ms:number) => { const s=Math.round(ms/1000); return s<60?`${s} sn`:`${Math.floor(s/60)} dk ${s%60} sn` }
