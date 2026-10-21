import { canCommand, mobile } from '../mobile'
import { useCallback, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { ChevronLeft, Pin, Plus } from 'lucide-react'
import { deleteWork, isolationRemove, listWorks, listLegacySessions, patchWork, type Work, type WorksPage, type LegacySession } from '../api'
import { deleteWorkErrorMessage, loadSettings, PINNED_WORKS_LIMIT, saveSettings, sortPinnedWorks, togglePinnedWork, workMenuItems, type WorkMenuAction } from '../settings'
import { Modal, Pill } from './work/shared'
import { NewWorkForm } from './work/NewWorkForm'
import { WorkView } from './work/WorkView'
import { SessionView } from './work/SessionView'

type Route = {kind:'home'} | {kind:'new'} | {kind:'work';id:string} | {kind:'session';id:string}
// Hash routes survive a reload: #/works/<id>, #/sessions/<id> (earlier, unlinked runs), #/new, and #/ (home).
// Home is the new-work form on a wide screen and the work list alone on a phone (like the Claude app).
function parseRoute():Route{
  const [,kind,id]=location.hash.match(/^#\/(works|sessions)\/(.+)$/)||[]
  return kind==='works'?{kind:'work',id:decodeURIComponent(id)}:kind==='sessions'?{kind:'session',id:decodeURIComponent(id)}
    :location.hash==='#/new'?{kind:'new'}:{kind:'home'}
}
const hashOf=(r:Route)=>r.kind==='home'?'#/':r.kind==='new'?'#/new':`#/${r.kind==='work'?'works':'sessions'}/${encodeURIComponent(r.id)}`

const MENU_LABELS: Record<WorkMenuAction, string> = {
  pin: 'Sabitle',
  unpin: 'Sabitlemeyi kaldır',
  rename: 'Yeniden adlandır',
  archive: 'Arşivle',
  unarchive: 'Arşivden çıkar',
  remove_isolation: 'Kopyayı kaldır',
  delete: 'Sil',
}

/** Merge a fresh first page into what is loaded: the keyset cursor of later pages stays valid under inserts. */
function mergeFirst<T extends {id:string}>(loaded:T[], first:T[], pages:number):T[]{
  if(pages<=1)return first
  const ids=new Set(first.map(x=>x.id))
  return [...first,...loaded.filter(x=>!ids.has(x.id))]
}

/** TB-20: the app's sidebar too. `head`/`foot` frame the work list; `override` replaces the main pane (approvals,
 *  settings); choosing a work or “Yeni çalışma” calls `onOpen` so the shell shows works again. */
export function WorkPanel({head,foot,top,override,onOpen}:{head?:ReactNode;foot?:ReactNode;top?:ReactNode;override?:ReactNode;onOpen?:()=>void}={}){
  // K-044 / #310: all hooks are called unconditionally at the top of the component before any early return.
  const [route,setRoute]=useState<Route>(parseRoute)
  const [archived,setArchived]=useState(false)
  const [works,setWorks]=useState<Work[]>([]), [page,setPage]=useState<Omit<WorksPage,'items'>|null>(null), [pages,setPages]=useState(0)
  const [legacy,setLegacy]=useState<LegacySession[]>([]), [legacyCursor,setLegacyCursor]=useState<string|null>(null), [legacyPages,setLegacyPages]=useState(0)
  const [listError,setListError]=useState(''), [loadingMore,setLoadingMore]=useState(false)
  const [pinnedIds,setPinnedIds]=useState<string[]>([])
  const [menuId,setMenuId]=useState<string|null>(null)
  const [renamingId,setRenamingId]=useState<string|null>(null), [renameTitle,setRenameTitle]=useState(''), [renameBusy,setRenameBusy]=useState(false)
  const [removingIso,setRemovingIso]=useState<Work|null>(null), [removeIsoBusy,setRemoveIsoBusy]=useState(false), [removeIsoError,setRemoveIsoError]=useState('')
  const [deleting,setDeleting]=useState<Work|null>(null), [deleteBusy,setDeleteBusy]=useState(false), [deleteError,setDeleteError]=useState('')
  const filterRef=useRef(archived); filterRef.current=archived   // drops answers for a filter no longer shown
  // the phone's back button steps history once when we came from the list (Android back agrees), else goes to it
  const fromList=useRef(false)
  const menuRef=useRef<HTMLDivElement|null>(null)
  const renameInputRef=useRef<HTMLInputElement|null>(null)

  // back at the list by any way (Android back, the tab): the next '‹ Çalışmalar' must not step history again
  useEffect(()=>{const on=()=>{const r=parseRoute();if(r.kind==='home')fromList.current=false;setRoute(r)};addEventListener('hashchange',on);return()=>removeEventListener('hashchange',on)},[])
  const go=(r:Route)=>{onOpen?.();fromList.current=route.kind==='home'&&r.kind!=='home';if(location.hash!==hashOf(r))location.hash=hashOf(r);else setRoute(r)}
  const back=()=>{if(fromList.current){fromList.current=false;history.back()}else go({kind:'home'})}

  const refreshWorks=useCallback(async()=>{
    try{const first=await listWorks(archived);if(filterRef.current!==archived)return
      setWorks(w=>mergeFirst(w,first.items,pages));setPage(p=>pages<=1||!p?first:{...first,next_cursor:p.next_cursor});setPages(n=>Math.max(n,1));setListError('')
    }catch(e){setListError(String(e))}
  },[archived,pages])
  const refreshLegacy=useCallback(async()=>{
    try{const first=await listLegacySessions()
      setLegacy(l=>mergeFirst(l,first.items,legacyPages));if(legacyPages<=1)setLegacyCursor(first.next_cursor);setLegacyPages(n=>Math.max(n,1))
    }catch(e){setListError(String(e))}
  },[legacyPages])

  // Switching the archive filter starts a fresh keyset listing.
  useEffect(()=>{setWorks([]);setPage(null);setPages(0);setMenuId(null);setRenamingId(null)},[archived])
  useEffect(()=>{void refreshWorks();const t=setInterval(()=>void refreshWorks(),5000);return()=>clearInterval(t)},[refreshWorks])
  useEffect(()=>{void refreshLegacy();const t=setInterval(()=>void refreshLegacy(),15000);return()=>clearInterval(t)},[refreshLegacy])
  useEffect(()=>{void loadSettings().then(v=>{if(v?.settings.pinned_works)setPinnedIds(v.settings.pinned_works)})},[])

  useEffect(()=>{
    if(!menuId)return
    const first=menuRef.current?.querySelector<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)')
    first?.focus()
    const onMouseDown=(e:MouseEvent)=>{
      const t=e.target as Node|null
      if(t&&menuRef.current?.parentElement?.contains(t))return
      setMenuId(null)
    }
    const onKeyDown=(e:KeyboardEvent)=>{
      if(e.key==='Escape'){
        e.preventDefault()
        const trigger=menuRef.current?.parentElement?.querySelector<HTMLButtonElement>('.session-more')
        setMenuId(null)
        trigger?.focus()
      }else if(e.key==='Tab'){
        setMenuId(null)
      }
    }
    document.addEventListener('mousedown',onMouseDown)
    document.addEventListener('keydown',onKeyDown)
    return()=>{document.removeEventListener('mousedown',onMouseDown);document.removeEventListener('keydown',onKeyDown)}
  },[menuId])

  useEffect(()=>{
    if(!renamingId)return
    renameInputRef.current?.focus()
    renameInputRef.current?.select()
  },[renamingId])

  async function moreWorks(){if(!page?.next_cursor)return;setLoadingMore(true);try{
    const next=await listWorks(archived,page.next_cursor);if(filterRef.current!==archived)return
    setWorks(w=>{const ids=new Set(w.map(x=>x.id));return [...w,...next.items.filter(x=>!ids.has(x.id))]});setPage(next);setPages(n=>n+1)
  }catch(e){setListError(String(e))}finally{setLoadingMore(false)}}
  async function moreLegacy(){if(!legacyCursor)return;setLoadingMore(true);try{
    const next=await listLegacySessions(legacyCursor)
    setLegacy(l=>{const ids=new Set(l.map(x=>x.id));return [...l,...next.items.filter(x=>!ids.has(x.id))]});setLegacyCursor(next.next_cursor);setLegacyPages(n=>n+1)
  }catch(e){setListError(String(e))}finally{setLoadingMore(false)}}

  async function onTogglePin(w:Work){
    setMenuId(null)
    if(!pinnedIds.includes(w.id)&&pinnedIds.length>=PINNED_WORKS_LIMIT){
      setListError(`En fazla ${PINNED_WORKS_LIMIT} çalışma sabitlenebilir.`)
      return
    }
    const prev=pinnedIds
    const next=togglePinnedWork(pinnedIds,w.id)
    setPinnedIds(next)
    try{
      const saved=await saveSettings({pinned_works:next})
      setPinnedIds(saved.settings.pinned_works??next)
      setListError('')
    }catch(e){
      setPinnedIds(prev)
      setListError(deleteWorkErrorMessage(e))
    }
  }

  function startRename(w:Work){
    setMenuId(null)
    setRenamingId(w.id)
    setRenameTitle(w.title)
  }

  async function submitRename(w:Work){
    const title=renameTitle.trim()
    if(!title){setRenamingId(null);return}
    setRenameBusy(true)
    try{
      const rev=works.find(x=>x.id===w.id)?.revision??w.revision
      const updated=await patchWork(w.id,{expected_revision:rev,title})
      setWorks(ws=>ws.map(x=>x.id===w.id?updated:x))
      setRenamingId(null)
      setListError('')
      void refreshWorks()
    }catch(e){
      setListError(deleteWorkErrorMessage(e))
    }finally{
      setRenameBusy(false)
    }
  }

  async function onToggleArchive(w:Work){
    setMenuId(null)
    try{
      const rev=works.find(x=>x.id===w.id)?.revision??w.revision
      await patchWork(w.id,{expected_revision:rev,archived:!w.archived_at})
      setListError('')
      void refreshWorks()
    }catch(e){
      setListError(deleteWorkErrorMessage(e))
    }
  }

  function openRemoveIsolationModal(w:Work){
    setMenuId(null)
    setRemoveIsoError('')
    setRemovingIso(w)
  }

  async function confirmRemoveIsolation(w:Work){
    setRemoveIsoBusy(true)
    setRemoveIsoError('')
    try{
      await isolationRemove(w.id)
      setRemovingIso(null)
      setListError('')
      void refreshWorks()
    }catch(e){
      setRemoveIsoError(deleteWorkErrorMessage(e))
    }finally{
      setRemoveIsoBusy(false)
    }
  }

  function openDeleteModal(w:Work){
    setMenuId(null)
    setDeleteError('')
    setDeleting(w)
  }

  async function confirmDeleteWork(w:Work){
    setDeleteBusy(true)
    setDeleteError('')
    try{
      const rev=works.find(x=>x.id===w.id)?.revision??w.revision
      await deleteWork(w.id,rev)
      setWorks(ws=>ws.filter(x=>x.id!==w.id))
      if(pinnedIds.includes(w.id)){
        const nextPins=pinnedIds.filter(id=>id!==w.id)
        setPinnedIds(nextPins)
        void saveSettings({pinned_works:nextPins}).catch(()=>{})
      }
      setDeleting(null)
      if(route.kind==='work'&&route.id===w.id){
        fromList.current=false
        if(location.hash!=='#/')location.hash='#/';else setRoute({kind:'home'})
      }
      void refreshWorks()
    }catch(e){
      setDeleteError(deleteWorkErrorMessage(e))
    }finally{
      setDeleteBusy(false)
    }
  }

  function onMenuAction(w:Work,action:WorkMenuAction){
    if(action==='pin'||action==='unpin')void onTogglePin(w)
    else if(action==='rename')startRename(w)
    else if(action==='archive'||action==='unarchive')void onToggleArchive(w)
    else if(action==='remove_isolation')openRemoveIsolationModal(w)
    else if(action==='delete')openDeleteModal(w)
  }

  function onMenuKeyDown(e:ReactKeyboardEvent<HTMLDivElement>){
    const items=Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>('button[role="menuitem"]:not(:disabled)')??[])
    if(!items.length)return
    const idx=items.indexOf(document.activeElement as HTMLButtonElement)
    if(e.key==='ArrowDown'){e.preventDefault();items[(idx+1)%items.length]?.focus()}
    else if(e.key==='ArrowUp'){e.preventDefault();items[(idx-1+items.length)%items.length]?.focus()}
    else if(e.key==='Home'){e.preventDefault();items[0]?.focus()}
    else if(e.key==='End'){e.preventDefault();items[items.length-1]?.focus()}
  }

  const changed=()=>{void refreshWorks()}
  const isNew=route.kind==='new'||route.kind==='home'
  const orderedWorks=sortPinnedWorks(works,pinnedIds)
  const allowCommand=canCommand()
  // ≤600px shows one pane at a time: the list on home, otherwise the open item with a way back (CSS only elsewhere)
  return <div className="body work-layout" data-pane={!override&&route.kind==='home'?'list':'detail'}><aside className="sidebar work-sidebar">
    {head}
    <div className="side-head">{canCommand()&&<button className="newwork" onClick={()=>go({kind:'new'})}><Plus size={16} aria-hidden/>Yeni çalışma</button>}</div>
    <p className="nav-label d-only">SON ÇALIŞMALAR</p>
    <div className="side-filter" role="group" aria-label="Çalışma filtresi">
      <button aria-pressed={!archived} onClick={()=>setArchived(false)}>Aktif{page?` (${page.counts.active})`:''}</button>
      <button aria-pressed={archived} onClick={()=>setArchived(true)}>Arşiv{page?` (${page.counts.archived})`:''}</button>
    </div>
    {listError&&<p className="side-error" role="alert">{listError}</p>}
    <nav className="session-list" aria-label="Çalışmalar">
      {orderedWorks.map(w=>{
        const sel=!override&&route.kind==='work'&&route.id===w.id   // another page open: nothing is selected
        const pinned=pinnedIds.includes(w.id)
        const actions=workMenuItems(w,{pinned,mobile:mobile.active,canCommand:allowCommand})
        const isRenaming=renamingId===w.id
        const statusBadge=w.blocked_reason==='needs_review_decision'
          ?<span className="pill warn"><span className="d" aria-hidden="true"/>Doğrulanmadı — kararınız bekleniyor</span>
          :w.account_wait
          ?<span className="pill warn"><span className="d" aria-hidden="true"/>Hesap bekliyor</span>
          :w.latest_session?<Pill status={w.latest_session.status}/>:<span className="pill idle"><span className="d" aria-hidden="true"/>Yürütme yok</span>
        const timeLine=<span className="tm">{new Date(w.created_at).toLocaleString('tr-TR')}{w.pending_messages?` · ${w.pending_messages} sırada`:''}</span>
        return <div key={w.id} className={`session-row ${sel?'sel':''}`}>
          {isRenaming
            ?<div className={`session ${sel?'sel':''}`}>
              <input ref={renameInputRef} className="session-rename" aria-label="Çalışma adı" value={renameTitle} disabled={renameBusy}
                onChange={e=>setRenameTitle(e.target.value)}
                onKeyDown={e=>{
                  if(e.key==='Enter'){e.preventDefault();e.stopPropagation();void submitRename(w)}
                  else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();setRenamingId(null)}
                }}/>
              {statusBadge}
              {timeLine}
            </div>
            :<button aria-pressed={sel} className={`session ${sel?'sel':''}`} onClick={()=>go({kind:'work',id:w.id})}>
              <span className="nm">{pinned&&<Pin size={12} className="pin-mark" aria-label="Sabitlendi"/>}{w.title}</span>
              {statusBadge}
              {timeLine}
            </button>}
          {actions.length>0&&<button type="button" className="session-more" aria-label={`${w.title} — çalışma menüsü`} aria-haspopup="menu"
            aria-expanded={menuId===w.id} onClick={e=>{e.stopPropagation();setMenuId(cur=>cur===w.id?null:w.id)}}>⋯</button>}
          {actions.length>0&&menuId===w.id&&<div ref={menuRef} role="menu" aria-label="Çalışma menüsü" className="work-menu" onKeyDown={onMenuKeyDown}>
            {actions.map(act=><button key={act} type="button" role="menuitem" className={act==='delete'?'danger':undefined}
              onClick={e=>{e.stopPropagation();onMenuAction(w,act)}}>{MENU_LABELS[act]}</button>)}
          </div>}
        </div>})}
      {page&&!works.length&&<p className="empty-list">{archived?'Arşivde çalışma yok.':<>Henüz bir çalışma yok.<br/>İlk görevinizle başlayın.</>}</p>}
      {!page&&!listError&&<p className="loading">Yükleniyor…</p>}
      {page?.next_cursor&&<button className="btn small more" disabled={loadingMore} onClick={()=>void moreWorks()}>Daha fazla</button>}
    </nav>
    <details className="legacy" open={!override&&route.kind==='session'}>
      <summary className="side-label">Önceki yürütmeler <span>{legacy.length}{legacyCursor?'+':''}</span></summary>
      <nav className="session-list" aria-label="Önceki yürütmeler">
        {legacy.map(s=><button key={s.id} aria-pressed={!override&&route.kind==='session'&&route.id===s.id} className={`session ${!override&&route.kind==='session'&&route.id===s.id?'sel':''}`} onClick={()=>go({kind:'session',id:s.id})}>
          <span className="nm">{s.workspace_path.split('/').pop()||s.workspace_path}</span><Pill status={s.status} recovered={!!s.recovered_by_human}/><span className="tm">{new Date(s.created_at).toLocaleString('tr-TR')}</span>
        </button>)}
        {legacyPages>0&&!legacy.length&&<p className="empty-list">Çalışmaya bağlı olmayan eski yürütme yok.</p>}
        {legacyCursor&&<button className="btn small more" disabled={loadingMore} onClick={()=>void moreLegacy()}>Daha fazla</button>}
      </nav>
    </details>
    {foot}
  </aside><main className="main work-main">{top}{!override&&<button className="m-back" onClick={back}><ChevronLeft size={18} aria-hidden/>Çalışmalar</button>}
    {override??<div className="pane">
    {isNew&&!allowCommand&&<p className="empty-list">Listeden bir çalışma seçin. Bu telefon yalnız izler ve dosya onaylar; görev vermek için Mac'te bu cihaza “Görev verebilir” izni verin.</p>}
    {isNew&&allowCommand&&<NewWorkForm folders={mobile.active?[...new Set(works.map(w=>w.workspace_path))]:undefined} onCreated={id=>{void refreshWorks();go({kind:'work',id})}}/>}
    {route.kind==='work'&&<WorkView key={route.id} workId={route.id} onChange={changed}/>}
    {route.kind==='session'&&<><p className="info-note">Önceki sürümden kalan, çalışmaya bağlı olmayan yürütme. Bu sürümde buna yeni mesaj eklenmez; durdurma, yeniden deneme ve kurtarma kontrolleri aynen geçerlidir.</p>
      <SessionView key={route.id} sessionId={route.id} onChange={()=>void refreshLegacy()}/></>}
  </div>}
    {removingIso&&<Modal label="Kopyayı kaldır" busy={removeIsoBusy} onClose={()=>{if(!removeIsoBusy){setRemovingIso(null);setRemoveIsoError('')}}}>
      <h3>Kopyayı kaldır</h3>
      <p>{removingIso.title} çalışmasının ayrı kopyası kaldırılsın mı? Kopya klasörü silinir; dal ana klasöre tamamen birleşmişse o da silinir, yoksa kalır. Kaydedilmemiş değişiklik varsa reddedilir.</p>
      {removeIsoError&&<p className="error-banner" role="alert">{removeIsoError}</p>}
      <div className="modal-actions">
        <button type="button" className="btn" disabled={removeIsoBusy} onClick={()=>{setRemovingIso(null);setRemoveIsoError('')}}>Vazgeç</button>
        <button type="button" className="btn danger" disabled={removeIsoBusy} onClick={()=>void confirmRemoveIsolation(removingIso)}>Kopyayı kaldır</button>
      </div>
    </Modal>}
    {deleting&&<Modal label="Çalışmayı sil" busy={deleteBusy} onClose={()=>{if(!deleteBusy){setDeleting(null);setDeleteError('')}}}>
      <h3>Çalışmayı sil</h3>
      <p>{deleting.title} silinsin mi? Listelerden kalıcı olarak kalkar; kayıtlar denetim için saklanır.</p>
      {deleteError&&<p className="error-banner" role="alert">{deleteError}</p>}
      <div className="modal-actions">
        <button type="button" className="btn" disabled={deleteBusy} onClick={()=>{setDeleting(null);setDeleteError('')}}>Vazgeç</button>
        <button type="button" className="btn danger" disabled={deleteBusy} onClick={()=>void confirmDeleteWork(deleting)}>Sil</button>
      </div>
    </Modal>}
  </main></div>
}
