/** K-039: context fill and remaining quota for one chain link. Pure (no React), so `npm test` covers it. */
import { baseOf, splitId } from './team.ts'

export type QuotaWindow = {label:string;kind:'weekly'|'five_hour'|'other';remaining:number|null;resets_in?:string|null;resets_at?:number|null;status?:string|null}
export type QuotaGroup = {key:string;title:string;models:string|null;windows:QuotaWindow[]}
export type QuotaAccount = {groups:QuotaGroup[];error:string|null;checked_at?:number;source?:'probe'|'run'}
export type QuotaSnapshot = {accounts:Record<string,QuotaAccount>;running:string[]}
export type StepUsage = {agent_name:string;usage_info:string|null}
export type Context = {used:number;window:number|null;approx:boolean}

/** agy shares one quota per group: Argon has its own, other Gemini models one, Claude/GPT-OSS another. */
export function quotaGroupKey(id:string):string|null {
  const s=splitId(id)
  if(!s)return null
  if(s.kind==='agy'){const m=s.model??''; return m.includes('argon')?'argon':m.startsWith('gemini')?'gemini':'claude_gpt'}
  return s.kind==='claude'||s.kind==='codex'?s.kind:null
}

/** The account reading and the group that applies to this link (null when nothing was read yet). */
export function quotaFor(snap:QuotaSnapshot|null, id:string){
  const s=splitId(id), key=quotaGroupKey(id)
  if(!s||!key||!snap)return null
  const account=snap.accounts[s.kind==='claude'?'claude':baseOf(id)]
  if(!account)return null
  return {account, group:account.groups.find(g=>g.key===key)??null}
}

/** The lowest remaining percentage across a group's windows (the one that runs out first). */
export const lowest = (g:QuotaGroup|null|undefined) =>
  g?.windows.reduce<number|null>((low,w)=>w.remaining===null?low:low===null?w.remaining:Math.min(low,w.remaining),null) ?? null
export const tone = (remaining:number|null) => remaining===null?'idle':remaining<=0?'bad':remaining<=20?'warn':'ok'

// agy reports no window; these are the published sizes (marked “yaklaşık” in the panel).
const KNOWN:[RegExp,number][] = [[/^gemini/,1_048_576],[/^claude/,200_000],[/^gpt-oss/,131_072]]

/** Tokens the next turn starts with (prompt + cache + last answer) and the model's window, from a step's usage. */
export function contextOf(usageJson:string|null, id:string):Context|null {
  let u:any
  try{u=usageJson?JSON.parse(usageJson):null}catch{return null}
  if(!u||typeof u!=='object')return null
  const n=(v:unknown)=>typeof v==='number'&&Number.isFinite(v)?v:0
  if(u.last&&typeof u.last==='object'){   // Codex tokenUsage
    const w=n(u.modelContextWindow)
    return {used:n(u.last.inputTokens)+n(u.last.outputTokens),window:w||null,approx:false}
  }
  const used=n(u.input_tokens)+n(u.cache_read_input_tokens)+n(u.cache_creation_input_tokens)+n(u.output_tokens)
  if(!used)return null
  if(n(u.context_window))return {used,window:n(u.context_window),approx:false}
  const s=splitId(id), model=s?.kind==='claude'?'claude':s?.model??s?.kind??''   // Claude aliases (`sonnet`) too
  const known=KNOWN.find(([re])=>re.test(model))
  return {used,window:known?known[1]:null,approx:true}
}

/** The link's own conversation: its newest step (same identity) — another model starts a new context. */
export function lastContext(steps:StepUsage[], id:string):Context|null {
  const strip=(x:string)=>x.replace(/~[^/@~]+$/,'')
  for(let i=steps.length-1;i>=0;i--){
    if(strip(steps[i].agent_name)!==strip(id))continue
    const c=contextOf(steps[i].usage_info,id)
    if(c)return c                                   // a step that has not run yet has no usage: look further back
  }
  return null
}

export const percent = (c:Context|null) => c&&c.window?Math.min(100,Math.round(c.used/c.window*100)):null
export const tokens = (n:number) => n>=1_000_000?`${(n/1_000_000).toFixed(n%1_000_000?1:0)}M`:n>=1000?`${Math.round(n/1000)}k`:`${n}`

/** "167h 25m" or an epoch second → "6 gün 23 sa" / "4 sa 25 dk". */
export function resetText(w:QuotaWindow, now=Date.now()/1000):string|null {
  let mins:number|null=null
  if(w.resets_at)mins=Math.max(0,Math.round((w.resets_at-now)/60))
  else if(w.resets_in){const m=/(?:(\d+)h)?\s*(?:(\d+)m)?/.exec(w.resets_in);if(m&&(m[1]||m[2]))mins=Number(m[1]??0)*60+Number(m[2]??0)}
  if(mins===null)return null
  const d=Math.floor(mins/1440), h=Math.floor(mins%1440/60), m=mins%60
  return d?`${d} gün ${h} sa`:h?`${h} sa ${m} dk`:`${m} dk`
}
