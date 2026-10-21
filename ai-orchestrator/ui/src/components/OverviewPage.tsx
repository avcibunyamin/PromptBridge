import { useEffect, useState } from 'react'
import { Activity, FileCheck2, Gauge, Plus, RefreshCw, UsersRound } from 'lucide-react'
import { listEnvelopes, listWorks, type Work } from '../api'
import { canCommand, mobile } from '../mobile'
import { Pill, api } from './work/shared'
import { lowest, resetText, tone, type QuotaGroup, type QuotaSnapshot, type QuotaWindow } from './work/usage'
import { SetupGuide } from './SetupGuide'

const TITLES:Record<string,string> = {gemini:'Gemini',claude_gpt:'Claude ve GPT',argon:'Gemini 4 Argon',claude:'Claude',codex:'Codex'}
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
      <div><p className="eyebrow">YEREL AJAN ORKESTRASYONU</p><h1>Ekibin hazır.</h1>
        <p>Yazar değişikliği yapar, inceleyici kontrol eder; sonuç senin onayınla klasörüne gelir.</p></div>
      {canCommand()&&<button className="btn primary" onClick={onNewWork}><Plus aria-hidden/>Yeni çalışma</button>}
    </div>
    <SetupGuide works={works} onNewWork={onNewWork} onSettings={onSettings}/>
    {error&&<p className="error-banner" role="alert">{error}</p>}
    <div className="stats-grid">
      <Stat icon={<Activity aria-hidden/>} label="Aktif çalışma" value={active} note={running?`${running} yürütme sürüyor`:'Şu an yürütme yok'}/>
      <button className="stat-card as-button" onClick={onApprovals}>
        <span className="stat-top">Onay bekleyen<FileCheck2 aria-hidden/></span>
        <span className="stat-value">{waiting??'—'}</span><span className="stat-note">Dosya onayları</span></button>
      <Stat icon={<UsersRound aria-hidden/>} label="Bağlı hesap" value={accounts.length?usable.length:null} note={accounts.length?`${accounts.length} hesap okundu`:'Henüz okunmadı'}/>
      <Stat icon={<Gauge aria-hidden/>} label="En düşük kota" value={low?`%${Math.round(low.left)}`:null} note={low?.where??'Henüz okunmadı'} tone={tone(low?.left??null)}/>
    </div>
    <div className="overview-grid">
      <section className="panel">
        <div className="panel-heading"><div><p className="eyebrow">SON ÇALIŞMALAR</p><h2>Kaldığın yerden devam et</h2></div></div>
        {works===null?<p className="muted small">Yükleniyor…</p>:!works.length?<p className="muted small">Henüz bir çalışma yok.</p>:
        <ul className="recent">{works.slice(0,6).map(w=><li key={w.id}><button onClick={()=>onOpenWork(w.id)}>
          <span className="recent-title">{w.title}<span className="mono small muted">{w.workspace_path.split('/').pop()}</span></span>
          {w.latest_session?<Pill status={w.latest_session.status}/>:<span className="pill idle"><span className="d" aria-hidden/>Yürütme yok</span>}
        </button></li>)}</ul>}
      </section>
      <section className="panel accent">
        <div className="panel-heading"><div><p className="eyebrow">HESAPLAR VE KOTA</p><h2>Kim ne kadar çalışabilir</h2></div>
          {!mobile.active&&<button className="icon-btn" disabled={refreshing} aria-busy={refreshing} onClick={()=>void refresh()} aria-label="Kotaları yenile"
            title="agy /usage ve Codex kota uçlarını okur (model çağrısı yok)"><RefreshCw className={refreshing?'spin':undefined} aria-hidden/></button>}</div>
        {!accounts.length?<p className="muted small">Henüz okunmadı. {mobile.active?'':'Sağ üstten yenileyin.'}</p>:
        <ul className="accounts">{accounts.map(([name,a])=><li key={name}>
          <p className="account-name mono">{name}{a.error&&<span className="pill warn">okunamadı</span>}</p>
          {a.groups.map(g=>{const w=bottleneck(g), l=lowest(g), soon=w?resetText(w):null   // the window that runs out first
            return <div key={g.key} className="quota-row">
              <span className="small">{TITLES[g.key]??g.title}</span>
              <div className={'bar '+tone(l)} aria-hidden="true"><span style={{width:`${l??0}%`}}/></div>
              <span className="small mono">{l===null?'—':`%${Math.round(l)}`}</span>
              {soon&&l!==null&&l<100&&<span className="small muted">{soon} sonra yenilenir</span>}
            </div>})}
        </li>)}</ul>}
        <p className="small muted">Claude kotası bir Claude adımı çalışınca görünür.</p>
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
