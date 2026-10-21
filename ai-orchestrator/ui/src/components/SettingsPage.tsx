import { useCallback, useEffect, useState } from 'react'
import { LoaderCircle, RotateCcw, Save } from 'lucide-react'
import { AccountsCard } from './AccountsCard'
import { MobileSettings } from './MobileSettings'
import { canCommand, mobile } from '../mobile'
import { NotificationsCard } from './NotificationsCard'
import { ModelsCard } from './ModelsCard'
import { githubStatus } from '../api'
import type { GhStatus } from '../githubRules'
import { TeamPicker, type TeamSelection } from './work/TeamPicker'
import { baseOf } from './work/team'
import { api, teamOf, type Policy } from './work/shared'
import { acceptedDigest, defaultTeam, loadSettings, saveSettings, type SettingsPatch, type SettingsView, type Theme } from '../settings'

const themes: [Theme, string][] = [['system', 'Sistem'], ['dark', 'Koyu'], ['light', 'Açık']]

/** TB-19 (K-026): preferences saved to .system/settings.json. The provider ceiling stays in providers.yaml.
 *  K-033: a phone allowed to give work sees the default team, the theme and the ceiling; the rest stays on the Mac. */
export function SettingsPage({ onTheme }: { onTheme: (t: Theme) => void }) {
  const [view, setView] = useState<SettingsView | null>(null), [policy, setPolicy] = useState<Policy | null>(null)
  const [sel, setSel] = useState<TeamSelection | null>(null), [cloneDir, setCloneDir] = useState('')
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [saved, setSaved] = useState('')
  const [gh, setGh] = useState<GhStatus | null>(null)   // TB-21: account name only
  useEffect(() => { if (!mobile.active) githubStatus().then(setGh, () => setGh({ installed: false, logged_in: false, account: null })) }, [])

  const loadPolicy = useCallback(async () => { setPolicy(await api<Policy>('/api/sessions/providers')) }, [])
  useEffect(() => {
    // a phone without the command scope has no settings to read (the gateway would refuse and audit it; agy P3)
    void Promise.all([mobile.active && !canCommand() ? Promise.resolve(null) : loadSettings(), loadPolicy()]).then(([v]) => { setView(v); setCloneDir(v?.settings.github?.clone_dir ?? '') })   // a phone gets no github field (K-033/4b)
      .catch(e => setError(String(e)))
  }, [loadPolicy])

  async function save(patch: SettingsPatch, label: string) {
    setBusy(true); setError(''); setSaved('')
    try {
      const v = await saveSettings(patch); setView(v); setSaved(label)
      if (patch.theme) onTheme(patch.theme)
    } catch (e) { setError(String(e)) } finally { setBusy(false) }
  }

  const accepted = !!view && !!acceptedDigest(view)
  const unchanged = !!sel?.preview && sel.preview.digest === acceptedDigest(view)
  const allowed = policy?.ceiling?.allowed
  return <div className="body mobile-body"><main className="main"><div className="pane settings-page">
    <div className="run-head"><div><h1>Ayarlar</h1>
      <p className="intro">{mobile.active ? 'Bu telefonun bildirimleri; görev verme izni varsa varsayılan takım ve tema. Diğer ayarlar Mac\'te.' : 'Bu Mac\'teki tercihler.'} İzinli sağlayıcılar ve modeller (tavan) yalnız <code>providers.yaml</code> dosyasından değişir.</p></div></div>
    {error && <div className="error-banner" role="alert">{error}</div>}
    {view?.error && <p className="notice">Ayar dosyası okunamadı; varsayılanlar kullanılıyor. Kaydetmek dosyayı düzeltir.</p>}
    {saved && <p className="muted small" role="status">{saved}</p>}

    {mobile.active && <NotificationsCard />}
    {canCommand() && <>
    <section className="card"><div className="card-h"><h2>Varsayılan takım</h2><span className="spacer" />
      {accepted && <span className="pill ok"><span className="d" aria-hidden="true" />Kayıtlı ve kabul edildi</span>}</div>
      <div className="card-b form-fields">
        <p className="muted">Yeni çalışmalarda ve mesaj kutusunda ilk seçili gelen yazar, inceleyici ve yedekler. <strong>Kaydetmek bu takımı kabul etmek demektir:</strong> bu takımla gönderilen mesajda kabul kutusu çıkmaz; başka bir model seçersen ya da <code>providers.yaml</code> değişirse yeniden sorulur.</p>
        {!!view?.problems.length && <p className="notice">Kayıtlı takım artık izinli değil (<code>providers.yaml</code> değişmiş). Şimdilik dosyadaki varsayılan kullanılıyor; yeni bir takım kaydedin.</p>}
        {policy && view !== null ? <TeamPicker policy={policy} initial={defaultTeam(view) ?? teamOf(policy.policy)} onReloadPolicy={loadPolicy} onChange={setSel} />
          : policy ? null : <p className="loading">Yükleniyor…</p>}
        <div className="form-footer">
          {view?.settings.team && <button className="btn" disabled={busy} onClick={() => void save({ team: null }, 'Varsayılan takım dosyadaki takıma döndü.')}>
            <RotateCcw aria-hidden />Dosyadaki takıma dön</button>}
          <button className="btn primary" disabled={busy || !sel?.preview || unchanged} onClick={() => sel && void save({ team: sel.team }, 'Varsayılan takım kaydedildi ve kabul edildi.')}>
            {busy ? <LoaderCircle className="spin" aria-hidden /> : <Save aria-hidden />}Varsayılan olarak kaydet ve kabul et</button>
        </div>
      </div></section>

    {!mobile.active && <AccountsCard onChanged={loadPolicy} />}

    <ModelsCard efforts={Object.fromEntries(Object.entries(policy?.capabilities ?? {}).map(([k, c]) => [k, c.efforts ?? []]))}
      bases={[...new Set([...(allowed?.author ?? []), ...(allowed?.reviewer ?? [])].map(baseOf))]} />

    <section className="card"><div className="card-h"><h2>Görünüm</h2></div><div className="card-b form-fields">
      <label htmlFor="set-theme">Tema<select id="set-theme" value={view?.settings.theme ?? 'system'} disabled={busy || !view}
        onChange={e => void save({ theme: e.target.value as Theme }, 'Tema kaydedildi.')}>
        {themes.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
    </div></section>
    </>}

    {!mobile.active && <>
    <section className="card"><div className="card-h"><h2>GitHub</h2></div><div className="card-b form-fields">
      <label htmlFor="set-clone">Depoların klonlanacağı klasör<input id="set-clone" className="mono" value={cloneDir} placeholder="/Users/…/Projeler"
        onChange={e => setCloneDir(e.target.value)} /></label>
      <p className="muted small">Ev klasörünüzün altında var olan bir klasör. Yeni çalışmada “GitHub'dan” seçilen depo buraya klonlanır.</p>
      <p className="small" role="status">{!gh ? 'GitHub oturumu kontrol ediliyor…' : !gh.installed ? <>GitHub CLI kurulu değil (<code>brew install gh</code>).</>
        : gh.logged_in ? <>Bu Mac'teki <code>gh</code> oturumu: <strong>{gh.account}</strong>. PromptBridge jetonu görmez; yalnız <code>gh</code>'i çalıştırır.</>
        : <>GitHub'a giriş yapılmamış: Terminal'de <code>gh auth login</code>.</>}</p>
      <div className="form-footer"><button className="btn" disabled={busy || !view || cloneDir === (view.settings.github.clone_dir ?? '')}
        onClick={() => void save({ github: { clone_dir: cloneDir.trim() || null } }, 'Klon klasörü kaydedildi.')}><Save aria-hidden />Kaydet</button></div>
    </div></section>

    <section className="card"><div className="card-h"><h2>Önizleme</h2></div><div className="card-b form-fields">
      <label className="accept-row"><input type="checkbox" checked={!!view?.settings.preview?.sandbox} disabled={busy || !view}
        onChange={e => void save({ preview: { sandbox: e.target.checked } }, e.target.checked ? 'Önizleme korumalı alanda çalışacak.' : 'Korumalı alan kapatıldı.')} />
        <span><strong>Önizlemeyi korumalı alanda çalıştır (deneysel).</strong> Proje kodu yalnız kendi klasörünü, kendi geçici klasörünü ve
          araç zincirlerini (Node, Python) görür; ev klasöründeki <code>.ssh</code>, <code>.netrc</code>, GitHub ve anahtar zinciri kapalıdır,
          dış ağa çıkamaz (yalnız <code>localhost</code>). Dış API kullanan bir proje bu kipte çalışmayabilir. Bir sonraki başlatmada geçerli olur.</span></label>
    </div></section>
    </>}

    {canCommand() && <section className="card"><div className="card-h"><h2>Sağlayıcı tavanı (salt okunur)</h2></div><div className="card-b">
      {allowed ? <ul className="ceiling-list">{(['author', 'reviewer'] as const).map(role => <li key={role}>
        <strong>{role === 'author' ? 'Yazar' : 'İnceleyici'}:</strong> <span className="mono small">{allowed[role].join(', ')}</span></li>)}</ul>
        : <p className="muted">Tavan tanımlı değil; yalnız <code>providers.yaml</code>'daki varsayılan takım kullanılabilir.</p>}
      <p className="muted small">Değiştirmek için <code>ai-orchestrator/.system/providers.yaml</code>; panel bu dosyayı yazmaz.</p>
    </div></section>}

    {!mobile.active && <MobileSettings embedded />}
  </div></main></div>
}
