import { mobile } from '../../mobile'
import { useState } from 'react'
import { ApiError } from '../../api'
import { api, active, duration, outcomeLabels, verdictLabels, invalidCodes, Modal } from './shared'

export type Loop = {max_turns:number;max_active_ms:number;current_turn:number;phase:'author'|'review'|'done';pause_requested_at:string|null;
  pause_revision:number;outcome:string|null;outcome_reason:string|null;last_author_unreviewed:number;used_ms:number;time_estimated:boolean}
export type Finding = {id:string;severity:'high'|'medium'|'low';summary:string;evidence:string[]}
export type Turn = {turn_no:number;author_step_id:string;reviewer_step_id:string;verdict:string|null;invalid_code:string|null;ended_reason:string|null;findings:Finding[]|null}
type Change = {path:string;certainty:string}
type Changed = {digest:string;changes:Change[];comparable:boolean;unknown:string[]}

const severity:Record<string,string> = {high:'Yüksek',medium:'Orta',low:'Düşük'}

/** F3-02 turn loop: counters, outcome, pause/resume (digest-bound), and the per-turn verdicts.
 *  Everything shown comes from GET /api/sessions/{id} (loop, turns). */
export function LoopView({sessionId,status,loop,turns,onChanged,onResumed}:{sessionId:string;status:string;loop:Loop;turns:Turn[];onChanged:()=>void;onResumed:()=>void}){
  const [busy,setBusy]=useState(false), [error,setError]=useState(''), [changed,setChanged]=useState<Changed|null>(null)
  const running=active(status)&&loop.phase!=='done'
  async function pause(){setBusy(true);setError('');try{await api(`/api/sessions/${sessionId}/pause`,{method:'POST'});onChanged()}
    catch(e){setError(String(e))}finally{setBusy(false)}}
  async function resume(digest?:string){setBusy(true);setError('');try{
    await api(`/api/sessions/${sessionId}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm:true,pause_revision:loop.pause_revision,...(digest?{diff_digest:digest}:{})})})
    setChanged(null);onResumed()
  }catch(e){
    const body=e instanceof ApiError?e.body:undefined
    if(e instanceof ApiError&&e.code==='workspace_changed'&&body){
      // Show exactly what changed; the next confirmation is bound to this digest.
      setChanged({digest:String(body.diff_digest),changes:(body.changes as Change[])||[],comparable:!!body.comparable,unknown:(body.unknown as string[])||[]})
    }else{setChanged(null);setError(e instanceof ApiError&&e.code==='workspace_busy'?'Klasör başka bir yürütmede kullanılıyor; o bitince devam edin.':
      e instanceof ApiError&&e.code==='stale_pause'?'Duraklatma durumu değişti; sayfa güncellendi.':String(e));onChanged()}
  }finally{setBusy(false)}}

  return <section className="card loop"><div className="card-h"><h2>Döngü</h2><span className="spacer"/>
      <span className="chain-note">Tur {loop.current_turn} / {loop.max_turns} · Etkin süre {duration(loop.used_ms)} / {duration(loop.max_active_ms)}{loop.time_estimated?' (tahmini süre içerir)':''}</span></div>
    <div className="card-b loop-body">
      {error&&<div className="error-banner" role="alert">{error}</div>}
      {loop.outcome&&<p className={'loop-outcome '+(loop.outcome==='verified'?'ok':'warn')} role="status"><strong>{outcomeLabels[loop.outcome]||loop.outcome}</strong>
        {loop.outcome==='review_invalid'&&loop.outcome_reason?` — ${invalidCodes[loop.outcome_reason]||loop.outcome_reason}`:''}</p>}
      {!!loop.last_author_unreviewed&&<p className="notice" role="status">Son yazar çıktısı incelenmedi: süre bütçesi yazar ya da inceleyici çalışırken doldu.</p>}
      {running&&loop.pause_requested_at&&<p className="info-note" role="status">Duraklatma istendi: çalışan adım bitince duraklayacak (adım yarıda kesilmez).</p>}
      {!mobile.active&&running&&!loop.pause_requested_at&&<button className="btn" disabled={busy} onClick={()=>void pause()}>Adım bitince duraklat</button>}
      {status==='PAUSED'&&<div className="paused">
        <p>Duraklatıldı — tur {loop.current_turn}, sıradaki adım: {loop.phase==='author'?'yazar':'inceleyici'}. Devam edince bu adımdan sürer; kilit o an yeniden alınır.</p>
        {!mobile.active&&<button className="btn primary" disabled={busy} onClick={()=>void resume()}>{busy?'…':'Devam et'}</button>}
      </div>}
      {changed&&<Modal label="Duraklama sırasında değişiklik" busy={busy} onClose={()=>setChanged(null)}>
        <h3>Klasör duraklama sırasında değişti</h3>
        {!changed.comparable&&<p className="notice">Klasör tam taranamadı ya da kapsam değişti; değişiklik olmadığı söylenemez.</p>}
        {!!changed.changes.length&&<ul className="change-list">{changed.changes.map(c=><li key={c.path} className="mono">{c.path}{c.certainty!=='hash'?' (kesin değil)':''}</li>)}</ul>}
        {!!changed.unknown.length&&<p className="chain-note">Taranamayan: {changed.unknown.join(', ')}</p>}
        <p>Devam ederseniz sıradaki yazar taze bir konuşmayla başlar ve bu farklar ona veri olarak iletilir. Onay yalnız bu listedeki değişiklikler için geçerlidir; klasör yeniden değişirse tekrar sorulur.</p>
        <div className="modal-actions"><button className="btn" disabled={busy} onClick={()=>setChanged(null)}>Vazgeç</button>
          <button className="btn primary" disabled={busy} onClick={()=>void resume(changed.digest)}>{busy?'…':'Farkları gördüm, devam et'}</button></div>
      </Modal>}
      <ol className="turns">{turns.map(t=><li key={t.turn_no} className="turn">
        <div className="turn-head"><strong>Tur {t.turn_no}</strong><span className="spacer"/>
          {t.verdict?<span className={'pill '+(t.verdict==='pass'?'ok':t.verdict==='changes_requested'?'idle':'warn')}><span className="d" aria-hidden="true"/>{verdictLabels[t.verdict]||t.verdict}</span>
            :<span className="pill idle"><span className="d" aria-hidden="true"/>İnceleme bekleniyor</span>}</div>
        {t.verdict==='invalid'&&<p className="chain-note">İnceleme sonucu okunamadı: {invalidCodes[t.invalid_code||'']||t.invalid_code}. Başarı sayılmadı.</p>}
        {!!t.findings?.length&&<><p className="chain-note">İnceleyicinin beyanı — bağımsız doğrulama değildir.</p>
          <ul className="findings">{t.findings.map(f=><li key={f.id}><span className={'sev '+f.severity}>{severity[f.severity]}</span><span className="mono muted">{f.id}</span> <span className="finding-text">{f.summary}</span>
            {!!f.evidence.length&&<div className="evidence mono">{f.evidence.join(' · ')}</div>}</li>)}</ul></>}
      </li>)}</ol>
    </div></section>
}
