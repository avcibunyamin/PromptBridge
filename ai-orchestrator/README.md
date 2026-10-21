# Conductor — Orkestratör ve Onay Paneli

> Günlük işletim, canlıya geçiş, yedek/geri dönüş ve sorun giderme: [`OPERATOR-REHBERI.md`](../OPERATOR-REHBERI.md). Mimari: [`AI-Orkestrasyon-Mimari-Dokumani.md`](../AI-Orkestrasyon-Mimari-Dokumani.md) §11–12.

## Kurulum (macOS / Linux)

Python 3.12 ile doğrulanmıştır. Aşağıdaki komutları deponun kök klasöründen çalıştırın:

```sh
cd ai-orchestrator
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# Sağlayıcı politika dosyasını oluşturun
mkdir -p .system
cp providers.example.yaml .system/providers.yaml
# Varsayılan zincir codex ve claude ister; yalnız birini kurduysanız .system/providers.yaml içindeki roles listelerinden kurulu olmayanı silin.

# Web panelini derleyin
cd ui && npm install && npm run build && cd ..

# Sunucuyu başlatın (watcher + API + panel)
.venv/bin/python -m app serve
```

Panel adresi: `http://127.0.0.1:8765/ui/` (sağlık kontrolü: `http://127.0.0.1:8765/health`).
Eski komut `.venv/bin/python -m app` (argümansız) yalnız iskelet sağlık mesajını yazıp sonlanır.

Bağımlılıklar `requirements.txt` içinde tanımlıdır (tutarlılık kontrolü için `python -m pip check` kullanın).
SQLite ve `unittest` Python standart kütüphanesindedir, ek paket kurulmaz.

## İki yönlü manuel zarf aktarımı

```sh
.venv/bin/python -m app serve --bidirectional --require-ready
```

`agents/chatgpt/outbox/` → Claude inbox ve `agents/claude/outbox/` → ChatGPT inbox.
Her yeni dosya panelde ayrı insan onayı ister. `--require-ready` kullanırken
`reply.md` dosyasını tamamladıktan sonra `reply.md.ready` oluşturun. Rota içerikteki
talimatlardan değil, kaynak klasöründen belirlenir. Inbox dosyaları izlenmez.
`--bidirectional` olmadan yalnız ChatGPT outbox'ı izlenir.

CLI ile ters yön testi: `python -m app capture <dosya> --source claude`, ardından
`python -m app approve <id>`. Teslim yalnız dosya yayımıdır; hedef asistanın
otomatik başlaması veya ACK/CONSUMED bu akışa dahil değildir.

Claude yerel Add folder bağlayıcısıyla inbox okur (K-007). Outbox yazımı için
outbox da bağlanmalı ve gerçek oturumda doğrulanmalıdır. `configure_mcp.py`
kullanımdan kaldırıldı; çalıştırılması mevcut ayarları değiştirmez.

Alt komutlar (`serve`, `watch`, `capture`, `approve`, `list`, `show`, `reconcile`) aynı giriş noktasından watcher ve manuel teslim akışını çalıştırır.
SQLite depo katmanı `app.store` modülündedir; durum geçiş servisi `app.transitions` modülündedir;
watcher, atomik dosya teslimi, FastAPI ve React onay paneli bu sürümde birlikte çalışır.

## Klasörler

```text
ai-orchestrator/
├── agents/
│   ├── chatgpt/
│   │   ├── inbox/
│   │   └── outbox/
│   └── claude/
│       ├── inbox/
│       └── outbox/
├── queue/
├── archive/
│   ├── delivered/
│   └── rejected/
├── .system/
│   └── tmp/
├── app/
│   ├── __init__.py
│   ├── __main__.py
│   ├── schema.sql
│   ├── store.py
│   └── transitions.py
├── tests/
│   ├── test_store.py
│   └── test_transitions.py
├── .gitignore
├── requirements.txt
└── README.md
```

Boş iletişim klasörlerindeki `.gitkeep` dosyaları yalnız klasörlerin sürüm kontrolünde
korunması içindir; mesaj değildir. `.venv/` yerel Python ortamıdır, paylaşılmaz.
İletişim klasörleri ChatGPT ve Claude içindir. Watcher varsayılan olarak `agents/chatgpt/outbox/`
izler; başarılı yakalamaları `archive/captured/` altına taşır. Teslim katmanı onaylanan ham
baytları `agents/claude/inbox/` altına `os.link` ile exclusive ve atomik olarak yayınlar.

## Depo kullanımı (`app.store`)

Ek bağımlılık gerekmez: `sqlite3` ve test çalıştırıcısı `unittest` standart kütüphanededir.
Aşağıdaki örnek yalnız bellekte DB oluşturur:

```python
from app import store

conn = store.connect(":memory:")
try:
    store.migrate(conn)
    eid, vid = store.insert_captured(conn, {
        "id": "example", "source_agent": "chatgpt", "target_agent": "claude"
    }, b"Example content")
    print(store.get_envelope(conn, eid)["state"])  # CAPTURED
finally:
    conn.close()
```

Kalıcı kullanımda `connect` açıkça verilen SQLite dosyasını açar/oluşturur;
üst klasörleri oluşturmaz. Depo katmanı (G2) payload dosyası veya inbox/outbox yazmaz.
Bu katman tek başına `.system/state.db` oluşturmaz; testler geçici DB dosyaları kullanır.

- Bağlantıları `store.connect` ile açın: FK her bağlantıda açık, transaction kipi açıkça yönetilir.
- Her yazım `BEGIN IMMEDIATE` ile doğrulama okumalarından önce kilidi alır.
  Birleşik işlemler için `with store.transaction(conn):` kullanın; iç DAO işlemleri savepoint
  kullanır ve dış transaction'ı erken commit etmez. Ham `with conn:` ile birleştirmeyin.
- Her thread ayrı bağlantı açmalıdır. Kilit bekleme varsayılan 5 saniyedir;
  aşılırsa `sqlite3.OperationalError` döner. Otomatik/sınırsız yeniden deneme yoktur.
- Şema migration'ları idempotent kurulur, bilinmeyen sürüm değiştirilmeden reddedilir (güncel şema: `GET /health` çıktısındaki `schema`).
- İçerik ham BLOB; önizleme seçilen sürümü, teslim onaylanan sürümü okur.
  Düzenleme yeni UUID sürümü ve base64 kodlu bayt diff'iyle audit oluşturur.
- `versions`/`history` için UPDATE ve DELETE engelleyen trigger'lar vardır;
  DAO yalnız INSERT sunar. DB bağlantısına sınırsız SQL yetkisi olan kişiye karşı
  bir yetkilendirme/saldırı koruması değildir.
- `set_state` yalnız CAS + audit deposudur; geçiş yasallığı geçiş servisine (G3) aittir.
- `record_delivered` çağıranın sağladığı gerçek hash'i saklı onaylı hash ile karşılaştırır;
  dosyadan ölçüm ve fiziksel teslim teslim katmanına (G5) aittir.
- `reset_for_reapproval` yalnız FAILED'dan, eski alanları audit'te koruyarak çalışır.

### Testler

`ai-orchestrator/` içinde:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Gerçek SQLite testleri; sürüm aidiyeti, hash uyuşmazlığı, eşzamanlı onay,
kilit bekleme, rollback, nested transaction, yeniden açılış ve reset akışını kapsar.

## Geçiş servisi (`app.transitions`)

Çağrı biçimi:

```python
from app.transitions import transition

result = transition(conn, envelope_id, "human_approve",
                    expected_state="PENDING_APPROVAL", actor="operator",
                    payload={"version_id": selected_version_id})
```

Her çağrı bir dış `store.transaction` içinde doğrulama + özel DAO işlemini yapar;
DAO savepoint'i dış commit'i erken tamamlamaz. Depo katmanının (G2) `InvalidTransition`,
`VersionMismatch` ve `DeliveredHashMismatch` sınıfları aynen kullanılır.
Olmayan mesaj `EnvelopeNotFound`; bilinmeyen olay, hatalı state tipi veya payload
`InvalidEvent` verir. Stale durum ve kapalı geçişler `InvalidTransition` verir.

| Olay | Payload |
|---|---|
| `enqueue` | `{}` |
| `human_approve` | `version_id` |
| `human_reject` | `{}` veya `reason` |
| `timeout`, `deliver_err`, `escalate` | `reason` |
| `route`, `retry` | `version_id`, `dest_path` |
| `deliver_ok` | `actual_sha256` (64 küçük hexadecimal karakter) |

Eksik/fazla alanlar ve boş metinler reddedilir. Aktör, çağıran tarafından doğrulanmış
bağlamdan gelmelidir; bu modül kimlik doğrulama sistemi değildir. `timeout` yalnız
çağıranın açık olay gönderimiyle çalışır; zamanlayıcı yoktur. Otomatik onay ve ACK
geçişleri kapalıdır. DELIVERED yalnız “İletildi” anlamına gelir.

Geçiş servisi (G3) `route/retry` audit'inde gerçek servis olay adını ve aktörü depo katmanına (G2) geçirir;
depo katmanının doğrudan çağrıları için önceki `deliver_intent`/`system` varsayılanları korunur.
Fiziksel teslim ve FAILED durumundan yeniden deneme öncesi uzlaştırma teslim katmanının (G5) işidir.
Geçiş servisi yalnız depo kaydını değiştirir; dosya yazmaz, hedef asistanı başlatmaz.

## Watcher ve manuel teslim demosu

Watcher yalnız `.md` dosyalarını işler. Dosya atomik taşınmışsa hemen; normal yazımda
boyut ve mtime 750 ms sabit kaldıktan sonra yakalar. İsteğe bağlı `output.md.ready`
işareti kullanılabilir. Başarılı kaynak `archive/captured/`'a taşınır; bozuk/başarısız
girdiler `.failed/` altında tutulur. Başlangıç taraması mevcut dosyaları da kapsar.

```sh
# Terminal 1
.venv/bin/python -m app watch

# Terminal 2
cat > agents/chatgpt/outbox/output.md <<'EOF'
---
title: "Deneme"
type: prose
---
Claude'un okuyacağı içerik.
EOF
.venv/bin/python -m app list
.venv/bin/python -m app approve <envelope-id>
.venv/bin/python -m app show <envelope-id>
.venv/bin/python -m app reconcile
```

`approve` yalnız insan onayından sonra route + teslim yapar ve `İletildi:` mesajı
verir. `--edit <dosya>` seçili içeriği yeni, değişmez bir edited sürüm olarak onaylar.
Teslim sırasında hedefte farklı bayt varsa üzerine yazılmaz; zarf `FAILED` olur.

## API ve onay paneli

Tek komutla watcher, API ve gömülü React paneli başlatılır:

```sh
.venv/bin/python -m app serve
```

Panel: `http://127.0.0.1:8765/ui/`  
API sağlık kontrolü: `http://127.0.0.1:8765/health`

API, zarf listesi/detayı, içerik önizlemesi, manuel onay/reddetme, edited sürüm,
uzlaştırma ve SSE canlı güncellemelerini sağlar. UI build'i `app/static/` altında
FastAPI tarafından sunulur; ayrı frontend sunucusu gerekmez.

## Tek panelden çalışma başlatma

Aynı `serve` komutuyla `/ui/` altında **Çalışmalar** bölümü açılır. **Yeni çalışma**
ile mevcut bir klasörün tam yolunu ve görevi girip **Başlat** seçilir:

1. Codex seçilen klasörde uygular.
2. Claude Code salt okunur araçlarla inceler.
3. İlk Codex oturumu incelemeyi alıp gereken düzeltmeyi yapar ve durur.

Sol listeden çalışma seçilebilir; adımlar, sonuçlar ve canlı çıktı görünür.
**Durdur**, aktif alt süreçlerin sonlanmasını bekleyerek çalışmayı iptal eder.
Eski inbox/outbox akışı **Dosya onayları** bölümündedir.

Codex ve Claude Code hesap girişleri önceden tamamlanmış olmalıdır. API anahtarı
zorunlu değildir; CLI'ların mevcut hesap oturumları kullanılır. Komut yolları
bulunamazsa `CONDUCTOR_CODEX_BIN` ve `CONDUCTOR_CLAUDE_BIN` ortam değişkenlerine
tam yürütülebilir dosya yolları verilebilir. Prompt shell üzerinden geçirilmez.

Temel çalışma akışı `codex → claude → codex` üçlüsünü ve otomatik adım aktarımını
sunar (F2-02). Claude'a yalnız Read/Glob/Grep verilir. Codex workspace-write sandbox'ı
kullanır; ağ ve ek yazma izinleri otomatik genişletilmez. İzin gerektiren
sağlayıcı isteği `WAITING_APPROVAL` olarak açıklanır; panelden izin verip kaldığı
yerden sürdürme henüz yoktur. Hesap/kota hataları ayrı bekleme durumlarıdır;
kota yedek zinciri ve sınırlı bekleme aşağıdaki bölümlerde açıklanır.

Sunucu kesilirse belirsiz çalışmalar `RECOVERY_REQUIRED` olur ve klasör kilitleri
korunur. Olası eski alt süreçler ve dosya değişiklikleri incelenmeden kilidi
silerek işi yeniden başlatmayın. Aynı DB için tek sunucu/worker desteklenir.
Bu yerel uygulamayı loopback dışında yayımlamak için kullanıcı kimlik doğrulaması
ayrıca tasarlanmalıdır.

Testler (hesap/kota kullanmaz):

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Gerçek sağlayıcı testi (hesap kotası kullanır; geçici klasör kendi temizlenir):

```sh
.venv/bin/python tests/smoke_runner.py
```

API: `POST /api/sessions/start`, `GET /api/sessions`, `GET /api/sessions/{id}`,
`POST /api/sessions/{id}/cancel`, `GET /api/sessions/{id}/events`.
SSE olayları SQLite'ta saklanır; `after` veya `Last-Event-ID` ile tekrar okunabilir.

### Work oturumlarında proje talimatı (`CLAUDE.md`)

Claude süreçleri kişisel ayarlar, skill'ler ve hook'lar etkinleşmesin diye hâlâ
`--safe-mode` ile çalışır. Work kuyruğundan başlayan oturumlarda Conductor bunun
yerine çalışma klasöründeki kök `CLAUDE.md` dosyasını kendisi güvenli biçimde
okur, oturum başında sabitler ve yalnız yeni Claude konuşmalarının prompt'una
düşük öncelikli **PROJECT INSTRUCTIONS** bölümü olarak ekler (TB-03). Eski
`POST /api/sessions/start` yolu dosya okumaz ve bu bölümü eklemez.

Desteklenen import biçimi yalnız kod bloğu dışında, tek başına bir satırdaki
`@göreli/yol` yazımıdır. Yol, import eden dosyanın klasörüne göre çözülür;
satır içindeki `@yol` ifadeleri import değildir. Bu gramer Claude Code'un yerel
CLAUDE.md davranışından bilinçli olarak daha dardır. Kontrol karakteri içeren, mutlak/`~`/workspace dışı
yollar, symlink'ler, normal dosya olmayan hedefler, eksik veya okunamayan
importlar, UTF-8 dışı içerik, döngü, 5'i aşan derinlik ve toplam 16 KiB sınırı
oturumu sağlayıcı başlamadan reddeder.

Ret panelde dosya ve varsa satırla görünür. Düzeltme akışı: `CLAUDE.md` veya
importu düzeltin, work'e yeni mesaj ekleyin, başarısız sonucu inceleyip
**Sonucu inceledim, sıradakine geç** ile onaylayın. Eski başarısız snapshot yeniden denenmez;
yeni mesaj dispatch edilirken yeni bir snapshot alınır. Çalışan bir session
sırasında dosyaların değişmesi o session'ın sabitlenmiş bölümünü değiştirmez.

### Codex izolasyonu ve `AGENTS.md`

Conductor'ın başlattığı her Codex süreci (çalışma, eski `POST /api/sessions/start`
yolu ve oturum kontrolü) kullanıcının `~/.codex`'i yerine Conductor'a ait
`.system/codex-home`'u `CODEX_HOME` olarak kullanır (`CONDUCTOR_CODEX_HOME` ile
değiştirilebilir; TB-05, K-014/K-015). Kişisel plugin'ler, MCP sunucuları, `notify` ve model ayarları
yüklenmez; Codex kendi varsayılan modeliyle çalışır. Bu alan için bir kez ayrı
giriş gerekir (`OPERATOR-REHBERI.md` §1).

Codex'in kendi `AGENTS.md` yükleyicisi `-c project_doc_max_bytes=0` ile, varsayılan “kurulabilir plugin” katalog önerisi `-c features.plugins=false` ile kapalıdır (Conductor hiçbir Codex plugin'i kullanmaz; `features.recommended_plugins=false` tek başına bu bloğu kaldırmadı).
Work oturumlarında politika Codex çalıştırıyorsa çalışma klasörünün **kök**
`AGENTS.md`'si `CLAUDE.md` ile aynı kurallarla okunur, sabitlenir
(`session_agents_instructions`) ve yalnız yeni Codex konuşmalarına eklenir;
`thread/resume` ile süren konuşmaya tekrar eklenmez. Codex'ten bilinçli olarak
daha dardır: import yoktur (`@yol` düz metindir), üst klasörlerdeki `AGENTS.md`,
`AGENTS.override.md` ve yedek dosya adları okunmaz. Eski
`POST /api/sessions/start` yolu `AGENTS.md` eklemez.

Codex WRITE oturumunda proje klasörünü kendiliğinden “güvenilir” işaretler; bu,
hedef reponun `.codex/` ayarlarını (MCP sunucuları, hook'lar) açar. Conductor
her Codex sürecinden önce kendi alanında çalışma klasörünü ve git kökünü
`untrusted` olarak sabitler ve dosyayı okuyarak doğrular; doğrulanamazsa süreç
başlamaz.

**Bilinen sınırlama:** çalışma klasöründeki `.agents/skills` repo skill'leri Codex
tarafından yine yüklenir; kapatma ayarı bulunamadı. Bunlar talimat metnidir, kod
çalıştırmaz ve `AGENTS.md` gibi sabitlenmez.

Codex izolasyonundan (TB-05) önce kullanıcının `~/.codex` alanında açılmış Codex konuşmaları devam
ettirilmez; sonraki mesaj taze konuşmayla başlar (`codex_home_changed`).

## Kota durumunda yetkili yedek

`providers.example.yaml` dosyasındaki zincirleri `.system/providers.yaml` içinde
ayarlayın (F2-03a). Yeni çalışma ekranında yazar/inceleyici sırasını görüp kutuyu
onaylayın. Başlat, bu zinciri ve izin profillerini o çalışma için dondurur;
sonradan config değişikliği çalışan oturumu etkilemez. API istemcisi önce
`GET /api/sessions/providers` ile zinciri göstermeli, kabul edilen `digest`
değerini `POST /api/sessions/start` isteğinde `policy_digest` olarak göndermelidir.
Eski digest 409, desteklenmeyen `require_handover_approval:true` 422 döndürür.

- Yalnız yapılandırılmış kota/rate-limit kodu yedeği tetikler. Metindeki “quota”,
  tek başına çıkış kodu 1 veya belirsiz hata tetiklemez.
- Rol profili sağlayıcıdan bağımsızdır. Codex: workspace-write/read-only sandbox.
  Claude yazar: Read/Glob/Grep/Write/Edit + acceptEdits; inceleyici:
  Read/Glob/Grep + dontAsk. Claude'da Bash/MCP kapalıdır; bu yazar profili
  dosya düzenler fakat shell testleri çalıştıramaz.
- **Efor seçimi (TB-10):** kimliğe efor eklendi: `provider[@account][/model][~effort]` (ör. `claude/opus~high`).
  Efor yalnız eforu ayrı parametre alan araçlarda (Claude `--effort`: low/medium/high/xhigh/max);
  `agy`'de efor model adının parçasıdır (`gemini-3.1-pro-high`), `~effort` kabul edilmez.
  Claude `--model` takma ad (`fable`/`opus`/`sonnet`/`haiku`) ya da tam ad alır; `--fallback-model`
  hiç verilmez. Claude'un `init` olayındaki model istenenle uyuşmazsa adım `model_mismatch` ile
  başarısız olur (takma ad, `claude-` ile başlayan adda tireyle ayrılmış parça olarak eşleşir).
  **Katalog:** `GET /api/sessions/catalog` (önbellek), `POST /api/sessions/catalog/refresh`
  (`mutation_guard`; her yapılandırılmış `agy` hesabında `agy models`, model çağrısı yok). Claude'da
  listeleme komutu olmadığı için takma adlar “Doğrulanmadı”; listede olmayan `agy` modeli “Model yok”.
  **`allowed` jokerleri:** modelsiz/eforsuz girdi (`claude`, `agy@acct1`) o araç+hesabın bütün
  model/eforlarına izin verir; hesap her zaman birebir. Panel: her halkada sağlayıcı/hesap → model → efor.
- **Değişen dosya listesi (TB-09):** inceleyici istemi (ve yeni konuşmadaki düzeltme istemi), Conductor'ın oturum
  başı ↔ deneme başı anlık görüntülerinden çıkan **değişen dosya listesini** içerir:
  satır başına `{"change": "added|modified|deleted", "path": …}` (okunamayanlarda
  `"certainty": "unknown"`), en fazla 300 satır / 24 KB, fazlası `{"not_listed": N}`.
  Dosya içeriği ve hash eklenmez; inceleyici dosyaları kendisi açar. Yazar istemi
  değişmedi.
- **Antigravity CLI (`agy`) hesapları (TB-08):** `agy` (Antigravity CLI) hesap ✓ model ✓ (**zorunlu**), örn. `agy@acct1/claude-sonnet-4-6`.
  Profil: READ_ONLY varsayılan izin, WRITE `--mode accept-edits`; komut aracı yok. Komut reddi
  `COMMAND_DENIED` → aynı konuşmada bir kez otomatik devam (K-020, bağlam bütçesinden sayılmaz),
  ikinci ret `WAITING_APPROVAL`. Çalışan model istenenden farklıysa `model_mismatch`, devam
  istenen konuşma yerine yeni konuşma açılırsa `resume_mismatch` → başarısız. Çalışma klasöründe
  `agy` MCP/hook tanımı varsa adım başlamaz (`agy_workspace_config`). Kota/AUTH yalnız
  `result.error`/stderr metninden (doğrulanmamış). Ayrıntı: operatör rehberi.
- **Çoklu hesap ve model kimlikleri (TB-06):** zincir rol başına en fazla 4 halka; her halka bir **kimlik**tir:
  `provider[@account][/model]` (ör. `codex@work`). YAML'da `account:` / `model:`
  alanları ya da kısa biçim kullanılır, ikisi birlikte kullanılamaz. Aynı araç
  farklı hesap/modelle tekrar edebilir; aynı kimlik edemez. Codex hesap destekler
  (`.system/codex-home-<account>`), model alanı yok; Claude ikisini de desteklemez.
  Hesapsız/modelsiz politikalar eskisiyle bayt bayt aynıdır (digest değişmez).
  `allowed` rol başına 1..8 kimlik. Önceki konuşmayı sürdürme yalnız aynı kimlikle:
  hesap ya da model değiştiyse yeni konuşma (`identity_changed`).
- Her sağlayıcı adım başına bir kez denenir; zincir tükenince WAITING_CAPACITY.
  AUTH → WAITING_AUTH; izin reddi → WAITING_APPROVAL. Bekleyen çalışma
  kendiliğinden sürmez; yalnız kota için isteğe bağlı sınırlı bekleme vardır (F3-04).
- Süreç sahipliği kalıcıdır. İlk sağlayıcının grup kapanışı doğrulanmadan
  yedek başlamaz. Belirsizlikte RECOVERY_REQUIRED ve klasör kilidi korunur.
- Kilit yalnız Conductor çalışmalarını kapsar. Dış editör/ajanları engellemez.
  Hash snapshot değişiklik kanıtıdır; kimin yaptığına veya dosyaların üzerine
  yazılmayacağına garanti değildir. Snapshot sınırları ve okunamayan dosyalar
  `unknown` olarak kaydedilir; .git/.venv/node_modules/.system dışlanır.
- Mevcut yürütme üç adım sonunda durur. Sınırsız sohbet, bağlam özetleme,
  kapasite yenilenince otomatik devam ve kurtarma UI bu dilimde yoktur.

Doğrulama: `.venv/bin/python -m unittest discover -s tests -v`.
`tests/smoke_runner.py` ve `tests/smoke_profiles.py` gerçek sağlayıcı oturumları
kullanır, kota tüketebilir; normal test keşfine dahil değildir.

## Kotada sınırlı bekleme ve otomatik yeniden deneme

Mesaj gönderirken “Kota dolarsa bekle ve otomatik yeniden dene” seçilirse
(`POST /api/works/{id}/messages` alanları `wait_on_quota`, `wait_deadline_hours`
1–24, `wait_max_attempts` 1–5; F3-04) seçim mesajla dondurulur ve idempotency
özetine girer (aynı kimlik + farklı seçim → 409). Varsayılan kapalıdır; kapalıyken
davranış değişmez (K-010, K-012).

- Yalnız zincir sonundaki doğrulanmış kota (QUOTA_EXCEEDED) plan açar;
  AUTH/PERMISSION/UNKNOWN/RECOVERY_REQUIRED otomatik denenmez.
- Oturum başına tek plan: son süre ilk kota anından sabittir, deneme sayısı
  yeniden kotalarda birikir; biri dolunca plan EXPIRED, oturum FAILED.
  Aralık 15 dk'dan başlar, katlanır, en çok 4 saat.
- Zamanlayıcı dakikada bir vadesi gelen planı DB CAS ile alır ve insan
  “Yeniden dene” yolunu (`retry_session`) kullanır; klasör kilidi, çözülmemiş
  temizlik ve başarısız adım kontrolleri aynen geçerlidir. Klasör meşgulse
  deneme sayılmadan 15 dk sonraya ertelenir.
- Karşılaştırma tabanı her kota anında (kilit tutulurken) alınır. Bekleme
  sırasında klasör başkası tarafından değişirse oturum `WAITING_HUMAN` olur;
  otomatik devam etmez, iş kuyruğu bekler. Panelden yeniden dene ya da durdur.
- İnsan “Şimdi dene”si bekleyen planı iptal eder; o denemenin yeni kotası
  yeni bir plan açar. `DELETE /api/sessions/{id}/wait` bekleyen planı ve oturumu
  iptal eder (tetiklenmiş plan 409).
- Sunucu açılışındaki kural değişmez (QUEUED/RUNNING → RECOVERY_REQUIRED);
  tetiklenmiş ama işlenmemiş plan yeniden bekler, kurtarma gerektiren oturumun
  planı iptal edilir. Hiçbir oturum açılışta kendiliğinden başlatılmaz.
- `resetsAt` kullanılmaz: kurulu Codex şemasında `RateLimitWindow.resetsAt`
  (`int64|null`) var, birimi ve kota hatasıyla ilişkisi doğrulanmadı (K-012);
  API'de `resets_at` her zaman `null`. Kota sıfırlanması çalışmanın süreceğini
  garanti etmez.

Migration (`retry_plans`, eklemeli) canlıya alınmadan önce DB yedeği alınır.
Doğrulama: `.venv/bin/python -m unittest tests.test_retry -v`; panel fikstürü
`tests/preview_wait.py` (sahte sağlayıcı, gerçek model yok).
