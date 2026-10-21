import { useEffect, useState } from 'react'
import { Activity, FileCheck2, Gauge, Plus, RefreshCw, UsersRound } from 'lucide-react'
import { listEnvelopes, listWorks, type Work } from '../api'
import { canCommand, mobile } from '../mobile'
import { Pill, api } from './work/shared'
import { lowest, resetText, tone, type QuotaGroup, type QuotaSnapshot, type QuotaWindow } from './work/usage'
import { SetupGuide } from './SetupGuide'
import { t } from '../i18n'

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
      {canCommand()&&<button className="btn primary" onClick={onNewWork}><Plus aria-hidden/>{t('shell.overview.newWork')}</button>}
    </div>
    <SetupGuide works={works} onNewWork={onNewWork} onSettings={onSettings}/>
    {error&&<p className="error-banner" role="alert">{error}</p>}
    <div className="stats-grid">
      <Stat icon={<Activity aria-hidden/>} label={t('shell.overview.activeWorks')} value={active} note={running?t('shell.overview.runningCount',{count:running}):t('shell.overview.noneRunning')}/>
      <button className="stat-card as-button" onClick={onApprovals}>
        <span className="stat-top">{t('shell.overview.pendingApprovals')}<FileCheck2 aria-hidden/></span>
        <span className="stat-value">{waiting??'—'}</span><span className="stat-note">{t('shell.bridge.title')}</span></button>
      <Stat icon={<UsersRound aria-hidden/>} label={t('shell.overview.connectedAccounts')} value={accounts.length?usable.length:null} note={accounts.length?t('shell.overview.accountsRead',{count:accounts.length}):t('shell.overview.notReadYet')}/>
      <Stat icon={<Gauge aria-hidden/>} label={t('shell.overview.lowestQuota')} value={low?t('shell.overview.percent',{pct:Math.round(low.left)}):null} note={low?.where??t('shell.overview.notReadYet')} tone={tone(low?.left??null)}/>
    </div>
    <div className="overview-grid">
      <section className="panel">
        <div className="panel-heading"><div><p className="eyebrow">{t('shell.overview.recentEyebrow')}</p><h2>{t('shell.overview.recentHeading')}</h2></div></div>
        {works===null?<p className="muted small">{t('shell.loading')}</p>:!works.length?<p className="muted small">{t('shell.overview.noWorksYet')}</p>:
        <ul className="recent">{works.slice(0,6).map(w=><li key={w.id}><button onClick={()=>onOpenWork(w.id)}>
          <span className="recent-title">{w.title}<span className="mono small muted">{w.workspace_path.split('/').pop()}</span></span>
          {w.latest_session?<Pill status={w.latest_session.status}/>:<span className="pill idle"><span className="d" aria-hidden/>{t('shell.overview.noRun')}</span>}
        </button></li>)}</ul>}
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
}

/** The window with the least left: its percentage is shown, so its reset time is too. */
const bottleneck=(g:QuotaGroup)=>g.windows.reduce<QuotaWindow|null>((m,w)=>w.remaining===null?m:!m||w.remaining<(m.remaining??101)?w:m,null)

function Stat({icon,label,value,note,tone}:{icon:JSX.Element;label:string;value:number|string|null;note:string;tone?:string}){
  return <div className={'stat-card'+(tone?' '+tone:'')}><span className="stat-top">{label}{icon}</span>
    <span className="stat-value">{value??'—'}</span><span className="stat-note">{note}</span></div>
}
