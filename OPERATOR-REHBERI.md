# Conductor — Operatör Rehberi

Conductor'ı günlük çalıştırmak, canlıya almak, yedeklemek ve sorun gidermek için. Mimari ayrıntı: `AI-Orkestrasyon-Mimari-Dokumani.md` (§11 Faz 2, §12 Faz 3). Kararlar: `_isbirligi/KARARLAR.md`. Tüm komutlar `ai-orchestrator/` klasöründe çalıştırılır.

> **Temel kural:** Conductor yereldir (yalnız `127.0.0.1`), API anahtarı kullanmaz; sağlayıcılar (Codex, Claude) kendi CLI'ları ve senin oturumunla çalışır. Gerçek model çağıran her iş kota harcar.

---

## 1. Kurulum

```sh
cd ai-orchestrator
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd ui && npm install && npm run build && cd ..     # paneli app/static/ içine üretir
```

Sağlayıcı CLI'ları: `codex` (ChatGPT uygulamasındaki ikili de bulunur) ve `claude`, ikisi de kendi hesabınla giriş yapılmış olmalı. Farklı bir ikili için `CONDUCTOR_CODEX_BIN` / `CONDUCTOR_CLAUDE_BIN` ortam değişkenleri.

**Codex için ayrı giriş (TB-05, K-014):** Conductor Codex'i senin `~/.codex`'inle değil, kendi `.system/codex-home` alanıyla çalıştırır. Bu alana bir kez giriş yap (ChatGPT uygulamasının ikilisiyle):

```sh
CODEX_HOME="$PWD/.system/codex-home" /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex login
```

Giriş yapılmadıysa panelde Codex oturumu “giriş yok” görünür ve Codex adımları AUTH hatasıyla durur. `auth.json`'ı kopyalama ya da bağlama: iki alan aynı yenileme jetonunu paylaşırsa biri diğerinin oturumunu düşürebilir. `.system/codex-home` yedeğin parçasıdır (`.system/` içinde). Senin `~/.codex/config.toml`'unda eski Conductor geçici klasörleri (`conductor-probe-*`, `conductor-live-*`) `trusted` olarak duruyor olabilir; Conductor oraya dokunmaz, istersen elle silebilirsin.

**İkinci hesap (TB-06, K-016):** Zincirde `provider: codex, account: is` (ya da kısaca `codex@is`) yazılırsa o hesap kendi `.system/codex-home-is` alanını kullanır; girişi de oraya ayrı yapılır (`CODEX_HOME="$PWD/.system/codex-home-is" … codex login`). Panelde oturum durumu kimlik başına (`codex`, `codex@is`) ayrı kontrol edilir. `CONDUCTOR_CODEX_HOME` yalnız hesapsız `codex`'i etkiler. Claude için hesap alanı yoktur.

Desteklenen sağlayıcılar **Codex, Claude, Antigravity CLI (`agy`) ve yerel Ollama modelleri**dir (K-013 → K-017, TB-14). Değerlendirilip alınmayanlar: Copilot CLI fizibilitesi açık (TB-18); Lovable, web arayüzü otomasyonu ve Cursor (ücretli plan olmadan) kapalı (K-025).

**Model ve efor seçimi (TB-10, TB-20):** Mesaj kutusundaki model seçici yazarın ana halkasını (sağlayıcı@hesap → model → efor) değiştirir; yanında inceleyici yazar. **Gelişmiş** tam düzenleyiciyi açar: her rolün halkaları ve yedekleri, durum rozetleri, **Oturum durumu** ve **“Yenile”** (politikayı yeniden okur, agy hesaplarında `agy models` çalıştırır, kota harcamaz; yeni modeller için önce bu). Mesaj kutusu ekranın altında sabittir. Seçeneklerin sınırı `providers.yaml` `allowed`'dır; serbest seçim için modelsiz yazın, ör. `allowed: {author: [claude, agy@hesap1, agy@hesap2], reviewer: [claude, agy@hesap2]}`. Claude modelleri “Doğrulanmadı” görünür (Claude CLI liste vermez); çalışan model her adımda kontrol edilir, uyuşmazsa adım durur.

**Antigravity CLI — `agy` (TB-08, K-017/K-018/K-020):** kurulum Google'ın resmî betiğiyle (`curl -fsSL https://antigravity.google/cli/install.sh | bash` → `~/.local/bin/agy`; Conductor bu yolu kendisi bulur, başka yer için `CONDUCTOR_AGY_BIN`). Her Google hesabı kendi `.system/agy-home-<hesap>` alanında çalışır; gerçek `~` hiç kullanılmaz (Antigravity masaüstüyle paylaşılan `~/.gemini/config` dönüştürülmesin). Zincirde model **zorunludur** (varsayılan Flash): `agy@hesap1/claude-sonnet-5-5-high`, `agy@hesap1/gemini-3.8-flash-high`, `agy@hesap2/gemini-3.8-flash-high` (`agy models` listesi; 1.3.5'te Gemini 3.1 Pro kaldırıldı, 2026-10-14). Model listesi değişince panelde **Yenile**, kayıp modeller “model yok” görünür; `providers.yaml`'daki adları güncelleyin.

Giriş sırası (hesap başına bir kez):
1. Önce alanı ve anahtar zincirini Conductor hazırlasın: panelde “Oturum durumunu kontrol et” (ya da ilk `agy` adımı) `.system/agy-home-<hesap>/Library/Keychains/` altında boş parolalı ayrı bir anahtar zinciri oluşturur. Senin anahtar zincirine dokunulmaz.
2. Sonra giriş (`ai-orchestrator/` içinden): `(H="$PWD/.system/agy-home-<hesap>"; cd /tmp && HOME="$H" ~/.local/bin/agy)` → Google OAuth → doğru Gmail → kodu terminale yapıştır → veri toplama tercihi → klasör güveni **“No, exit”**.
3. **Alanı silme.** Silersen anahtar zinciri de gider; Conductor bir sonraki adımda yeniden oluşturur ama o arada yapılan giriş jetonu dosyaya düşer.

Davranış: READ_ONLY varsayılan izin kipi (yazma reddedilir), WRITE `--mode accept-edits`; **komut çalıştırma yoktur** (izin kuralları her süreçten önce boşa sabitlenir). Model komut isterse tur boş biter; Conductor aynı konuşmada **bir kez** “komut yok, arama aracını kullan” diye devam eder (K-020), ikinci retde adım onay bekler. Çalışma klasöründe `.agents/`, `.agent/`, `_agents/` ya da `_agent/` altında `mcp_config.json`, `hooks.json` ya da eklenti MCP tanımı varsa `agy` adımı hiç başlamaz (`agy_workspace_config`). `GEMINI.md`/`AGENTS.md`/skill'ler `agy` tarafından kendisince okunur (bilinen sınırlama). `agy` kendini otomatik günceller. Kaldırma: `rm ~/.local/bin/agy`, `~/.zshrc`/`~/.zprofile` sonundaki “Added by Antigravity CLI installer” satırları, `~/.cache/antigravity`.

**Yerel modeller — Ollama (TB-14):**
- Ollama uygulaması kurulu ve açık olmalı; model `ollama pull <model>` ile indirilir. Conductor yalnız `127.0.0.1:11434`'e bağlanır.
- Model araç çağırabilmeli: araç desteği olmayan model panelde seçilemez.
- Ollama'yı `OLLAMA_CONTEXT_LENGTH=16384` ile başlat; küçük bağlamda panel uyarır.
- `allowed`'a `ollama` (her model) ya da `ollama/<model>` yazılır. Model panelde katalogdan seçilir.
- Ollama, Codex app-server üzerinden çalışır (`.system/ollama-codex-home`, giriş yok). Kota ve hesap kavramı yoktur; kota devri tetiklenmez.
- Yazar adımı klasörde değişiklik yapmazsa adımın altında “Değişiklik yok” notu görünür.

**Efor “(varsayılan)” (TB-17b):** efor boş bırakılırsa sağlayıcı kendi varsayılanını kullanır. Panel bir efor adı yalnız sağlayıcı bildirdiyse gösterir (ör. Codex modelinin `defaultReasoningEffort`'u). Claude için varsayılan bilinmez.

## 2. Sağlayıcı politikası — `.system/providers.yaml`

Şablon: `providers.example.yaml`. Bu dosya **tavandır**; tarayıcı onu değiştirmez.

```yaml
roles:                       # varsayılan takım (panelde ilk seçili gelir)
  author:   [codex, claude]  # yazar: ana → yedek (yalnız doğrulanmış kotada devralır)
  reviewer: [claude]         # inceleyici: salt okunur
allowed:                     # panelde seçilebilecekler (yoksa yalnız varsayılan takım)
  author:   [codex, claude]
  reviewer: [claude, codex]
max_context_continuations: 3 # Codex bağlamı dolarsa adım başına ek oturum (0 = kapalı)
# loop:                      # açılırsa mesajlar yazar ↔ inceleyici turlarıyla yürür
#   max_turns: 3             # 1..5
#   max_active_minutes: 30   # 5..120, yalnız etkin çalışma süresi
```

Değişiklik **çalışan yürütmeleri etkilemez**; her mesaj kendi politikasını dondurur. Politika değişince panel yeni mesaj için takımı yeniden onaylatır.

### 2a. Ayarlar — panelden tercihler (TB-19, K-026)
Mac panelinde sol kenar çubuğunun altındaki **Ayarlar** (telefonda yok). Tercihler `.system/settings.json`'a yazılır (yalnız 8765, `mutation_guard`, 0600, atomik).
- **Varsayılan takım:** yeni çalışmada ve mesaj kutusunda ilk seçili gelen yazar, inceleyici ve yedekler. “Varsayılan olarak kaydet ve kabul et” **o takımı kabul etmek demektir**: bu takımla gönderilen mesajda kabul kutusu çıkmaz (K1). Başka bir model seçilirse ya da `providers.yaml` değişirse kutu yeniden çıkar. “Dosyadaki takıma dön” ayarı siler, `roles` varsayılanı geçerli olur.
- **Tavan değişirse:** kayıtlı takım artık izinli değilse ayar silinmez; panel uyarır ve `roles`'a düşer. Yeni bir takım kaydedin.
- **Tema:** sistem / koyu / açık. Başlıktaki düğme de bu ayarı değiştirir.
- **GitHub klon klasörü:** ev klasörünüzün altında var olan bir klasör. `.system/` ya da Conductor klasörünün içi olamaz. GitHub bağlantısı TB-21'de.
- **Sağlayıcı tavanı** burada yalnız gösterilir; değiştirmek için `providers.yaml` (§2). Panel o dosyayı yazmaz.
- **Mobil erişim** ayarları da burada (§5a).
- Dosya bozulursa panel varsayılanlarla çalışır ve uyarır; bir kayıt dosyayı düzeltir. Elle düzenlemek gerekmez.

### 2b. GitHub (TB-21, K-026/3)
Conductor bu Mac'teki `gh` oturumunu kullanır: jetonu okumaz, saklamaz; yalnız `gh` ve `git`'i sabit argümanlarla, kabuksuz çalıştırır. Hepsi yalnız Mac panelinde; telefonda yok.
- **Kurulum:** `brew install gh`, ardından Terminal'de `gh auth login`. **Ayarlar → GitHub**'da hesap adı görünür; aynı yerde klonların gideceği klasör seçilir.
- **Depodan yeni çalışma:** “Yeni çalışma” → klasör alanının yanındaki **GitHub'dan** → depoyu seç. Depo `<klon klasörü>/<ad>`'a klonlanır, klasör alanı dolar. Aynı depo zaten oradaysa yeniden klonlanmaz. Başka bir içerik ya da symlink varsa reddedilir.
- **Değişiklikleri GitHub'a gönder:** çalışma başlığındaki **GitHub** düğmesi. Yürütme sürerken pasiftir. Üç ayrı adım, her biri ayrı tıklama:
  1. **Kaydet (commit):** değişen dosyalar listelenir. “gizli olabilir” işaretli olanlar (`.env*`, `*.pem`, `*.key`, `id_*`, `credentials*`) seçili gelmez; seçilirse ayrıca onay istenir. Ana daldaysanız `conductor/<tarih>-<başlık>-<id>` dalı açılır; ana dala yazılmaz.
  2. **Gönder (push):** önce gönderilecekler gösterilir: depo, dal, commit'ler, dosya sayısı. `.github/workflows/` değişikliği varsa ayrı onay kutusu çıkar (Actions'ta kod çalıştırabilir). Gösterilen şey onaydan sonra değiştiyse sunucu reddeder; yeniden bakın. Force push yok.
  3. **Taslak PR:** başlık ve açıklama düzenlenebilir. PR her zaman **taslak** açılır; GitHub'da “Ready for review” dersiniz. Bağlantı pencerede görünür ve çalışmanın olaylarına yazılır.
- **Reddedilen depo:** deponun kendi `.git/config`'inde kimlik yardımcısı, kanca klasörü, adres yönlendirme, filtre, `include` gibi anahtarlar varsa hiçbir adım çalışmaz. Pencere anahtarları listeler. Ajanın yazdığı git kancaları hiçbir zaman çalışmaz.
- **Ajanlar ve önizleme senin kimliğinle push yapamaz** (TB-21 adım 1). Önizleme varsayılan olarak sandbox'sız çalışır, ev klasörünü okuyabilir; **Ayarlar → Önizleme**'deki korumalı alanı aç ya da yalnız güvendiğin komutun önizlemesini onayla (TB-22).

### 2c. Ayrı kopyada çalışma (TB-26, K-034)
Ajanların proje klasörünüze hiç dokunmadan çalışması için. Yalnız bir git deposunun kökünde ve en az bir commit varken açılır.
- **Açmak:** “Yeni çalışma” → **Ayrı kopyada çalış**. Conductor `conductor/<tarih>-<başlık>-<id>` dalını ve `~/.conductor/worktrees/<proje>-<id>` kopyasını açar. Çalışma bu kopyada yürür; aynı projede birden fazla kopyalı iş paralel çalışabilir. Telefondan da seçilebilir.
- **Bilmeniz gereken:** kopya, projenin **son commit'inden** açılır; klasördeki kaydedilmemiş değişiklikler kopyaya geçmez. Kopyada `node_modules` gibi izlenmeyen klasörler de yoktur; önizleme gerekiyorsa orada kurulur.
- **Sonucu almak:** çalışmada **Ana klasöre getir** (yalnız Mac, yürütmeler arasında):
  - pencere kopyadaki dosyaları ve commit'leri gösterir; `.gitmodules`, `.gitattributes` ya da iş akışı değiştiyse ayrıca uyarır;
  - onaylayınca kopyadaki değişiklikler kopyanın dalına commit'lenir ve projenizin o anki dalına birleştirilir (`merge --no-ff`).
  - Projede kaydedilmemiş değişiklik varsa ya da projede bir yürütme sürüyorsa reddedilir. Çakışma olursa birleştirme geri alınır, projeniz değişmez.
  - Alternatif: aynı çalışmada **GitHub** ile dalı gönderip taslak PR açın.
- **Temizlik:** çalışmayı arşivleyin → **Ana klasöre getir** penceresinde **Kopyayı kaldır**. Kopyada kaydedilmemiş değişiklik varsa reddedilir. Dal projenize tamamen birleşmişse silinir, yoksa kalır.
- Proje başına en çok 10 açık kopya olabilir.

### 2d. İncelemeden önce testler ve lint (TB-27, TB-28, K-035)
İnceleyici komut çalıştırmaz; testleri Conductor çalıştırır ve sonucu inceleyiciye verir.
- **Açmak:** çalışmada (Mac) **Testler**. Conductor proje dosyalarından bir komut önerir: `package.json` `test` betiği → `npm test`; `pytest` (projede `.venv` varsa) ya da `tests/` → `python -m unittest discover`. **Bu komutu onayla**.
- **Ne olur:** her inceleme adımından önce komut **korumalı alanda** çalışır: ağ yok, yalnız proje klasörüne ve özel bir geçici klasöre yazar, ev klasörüne hiç yazamaz, gizli dosyalar kapalı, dosya başına 200 MB, en çok 5 dakika. Sonuç (geçti/kaldı/zaman aşımı, çıkış kodu, çıktının sonu) inceleyiciye veri olarak gider; yürütme kartında **Testler geçti/kaldı** görünür (telefonda da).
- **Onayın anlamı:** yazarın yazdığı test kodunun da korumalı alanda çalışmasına izin verirsin. Komutu ya da aracı belirleyen dosyalar (`package.json`, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `requirements*.txt`) değişirse onay düşer; kartta “Testler çalışmadı · onay geçersiz” görünür, yeniden onaylarsın.
- **Ayrı kopyalı işler** kaynak klasörün onayını kullanır; kopyada `node_modules`/`.venv` yoksa npm testleri “bağımlılıklar kurulu değil” diye çalışmaz, Python testleri Conductor'ın Python'uyla (`unittest`) koşar.
- Ağ isteyen testler bu kipte başarısız olur.
- **Lint / tip denetimi (TB-28):** aynı pencerede ikinci bölüm, ayrı onay. Öneri sırası: `package.json` `lint` (yoksa `typecheck`) betiği → `npm run …`; `tsconfig.json` + projedeki `node_modules/.bin/tsc` → `tsc --noEmit`; Python'da `.venv` içindeki `ruff` / `flake8` / `mypy` (ayar dosyasıyla). Sonuç inceleyiciye ayrı bölüm olarak gider; kartta **Lint temiz / Lint hatası**. Araç projede kurulu değilse (ör. ayrı kopyada `node_modules` yok) “bağımlılıklar kurulu değil” yazar, başka bir şey çalıştırılmaz.

## 3. Sunucuyu başlatma, durdurma, kontrol

**Birincil yol — launchd kullanıcı ajanı `com.promptbridge.conductor` (TB-04).** Makine açılınca ve her çıkışta kendiliğinden başlar (`KeepAlive`, yeniden başlatmalar arası en az 30 sn). Log: `~/Library/Logs/Conductor/server-launchd.log` (2026-10-18'e kadar `.system/server-launchd.log`).

```sh
deploy/launchd.sh install      # kur/güncelle: plist'i şablondan üretir (PATH = bu kabuğun PATH'i), yükler, /health bekler
deploy/launchd.sh status       # launchd durumu (state, pid, last exit code, runs) + /health
deploy/launchd.sh restart      # yeniden başlat (launchctl kickstart -k)
deploy/launchd.sh uninstall    # DURDUR ve kaldır (launchctl bootout + plist silinir)
curl -s http://127.0.0.1:8765/health                                      # {"status":"ok", "version": {...}}
```

- **Ajan kuruluyken `kill` sunucuyu durdurmaz:** launchd süreci hemen (en fazla 30 sn içinde) yeniden başlatır. Durdurmanın tek yolu `deploy/launchd.sh uninstall` (ya da `launchctl bootout gui/$UID/com.promptbridge.conductor`).
- `install` PATH'i kurulum anında kopyalar (Codex'in çalışma klasöründe çalıştırdığı `node`/`npm`/Homebrew komutları için); PATH değişince (ör. yeni nvm sürümü) `install`'ı yeniden çalıştır. Plist'e yalnız PATH yazılır, sır yazılmaz.
- `install`, 8765'i başka bir süreç (ör. elle açılmış `nohup` sunucu) tutuyorsa **reddeder**; önce onu durdur.
- **Ajan kurulu değilken (eski yol):** `nohup .venv/bin/python -m app serve > .system/server-<ad>.log 2>&1 &` ile başlat, `kill -TERM <PID>` ile durdur (`lsof -nP -iTCP:8765 -sTCP:LISTEN` → PID). Bu durumda makine yeniden başlatılınca sunucu kendiliğinden açılmaz (2026-09-30: 22:05 kapanış, ~45 dk kesinti).
- `kill -9` kullanma: süreç temizliği atlanır; çalışan bir iş varsa açılışta **Kurtarma gerekiyor** (§6) olur. Ajan kuruluyken launchd ayrıca süreci hemen yeniden başlatır; yine §6.
- Aynı `state.db` için ikinci bir `serve` açma; runner kilidi (`.system/state.runner.lock`) ikincisini reddeder.
- `/health` → `version.commit` (çalışan kod; `+dirty` = `ai-orchestrator/` altında commit edilmemiş değişiklik), `version.schema` (DB şeması), `version.ui_bundle` (sunulan panel). Panelde sol kenar çubuğunun altında da görünür. Commit/bundle beklediğinden farklıysa sunucu eski kodla ya da eski panelle çalışıyordur.

## 4. Panel — günlük kullanım

**Düzen (TB-20):**
- **Sol kenar çubuğu:** “Yeni çalışma”, çalışmalar (etkin/arşiv). Altta **Onaylar** (bekleyen dosya onayı sayısıyla), **Ayarlar** (§2a), tema ve çalışan sürüm.
- **Ortada sohbet:** senin mesajın bir balon. Her yürütme bir “Conductor” kartı: durum, yazar, inceleyici; **Ayrıntılar** adımları ve olayları açar. Bekleyen, hatalı, kurtarma isteyen ya da duraklatılmış bir yürütmenin kartı kendiliğinden açılır, düğmeleri görünür.
- **Altta mesaj kutusu:** yazar modeli ve efor seçicileri, “İnceleme: …” etiketi. **Gelişmiş** tam takım düzenleyiciyi (yedekler, inceleyici, Oturum durumu, Yenile) açar. **Kota bekleme** mesaj kutusunun altında.
- **Başlıkta:** **Önizleme** (§4'ün sonunda) ve **Arşivle**; başlık kalem simgesiyle değişir.

1. **Yeni çalışma:** klasörü yaz ya da “Klasör seç”, isteğini yaz, gerekirse modeli değiştir, **Başlat**. Başlık ilk mesajdan türetilir (sonra değiştirilebilir).
   - Takım Ayarlar'daki varsayılansa ek onay istenmez (TB-19 K1).
   - Başka bir model seçersen tek satırlık “bu takımla başlayacak … kabul ediyorum” onayı çıkar.
2. İstersen **“Kota dolarsa bekle ve otomatik yeniden dene”** (yeni çalışmada “Seçenekler” altında): son süre (1–24 saat) ve en fazla deneme (1–5). Seçim mesajla dondurulur.
3. Takip mesajları aynı çalışmaya eklenir; önceki yürütme bitmeden sıraya girer. Yazar uygunsa önceki Codex konuşmasını sürdürür; panel “taze / devam” nedenini gösterir.
4. Döngü açıksa inceleyici onaylayana (`verified`) ya da bütçe bitene kadar turlar sürer; **Duraklat** yalnız adım sınırında etkili olur.

**Durumlar ve yapılacaklar**

| Panelde | Anlamı | Senin adımın |
|---|---|---|
| Sırada / Çalışıyor | Kuyrukta / yürütülüyor | Bekle; gerekirse **Durdur** |
| Tamamlandı | Başarılı | — |
| Hata | Kurtarılamaz hata | Adım sonucunu incele; çalışmada **“Sonucu inceledim, sıradakine geç”** |
| Kota bekleniyor | Zincirin tüm sağlayıcıları kotada | Plan varsa bekle (**Şimdi dene** / **Beklemeyi iptal et**); plan yoksa uygun zamanda **Yeniden dene** |
| Klasör değişti — karar gerekiyor | Kota beklerken klasör dışarıdan değişti | Değişen dosyaları incele → **Yeniden dene** ya da **Durdur** |
| Hesap girişi gerekiyor | Sağlayıcı oturumu yok | CLI'da giriş yap → **Yeniden dene** |
| Araç izni gerekiyor | Sağlayıcı izin istedi | Kenar çubuğunda **Onaylar** |
| Bağlam incelemesi gerekiyor | Bağlam devamı kapalı/bitti/sığmıyor | İncele → **Yeniden dene** (yeni hak açmaz) |
| Duraklatıldı | Döngü adım sınırında durdu | Değişikliği onaylayıp **Devam** ya da **Durdur** |
| Kurtarma incelemesi gerekiyor | Süreç temizliği doğrulanamadı / sunucu iş ortasında kapandı; **klasör kilidi tutuluyor** | §6 |

**Önizleme (TB-13/13a):**
- Çalışma ekranındaki **Önizleme** kartı klasör için önerilen geliştirme sunucusu komutunu gösterir.
- Komut sen **Onayla** demeden çalışmaz; onay komutun özetine bağlıdır, komut değişirse yeniden sorulur.
- **Başlat / Durdur / Yeniden başlat**. Sunucu yalnız `127.0.0.1`'de, kendi süreç grubunda çalışır; ortama yalnız izin listesindeki değişkenler geçer.
- Statik siteler de açılır. Mac uygulamasında da çalışır, telefonda yoktur.
- **Korumalı alan (TB-22, deneysel, varsayılan kapalı):** **Ayarlar → Önizleme**'den açılır, bir sonraki başlatmada geçerli olur. Önizleme macOS `sandbox-exec` altında çalışır:
  - yalnız proje klasörünü, kendine ait geçici klasörü ve araç zincirlerini (`.nvm`, Python…) görür;
  - `.ssh`, `.netrc`, `.npmrc`, `~/.config/gh`, anahtar zinciri ve `.system` kapalıdır;
  - `localhost`'ta dinleyebilir ama **hiçbir yere bağlanamaz** (`localhost` dahil; Conductor'ın API'sine ulaşıp korumayı kapatamasın diye);
  - `open`, `osascript`, `pbcopy`, `pbpaste` çalıştırılamaz.

  Statik site ve Python sunucusu çalışır. **Next.js'in Turbopack geliştirme sunucusu bu kipte çalışmaz** (kendi alt süreçlerine `localhost` üzerinden bağlanıyor). Dış API ya da yerel veritabanı kullanan projeler de çalışmaz. O zaman kipi kapatıp yalnız güvendiğin komutu onayla. `sandbox-exec` Apple'ın kullanımdan kaldırdığı ama hâlâ çalışan bir araçtır.

Sıradaki mesaj, son yürütme bekliyor/kurtarma istiyor/duraklatılmışken ya da Hata/Durduruldu bitip sen “devam” demeden başlamaz.

## 5. Oturum kontrolü (isteğe bağlı)

Takım kartındaki **Oturum durumunu kontrol et** sağlayıcı CLI'ının giriş durumunu model çağırmadan sorar. Sonuç “doğrulanamadı / probe_cleanup_failed” ise o sağlayıcı için sunucu yeniden başlayana kadar yeni kontrol yapılmaz; `ps` ile artık süreç var mı bak.

## 5a. Mobil erişim (TB-12, K-024, K-027, K-033)

Telefondan çalışmaları **izlemek**, **dosya onaylarını yanıtlamak** ve Mac izin verirse **durdurmak** ve **görev vermek** için. Conductor yalnız bu Mac'te (`127.0.0.1`) dinler; telefona HTTPS ile Tailscale açar. **Funnel asla** (internete açar). Telefonda Tailscale'i yalnız kullanacağınız zaman açmanız yeterli; açıkken yalnız Mac'e giden trafik Tailscale'den geçer (çıkış düğümü seçmeyin).

**Bir kez kurulum**
1. Tailscale'i Mac'e ve telefonlara (Android ve/veya iPhone) **aynı hesapla** kurun. Tailscale yönetim panelinde **MagicDNS** ve **HTTPS Certificates** açık olsun.
2. Mac'in Tailscale adını öğrenin: `tailscale status` (ör. `mac-adi.tailXXXX.ts.net`).
3. Panel → **Ayarlar → Mobil erişim**: adı yazın, **Mobil geçidi aç** → **Kaydet**. Sonra `deploy/launchd.sh restart`. Geçit `127.0.0.1:8770`'te açılır; panelde “Açık” görünür. `GET /health` ve 8765 değişmez.
4. Mac'te: `tailscale serve --bg --https=443 http://127.0.0.1:8770` (panelde **Kopyala**).

**Telefon eşleştirme**
1. Panel → **Ayarlar → Mobil erişim** → **Eşleştirme kodu oluştur**. 6 haneli kod 5 dakika geçerli ve tek kullanımlıktır; 5 yanlış denemede kod geçersiz olur.
2. Telefonda Tailscale açıkken `https://<mac-adi>.ts.net/ui/` adresini açın (ya da paneldeki adresi kopyalayıp gönderin), kodu girin, **Eşleştir**.
3. Uygulama olarak kurun: Android Chrome → menü → **Uygulamayı yükle** (ya da **Ana ekrana ekle**); iPhone Safari → Paylaş → **Ana Ekrana Ekle**. Adres çubuğu olmadan, ayrı simgeyle açılır.

**Telefonda kullanım (TB-25):** Claude uygulaması gibi: açılışta çalışma listesi; bir çalışmaya dokununca tam ekran açılır, üstte **‹ Çalışmalar** ile geri. Altta sekmeler: **Çalışmalar · Onaylar · Ayarlar · tema**. Yazarken alt çubuk çekilir.

**Cihazlar ve iptal:** Panel → **Ayarlar → Mobil erişim** → **Cihazlar**. **İptal et**, açık bağlantıyı hemen keser; telefon eşleştirme ekranına döner. Jetonun süresi yoktur, yalnız elle iptal edilir. **Son mobil işlemler** her değiştiren isteği ve her reddi gösterir.


**Telefondan durdurma (TB-12b, K-027):** yeni cihaz yalnız izler ve dosya onaylar. Bir cihaza Mac'ten **Ayarlar → Mobil erişim → Cihazlar → “Durdurabilir”** işaretlenirse o telefon üç şey daha yapabilir:
- çalışan bir yürütmeyi **Durdur**;
- kota beklemesini **iptal et**;
- sıradaki bir mesajı **sıradan çıkar**.

**Telefondan görev verme (TB-24, K-033):** Mac'ten **“Görev verebilir”** işaretlenen telefon:
- **Yeni çalışma** başlatır, ama yalnız Mac'te açılmış ve arşivlenmemiş bir çalışmanın klasöründe (klasör listeden seçilir; yeni klasör ve GitHub'dan proje Mac'ten);
- çalışmaya **mesaj** gönderir; yazar/inceleyici **model ve eforunu** seçer (`providers.yaml` tavanı içinde);
- **Ayarlar**'da yalnız **varsayılan takım** ve **tema**yı değiştirir.
Telefondan iş başlayınca Mac'te bildirim çıkar. Telefon kaybolursa: Mac → **Ayarlar → Mobil erişim → Cihazlar → İptal et**.

Yeniden deneme, devam (“sıradakine geç”), kurtarma, duraklat/sürdür, önizleme, GitHub, oturum kontrolü ve model listesini yenileme telefonda yoktur. İşaret değişiklikleri hemen geçerlidir (telefonda sayfayı yenileyin) ve **Son mobil işlemler**'e yazılır.

**Kapatmak:** `tailscale serve reset` ve/veya panelde **Mobil geçidi aç** işaretini kaldırıp `launchd.sh restart`.

**Sorun giderme**
| Belirti | Neden / çözüm |
|---|---|
| Telefonda sayfa açılmıyor | Telefonda Tailscale kapalı ya da `tailscale serve` açık değil (`tailscale serve status`). |
| “Tailscale kimliği görülmedi” | İstek `tailscale serve` üzerinden gelmiyor ya da cihaz bir kullanıcıya bağlı değil (etiketli cihaz). |
| 421 | Adres, Mobil bölümündeki adla aynı değil. |
| “Eşleştirme kodu oluştur” pasif | Geçit çalışmıyor: ayar kaydedildi ama Conductor yeniden başlatılmadı. |
| Telefon eşleştirme ekranına döndü | Cihaz Mac'ten iptal edildi; yeni kodla yeniden eşleştirin. |
| Telefonda “Durdur” yok | Cihaza “Durdurabilir” verilmemiş (Mac → Ayarlar → Mobil erişim → Cihazlar). Telefonda sayfayı yenileyin. |
| Telefonda “Yeni çalışma”/mesaj kutusu yok | Cihaza “Görev verebilir” verilmemiş; verip sayfayı yenileyin. |
| “Mac'e ulaşılamıyor…” | Telefonda Tailscale kapalı ya da Mac uykuda / Mac'te Tailscale bağlı değil. |
| “From a phone only the folder of an active work” | Telefondan yalnız Mac'te açılmış, arşivde olmayan bir çalışmanın klasörü seçilebilir. |

## 6. Kurtarma (RECOVERY_REQUIRED)

1. Yürütmeyi aç: **Kurtarma gerekiyor** kartı sahipli süreçleri (PID/PGID, “Süreç yaşıyor”) gösterir.
2. **Kurtar** → onay penceresi (etkilenecek PID'ler) → **Süreçleri sonlandır ve kurtar**.
3. Sonuç: süreç grubu sonlandırılır, ölüm doğrulanınca kilit bırakılır, yürütme “İnsan kurtardı — temizlendi” olur. Doğrulanamayan bir süreç varsa kilit korunur ve PID'ler gösterilir: `ps -p <PID>` ile bakıp elle sonlandır, sonra yeniden **Kurtar**.

Kurtarma otomatik değildir; yanlış süreci öldürmemek için onay senden gelir.

## 7. Canlıya geçiş (R1 prosedürü)

Ayrıntı ve kayıt örnekleri: `_isbirligi/teslimler/R1-Canliya-Gecis-Proseduru.md`, `F3-03-Canliya-Gecis.md`, `F3-04-Canliya-Gecis.md`.

1. Salt okunur durum: aktif yürütme (QUEUED/RUNNING/PAUSED/RECOVERY_REQUIRED) ve klasör kilidi yok. Varsa **dur**.
2. Yazmaları dondur: ajan kuruluysa `deploy/launchd.sh uninstall` (ya da `launchctl bootout gui/$UID/com.promptbridge.conductor`); ajan yoksa `kill -TERM <PID>`. Süreç bitti ve 8765 kapalı doğrulanır (ajan kuruluyken `kill` durdurmaz, launchd geri açar).
3. Yedek — SQLite backup API ile (`cp` değil):
   ```sh
   .venv/bin/python -c "import sqlite3; s=sqlite3.connect('file:.system/state.db?mode=ro',uri=True); d=sqlite3.connect('.system/state.pre-<ad>.db'); s.backup(d)"
   ```
   `PRAGMA integrity_check` = ok, satır sayılarını kaydet.
4. Kod `main`'de; `cd ui && npm run build` (panel yalnız sunucu durmuşken değişsin).
5. **Şema değişiyorsa kopya provası:** yedeğin kopyasını geçici bir köke koy, yeni kodla `store.migrate` ×2, `--root <kök> serve --port 8766` ile GET uçları ve panel; sonra kapat.
6. Başlat: `deploy/launchd.sh install` (ajan kullanılmıyorsa `nohup … serve > .system/server-<ad>.log 2>&1 &`) → yeni PID (`deploy/launchd.sh status`). 5. adımdaki kopya provası sunucusu (`--port 8766`) launchd dışındadır, `kill -TERM` ile kapatılır.
7. Doğrula: `/health` (commit/şema/bundle beklenen), uçlar 200, yabancı origin POST 403, satır sayıları yedekle aynı, log ve tarayıcı konsolu hatasız.
8. Kayıt: `app/static` + `_isbirligi/DURUM.md` + canlıya geçiş notu → commit.

## 8. Yedekler ve geri dönüş

- Yedekler `.system/state.pre-<ad>.db` (+ `.counts.json`). Asla yerinde düzenleme; deneme hep **kopyada**.
- Şema yükseltmesi tek yönlüdür: **eski kod yeni şemayı açmaz** (`Unsupported database schema version`). Geri dönmek = yedeği geri koymak **ve** o yedeğin kodunu çalıştırmak.
- **Önce güncel veriyi koru:** yedek geri konunca yükseltmeden sonra yazılan her şey (yeni çalışmalar, mesajlar, yürütmeler) kaybolur (F3-06 provasında görüldü). Geri dönmeden önce güncel `state.db`'yi backup API ile ayrı bir dosyaya al; hangi verinin kaybolacağını bilerek karar ver.
- Sıra: sunucuyu durdur (ajan kuruluysa `deploy/launchd.sh uninstall`, değilse `kill -TERM`) → güncel DB'nin yedeği → `state.db` (+ `-wal`, `-shm`) yerine yedeğin kopyası → eski kodu (`git worktree` ya da `git archive <commit>`) o DB ile başlat (eski kod için ajan kurmadan `nohup … serve`; ajan her zaman bu checkout'un kodunu çalıştırır) → `/health` ve panel → kalıcı dönüşte ajanı yeniden `install`.

## 9. Sorun giderme

| Belirti | Neden / çözüm |
|---|---|
| Panel açılmıyor, 8765 boş | Sunucu çalışmıyor (ör. yeniden başlatma sonrası) → §3 |
| “Adres kullanımda” | Başka bir `serve` açık → `lsof -nP -iTCP:8765 -sTCP:LISTEN` |
| `install`: “port 8765 is in use by another process” | Elle açılmış `serve`/eski `nohup` sunucu çalışıyor → onu `kill -TERM` ile durdur, sonra `install` |
| `kill` sonrası sunucu geri geldi | Beklenen: launchd ajanı `KeepAlive` → durdurmak için `deploy/launchd.sh uninstall` |
| Ajan durumu / son çıkış | `deploy/launchd.sh status` ya da `launchctl print gui/$UID/com.promptbridge.conductor` (state, pid, last exit code, runs); ayrıntı `~/Library/Logs/Conductor/server-launchd.log` |
| Ajan başlamıyor, `last exit code = 78: EX_CONFIG`, logda yeni satır yok | launchd programı hiç çalıştıramıyor: genellikle macOS gizliliği (Masaüstü'ndeki bir dosyaya/klasöre erişim, `com.apple.macl` etiketi). Log artık Masaüstü dışında; sürerse `deploy/launchd.sh uninstall` → `install`, yine sürerse Python'a Tam Disk Erişimi. |
| Sunucu ~30 sn'de bir düşüp kalkıyor | Açılışta kalıcı hata (ör. bilinmeyen şema, runner kilidi) → log'a bak; düzeltene kadar `uninstall` |
| Mesaj 409 `conflict` | Politika değişti; takımı yeniden önizle ve kabul et |
| Mesaj 409 `IDEMPOTENCY_CONFLICT` | Aynı mesaj kimliği farklı içerikle; panel yeni kimlik üretir, bekleyen yerel kaydı sil |
| Mesaj 422 `team_invalid` | Seçilen sağlayıcı `allowed`'da yok ya da profil desteklemiyor |
| Yeniden dene 409 `workspace_busy` | Klasör başka bir yürütmede/kilitli; o bitince ya da kurtarmadan sonra |
| Yeniden dene 409 `recovery_required` | Önceki sürecin temizliği çözülmedi → §6 |
| Panel eski görünüyor | `/health` `ui_bundle`'a bak; `npm run build` yapılmamış olabilir; sayfayı yenile |
| `/health` `commit` `+dirty` | Çalışan kodda commit edilmemiş değişiklik var; canlı klasörde düzenleme yapma |
| “Oturum doğrulanamadı / probe_cleanup_failed” | §5 |
| Panel “Claude kurulu değil” diyor, Claude takımda olduğu için mesaj gönderilemiyor | Claude app Claude Code'u güncelleyip yerini değiştirmiş olabilir (2026-10-07'de `<sürüm>/<hash>/claude.app` oldu). `ls ~/Library/Application\ Support/Claude/claude-code/*/` ile bak; Conductor iki düzeni de arar. Yeni bir düzen varsa `claude_binary()` güncellenir. Geçici çözüm: launchd ajanı ortamda yalnız `PATH` taşır; ajanı `uninstall` edip `CONDUCTOR_CLAUDE_BIN=<tam yol> nohup … serve` ile elle başlat (§3). |
| Codex oturumu “giriş yok”, Codex adımı AUTH | Conductor'ın Codex alanına giriş yapılmamış → §1 “Codex için ayrı giriş” |
| Ajan ya da önizleme `git push`/`gh` çalıştırınca “giriş yok”, “terminal prompts disabled” | Bilinçli (TB-21): Conductor'ın başlattığı süreçlerde `git`/`gh` senin GitHub kimliğini kendiliğinden bulamaz. GitHub'a gönderme Conductor'ın kendi onaylı adımlarıyla yapılır. Not: önizleme varsayılan olarak sandbox'sız çalışır; ev klasöründeki dosyaları doğrudan okuyabilir. Ayarlar → Önizleme'de korumalı alanı aç (TB-22) |
| Korumalı alanda önizleme açılmıyor, logda `Operation not permitted` / `EPERM` | Proje izin verilmeyen bir yeri okuyor ya da dış ağa çıkıyor. Ayarlar → Önizleme'de korumalı alanı kapatıp dene; kalıcı ihtiyaçsa bildir (profil `app/preview.py`) |
| Yürütme kartında “Bu yürütme deponun git kancalarına/ayarına dokundu” (TB-23) | Ajan `.git/hooks/*` ya da `.git/config` gibi git'in kendisinin çalıştırdığı bir dosyayı değiştirdi. Conductor'ın GitHub adımları kancaları çalıştırmaz; ama kendi terminalinden `git commit`/`push` yaparsan çalışırlar. Önce bak: `ls .git/hooks`, `git config --local --list`; tanımadığını sil |
| Codex adımı “Codex home is not a real directory” ya da “could not pin the workspace untrusted” | `.system/codex-home` symlink/dosya olmuş ya da içindeki `config.toml` bozuk → düzelt (bozuksa yedekle ve kaldır, sonra yeniden giriş) |

## 10. Kanıt türleri ve testler

- **Birim/entegrasyon testleri** (kota harcamaz): `.venv/bin/python -m unittest discover -s tests`; panel: `cd ui && npm test && npx tsc --noEmit`.
- **Provalar** (gerçek sunucu ve alt süreçler, sahte sağlayıcı ikilileri, geçici kök): `tests/drill_f306.py` (kota devri, bağlam devamı, bekleme ve otomatik deneme, WAITING_HUMAN, yeniden başlatma, çökme → kurtarma), `tests/drill_f306_data.py <.system> <eski commit>` (eski yedek kopyalarının yükseltilmesi ve geri yükleme), `tests/drill_tb04.py` (launchd ajanı: geçici etiket/kök/port ile kurulum, `kill`/SIGKILL sonrası yeniden başlatma, `bootout`, yavaşlatılmış başarısız açılış). Gerçek yedeklere yalnız okuma ile dokunur.
- **Panel fikstürleri:** `tests/preview_*.py` (sahte sağlayıcı, geçici kök).
- **Gerçek sağlayıcı** (`tests/smoke_*.py`): kota harcar; yalnız açık onayla.

Raporlarda gerçek sağlayıcı kanıtı ile sahte/fixture kanıtı **ayrı** yazılır.
