import { useEffect, useRef, type MouseEvent, type ReactNode } from 'react'
import { checkedFetch } from '../../api'
import { locale, t } from '../../i18n'
import { splitId, type Cap } from './team'

/** Confirmation dialog on the native <dialog>: showModal() keeps focus inside and makes the page inert,
 *  Escape or a click on the backdrop closes (unless busy). React removes the element before close() runs, so the
 *  browser's own focus restore does not fire: the opener is refocused explicitly. `wide` fits forms (K-059). */
export function Modal({label,onClose,busy,wide,children}:{label:string;onClose:()=>void;busy?:boolean;wide?:boolean;children:ReactNode}){
  const ref=useRef<HTMLDialogElement>(null)
  useEffect(()=>{const d=ref.current,opener=document.activeElement as HTMLElement|null;d?.showModal()
    return()=>{d?.close();opener?.focus()}},[])
  // The backdrop belongs to the <dialog> itself: a click there targets it and lands outside its box.
  // Clicks inside (and keyboard "clicks", at 0,0) target a child, so they never close it.
  const outside=(e:MouseEvent<HTMLDialogElement>)=>{
    const d=ref.current;if(busy||!d||e.target!==d)return
    const r=d.getBoundingClientRect()
    if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)onClose()
  }
  return <dialog ref={ref} className={'modal'+(wide?' wide':'')} aria-label={label} onClick={outside}
    onCancel={e=>{e.preventDefault();if(!busy)onClose()}}>{children}</dialog>
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
export const labels:Record<string,string> = {QUOTA:t('shared.status.quota'),PENDING:t('shared.status.pending'),COMPLETED:t('shared.status.completed'),QUEUED:t('shared.status.queued'),RUNNING:t('shared.status.running'),SUCCEEDED:t('shared.status.succeeded'),FAILED:t('shared.status.failed'),CANCELLED:t('shared.status.cancelled'),RECOVERY_REQUIRED:t('shared.status.recovery_required'),WAITING_APPROVAL:t('shared.status.waiting_approval'),WAITING_AUTH:t('shared.status.waiting_auth'),WAITING_CAPACITY:t('shared.status.waiting_capacity'),WAITING_CONTEXT:t('shared.status.waiting_context'),WAITING_HUMAN:t('shared.status.waiting_human'),CONTEXT_EXHAUSTED:t('shared.status.context_exhausted'),PAUSED:t('shared.status.paused')}
export const contextLabels:Record<string,string> = {context_continuation_disabled:t('shared.ctx.disabled'),context_continuation_exhausted:t('shared.ctx.exhausted'),context_prompt_too_large:t('shared.ctx.prompt_too_large')}
export const reasonLabels:Record<string,string> = {...contextLabels,[RECOVERED]:t('shared.reason.recovered'),
  PROMPT_TOO_LARGE:t('shared.reason.prompt_too_large'),
  budget_exhausted:t('shared.reason.budget_exhausted')}
/** F3-01a resume gate reasons (backend resume_decision event). */
export const resumeReasons:Record<string,string> = {no_previous_run:t('shared.resume.no_previous_run'),previous_run_not_succeeded:t('shared.resume.previous_run_not_succeeded'),
  first_provider_not_codex:t('shared.resume.first_provider_not_codex'),policy_changed:t('shared.resume.policy_changed'),workspace_mismatch:t('shared.resume.workspace_mismatch'),
  no_completed_author_attempt:t('shared.resume.no_completed_author_attempt'),last_author_not_codex:t('shared.resume.last_author_not_codex'),
  no_provider_session:t('shared.resume.no_provider_session'),codex_home_changed:t('shared.resume.codex_home_changed'),author_attempt_unclean:t('shared.resume.author_attempt_unclean'),
  other_writer:t('shared.resume.other_writer'),baseline_missing:t('shared.resume.baseline_missing'),
  snapshot_incomplete:t('shared.resume.snapshot_incomplete'),snapshot_scope_mismatch:t('shared.resume.snapshot_scope_mismatch'),workspace_changed:t('shared.resume.workspace_changed'),
  retry_fresh:t('shared.resume.retry_fresh')}
export const promptFormats:Record<string,string> = {RESUME_AUTHOR:t('shared.format.resume_author'),FRESH_AUTHOR:t('shared.format.fresh_author'),REVIEWER:t('shared.format.reviewer'),
  CORRECTION:t('shared.format.correction'),FRESH_CORRECTION:t('shared.format.fresh_correction')}
export const promptRejections:Record<string,string> = {mandatory_exceeds_limit:t('shared.reject.mandatory_exceeds_limit'),
  final_prompt_exceeds_limit:t('shared.reject.final_prompt_exceeds_limit'),history_metadata_exceeds_budget:t('shared.reject.history_metadata_exceeds_budget')}

export function Pill({status,recovered=false}:{status:string;recovered?:boolean}) {
  // K1: a human-recovered run is FAILED, but shown apart from ordinary failures.
  if(recovered&&status==='FAILED')return <span className="pill warn"><span className="d" aria-hidden="true"/>{t('shared.pill.recovered')}</span>
  const tone = ['COMPLETED','SUCCEEDED'].includes(status)?'ok':status==='RUNNING'?'run':status==='FAILED'?'bad':['CONTEXT_EXHAUSTED','WAITING_CONTEXT','QUOTA','WAITING_CAPACITY','WAITING_APPROVAL','WAITING_AUTH','WAITING_HUMAN','RECOVERY_REQUIRED','PAUSED'].includes(status)?'warn':'idle'
  return <span className={'pill '+tone}><span className="d" aria-hidden="true"/>{labels[status]||status}</span>
}
// TB-06: `codex@is` keeps the codex badge colour; the full identity stays visible.
export function Provider({name}:{name:string}) {return <span className="prov"><span className={'g '+name.split(/[@/]/)[0]} aria-hidden="true">{name.slice(0,1).toUpperCase()}</span>{name}</span>}
export function Chains({policy,installed}:{policy:Policy['policy'];installed?:Policy['installed']}) {
  return <div className="chains">{Object.entries(policy.roles).map(([role,chain])=><div className="chain-row" key={role}>
    <span className={'chip '+role}>{role==='author'?t('shared.role.author'):t('shared.role.reviewer')}</span>
    <div className="flow">{chain.map((p,i)=><span className="flow" key={p.provider}>{i>0&&<span className="arr" aria-hidden="true">→</span>}<Provider name={p.provider}/>{installed&&!(installed[p.provider]??installed[splitId(p.provider)?.kind??''])&&<span className="pill bad">{t('shared.not_installed')}</span>}</span>)}</div>
  </div>)}</div>
}
export const active = (s:string) => s === 'QUEUED' || s === 'RUNNING'
/** F3-04 (K-012): opt-in bounded quota wait, sent with a message and frozen with it (part of the payload hash). */
export type WaitChoice = {wait_on_quota:true;wait_deadline_hours:number;wait_max_attempts:number}
const WAIT_DEFAULT:WaitChoice = {wait_on_quota:true,wait_deadline_hours:4,wait_max_attempts:3}
const whole = (v:string,low:number,high:number) => Math.min(high,Math.max(low,Math.round(Number(v))||low))
export function WaitOptions({value,onChange,id}:{value:WaitChoice|null;onChange:(v:WaitChoice|null)=>void;id:string}){
  return <div className="wait-options">
    <label className="accept-row"><input type="checkbox" checked={!!value} onChange={e=>onChange(e.target.checked?WAIT_DEFAULT:null)}/> {t('shared.wait.label')}</label>
    {value&&<div className="wait-limits">
      <label htmlFor={id+'-hours'}>{t('shared.wait.hours')}<input id={id+'-hours'} type="number" min={1} max={24} value={value.wait_deadline_hours} onChange={e=>onChange({...value,wait_deadline_hours:whole(e.target.value,1,24)})}/></label>
      <label htmlFor={id+'-attempts'}>{t('shared.wait.attempts')}<input id={id+'-attempts'} type="number" min={1} max={5} value={value.wait_max_attempts} onChange={e=>onChange({...value,wait_max_attempts:whole(e.target.value,1,5)})}/></label>
    </div>}
    <p className="chain-note">{value
      ?t('shared.wait.note_on',{hours:value.wait_deadline_hours,attempts:value.wait_max_attempts})
      :t('shared.wait.note_off')}</p>
  </div>
}
/** Local, short date/time for plan moments (ISO UTC from the backend). */
export const when = (iso:string) => new Date(iso).toLocaleString(locale,{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})
export const missingProvider = (p:Policy, body:PolicyBody=p.policy) => Object.values(body.roles).some(chain=>chain.some(i=>!installedOf(p,i.provider)))
export async function api<T>(url:string, options?:RequestInit):Promise<T> {
  return (await checkedFetch(url,options)).json()
}
export const newId = () => crypto.randomUUID()

/** F3-02 loop outcomes (backend `session_loop_state.outcome`). */
export const outcomeLabels:Record<string,string> = {verified:t('shared.outcome.verified'),reviewer_blocked:t('shared.outcome.reviewer_blocked'),
  review_invalid:t('shared.outcome.review_invalid'),no_progress:t('shared.outcome.no_progress'),
  turn_limit:t('shared.outcome.turn_limit'),budget_exhausted:t('shared.outcome.budget_exhausted')}
export const verdictLabels:Record<string,string> = {pass:t('shared.verdict.pass'),changes_requested:t('shared.verdict.changes_requested'),blocked:t('shared.verdict.blocked'),invalid:t('shared.verdict.invalid')}
export const invalidCodes:Record<string,string> = {no_block:t('shared.invalid.no_block'),multiple_blocks:t('shared.invalid.multiple_blocks'),unclosed_block:t('shared.invalid.unclosed_block'),
  trailing_text:t('shared.invalid.trailing_text'),too_large:t('shared.invalid.too_large'),invalid_json:t('shared.invalid.invalid_json'),duplicate_key:t('shared.invalid.duplicate_key'),
  non_finite:t('shared.invalid.non_finite'),schema:t('shared.invalid.schema')}
export const loopLine = (loop?:{max_turns:number;max_active_minutes:number}) => loop
  ? t('shared.loop_line',{turns:loop.max_turns,minutes:loop.max_active_minutes})
  : null
export const duration = (ms:number) => { const s=Math.round(ms/1000); return s<60?t('shared.duration.sec',{s}):t('shared.duration.min_sec',{m:Math.floor(s/60),s:s%60}) }
