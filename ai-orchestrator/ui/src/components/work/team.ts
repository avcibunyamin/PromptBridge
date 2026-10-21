/** TB-10: chain identities `provider[@account][/model][~effort]` and the options of one chain link.
 *  Pure (no React), so `npm test` covers the provider/account → model → effort flow. Mirrors
 *  provider_policy.split / covers; the server re-checks every team with the same rules. */
import { t } from '../../i18n/index.ts'

export type CatalogEntry = {identity:string;provider:string;account:string|null;model:string;display_name?:string;efforts:string[];default_effort?:string;status:'available'|'unverified'|'unsupported';source:'live'|'fallback'}
export type Catalog = {entries:CatalogEntry[];errors:Record<string,string>;notes?:Record<string,string>;checked_at:number|null;efforts:Record<string,string[]>}
export type Cap = {models?:boolean;model_required?:boolean;efforts?:string[]}

export const splitId = (id:string) => { const m=/^([a-z]+)(?:@([^/@~]+))?(?:\/([^/@~]+))?(?:~([^/@~]+))?$/.exec(id)
  return m?{kind:m[1],account:m[2]??null,model:m[3]??null,effort:m[4]??null}:null }
export const baseOf = (id:string) => { const s=splitId(id); return s?s.kind+(s.account?'@'+s.account:''):id }
export const joinId = (base:string, model:string|null, effort:string|null) => base+(model?'/'+model:'')+(effort?'~'+effort:'')
/** An `allowed` entry without a model (or effort) admits every model (or effort); the account always matches. */
export const covers = (entry:string, id:string) => { const e=splitId(entry), i=splitId(id)
  return !!e&&!!i&&e.kind===i.kind&&e.account===i.account&&(e.model===null||e.model===i.model)&&(e.effort===null||e.effort===i.effort) }

const titleCase = (value:string) => value.split(/[-_\s]+/).filter(Boolean)
  .map(part => /^[0-9]+(?:\.[0-9]+)*$/.test(part) ? part
    : part.toLowerCase()==='gpt' ? 'GPT'
    : part.charAt(0).toUpperCase() + part.slice(1)).join(' ')
const aliasLabels:Record<string,string> = {
  opus:'Opus (latest alias)',
  sonnet:'Sonnet (latest alias)',
  haiku:'Haiku (latest alias)',
  fable:'Fable (latest alias)',
}
export const effortLabel = (effort:string) => ({low:'Low',medium:'Medium',high:'High',xhigh:'XHigh',max:'Max'} as Record<string,string>)[effort] ?? effort
export function modelLabel(catalog:Catalog|null, base:string, model:string|null){
  if(!model)return t('session.team.defaultModel')
  const entry = catalog?.entries.find(e => e.identity === joinId(base, model, null))
  if(entry?.display_name)return entry.display_name
  if(aliasLabels[model])return aliasLabels[model]
  if(model.startsWith('claude-'))return titleCase(model.replace(/^claude-/, 'Claude '))
  return titleCase(model)
}

const EFFORT_ORDER=['low','medium','high','xhigh','max']
/** K-038: an agy model name carries its effort (`gemini-4-argon-high`). The panel shows family + effort and
 *  joins them back into the same full name, so identities, frozen policies and the adapter are unchanged. */
export const agyParts=(model:string)=>{const m=/^(.+)-(low|medium|high|xhigh|max)$/.exec(model)
  return m?{family:m[1],effort:m[2] as string|null}:{family:model,effort:null as string|null}}
export const agyName=(family:string,effort:string|null)=>effort?`${family}-${effort}`:family
/** family -> its efforts in low…max order ([] for a name without an effort suffix); families keep list order. */
export function agyGroups(models:string[]){
  const groups=new Map<string,string[]>()
  for(const m of models){const p=agyParts(m), list=groups.get(p.family)??[];if(p.effort)list.push(p.effort);groups.set(p.family,list)}
  for(const list of groups.values())list.sort((a,b)=>EFFORT_ORDER.indexOf(a)-EFFORT_ORDER.indexOf(b))
  return groups
}
/** "Gemini 4 Argon (High)" -> "Gemini 4 Argon": the catalog label of any variant without its effort. */
export function agyFamilyLabel(catalog:Catalog|null, base:string, family:string, efforts:string[]){
  const label=modelLabel(catalog,base,agyName(family,efforts[0]??null))
  return efforts.length?label.replace(/\s*\((low|medium|high|xhigh|max)\)$/i,'').replace(/ (Low|Medium|High|Xhigh|Max)$/,''):label
}

/** K-041: what a link still lacks — every link names a model, and an effort when its model offers efforts.
 *  `o` is the role's linkOptions; a tool whose list is not known yet (no models offered) is not blocked. */
export function linkMissing(id:string, o:{modelsFor:(base:string)=>string[];effortsFor:(base:string,model:string|null)=>string[]}):'model'|'effort'|null{
  const s=splitId(id)
  if(!s)return null
  const base=baseOf(id)
  if(!s.model)return o.modelsFor(base).length?'model':null
  if(s.kind==='agy'){   // the effort is the name's suffix: a family offering efforts needs one
    const p=agyParts(s.model)
    return !p.effort&&(agyGroups(o.modelsFor(base)).get(p.family)?.length??0)>0?'effort':null
  }
  return !s.effort&&o.effortsFor(base,s.model).length?'effort':null
}

/** The choices for one role: `entries` is that role's `allowed` ceiling, `capOf` the server's capability map. */
export function linkOptions(entries:string[], catalog:Catalog|null, capOf:(id:string)=>Cap|undefined){
  const bases=[...new Set(entries.map(baseOf))]
  // TB-14: a model the catalog marks `unsupported` (no tool calls) is never an option, whether the ceiling
  // names it or a wildcard admits it, so no default, backup or "ready" state is built from it.
  const unsupported=(base:string,m:string)=>catalog?.entries.some(c=>c.identity===joinId(base,m,null)&&c.status==='unsupported')
  const modelsFor=(base:string)=>{
    const list:string[]=[]
    for(const e of entries.filter(x=>baseOf(x)===base)){
      const m=splitId(e)?.model
      if(m)list.push(m)
      else for(const c of catalog?.entries??[])if(baseOf(c.identity)===base)list.push(c.model)
    }
    return [...new Set(list)].filter(m=>!unsupported(base,m))
  }
  // A live list is the authority, also when it offers no effort (Codex, TB-15); the static provider efforts only back a
  // fallback entry (Claude, TB-10). K-041: an effort belongs to a chosen model.
  const effortsFor=(base:string,model:string|null)=>{
    if(!model)return []
    const entry = catalog?.entries.find(e=>e.identity===joinId(base,model,null))
    const source = entry?.source==='live' ? entry.efforts : entry?.efforts.length ? entry.efforts : (capOf(base)?.efforts??[])
    return source.filter(ef=>ef!=='ultra'&&entries.some(e=>covers(e,joinId(base,model,ef))))
  }
  const defaultEffortFor=(base:string,model:string|null)=>{
    const value = model ? catalog?.entries.find(e=>e.identity===joinId(base,model,null))?.default_effort : undefined
    const efforts = effortsFor(base,model)
    return value&&efforts.includes(value) ? value : null
  }
  // A tool that requires a model can only be picked once the ceiling or the catalog names one.
  const ready=(base:string)=>!capOf(base)?.model_required||modelsFor(base).length>0
  /** The first allowed full link for a tool/account that the chain does not hold yet. */
  // K-041: always a named model when any is offered; a model the chain already uses (any effort) is skipped.
  const firstOf=(base:string,chain:string[])=>{
    const used=(m:string)=>chain.some(c=>baseOf(c)===base&&splitId(c)?.model===m)
    const exact=entries.find(e=>baseOf(e)===base&&!!splitId(e)?.model&&!used(splitId(e)!.model!))
    if(exact)return exact
    const m=modelsFor(base).find(x=>!used(x))
    if(m)return joinId(base,m,null)
    if(capOf(base)?.model_required||modelsFor(base).length)return null
    return entries.find(e=>baseOf(e)===base&&!chain.includes(e))??null   // no list known yet: the bare tool
  }
  /** What "add backup" appends: an unused tool/account first, else another model of a used one (K-022). */
  const nextLink=(chain:string[])=>{
    const usable=bases.filter(ready)
    for(const b of [...usable.filter(b=>!chain.some(c=>baseOf(c)===b)),...usable]){const l=firstOf(b,chain);if(l)return l}
    return null
  }
  return {bases,modelsFor,effortsFor,defaultEffortFor,ready,firstOf,nextLink}
}
