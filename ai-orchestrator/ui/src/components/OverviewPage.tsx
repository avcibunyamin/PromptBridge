import { useEffect, useState, type ReactNode } from 'react'
import { Activity, FileCheck2, Gauge, Plus, RefreshCw, SlidersHorizontal, UsersRound } from 'lucide-react'
import { listEnvelopes, listWorks, type Work } from '../api'
import { canCommand, mobile } from '../mobile'
import { Pill, api } from './work/shared'
import { lowest, resetText, tone, type QuotaGroup, type QuotaSnapshot, type QuotaWindow } from './work/usage'
import { SetupGuide } from './SetupGuide'
import { RECIPES } from '../recipes'
import { lang, t } from '../i18n'

const TITLES:Record<string,string> = {gemini:'Gemini',claude_gpt:t('shell.overview.claudeGpt'),argon:'Gemini 4 Argon',claude:'Claude',codex:'Codex'}
const ACTIVE = new Set(['QUEUED','RUNNING','PAUSED','RECOVERY_REQUIRED','WAITING_CAPACITY','WAITING_AUTH','WAITING_APPROVAL','WAITING_CONTEXT','WAITING_HUMAN'])

/** K-040: the Mac's landing page, like ZeroCost's “Genel bakış”: counts, recent works, accounts and their quota. */
export function OverviewPage({onOpenWork,onNewWork,onApprovals,onSettings}:{onOpenWork:(id:string)=>void;onNewWork:()=>void;onApprovals:()=>void;onSettings:()=>void}){
  const [works,setWorks]=useState<Work[]|null>(null), [active,setActive]=useState<number|null>(null)
  const [waiting,setWaiting]=useState<number|null>(null), [quota,setQuota]=useState<QuotaSnapshot|null>(null)
  const [refreshing,setRefreshing]=useState(false), [error,setError]=useState('')
  useEffect(()=>{
    const load=()=>{
      void listWorks(false).then(p=>{setWorks(p.items);setActive(p.counts.active);setError('')}).catch(e=>setError(String(e)))
      void listEnvelopes('PENDING_APPROVAL').then(e=>setWaiting(e.length)).catch(()=>{})
      void api<QuotaSnapshot>('/api/sessions/quota').then(setQuota).catch(()=>{})
    }
    load(); const t=setInterval(load,15000); return ()=>clearInterval(t)
  },[])
  async function refresh(){
    setRefreshing(true); setError('')
    try{setQuota(await api<QuotaSnapshot>('/api/sessions/quota/refresh',{method:'POST'}))}catch(e){setError(String(e))}finally{setRefreshing(false)}
  }
  const accounts=Object.entries(quota?.accounts??{}).sort(([a],[b])=>a.localeCompare(b))
  const usable=accounts.filter(([,a])=>!a.error&&a.groups.length)
  const running=(works??[]).filter(w=>w.latest_session&&ACTIVE.has(w.latest_session.status)).length
  let low:{left:number;where:string}|null=null
  for(const [name,a] of usable)for(const g of a.groups){const l=lowest(g);if(l!==null&&(!low||l<low.left))low={left:l,where:`${name} · ${TITLES[g.key]??g.title}`}}

  return <div className="pane overview">
    <div className="page-heading">
      <div><p className="eyebrow">{t('shell.overview.eyebrow')}</p><h1>{t('shell.overview.heading')}</h1>
        <p>{t('shell.overview.subtitle')}</p></div>
      {canCommand()&&<button type="button" className="btn primary" onClick={onNewWork}><Plus aria-hidden/>{t('shell.overview.newWork')}</button>}
    </div>
    <SetupGuide works={works} onNewWork={onNewWork} onSettings={onSettings}/>
    {error&&<p className="error-banner" role="alert">{error}</p>}
    <div className="stats-grid">
      <Stat icon={<Activity aria-hidden/>} label={t('shell.overview.activeWorks')} value={active}
        note={running?t('shell.overview.runningCount',{count:running}):t('shell.overview.noneRunning')}
        href="#/"/>
      <Stat icon={<FileCheck2 aria-hidden/>} label={t('shell.overview.pendingApprovals')} value={waiting}
        note={t('shell.bridge.title')} onClick={onApprovals}/>
      <Stat icon={<UsersRound aria-hidden/>} label={t('shell.overview.connectedAccounts')} value={accounts.length?usable.length:null}
        note={accounts.length?t('shell.overview.accountsRead',{count:accounts.length}):t('shell.overview.notReadYet')}
        onClick={onSettings}/>
      <Stat icon={<Gauge aria-hidden/>} label={t('shell.overview.lowestQuota')} value={low?t('shell.overview.percent',{pct:Math.round(low.left)}):null}
        note={low?.where??t('shell.overview.notReadYet')} tone={tone(low?.left??null)}/>
    </div>
    <div className="overview-grid">
      <section className="panel">
        <div className="panel-heading"><div><p className="eyebrow">{t('shell.overview.recentEyebrow')}</p><h2>{t('shell.overview.recentHeading')}</h2></div></div>
        {works===null?<p className="muted small">{t('shell.loading')}</p>:!works.length?<p className="muted small">{t('shell.overview.noWorksYet')}</p>:
        <ul className="recent">{works.slice(0,6).map(w=>{
          const rel=formatRelativeTime(w.updated_at)
          return <li key={w.id}><button type="button" onClick={()=>onOpenWork(w.id)}>
            <span className="recent-title">{w.title}<span className="mono small muted">{w.workspace_path.split('/').pop()}</span></span>
            <span className="recent-meta">
              {rel&&<time className="recent-time small muted" dateTime={w.updated_at}>{rel}</time>}
              {w.latest_session?<Pill status={w.latest_session.status}/>:<span className="pill idle"><span className="d" aria-hidden/>{t('shell.overview.noRun')}</span>}
            </span>
          </button></li>
        })}</ul>}
      </section>
      <div className="overview-side">
        <section className="panel quick-start" aria-label={t('shell.overview.quickStartHeading')}>
          <div className="panel-heading"><div><p className="eyebrow">{t('shell.overview.quickStartEyebrow')}</p><h2>{t('shell.overview.quickStartHeading')}</h2></div></div>
          <div className="quick-start-actions">
            {canCommand()&&<button type="button" className="btn quick-start-btn" onClick={onNewWork}><Plus aria-hidden/>{t('shell.overview.newWork')}</button>}
            <a className="btn quick-start-link" href="#/setup"><SlidersHorizontal aria-hidden/>{t('shell.overview.setupLink')}</a>
          </div>
          <div className="quick-start-recipes">
            <p className="small muted quick-start-label">{t('shell.overview.startWithRecipe')}</p>
            <div className="recipe-chips">
              {RECIPES.map(r=><button key={r.id} type="button" className="recipe-chip" title={r.hint} disabled={!canCommand()} onClick={onNewWork}>{r.title}</button>)}
            </div>
          </div>
        </section>
        <section className="panel accent">
          <div className="panel-heading"><div><p className="eyebrow">{t('shell.overview.quotaEyebrow')}</p><h2>{t('shell.overview.quotaHeading')}</h2></div>
            {!mobile.active&&<button className="icon-btn" disabled={refreshing} aria-busy={refreshing} onClick={()=>void refresh()} aria-label={t('shell.overview.refreshQuotaLabel')}
              title={t('shell.overview.refreshQuotaTitle')}><RefreshCw className={refreshing?'spin':undefined} aria-hidden/></button>}</div>
          {!accounts.length?<p className="muted small">{t('shell.overview.notReadYetDot')} {mobile.active?'':t('shell.overview.refreshHint')}</p>:
          <ul className="accounts">{accounts.map(([name,a])=><li key={name}>
            <p className="account-name mono">{name}{a.error&&<span className="pill warn">{t('shell.unreadable')}</span>}</p>
            {a.groups.map(g=>{const w=bottleneck(g), l=lowest(g), soon=w?resetText(w):null   // the window that runs out first
              return <div key={g.key} className="quota-row">
                <span className="small">{TITLES[g.key]??g.title}</span>
                <div className={'bar '+tone(l)} aria-hidden="true"><span style={{width:`${l??0}%`}}/></div>
                <span className="small mono">{l===null?'—':t('shell.overview.percent',{pct:Math.round(l)})}</span>
                {soon&&l!==null&&l<100&&<span className="small muted">{t('shell.overview.resetsIn',{soon})}</span>}
              </div>})}
          </li>)}</ul>}
          <p className="small muted">{t('shell.overview.claudeQuotaHint')}</p>
        </section>
      </div>
    </div>
  </div>
}

/** The window with the least left: its percentage is shown, so its reset time is too. */
const bottleneck=(g:QuotaGroup)=>g.windows.reduce<QuotaWindow|null>((m,w)=>w.remaining===null?m:!m||w.remaining<(m.remaining??101)?w:m,null)

/** Format an ISO timestamp as a localized relative time string using Intl.RelativeTimeFormat. */
function formatRelativeTime(iso:string|null|undefined,locale:string=lang):string|null{
  if(!iso)return null
  const ts=Date.parse(iso)
  if(!Number.isFinite(ts))return null
  const diffSec=Math.round((ts-Date.now())/1000)
  const absSec=Math.abs(diffSec)
  const rtf=new Intl.RelativeTimeFormat(locale,{style:'short',numeric:'auto'})
  if(absSec<60)return rtf.format(diffSec,'second')
  const diffMin=Math.round(diffSec/60)
  if(Math.abs(diffMin)<60)return rtf.format(diffMin,'minute')
  const diffHour=Math.round(diffMin/60)
  if(Math.abs(diffHour)<24)return rtf.format(diffHour,'hour')
  const diffDay=Math.round(diffHour/24)
  if(Math.abs(diffDay)<30)return rtf.format(diffDay,'day')
  const diffMonth=Math.round(diffDay/30)
  if(Math.abs(diffMonth)<12)return rtf.format(diffMonth,'month')
  return rtf.format(Math.round(diffDay/365),'year')
}

function Stat({icon,label,value,note,tone,onClick,href}:{icon:ReactNode;label:string;value:number|string|null;note:string;tone?:string;onClick?:()=>void;href?:string}){
  const interactive=Boolean(onClick||href)
  const cls='stat-card'+(interactive?' as-button':'')+(tone?' '+tone:'')
  const content=<><span className="stat-top">{label}{icon}</span>
    <span className="stat-value">{value??'—'}</span><span className="stat-note">{note}</span></>
  if(href)return <a className={cls} href={href} onClick={onClick}>{content}</a>
  if(onClick)return <button type="button" className={cls} onClick={onClick}>{content}</button>
  return <div className={cls}>{content}</div>
}
