import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { mobile } from '../../mobile'
import { api } from './shared'
import { lastContext, lowest, percent, quotaFor, resetText, tokens, tone, type Context, type QuotaSnapshot, type StepUsage } from './usage'

const TITLES:Record<string,string>={gemini:'Gemini modelleri',claude_gpt:'Claude ve GPT modelleri',argon:'Gemini 4 Argon',claude:'Claude'}

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
    const t=setInterval(()=>void load().catch(()=>{}),30_000)
    return ()=>{shared.subs.delete(setSnap);clearInterval(t)}
  },[])
  useEffect(()=>{
    if(!sessionId){setCtx(null);return}
    let live=true
    const read=()=>api<{steps:StepUsage[]}>(`/api/sessions/${encodeURIComponent(sessionId)}`).then(d=>{if(live)setCtx(lastContext(d.steps,identity))},()=>{})
    void read()
    const t=setInterval(read,30_000)   // steps finish while the page stays open
    return ()=>{live=false;clearInterval(t)}
  },[sessionId,identity,status])
  useEffect(()=>{
    if(!open)return
    const close=(e:MouseEvent|KeyboardEvent)=>{if(e instanceof KeyboardEvent?e.key==='Escape':!box.current?.contains(e.target as Node))setOpen(false)}
    document.addEventListener('mousedown',close); document.addEventListener('keydown',close)
    return ()=>{document.removeEventListener('mousedown',close);document.removeEventListener('keydown',close)}
  },[open])

  const q=quotaFor(snap,identity), left=lowest(q?.group), pct=percent(ctx), t=tone(left)
  const r=9, c=2*Math.PI*r, fill=pct===null?0:pct/100*c
  const label=`Context ${pct===null?'bilinmiyor':`%${pct}`}${left===null?'':` · kota %${Math.round(left)} kaldı`}`
  async function refresh(){
    setRefreshing(true); setErr('')
    try{const s=await api<QuotaSnapshot>('/api/sessions/quota/refresh',{method:'POST'}); shared.snap=s; shared.at=Date.now(); shared.subs.forEach(f=>f(s))}
    catch(e){setErr(String(e))} finally{setRefreshing(false)}
  }
  return <div className="usage" ref={box}>
    <button type="button" className={'usage-ring '+t} aria-label={label} title={label} aria-expanded={open} onClick={()=>setOpen(o=>!o)}>
      <svg viewBox="0 0 24 24" aria-hidden><circle className="track" cx="12" cy="12" r={r}/>
        <circle className="fill" cx="12" cy="12" r={r} strokeDasharray={`${fill} ${c}`} transform="rotate(-90 12 12)"/></svg>
    </button>
    {open&&<div className="usage-pop" role="dialog" aria-label="Context ve kota">
      <section>
        <h4>Context</h4>
        {ctx?<><div className="bar"><span style={{width:`${pct??0}%`}}/></div>
          <p className="small">{tokens(ctx.used)}{ctx.window?` / ${tokens(ctx.window)}${ctx.approx?' (yaklaşık)':''} · %${pct}`:' token'}</p></>
          :<p className="small muted">Bu model bu çalışmada henüz konuşmadı; yeni bir context ile başlar.</p>}
      </section>
      <section>
        <h4>Kota{q?.account.checked_at?<span className="muted small"> · {new Date(q.account.checked_at*1000).toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit'})}</span>:null}</h4>
        {!q?<p className="small muted">{identity.startsWith('claude')?'Claude kotası Claude bir adım çalıştırınca görünür.':`Henüz okunmadı.${mobile.active?'':' “Yenile” ile alın.'}`}</p>
          :q.account.error?<p className="small muted">Okunamadı ({q.account.error}).</p>
          :q.account.groups.map(g=><div key={g.key} className={'quota-group'+(g.key===q.group?.key?' current':'')}>
            <p className="quota-title">{TITLES[g.key]??g.title}{g.key===q.group?.key?<span className="pill idle">bu model</span>:null}</p>
            {g.windows.map(w=><div key={w.label} className="quota-row">
              <span className="small">{w.label}</span>
              <div className={'bar '+tone(w.remaining)}><span style={{width:`${w.remaining??0}%`}}/></div>
              <span className="small mono">{w.remaining===null?(w.status??'—'):`%${w.remaining.toFixed(w.remaining%1?1:0)}`}</span>
              {resetText(w)&&<span className="small muted">{resetText(w)} sonra</span>}
            </div>)}
          </div>)}
        {!mobile.active&&<button type="button" className="btn small" disabled={refreshing} aria-busy={refreshing} onClick={()=>void refresh()}
          title="agy /usage ve Codex kota uçlarını okur (model çağrısı yok)"><RefreshCw className={refreshing?'spin':undefined} aria-hidden/>{refreshing?'Okunuyor…':'Yenile'}</button>}
        {err&&<p className="small error-text">{err}</p>}
      </section>
    </div>}
  </div>
}
