import { useEffect, useState } from 'react'
import { events } from '../../mobile'
import { t } from '../../i18n'
import { elapsedMinutes, foldLive, providerLabel, type LiveState } from './live'

/** K-059: under a running execution, who works on it now and what it last did (from its event stream). */
export function RunLive({sessionId,status}:{sessionId:string;status:string}){
  const [live,setLive]=useState<LiveState|null>(null), [now,setNow]=useState(()=>Date.now())
  useEffect(()=>{
    if(status!=='RUNNING')return
    let mounted=true
    const es=events(`/api/sessions/${sessionId}/events`), seen=new Set<string>()
    es.addEventListener('work_event',e=>{
      const m=e as MessageEvent;if(!mounted||seen.has(m.lastEventId))return;seen.add(m.lastEventId)
      const d=JSON.parse(m.data);setLive(s=>foldLive(s,d))
    })
    es.addEventListener('work_status',e=>{if(JSON.parse((e as MessageEvent).data).status!=='RUNNING')es.close()})
    const tick=setInterval(()=>setNow(Date.now()),15000)
    return()=>{mounted=false;es.close();clearInterval(tick)}
  },[sessionId,status])
  if(status!=='RUNNING'||!live)return null
  const role=live.role==='reviewer'?t('session.role.reviewer'):t('session.role.author')
  const minutes=elapsedMinutes(live.since,now)
  return <div className="run-live" role="status">
    <span className="d" aria-hidden="true"/>
    <span className="run-live-who">{t('work.live.now',{role,provider:providerLabel(live.provider),elapsed:minutes?t('work.live.minutes',{n:minutes}):t('work.live.justNow')})}</span>
    {live.last&&<span className="run-live-last" title={live.last}>{live.kind==='command'?t('work.live.command',{text:live.last}):live.kind==='activity'?t('work.live.step',{text:live.last}):t('work.live.output',{text:live.last})}</span>}
  </div>
}
