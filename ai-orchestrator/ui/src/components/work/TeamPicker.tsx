import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { LoaderCircle, Plus, RefreshCw, ShieldCheck, SlidersHorizontal, Trash2, TriangleAlert, X } from 'lucide-react'
import { ApiError, previewTeam } from '../../api'
import { Provider, api, installedOf, type Availability, type Policy, type Team, type TeamPreview, type TeamProblem } from './shared'
import { agyFamilyLabel, agyGroups, agyName, agyParts, baseOf, covers, effortLabel, joinId, linkMissing, linkOptions, modelLabel, splitId, type Catalog } from './team'
import { teamPreviewer } from './flows'
import { mobile } from '../../mobile'
import { UsageRing } from './UsageRing'
import { locale, t } from '../../i18n'

/** `preview` belongs to exactly `team`; it is null while the newest selection's preview is pending or invalid. */
export type TeamSelection = {team:Team;preview:TeamPreview|null;invalid?:boolean}

const ROLES = [['author','WRITE',t('session.team.role.authorWrite')],['reviewer','READ_ONLY',t('session.team.role.reviewerReadOnly')]] as const
const SHORT = {author:t('session.role.author'),reviewer:t('session.role.reviewer')} as const
const authLabels:Record<string,[string,string]> = {authenticated:[t('session.team.auth.authenticated'),'ok'],unauthenticated:[t('session.team.auth.unauthenticated'),'warn'],unknown:[t('session.team.auth.unknown'),'idle'],
  model_unavailable:[t('session.team.auth.modelUnavailable'),'bad']}
const MAX_CHAIN=4
const reasons:Record<string,string> = {provider_not_allowed:t('session.team.reason.providerNotAllowed'),profile_unsupported:t('session.team.reason.profileUnsupported'),
  duplicate_in_role:t('session.team.reason.duplicateInRole'),chain_length:t('session.team.reason.chainLength',{max:MAX_CHAIN-1}),unknown_provider:t('session.team.reason.unknownProvider'),
  account_unsupported:t('session.team.reason.accountUnsupported'),model_unsupported:t('session.team.reason.modelUnsupported'),model_required:t('session.team.reason.modelRequired'),
  model_unavailable:t('session.team.reason.modelUnavailable'),
  effort_unsupported:t('session.team.reason.effortUnsupported'),tools_unsupported:t('session.team.reason.toolsUnsupported')}
const catalogLabels:Record<string,[string,string]> = {unverified:[t('session.team.catalog.unverified'),'idle'],model_unavailable:[t('session.team.catalog.modelUnavailable'),'bad'],unsupported:[t('session.team.catalog.unsupported'),'bad']}
const catalogErrors:Record<string,string> = {unauthenticated:t('session.team.catalogError.unauthenticated'),timeout:t('session.team.catalogError.timeout'),
  unavailable:t('session.team.catalogError.unavailable'),cleanup_unverified:t('session.team.catalogError.cleanupUnverified')}
const time = (s:number) => new Date(s*1000).toLocaleTimeString(locale,{hour:'2-digit',minute:'2-digit'})

/** F3-03 team card: per role an ordered chain (TB-06: a primary and up to three backups, each an identity such as
 *  `codex@is`), only providers the server allows for that role (and whose profile fits). The policy, digest and badges come from the server; nothing is computed here.
 *  The session check runs only when the user presses the button (GET ?check_auth=true).
 */
export function TeamPicker({policy,initial,compact=false,startOpen=false,onChange,onReloadPolicy,sessionId,sessionStatus,trailing}:{policy:Policy;initial:Team;
  compact?:boolean;startOpen?:boolean;onChange:(s:TeamSelection)=>void;onReloadPolicy?:()=>Promise<void>
  sessionId?:string|null;sessionStatus?:string|null;trailing?:JSX.Element}){   // K-039: the composer's last run (context) and its send button
  // TB-20: `compact` is the composer's model picker: the author's primary link only; “Gelişmiş” opens the full editor.
  const [team,setTeam]=useState<Team>(initial), [open,setOpen]=useState(!compact||startOpen)
  const [preview,setPreview]=useState<TeamPreview|null>(null), [problems,setProblems]=useState<TeamProblem[]>([]), [error,setError]=useState('')
  const [availability,setAvailability]=useState<Availability|undefined>(policy.availability), [checking,setChecking]=useState(false)
  const [catalog,setCatalog]=useState<Catalog|null>(null), [catalogError,setCatalogError]=useState(''), [refreshing,setRefreshing]=useState(false)
  // K-041: right after a restart the server is still listing models; read again until it has (or ~30 s pass)
  // An unchanged “still listing” answer keeps the same object, so the preview (and an acceptance) is not reset.
  useEffect(()=>{let live=true,tries=0,timer:ReturnType<typeof setTimeout>|undefined
    const read=()=>api<Catalog>('/api/sessions/catalog').then(c=>{if(!live)return;setCatalog(prev=>prev&&c.checked_at===null?prev:c)
      if(c.checked_at===null&&++tries<8)timer=setTimeout(read,4000)},e=>{if(live)setCatalogError(String(e))})
    void read();return()=>{live=false;clearTimeout(timer)}},[])
  const report=useRef(onChange);report.current=onChange
  const gaps=useRef<(t:Team)=>string[]>(()=>[])   // K-041: a team with a link lacking a model/effort is never sendable
  const request=useMemo(()=>teamPreviewer(previewTeam,a=>{
    if(a.ok){setPreview(a.value);setProblems([]);setError('')
      report.current({team:a.team,preview:gaps.current(a.team).length?null:a.value,invalid:gaps.current(a.team).length>0||undefined});return}
    const e=a.error
    if(e instanceof ApiError&&e.code==='team_invalid'){setProblems(Array.isArray(e.body?.problems)?e.body.problems as TeamProblem[]:[]);setError('')
      report.current({team:a.team,preview:null,invalid:true})}
    else setError(String(e))
  }),[])
  useEffect(()=>setAvailability(policy.availability),[policy])
  // Every selection (or reloaded ceiling) invalidates the previous preview and its acceptance until the new one answers.
  // K-041: previewed once the model list answered (or failed): earlier its gaps are unknown.
  useEffect(()=>{setPreview(null);setProblems([]);report.current({team,preview:null});if(catalog!==null||catalogError)request(team)},
    [team,policy,request,catalog,catalogError])
  /** An explicit user action: the policy again (providers.yaml may have changed) and `agy models` per account
   *  (no model call); the preview then reports the new catalog statuses. */
  async function refreshAll(){
    setRefreshing(true);setCatalogError('')
    try{await onReloadPolicy?.()}catch(e){setError(String(e))}
    try{setCatalog(await api<Catalog>('/api/sessions/catalog/refresh',{method:'POST'}))}   // the catalog effect previews again
    catch(e){setCatalogError(String(e))}finally{setRefreshing(false)}
  }
  const editorId=useId()

  const allowed=(role:'author'|'reviewer',profile:string)=>(policy.ceiling?.allowed[role]??policy.policy.roles[role].map(i=>i.provider))
    .filter(p=>!policy.capabilities||policy.capabilities[p]?.profiles.includes(profile))
  const capOf=(id:string)=>policy.capabilities?.[id]??policy.capabilities?.[splitId(id)?.kind??'']
  const setRole=(role:'author'|'reviewer',chain:string[])=>setTeam(t=>({...t,[role]:chain}))
  const missing=(tm:Team)=>catalog===null&&!catalogError?[t('session.team.gap.loadingModels')]:ROLES.flatMap(([role,profile])=>{const o=linkOptions(allowed(role,profile),catalog,capOf)
    return tm[role].flatMap((id,i)=>{const m=linkMissing(id,o)
      return m?[t('session.team.gap.selectField',{role:SHORT[role],link:i===0?t('session.team.link.primaryLower'):t('session.team.link.backupLower',{index:i}),base:baseOf(id),field:m==='model'?t('session.team.field.modelLower'):t('session.team.field.effortLower')})]:[]})})
  gaps.current=missing
  const gapList=missing(team)
  async function checkAuth(){
    setChecking(true);setError('')
    try{setAvailability((await api<Policy>('/api/sessions/providers?check_auth=true')).availability)}
    catch(e){setError(String(e))}finally{setChecking(false)}
  }
  const badges=(name:string)=>{
    const a=availability?.[name]??availability?.[baseOf(name)]
    if(!installedOf(policy,name))return <span className="pill bad">{t('session.team.badge.notInstalled')}</span>
    const cs=preview?.catalog?.[name], listError=catalog?.errors[baseOf(name)]
    const catalogPill=cs&&catalogLabels[cs]?<span className={'pill '+catalogLabels[cs][1]} title={cs==='unverified'?t('session.team.badge.unverifiedTitle'):t('session.team.badge.notInLastListTitle')}>{catalogLabels[cs][0]}</span>
      :listError&&!(listError==='unauthenticated'&&a?.auth==='unauthenticated')?<span className="pill warn" title={catalogErrors[listError]??listError}>{listError==='unauthenticated'?t('session.team.badge.noSignIn'):t('session.team.badge.listFailed')}</span>:null
    if(a?.health==='probe_cleanup_failed')return <>{catalogPill}<span className="pill bad" title={t('session.team.badge.probeDisabledTitle')}>{t('session.team.badge.probeDisabled')}</span></>
    const [label,tone]=authLabels[a?.auth??'unknown'];return <>{catalogPill}<span className={'pill '+tone}>{label}</span></>
  }
  const checkedAt=Object.values(availability??{}).map(a=>a.checked_at).filter((x):x is number=>!!x).sort().pop()

  /** One chain link's provider/account → model → effort selects (the full editor and the composer share it). */
  function linkFields(role:'author'|'reviewer',profile:string,i:number,bare=false){
    const entries=allowed(role,profile), chain=team[role], o=linkOptions(entries,catalog,capOf)
    const put=(value:string)=>setRole(role,chain.map((q,j)=>j===i?value:q))
    const current=chain[i], s=splitId(current), base=baseOf(current), model=s?.model??null, effort=s?.effort??null
    const others=chain.filter((_,j)=>j!==i), models=o.modelsFor(base), efforts=o.effortsFor(base,model)
    const defaultEffort=o.defaultEffortFor(base,model)
    const name=bare?SHORT[role]:i===0?t('session.team.link.primary'):t('session.team.link.backup',{index:i})
    // Changing the model keeps the effort while the ceiling still admits it.
    const withModel=(m:string|null)=>joinId(base,m,effort&&o.effortsFor(base,m).includes(effort)?effort:null)
    const wrap=(text:string,el:JSX.Element)=>bare?el:<label>{text}{el}</label>
    const provider=wrap(t('session.team.field.providerLabel',{name}),<select value={base} aria-label={t('session.team.field.providerAria',{name})} onChange={e=>put(o.firstOf(e.target.value,others)??e.target.value)}>
        {!o.bases.includes(base)&&<option value={base}>{t('session.team.option.notAllowed',{value:base})}</option>}
        {o.bases.map(b=><option key={b} value={b} disabled={b!==base&&(!installedOf(policy,b)||!o.ready(b)||!o.firstOf(b,others))}>
          {b}{installedOf(policy,b)?'':t('session.team.option.notInstalledSuffix')}{o.ready(b)?'':catalog?.errors[b]?t('session.team.option.listFailedSuffix'):t('session.team.option.refreshFirstSuffix')}</option>)}
      </select>)
    // K-038: agy shows model family + effort; anything off-list keeps the plain selects below.
    if(s?.kind==='agy'&&model&&models.includes(model)){
      const groups=agyGroups(models), cur=agyParts(model), famEfforts=groups.get(cur.family)??[]
      const taken=(m:string)=>others.includes(joinId(base,m,null))
      const pick=(fam:string)=>{const list=groups.get(fam)??[]
        const ef=cur.effort&&list.includes(cur.effort)?cur.effort:list.includes('medium')?'medium':list[0]??null
        const free=[ef,...list].find(x=>!taken(agyName(fam,x??null)))
        return agyName(fam,free??ef)}
      return <>
        {provider}
        {wrap(t('session.team.field.model'),<select value={cur.family} aria-label={t('session.team.field.modelAria',{name})} onChange={e=>put(joinId(base,pick(e.target.value),null))}>
          {[...groups].map(([fam,list])=><option key={fam} value={fam} disabled={fam!==cur.family&&(list.length?list:[null]).every(x=>taken(agyName(fam,x)))}>
            {bare?agyFamilyLabel(catalog,base,fam,list):`${agyFamilyLabel(catalog,base,fam,list)} — ${fam}`}</option>)}
        </select>)}
        {famEfforts.length>0&&wrap(t('session.team.field.effort'),<select value={cur.effort??''} aria-label={t('session.team.field.effortAria',{name})} onChange={e=>put(joinId(base,agyName(cur.family,e.target.value),null))}>
          {!cur.effort&&<option value="" disabled hidden>{t('session.team.option.selectEffort')}</option>}
          {famEfforts.map(ef=><option key={ef} value={ef} disabled={ef!==cur.effort&&taken(agyName(cur.family,ef))}>{effortLabel(ef)}</option>)}
        </select>)}
      </>
    }
    return <>
      {provider}
      {(models.length>0||model)&&wrap(t('session.team.field.model'),<select value={model??''} aria-label={t('session.team.field.modelAria',{name})} onChange={e=>put(withModel(e.target.value||null))}>
        {!model&&<option value="" disabled hidden>{t('session.team.option.selectModel')}</option>}
        {model&&!models.includes(model)&&<option value={model}>{model} ({!entries.some(e=>covers(e,current))?t('session.team.option.reason.notAllowed')
          :preview?.catalog?.[current]==='unsupported'?t('session.team.option.reason.unsupported')
          :preview?.catalog?.[current]==='model_unavailable'?t('session.team.option.reason.modelUnavailable'):t('session.team.option.reason.notListed')})</option>}
        {models.map(m=><option key={m} value={m} disabled={m!==model&&others.includes(joinId(base,m,effort))}>
          {bare?modelLabel(catalog,base,m):`${modelLabel(catalog,base,m)} — ${m}`}</option>)}
      </select>)}
      {efforts.length>0&&wrap(t('session.team.field.effort'),<select value={effort??''} aria-label={t('session.team.field.effortAria',{name})} onChange={e=>put(joinId(base,model,e.target.value||null))}>
        {!effort&&<option value="" disabled hidden>{defaultEffort?t('session.team.option.selectEffortRecommended',{effort:effortLabel(defaultEffort)}):t('session.team.option.selectEffort')}</option>}
        {efforts.map(ef=><option key={ef} value={ef} disabled={ef!==effort&&others.includes(joinId(base,model,ef))}>{effortLabel(ef)}</option>)}
      </select>)}
    </>
  }
  const short=(id:string)=>{const s=splitId(id), base=baseOf(id);return s?.model?modelLabel(catalog,base,s.model):base}

  const links=[...team.author,...team.reviewer]
  const authOf=(id:string)=>(availability?.[id]??availability?.[baseOf(id)])?.auth
  const notReady=ROLES.flatMap(([role,profile])=>{const o=linkOptions(allowed(role,profile),catalog,capOf);return o.bases.filter(b=>!o.ready(b))})
  // Calm, specific notes; real errors (an invalid team) stay in the editor's alert list.
  const notes=[...new Set([
    ...(catalog===null&&!catalogError?[]:gapList.map(g=>t('session.team.note.gap',{gap:g}))),
    ...(preview?.warnings.includes('same_provider_both_roles')?[t('session.team.note.sameProvider')]:[]),
    ...links.filter(id=>preview?.catalog?.[id]==='model_unavailable'||authOf(id)==='model_unavailable').map(id=>t('session.team.note.modelUnavailable',{id})),
    ...links.filter(id=>authOf(id)==='unauthenticated').map(id=>t('session.team.note.unauthenticated',{base:baseOf(id)})),
    // One note per account: a sign-in problem already said above is not repeated as a listing error.
    ...Object.entries(catalog?.errors??{}).filter(([b,e])=>!(e==='unauthenticated'&&links.some(id=>baseOf(id)===b&&authOf(id)==='unauthenticated')))
      .map(([b,e])=>t('session.team.note.catalogError',{base:b,error:b==='ollama'&&e==='unavailable'?t('session.team.catalogError.ollamaUnavailable'):catalogErrors[e]??e})),
    // TB-14: a small Ollama context breaks Codex's tool calls
    ...Object.entries(catalog?.notes??{}).filter(([,n])=>n.startsWith('small_context:')).map(([b,n])=>t('session.team.note.smallContext',{base:b,ctx:n.split(':')[1]??''})),
    ...notReady.filter(b=>!catalog?.errors[b]).map(b=>t('session.team.note.notReady',{base:b})),
    ...(catalogError?[t('session.team.note.catalogFetchFailed',{error:catalogError})]:[]),
  ])]

  // the gateway refuses both (they start provider processes); a phone uses what the Mac last fetched (K-033/4)
  const actions=mobile.active?null:<div className="team-actions">
    <button type="button" className="btn small" disabled={checking} aria-busy={checking} onClick={()=>void checkAuth()}
      title={t('session.team.checkAuth.title')}>{checking?<LoaderCircle className="spin" aria-hidden/>:<ShieldCheck aria-hidden/>}{checking?t('session.team.checkAuth.checking'):t('session.team.checkAuth.label')}</button>
    <button type="button" className="btn small" disabled={refreshing} aria-busy={refreshing} onClick={()=>void refreshAll()}
      title={t('session.team.refresh.title')}><RefreshCw className={refreshing?'spin':undefined} aria-hidden/>{refreshing?t('session.team.refresh.refreshing'):t('session.team.refresh.label')}</button>
  </div>
  const times=<span className="muted small team-times" role="status">{checkedAt?t('session.team.times.authCheckedAt',{time:time(checkedAt)}):t('session.team.times.authNotChecked')} · {catalog?.checked_at?t('session.team.times.catalogCheckedAt',{time:time(catalog.checked_at)}):t('session.team.times.catalogNotChecked')}</span>

  const editor=<div className="team-editor" id={editorId}>
    {ROLES.map(([role,profile,label])=>{
      const chain=team[role], roleProblems=problems.filter(p=>p.role===role)
      const o=linkOptions(allowed(role,profile),catalog,capOf), next=chain.length<MAX_CHAIN?o.nextLink(chain):null
      return <fieldset className="team-role" key={role}>
        <legend><span className={'chip '+role}>{label}</span></legend>
        <div className="team-selects">
          {chain.map((_,i)=><div className="team-link" key={i}>
              {linkFields(role,profile,i)}
              {i>0&&<button type="button" className="icon-btn remove-link" aria-label={t('session.team.removeBackupAria',{index:i})} title={t('session.team.removeBackupTitle')} onClick={()=>setRole(role,chain.filter((_,j)=>j!==i))}><Trash2 aria-hidden/></button>}
            </div>)}
          {next&&<button type="button" className="btn small add-link" onClick={()=>setRole(role,[...chain,next])}><Plus aria-hidden/>{t('session.team.addBackup')}</button>}
        </div>
        <div className="team-badges">{team[role].map(p=><span className="flow" key={p}><Provider name={p}/>{badges(p)}</span>)}</div>
        {roleProblems.length>0&&<ul className="team-problems" role="alert">{roleProblems.map((p,i)=><li key={i}><strong>{p.provider||label}</strong>: {reasons[p.reason]??p.reason}</li>)}</ul>}
      </fieldset>
    })}
      <p className="muted small">{t('session.team.editorNote')}</p>
  </div>

  // Send stays disabled until the preview answers; say why.
  const waiting=!preview&&!problems.length&&!error&&<p className="muted small" role="status">{t('session.team.previewing')}</p>

  if(compact)return <section className="composer-picker" aria-label={t('session.team.field.model')}>
    {/* K-039: Claude-like bar — tools on the left; model, effort, the context/quota ring and send on the right. */}
    <div className="composer-model">
      <button type="button" className="icon-btn composer-tools" aria-expanded={open} aria-controls={editorId} onClick={()=>setOpen(o=>!o)}
        aria-label={open?t('session.team.advanced.close'):t('session.team.advanced.open')} title={open?t('session.team.advanced.close'):t('session.team.advanced.open')}>{open?<X aria-hidden/>:<SlidersHorizontal aria-hidden/>}</button>
      <span className="composer-reviewer" title={t('session.team.reviewerChainTitle',{chain:team.reviewer.join(' → ')})}>{team.reviewer.length>1?t('session.team.reviewerSummaryMore',{reviewer:team.reviewer[0]?short(team.reviewer[0]):'—',more:team.reviewer.length-1}):t('session.team.reviewerSummary',{reviewer:team.reviewer[0]?short(team.reviewer[0]):'—'})}</span>
      <span className="spacer"/>
      <span className="composer-choice">
        {team.author.length>0&&linkFields('author','WRITE',0,true)}
        {team.author[0]&&<UsageRing identity={team.author[0]} sessionId={sessionId} status={sessionStatus}/>}
        {trailing}
      </span>
    </div>
    {!open&&problems.length>0&&<p className="notice">{t('session.team.invalidNotice')}</p>}
    {catalog===null&&!catalogError?<p className="muted small" role="status">{t('session.team.loadingModels')}</p>
      :gapList.length>0&&<p className="notice" role="status">{t('session.team.selectBeforeSend',{gaps:gapList.join(' · ')})}{gapList.length>1||!gapList[0].startsWith(t('session.team.gap.authorPrimaryPrefix'))?t('session.team.advancedSuffix'):''}</p>}
    {open&&<div className="composer-advanced">
      {notes.length>0&&<ul className="team-notes">{notes.map(n=><li key={n}><TriangleAlert aria-hidden/>{n}</li>)}</ul>}
      {editor}
      <div className="team-auth">{actions}{times}</div>
    </div>}
    {error&&<p className="error-banner" role="alert">{error}</p>}
    {waiting}
  </section>

  return <section className="team-picker" aria-label={t('session.team.ariaLabel')}>
    {notes.length>0&&<ul className="team-notes">{notes.map(n=><li key={n}><TriangleAlert aria-hidden/>{n}</li>)}</ul>}
    {open&&editor}
    {error&&<p className="error-banner" role="alert">{error}</p>}
    <div className="team-auth">{actions}{times}</div>
    {waiting}
  </section>
}
