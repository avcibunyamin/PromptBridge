import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { locale, t } from '../../i18n'
import { mobile } from '../../mobile'
import { api } from './shared'
import { lastContext, lowest, percent, quotaFor, resetText, tokens, tone, type Context, type QuotaSnapshot, type StepUsage } from './usage'

const TITLES:Record<string,string>={gemini:t('shared.usage.title.gemini'),claude_gpt:t('shared.usage.title.claude_gpt'),argon:t('shared.usage.title.argon'),claude:t('shared.usage.title.claude')}

// One poll for every ring on the page; the GET never starts a process (K-039).
let shared:{snap:QuotaSnapshot|null;at:number;subs:Set<(s:QuotaSnapshot)=>void>}={snap:null,at:0,subs:new Set()}
async function load(force=false){
  if(!force&&shared.snap&&Date.now()-shared.at<20_000)return shared.snap
  const snap=await api<QuotaSnapshot>('/api/sessions/quota'); shared.snap=snap; shared.at=Date.now()
  shared.subs.forEach(f=>f(snap)); return snap
}

/** K-039: a ring beside the model and effort: its fill is the context used, its colour the quota left. */
export function UsageRing({identity, sessionId, status}:{identity:string; sessionId?:string|null; status?:string|null}){
  const [snap,setSnap]=useState<QuotaSnapshot|null>(shared.snap), [ctx,setCtx]=useState<Context|null>(null)
  const [open,setOpen]=useState(false), [refreshing,setRefreshing]=useState(false), [err,setErr]=useState('')
  const box=useRef<HTMLDivElement>(null)
  useEffect(()=>{
    shared.subs.add(setSnap); void load().catch(()=>{})
    const timer=setInterval(()=>void load().catch(()=>{}),30_000)
    return ()=>{shared.subs.delete(setSnap);clearInterval(timer)}
  },[])
  useEffect(()=>{
    if(!sessionId){setCtx(null);return}
    let live=true
    const read=()=>api<{steps:StepUsage[]}>(`/api/sessions/${encodeURIComponent(sessionId)}`).then(d=>{if(live)setCtx(lastContext(d.steps,identity))},()=>{})
    void read()
    const timer=setInterval(read,30_000)   // steps finish while the page stays open
    return ()=>{live=false;clearInterval(timer)}
  },[sessionId,identity,status])
  useEffect(()=>{
    if(!open)return
    const close=(e:MouseEvent|KeyboardEvent)=>{if(e instanceof KeyboardEvent?e.key==='Escape':!box.current?.contains(e.target as Node))setOpen(false)}
    document.addEventListener('mousedown',close); document.addEventListener('keydown',close)
    return ()=>{document.removeEventListener('mousedown',close);document.removeEventListener('keydown',close)}
  },[open])

  const q=quotaFor(snap,identity), left=lowest(q?.group), pct=percent(ctx), ringTone=tone(left)
  const r=9, c=2*Math.PI*r, fill=pct===null?0:pct/100*c
  const label=pct===null
    ?(left===null?t('shared.usage.label_unknown'):t('shared.usage.label_unknown_quota',{left:Math.round(left)}))
    :(left===null?t('shared.usage.label_ctx',{pct}):t('shared.usage.label_ctx_quota',{pct,left:Math.round(left)}))
  async function refresh(){
    setRefreshing(true); setErr('')
    try{const s=await api<QuotaSnapshot>('/api/sessions/quota/refresh',{method:'POST'}); shared.snap=s; shared.at=Date.now(); shared.subs.forEach(f=>f(s))}
    catch(e){setErr(String(e))} finally{setRefreshing(false)}
  }
  return <div className="usage" ref={box}>
    <button type="button" className={'usage-ring '+ringTone} aria-label={label} title={label} aria-expanded={open} onClick={()=>setOpen(o=>!o)}>
      <svg viewBox="0 0 24 24" aria-hidden><circle className="track" cx="12" cy="12" r={r}/>
        <circle className="fill" cx="12" cy="12" r={r} strokeDasharray={`${fill} ${c}`} transform="rotate(-90 12 12)"/></svg>
      {ctx&&<span className="usage-num" aria-hidden>{tokens(ctx.used)}{ctx.window?` / ${tokens(ctx.window)}`:''}</span>}
    </button>
    {open&&<div className="usage-pop" role="dialog" aria-label={t('shared.usage.dialog_label')}>
      <section>
        <h4>{t('shared.usage.context_heading')}</h4>
        {ctx?<><div className="bar"><span style={{width:`${pct??0}%`}}/></div>
          <p className="small">{ctx.window
            ?(ctx.approx?t('shared.usage.tokens_approx',{used:tokens(ctx.used),window:tokens(ctx.window),pct:pct??0}):t('shared.usage.tokens_exact',{used:tokens(ctx.used),window:tokens(ctx.window),pct:pct??0}))
            :t('shared.usage.tokens_only',{used:tokens(ctx.used)})}</p></>
          :<p className="small muted">{t('shared.usage.no_context')}</p>}
      </section>
      <section>
        <h4>{t('shared.usage.mcp_heading')}</h4>
        <p className="small muted">{t('shared.usage.mcp_off')}</p>
      </section>
      <section>
        <h4>{t('shared.usage.quota_heading')}{q?.account.checked_at?<span className="muted small"> · {new Date(q.account.checked_at*1000).toLocaleTimeString(locale,{hour:'2-digit',minute:'2-digit'})}</span>:null}</h4>
        {!q?<p className="small muted">{identity.startsWith('claude')?t('shared.usage.claude_unread'):(mobile.active?t('shared.usage.unread_mobile'):t('shared.usage.unread_desktop'))}</p>
          :q.account.error?<p className="small muted">{t('shared.usage.read_error',{error:q.account.error})}</p>
          :q.account.groups.map(g=><div key={g.key} className={'quota-group'+(g.key===q.group?.key?' current':'')}>
            <p className="quota-title">{TITLES[g.key]??g.title}{g.key===q.group?.key?<span className="pill idle">{t('shared.usage.this_model')}</span>:null}</p>
            {g.windows.map(w=><div key={w.label} className="quota-row">
              <span className="small">{w.label}</span>
              <div className={'bar '+tone(w.remaining)}><span style={{width:`${w.remaining??0}%`}}/></div>
              <span className="small mono">{w.remaining===null?(w.status??'—'):t('shared.usage.remaining_pct',{pct:w.remaining.toFixed(w.remaining%1?1:0)})}</span>
              {resetText(w)&&<span className="small muted">{t('shared.usage.resets_in',{time:resetText(w)!})}</span>}
            </div>)}
          </div>)}
        {!mobile.active&&<button type="button" className="btn small" disabled={refreshing} aria-busy={refreshing} onClick={()=>void refresh()}
          title={t('shared.usage.refresh_title')}><RefreshCw className={refreshing?'spin':undefined} aria-hidden/>{refreshing?t('shared.usage.refreshing'):t('shared.usage.refresh')}</button>}
        {err&&<p className="small error-text">{err}</p>}
      </section>
    </div>}
  </div>
}
