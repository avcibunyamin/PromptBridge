# PromptBridge (Conductor) — Kullanım Kılavuzu

> **English version:** [docs/USER-GUIDE.md](USER-GUIDE.md)

**PromptBridge (Conductor)**, yapay zeka kodlama CLI araçlarını (**Claude Code**, **Antigravity `agy` / Gemini**, **Codex**, **Ollama**) kendi aboneliklerinizle, API anahtarı kullanmadan ve tamamen yerel makinenizde (`127.0.0.1`) **yazar → inceleyici** takım düzeninde çalıştıran insan onaylı bir orkestrasyon sistemidir.

---

## İçindekiler

1. [Kurulum (Python venv, npm build, launchd)](#1-kurulum-python-venv-npm-build-launchd)
2. [Sağlayıcı CLI'lerini Kurma ve Hesap Bağlama](#2-sağlayıcı-clilerini-kurma-ve-hesap-bağlama)
3. [`providers.yaml` Tavanı ve Varsayılan Takım](#3-providersyaml-tavanı-ve-varsayılan-takım)
4. [İlk Çalışma (Klasör Seçimi, Ayrı Kopya, Takım, Kabul)](#4-ilk-çalışma-klasör-seçimi-ayrı-kopya-takım-kabul)
5. [Mesaj Kutusu (Model / Efor / Halka, `/` Kısayolları)](#5-mesaj-kutusu-model--efor--halka--kısayolları)
6. [Testler, Lint ve Önizleme](#6-testler-lint-ve-önizleme)
7. [GitHub Entegrasyonu](#7-github-entegrasyonu)
8. [Telefon Eşleştirme (Tailscale)](#8-telefon-eşleştirme-tailscale)
9. [Mac Uygulaması (`apps/mac`, `make install`)](#9-mac-uygulaması-appsmac-make-install)
10. [Güvenlik Modeli](#10-güvenlik-modeli)
11. [Sorun Giderme](#11-sorun-giderme)
12. [Sıkça Sorulan Sorular (SSS)](#12-sıkça-sorulan-sorular-sss)

---

## 1. Kurulum (Python venv, npm build, launchd)

### Ön Koşullar

- **Python 3.12+**
- **Node.js 18+** ve **npm**
- En az bir desteklenen sağlayıcı CLI aracı (`claude`, `agy`, `codex` veya `ollama`)

### Adım 1: Python Sanal Ortamı (`venv`) ve Bağımlılıklar

Depoyu klonladıktan sonra `ai-orchestrator/` dizininde Python 3.12 sanal ortamını oluşturun:

```sh
cd ~/Projeler/PromptBridge/ai-orchestrator
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### Adım 2: Web Panelini Derleme (`npm run build`)

React + TypeScript + Vite tabanlı web panelini derleyin. Derleme çıktısı doğrudan FastAPI sunucusunun sunduğu `ai-orchestrator/app/static/` klasörüne yazılır:

```sh
cd ui
npm install
npm run build
cd ..
```

### Adım 3: Sağlayıcı Politikası Dosyasını Oluşturma

Şablon politika dosyasını `.system/providers.yaml` konumuna kopyalayın:

```sh
mkdir -p .system
cp providers.example.yaml .system/providers.yaml
```

### Adım 4: Sunucuyu Başlatma ve `launchd` Servisi (macOS)

Sunucuyu tek seferlik ön planda çalıştırmak için:

```sh
.venv/bin/python -m app serve
```

macOS üzerinde bilgisayar açıldığında otomatik başlaması ve beklenmedik kapanmalarda yeniden ayağa kalkması (`KeepAlive`) için `launchd` kullanıcı ajanını (`com.promptbridge.conductor`) kurun:

```sh
deploy/launchd.sh install      # Servisi kurar/günceller, mevcut PATH'i kaydeder ve /health yanıtını bekler
deploy/launchd.sh status       # launchd durumunu (PID, son çıkış kodu) ve /health çıktısını gösterir
deploy/launchd.sh restart      # Servisi yeniden başlatır
deploy/launchd.sh uninstall    # Servisi durdurur ve plist dosyasını kaldırır
```

> **Not:** `launchd` ajanı kuruluyken `kill` komutu sunucuyu kalıcı olarak durdurmaz; `launchd` en fazla 30 saniye içinde süreci yeniden başlatır. Sunucuyu durdurmak için `deploy/launchd.sh uninstall` komutunu kullanın. Günlükler `~/Library/Logs/Conductor/server-launchd.log` dosyasına yazılır.

Sunucu çalıştığında yönetim paneli şu adrestedir: **<http://127.0.0.1:8765/ui/>**

---

## 2. Sağlayıcı CLI'lerini Kurma ve Hesap Bağlama

PromptBridge API anahtarı istemez; her sağlayıcıyı kendi resmi CLI aracı ve kullanıcı oturumuyla çalıştırır. Kişisel ayarlarınızın, eklentilerinizin veya anahtar zincirinizin etkilenmemesi için `agy` ve `codex` sağlayıcıları `.system/` altında izole ev (`HOME`) klasörleri kullanır.

### 2.1 Antigravity CLI (`agy` / Gemini ve Claude Modelleri)

1. **Kurulum:** Resmi kurulum betiği ile kurun (`~/.local/bin/agy` konumuna kurulur; farklı bir yol için `CONDUCTOR_AGY_BIN` ortam değişkenini kullanabilirsiniz):
   ```sh
   curl -fsSL https://antigravity.google/cli/install.sh | bash
   ```
2. **Ayrı `HOME` ve Anahtar Zinciri Hazırlığı:** Her `agy` hesabı kendi `.system/agy-home-<hesap>` klasöründe çalışır (gerçek `~` dizininiz kullanılmaz). Önce `providers.yaml` dosyasına `agy@<hesap>/<model>` kimliğini ekleyin ve panelde **Oturum durumunu kontrol et** düğmesine tıklayın (veya ilk `agy` adımını başlatın). Bu işlem `.system/agy-home-<hesap>/Library/Keychains/` altında izole bir anahtar zinciri hazırlar.
3. **Hesap Girişi Komutu:** `ai-orchestrator/` dizinindeyken ilgili hesabın izole `HOME` klasörüyle giriş yapın:
   ```sh
   (H="$PWD/.system/agy-home-<hesap>"; cd /tmp && HOME="$H" ~/.local/bin/agy)
   ```
   - Tarayıcıda açılan Google OAuth ekranında `<hesap>` için kullanacağınız Google hesabını seçin.
   - Verilen doğrulama kodunu terminale yapıştırın.
   - Veri toplama tercihini yanıtlayın ve klasör güveni sorusunda **"No, exit"** seçeneğini seçerek çıkın.
4. **Önemli Kurallar:**
   - `.system/agy-home-<hesap>` klasörünü silmeyin (silerseniz izole anahtar zinciri de silinir).
   - `agy` kimliklerinde model belirtmek **zorunludur** (ör. `agy@<hesap>/gemini-3.8-flash-high` veya `agy@<hesap>/gemini-4-argon-high`).
   - Yeni modeller eklendiğinde paneldeki **Gelişmiş → Yenile** düğmesi her yapılandırılmış hesapta `agy models` komutunu çalıştırarak model kataloğunu günceller.

### 2.2 Codex CLI (`codex`)

1. **Kurulum:** Bağımsız `codex` CLI aracını veya macOS ChatGPT uygulamasının içindeki ikiliyi (`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`) kullanabilirsiniz. Farklı bir ikili yolu için `CONDUCTOR_CODEX_BIN` ortam değişkenini tanımlayın.
2. **İzole Alana Giriş:** PromptBridge, kişisel `~/.codex` ayarlarınızdan ve MCP/plugin tanımlarınızdan etkilenmemek için `.system/codex-home` alanını kullanır. Bu alana bir kez giriş yapın:
   ```sh
   cd ~/Projeler/PromptBridge/ai-orchestrator
   CODEX_HOME="$PWD/.system/codex-home" codex login
   # ChatGPT.app ikilisi kullanıyorsanız:
   CODEX_HOME="$PWD/.system/codex-home" /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex login
   ```
3. **Çoklu Hesap (`codex@<hesap>`):** Birden fazla Codex hesabı kullanmak isterseniz `codex@<hesap>` kimliği `.system/codex-home-<hesap>` klasörünü kullanır:
   ```sh
   CODEX_HOME="$PWD/.system/codex-home-<hesap>" codex login
   ```
   > **Uyarı:** `auth.json` dosyasını hesap klasörleri arasında kopyalamayın veya sembolik bağ (`symlink`) yapmayın; yenileme jetonu (refresh token) paylaşılırsa bir oturum diğerini düşürür.

### 2.3 Claude Code (`claude`)

1. **Kurulum ve Giriş:** `claude` CLI aracını kurun ve kendi terminalinizde `claude` çalıştırarak hesabınızla giriş yapın. Farklı bir ikili yolu için `CONDUCTOR_CLAUDE_BIN` ortam değişkenini kullanabilirsiniz.
2. **Çalışma Biçimi:** PromptBridge, Claude süreçlerini kişisel hook ve eklentilerin devreye girmemesi için `--safe-mode` ile başlatır. Çalışma klasörünün kökündeki `CLAUDE.md` dosyası güvenli kurallarla okunup oturum başında sabitlenir.

### 2.4 Ollama (Yerel Modeller)

1. **Kurulum ve Model İndirme:** Ollama uygulamasını kurun ve **araç çağırma (tool calling)** destekleyen bir model indirin:
   ```sh
   ollama pull qwen2.5-coder:14b
   ```
2. **Bağlam Uzunluğu:** Kodlama görevleri için Ollama'yı en az `16384` bağlam uzunluğuyla başlatmanız önerilir (`OLLAMA_CONTEXT_LENGTH=16384`). Küçük bağlam uzunluğunda panel uyarı gösterir.
3. **Bağlantı:** PromptBridge yalnızca yerel `127.0.0.1:11434` adresine bağlanır ve Ollama modellerini izole `.system/ollama-codex-home` alanı üzerinden çalıştırır. Hesap girişi veya kota sınırı yoktur.

---

## 3. `providers.yaml` Tavanı ve Varsayılan Takım

`ai-orchestrator/.system/providers.yaml` dosyası sistemin **güvenlik tavanıdır (ceiling)**. Tarayıcı veya mobil istemci bu dosyayı değiştiremez; panelden yalnızca bu dosyada izin verilen sağlayıcı ve hesaplar seçilebilir.

Örnek `.system/providers.yaml`:

```yaml
# Varsayılan takım (panelde ilk seçili gelen yazar ve inceleyici zinciri)
roles:
  author:
    - provider: claude
      profile: WRITE
    - provider: agy
      account: <hesap1>
      model: gemini-4-argon-high
      profile: WRITE
    - provider: codex
      profile: WRITE
  reviewer:
    - provider: agy
      account: <hesap2>
      model: gemini-3.8-flash-high
      profile: READ_ONLY
    - provider: claude
      profile: READ_ONLY

require_handover_approval: false

# Panelden seçilebilecek kimliklerin üst sınırı (tavan)
# Modelsiz/eforsuz yazılan girdiler (ör. claude, agy@<hesap1>, ollama)
# o sağlayıcı+hesabın tüm model ve eforlarına izin veren jokerdir.
allowed:
  author:
    - claude
    - agy@<hesap1>
    - agy@<hesap2>
    - codex
    - codex@<hesap>
    - ollama
  reviewer:
    - agy@<hesap2>
    - claude
    - codex
    - ollama

# Adım başına toplam ek bağlam devam oturumu (0 = kapalı)
max_context_continuations: 3

# İsteğe bağlı: Yazar ↔ İnceleyici döngüsü (açılırsa inceleyici onaylayana dek tur döner)
loop:
  max_turns: 3            # 1..5 tur
  max_active_minutes: 30  # 5..120 dakika (yalnız aktif çalışma süresi)
```

### Temel Kurallar

- **Rol Profilleri:** `author` (yazar) her zaman `WRITE`, `reviewer` (inceleyici) her zaman `READ_ONLY` profilindedir.
- **Zincir ve Yedek Devri:** Rol başına en fazla **4 halka** tanımlanabilir. Birinci halka doğrulanmış bir kota (`QUOTA_EXCEEDED`) hatası aldığında sıradaki yedek halka görevi devralır.
- **Panelden Varsayılan Takım Kaydetme:** Sol kenar çubuğundaki **Ayarlar** bölümünden (`allowed` tavanı içinde kalmak şartıyla) tercih ettiğiniz varsayılan takımı `.system/settings.json` içine kaydedebilirsiniz. **"Varsayılan olarak kaydet ve kabul et"** seçildiğinde, bu takımla gönderilen mesajlarda tekrar onay kutusu çıkmaz.

---

## 4. İlk Çalışma (Klasör Seçimi, Ayrı Kopya, Takım, Kabul)

1. **Yeni Çalışma Başlatma:** Sol kenar çubuğundan **Yeni çalışma** düğmesine tıklayın.
2. **Klasör Seçimi:** Üzerinde çalışılacak proje klasörünün tam yolunu (ör. `~/Projeler/ornek-uygulama`) yazın, **Klasör seç** ile seçin veya **GitHub'dan** düğmesiyle klonlayın.
3. **Ayrı Kopyada Çalışma (İsteğe Bağlı — Git Worktree):**
   - Proje bir Git deposuysa ve en az bir commit içeriyorsa **Ayrı kopyada çalış** seçeneğini işaretleyebilirsiniz.
   - PromptBridge `conductor/<tarih>-<başlık>-<id>` dalını ve `~/.conductor/worktrees/<proje>-<id>` altında izole bir çalışma kopyasını oluşturur.
   - Bu sayede ajanlar ana proje klasörünüze hiç dokunmaz; aynı projede birden fazla çalışmayı paralel yürütebilirsiniz (proje başına en fazla 10 açık kopya).
   - Çalışma tamamlandığında üst başlıktaki **Ana klasöre getir** düğmesiyle değişen dosyaları ve commit'leri inceleyip projenizin dalına birleştirebilir (`merge --no-ff`) veya işiniz bittiğinde **Kopyayı kaldır** ile temizleyebilirsiniz.
4. **Takım Seçimi ve Kabul:**
   - Varsayılan takımınızla çalışıyorsanız ek onay kutusu gerekmez.
   - Mesaj kutusundan farklı bir model/takım seçtiyseniz veya `providers.yaml` değiştiyse **"Bu takımla başlayacak … kabul ediyorum"** onay kutusunu işaretleyin.
   - İsterseniz **Seçenekler** altından **"Kota dolarsa bekle ve otomatik yeniden dene"** seçeneğini (1–24 saat son süre, 1–5 deneme) etkinleştirin.
5. **Gönderim ve Akış:** İsteğinizi yazıp **Başlat**'a basın:
   - **Adım 1 (Yazar):** Seçilen yazar modeli kodu uygular.
   - **Adım 2 (İnceleyici):** Varsa onaylı testler ve lint çalışır; ardından inceleyici model değişen dosya listesini ve test çıktılarını salt okunur araçlarla inceler.
   - **Adım 3 (Yazar Düzeltmesi / Döngü):** İnceleyici bulgularını yazara aktarır; yazar gerekli düzeltmeleri yapar. `loop` açıksa inceleyici onaylayana (`verified`) veya tur/süre bütçesi dolana kadar döngü devam eder.

---

## 5. Mesaj Kutusu (Model / Efor / Halka, `/` Kısayolları)

Ekranın altında sabit duran mesaj kutusu (composer), çalışma sırasında takımınızı ve oturum durumunuzu yönetmenizi sağlar:

- **Model ve Hesap Seçici:** Yazarın birincil halkasını (`sağlayıcı@<hesap>` → `model`) doğrudan mesaj kutusundan değiştirebilirsiniz. Yanında aktif inceleyici kimliği gösterilir.
- **Düşünme Eforu (Reasoning Effort):** Destekleyen araçlarda (ör. Claude için `low`, `medium`, `high`, `xhigh`, `max` veya Codex varsayılanı) efor düzeyini seçebilirsiniz (`claude/opus~high`). `agy` modellerinde efor model adının parçasıdır (ör. `gemini-3.8-flash-high`).
- **Kota ve Bağlam Halkası (Context / Quota Ring):** Mesaj kutusunda model ve efor seçicinin yanında yer alan dairesel gösterge (`UsageRing`), aktif çalışmadaki **bağlam (context token) kullanım yüzdesini doluluk oranıyla**, **kalan kota yüzdesini ise rengiyle** gösterir. Halkaya tıklandığında açılan **Context ve kota** penceresinde kullanılan/toplam token miktarı, sağlayıcı kota pencereleri ve kotayı güncelleyen **Yenile** düğmesi (`agy /usage` ve Codex kota uçlarını model çağrısı yapmadan okur) yer alır.
- **Gelişmiş Takım Düzenleyici:** Mesaj kutusundaki **Gelişmiş** düğmesi; yazar ve inceleyici için tüm yedek halkaları (1..4), her halkanın doğrulama rozetini, **Oturum durumunu kontrol et** aracını ve model kataloğunu güncelleyen **Yenile** düğmesini açar.
- **`/` Kısayolları (Slash Komutları):** Mesaj kutusunun başında `/` yazdığınızda kısayol menüsü açılır (`↑`/`↓` ile seçilir, `Enter` veya `Tab` ile çalıştırılır, `Esc` ile kapanır). Bu komutlar kendi başına mesaj göndermez veya yürütme başlatmaz; yalnızca sayfadaki seçicileri ve panelleri açar:
  - **Ortak:** `/model` (yazarın modelini seç), `/efor` (yazarın eforunu seç), `/kota` (Context ve kota penceresi), `/takim` (Gelişmiş: yedekler ve inceleyici).
  - **Yeni çalışmada (Mac):** `/klasor` (Finder'dan proje klasörü seç), `/github` (GitHub'dan klonla).
  - **Mevcut çalışmada:** `/onizleme` (projenin önizlemesi, Mac), `/testler` (incelemeden önceki test ve lint, Mac), `/github` (kaydet, gönder, taslak PR — yürütmeler arasında, Mac), `/yeni` (yeni çalışma ekranı).

---

## 6. Testler, Lint ve Önizleme

### 6.1 İncelemeden Önce Testler ve Lint / Tip Denetimi

İnceleyici model güvenlik gereği kabuk komutu çalıştıramaz. Bunun yerine testleri ve lint araçlarını PromptBridge güvenli biçimde çalıştırıp çıktısını inceleyiciye verir:

1. Çalışma başlığındaki **Testler** düğmesine tıklayın.
2. PromptBridge proje yapısına göre otomatik komut önerir:
   - **Test:** `package.json` içinde `npm test`, Python projelerinde `.venv` ile `pytest` veya `python -m unittest discover`.
   - **Lint / Tip Denetimi:** `npm run lint`, `npm run typecheck`, `tsc --noEmit` veya `.venv` içindeki `ruff`, `flake8`, `mypy`.
3. **Bu komutu onayla** ile onayladığınızda, her inceleyici adımdan önce komut **korumalı alanda (sandbox)** çalıştırılır:
   - Ağ erişimi tamamen kapalıdır.
   - Yalnızca proje klasörüne ve özel geçici klasöre yazabilir; ev klasörünüze (`~`) yazamaz, gizli dosyaları okuyamaz.
   - Dosya boyutu 200 MB ve çalışma süresi 5 dakika ile sınırlıdır.
4. `package.json`, `pyproject.toml`, `requirements.txt` gibi yapılandırma dosyaları değişirse güvenlik için onay otomatik olarak düşer ve panelde yeniden onayınız istenir.

### 6.2 Uygulama Önizleme (Preview)

1. Çalışma ekranının üst kısmındaki **Önizleme** düğmesine tıklayın.
2. Klasör için önerilen geliştirme sunucusu komutunu (veya statik site sunucusunu) inceleyip **Onayla** ve **Başlat**'a basın.
3. Önizleme sunucusu yalnızca `127.0.0.1` üzerinde kendi süreç grubunda çalışır.
4. **Korumalı Alan (`sandbox-exec`):** **Ayarlar → Önizleme** altından deneysel korumalı alanı açabilirsiniz. Açıldığında önizleme sunucusu `.ssh`, `.netrc`, `.npmrc`, `~/.config/gh`, Keychain ve `.system` klasörlerine erişemez ve dış ağa bağlanamaz. *(Not: Alt süreçlerine `localhost` üzerinden bağlanan Next.js Turbopack veya dış veritabanı gerektiren projelerde bu kipi kapatıp yalnızca güvendiğiniz komutu onaylayın.)*

---

## 7. GitHub Entegrasyonu

PromptBridge, Mac'inizdeki mevcut `gh` (GitHub CLI) oturumunu kullanır; GitHub jetonunuzu okumaz veya veritabanında saklamaz.

### Kurulum

```sh
brew install gh
gh auth login
```

Ardından **Ayarlar → GitHub** bölümünden GitHub hesabınızın göründüğünü doğrulayın ve klonlanacak depolar için ev klasörünüz altında bir **GitHub klon klasörü** (ör. `~/Projeler`) seçin.

### Depodan Yeni Çalışma Açma

- **Yeni çalışma** ekranında klasör alanının yanındaki **GitHub'dan** düğmesine tıklayın ve listeden deponuzu seçin. Depo `<klon-klasörü>/<depo-adı>` altına klonlanır.

### Değişiklikleri Gönderme (Commit → Push → Taslak PR)

Çalışma başlığındaki **GitHub** düğmesi üç ayrı onay adımı sunar:

1. **Kaydet (Commit):** Değişen dosyalar listelenir. Gizli olabilecek dosyalar (`.env*`, `*.pem`, `*.key`, `id_*`, `credentials*`) varsayılan olarak seçilmez; seçerseniz ek onay istenir. Ana daldaysanız (`main`/`master`) doğrudan ana dala yazılmaz; otomatik olarak `conductor/<tarih>-<başlık>-<id>` dalı açılır.
2. **Gönder (Push):** Gönderilecek dal, commit listesi ve dosya sayısı gösterilir. `.github/workflows/` altında değişiklik varsa GitHub Actions uyarısıyla ek onay istenir. Force push yapılmaz.
3. **Taslak PR (Draft PR):** PR başlığını ve açıklamasını düzenleyip gönderin. PR her zaman **Draft (Taslak)** olarak açılır.

> **Güvenlik Kısıtı:** Ajanların çalıştırdığı alt süreçlerde ve önizleme sunucusunda kimlik doğrulama ortamı temizlenir; ajanlar sizin adınıza `git push` veya `gh` komutu çalıştıramaz. Ayrıca deponun `.git/config` dosyasında şüpheli anahtarlar (`core.hooksPath`, `credential.helper`, `filter`, `include`) varsa veya ajan `.git/hooks/*` dosyalarına dokunduysa PromptBridge git kancalarını çalıştırmaz ve sizi uyarır.

---

## 8. Telefon Eşleştirme (Tailscale)

Telefonunuzdan çalışmaları canlı izlemek, dosya onaylarını yanıtlamak ve (izin verirseniz) yürütmeleri durdurmak veya yeni görev başlatmak için **Mobil Geçit** (`127.0.0.1:8770`) ve **Tailscale** kullanılır.

> **Önemli:** Asla `tailscale funnel` kullanmayın (sunucuyu genel internete açar). Yalnızca kendi Tailnet ağınız içinde `tailscale serve` kullanın.

### Adım 1: Tailscale ve Mobil Geçit Kurulumu (Bir Kez)

1. Mac'inize ve telefonunuza (iOS / Android) **aynı hesapla** Tailscale kurun. Tailscale yönetim konsolunda **MagicDNS** ve **HTTPS Certificates** özelliklerini etkinleştirin.
2. Mac'inizin Tailscale adını öğrenin:
   ```sh
   tailscale status
   # Örnek çıktı: mac-cihazim.tail12345.ts.net
   ```
3. Mac panelinde **Ayarlar → Mobil erişim** bölümüne gidin:
   - Tailscale adınızı (`mac-cihazim.tail12345.ts.net`) yazın.
   - **Mobil geçidi aç** kutusunu işaretleyip **Kaydet**'e basın.
   - Sunucuyu yeniden başlatın: `deploy/launchd.sh restart` (Mobil geçit `127.0.0.1:8770` portunda başlar).
4. Mac terminalinde Tailscale HTTPS yönlendirmesini başlatın:
   ```sh
   tailscale serve --bg --https=443 http://127.0.0.1:8770
   ```

### Adım 2: Telefonu Eşleştirme

1. Mac panelinde **Ayarlar → Mobil erişim → Eşleştirme kodu oluştur** düğmesine basın. Tek kullanımlık ve 5 dakika geçerli **6 haneli bir kod** üretilir (5 hatalı denemede iptal olur).
2. Telefonunuzda Tailscale'i açın ve tarayıcıdan `https://<mac-adiniz>.tail12345.ts.net/ui/` adresine gidin.
3. 6 haneli kodu girip **Eşleştir**'e dokunun.
4. **Ana Ekrana Ekle (PWA):** iPhone Safari'de *Paylaş → Ana Ekrana Ekle*, Android Chrome'da *Menü → Uygulamayı yükle* seçeneğiyle tam ekran uygulama olarak kurabilirsiniz.

### Adım 3: Cihaz İzinleri ve İptal

Mac panelinde **Ayarlar → Mobil erişim → Cihazlar** listesinden her telefonun yetkisini ayrı ayrı yönetebilirsiniz:

- **Varsayılan (Yeni Cihaz):** Yalnızca çalışmaları izler ve dosya onaylarını yanıtlar.
- **Durdurabilir:** Çalışan bir yürütmeyi durdurabilir, kota beklemesini iptal edebilir ve kuyruktaki mesajı sıradan çıkarabilir.
- **Görev verebilir:** Mac'te daha önce açılmış (arşivlenmemiş) bir çalışmanın klasöründe yeni çalışma başlatabilir, mevcut çalışmalara mesaj gönderebilir, `providers.yaml` tavanı içinde model/efor seçebilir ve varsayılan takım ile temayı değiştirebilir. Telefondan iş başlatıldığında Mac'te bildirim gösterilir.
- **İptal et:** Cihazın erişimini anında keser ve açık bağlantıyı düşürür.

---

## 9. Mac Uygulaması (`apps/mac`, `make install`)

Web tarayıcısı yerine bağımsız bir macOS masaüstü penceresinde çalışmak için SwiftUI + WKWebView tabanlı yerel Mac uygulamasını kullanabilirsiniz.

### Gereksinimler

- macOS ve Xcode Command Line Tools (`xcode-select --install`)
- Yerel sunucunun `127.0.0.1:8765` üzerinde çalışıyor olması

### Derleme ve Kurulum

```sh
cd ~/Projeler/PromptBridge/apps/mac
make              # Uygulamayı derler (build/PromptBridge.app)
make install      # /Applications/PromptBridge.app konumuna kurar
make run          # Uygulamayı derler ve doğrudan başlatır
```

Kurulumdan sonra `/Applications/PromptBridge.app` uygulamasını Spotlight veya Finder üzerinden açabilirsiniz.

---

## 10. Güvenlik Modeli

PromptBridge, yerel makinenizde yapay zeka ajanlarını çalıştırırken çok katmanlı bir savunma (defense-in-depth) modeli uygular:

1. **Yalnızca Loopback (`127.0.0.1`):** Ana sunucu (`8765`) ve mobil geçit (`8770`) yalnızca `127.0.0.1` adresini dinler; yerel ağa (`0.0.0.0`) açılmaz.
2. **`mutation_guard` (CSRF ve Origin Koruması):** Durum değiştiren tüm HTTP uç noktaları (`POST`, `PUT`, `DELETE`) `mutation_guard` ile korunur. Yabancı `Origin` / `Referer` başlıklarından gelen çapraz site istekleri `403 Forbidden` ile reddedilir.
3. **Kabuksuz Süreç Çalıştırma (No Shell):** Kullanıcı istemleri (prompt), dosya yolları ve git argümanları hiçbir zaman kabuk (`sh -c`) üzerinden geçirilmez; tüm alt süreçler sabit argüman dizileriyle (`execve`) başlatılır.
4. **Salt Okunur İnceleyici (`READ_ONLY`):** İnceleyici rolündeki sağlayıcılar yazma ve komut çalıştırma araçları kapatılmış olarak çalışır (ör. Claude'da yalnızca `Read/Glob/Grep` + `dontAsk`, Codex'te `read-only` sandbox, `agy`'de varsayılan salt okunur kip).
5. **İzole Sağlayıcı Ev Klasörleri ve `untrusted` Sabitlemesi:**
   - Codex her zaman `.system/codex-home*` altında çalışır ve hedef çalışma klasörü her adımdan önce `untrusted` olarak sabitlenir; böylece hedef depodaki `.codex/` ayarları veya MCP sunucuları kendiliğinden çalışamaz.
   - `agy` her hesap için `.system/agy-home-<hesap>` altında ayrı anahtar zinciriyle çalışır; hedef klasörde `.agents/` veya `.agent/` altında MCP/hook yapılandırması varsa adım başlatılmaz (`agy_workspace_config`).
   - Claude `--safe-mode` ile çalışır; `CLAUDE.md` ve `AGENTS.md` dosyaları sembolik bağ, dizin dışına çıkma ve boyut (16 KiB) kontrollerinden geçirilerek oturum başında dondurulur.
6. **İnsan Onayı ve Denetim İzi:** Politika değişiklikleri, test/lint ve önizleme komutları, GitHub commit/push/PR adımları ve belirsiz süreç kurtarmaları (`RECOVERY_REQUIRED`) açık insan onayı gerektirir. Tüm geçişler SQLite `history` ve `versions` tablolarında yalnız eklemeli (append-only trigger) olarak saklanır.

---

## 11. Sorun Giderme

| Belirti / Hata | Neden ve Çözüm |
|---|---|
| **Panel açılmıyor (`127.0.0.1:8765` yanıt vermiyor)** | Sunucu çalışmıyor. `deploy/launchd.sh status` ile kontrol edin veya `.venv/bin/python -m app serve` ile başlatın. |
| **`install`: "port 8765 is in use by another process"** | Elle başlatılmış bir `serve` süreci portu tutuyor. `lsof -nP -iTCP:8765 -sTCP:LISTEN` ile PID'yi bulup `kill -TERM <PID>` ile durdurun, ardından `deploy/launchd.sh install` çalıştırın. |
| **`kill` komutuyla kapattığım sunucu geri geliyor** | `launchd` ajanı `KeepAlive` kullanır. Sunucuyu durdurmak için `deploy/launchd.sh uninstall` komutunu çalıştırın. |
| **Codex oturumu "giriş yok" / `WAITING_AUTH`** | Kişisel `~/.codex` yerine PromptBridge'in izole alanına giriş yapılmalıdır: `CODEX_HOME="$PWD/.system/codex-home" codex login`. |
| **`agy` modeli "Model yok" veya `model_mismatch` veriyor** | Sağlayıcı model listesini güncellemiştir. Panelde **Gelişmiş → Yenile** düğmesine tıklayın ve `.system/providers.yaml` içindeki model adını güncelleyin. |
| **Mesaj gönderirken `409 conflict`** | `.system/providers.yaml` politikası değişmiştir. Takımı yeniden önizleyip kabul kutusunu onaylayın. |
| **Yeniden dene `409 workspace_busy` veya `recovery_required`** | Klasör başka bir çalışmada kilitli veya önceki süreç temizlenemedi. Çalışma kartındaki **Kurtar** düğmesiyle sahipsiz süreçleri sonlandırıp kilidi açın. |
| **Korumalı alanda önizleme açılmıyor (`EPERM` / `Operation not permitted`)** | Geliştirme sunucusu dış ağa veya `localhost` alt süreçlerine bağlanmaya çalışıyor (ör. Next.js Turbopack). **Ayarlar → Önizleme** altından korumalı alanı kapatın. |
| **Telefonda sayfa açılmıyor veya `421 Misdirected Request`** | Telefonda Tailscale açık olmalı, Mac'te `tailscale serve` çalışmalı ve **Ayarlar → Mobil erişim** içindeki Tailscale alan adı tarayıcıdaki adresle birebir eşleşmelidir. |
| **Yürütme kartında "git kancalarına/ayarına dokundu" uyarısı** | Yazar ajan `.git/hooks/*` veya `.git/config` dosyasını değiştirdi. Terminalde `git commit` çalıştırmadan önce `.git/hooks` klasörünü ve `git config --local --list` çıktısını kontrol edin. |

---

## 12. Sıkça Sorulan Sorular (SSS)

### PromptBridge kullanmak için OpenAI, Anthropic veya Google API anahtarına ihtiyacım var mı?
Hayır. PromptBridge, bilgisayarınızda kurulu olan CLI araçlarını (`claude`, `agy`, `codex`) mevcut abonelik oturumlarınızla veya yerel `ollama` modelleriyle çalıştırır.

### Yazar ve inceleyici aynı model olabilir mi?
`providers.yaml` teknik olarak izin verse de, bağımsız denetim için yazar ve inceleyicinin farklı sağlayıcılar veya farklı modeller (ör. yazar `claude`, inceleyici `agy@<hesap>/gemini-3.8-flash-high` veya `codex`) olması önerilir.

### Kota dolduğunda çalışmam kaybolur mu?
Hayır. Birincil sağlayıcı doğrulanmış kota hatası (`QUOTA_EXCEEDED`) verirse zincirdeki yedek sağlayıcı adımı devralır. Tüm zincir kotaya takılırsa çalışma `WAITING_CAPACITY` durumuna geçer; kota bekleme planı açıksa belirli aralıklarla (15 dk'dan başlayıp katlanarak) otomatik yeniden dener veya siz **Yeniden dene** diyene kadar bekler.

### Ayrı kopyada çalışma (`worktree`) ne zaman kullanılmalı?
Üzerinde aktif olarak kod yazdığınız bir projede ajanın dosyalarınızı değiştirmesini istemiyorsanız veya aynı depoda aynı anda birden fazla görev yürütmek istiyorsanız **Ayrı kopyada çalış** seçeneğini kullanın.

### Sunucu çökerse veya bilgisayar kapanırsa ne olur?
Durum makinesi SQLite (WAL) üzerinde kalıcıdır. Sunucu yeniden başladığında yarıda kalan yürütmeler otomatik olarak yeniden başlatılmaz; güvenli biçimde `RECOVERY_REQUIRED` durumuna alınır ve klasör kilidi korunur. Panelden **Kurtar** adımıyla süreç temizliğini doğruladıktan sonra çalışmaya devam edebilirsiniz.
