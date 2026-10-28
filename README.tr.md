[English](README.md) · **Türkçe**

[![CI](https://github.com/avcibunyamin/PromptBridge/actions/workflows/ci.yml/badge.svg)](https://github.com/avcibunyamin/PromptBridge/actions/workflows/ci.yml)

# PromptBridge

**PromptBridge — kendi makinenizde gözetimli bir yapay zeka kodlama ekibi.**

- **Claude Code**, **Codex**, **Antigravity (Gemini)** ve **Ollama** araçlarını **yazar → bağımsız salt okunur inceleyici** ekibi olarak çalıştırın.
- Halihazırda yapay zeka kodlama abonelikleri kullanan ve bir modelin yazdığını ikinci bir modelin denetlemesini isteyen geliştiriciler içindir — **API anahtarı gerekmez**.
- Her değişikliği farklı bir model inceler, incelemeden önce testleriniz kanıt olarak koşar (macOS korumalı alanı) ve her adım yalnızca ekleme yapılan yerel denetim izine kaydedilir.

![PromptBridge panelinde bir çalışma: yazar (Codex) yazar, bağımsız salt okur inceleyici (Claude) denetler, yazar düzeltir](docs/images/panel-work.png)
<sub>Sahte sağlayıcılı test fikstüründen örnek veri.</sub>

> **Adlandırma:** ürünün adı **PromptBridge**'dir. **Conductor**, yalnızca iç sunucu bileşeninin adıdır (`ai-orchestrator/` içindeki yerel izleyici + API + web paneli süreci).

---

## Gereksinimler

- **İşletim Sistemi:** macOS (birincil). Linux: yalnızca sunucu ve panel — bkz. [Platform desteği](#platform-desteği).
- **Python 3.12+** ve **Node.js 22.6+** (yalnızca paneli yeniden derlemek veya arayüz testlerini çalıştırmak için) (macOS'ta: `brew install python@3.12 node`).
- **En az bir sağlayıcı CLI aracı:** Claude Code (`claude`), Antigravity (`agy`), Codex (`codex`) veya Ollama (`ollama`).
- İsteğe bağlı: `gh` (GitHub klonlama / taslak PR), `tailscale` (telefon erişimi), Xcode Command Line Tools (Mac uygulaması).

---

## Hızlı başlangıç

1. adımdan sonraki tüm komutlar `ai-orchestrator/` dizini içinde çalıştırılır.

1. **Klonlayın:**
   ```sh
   git clone https://github.com/avcibunyamin/PromptBridge && cd PromptBridge
   cd ai-orchestrator
   ```
2. **Sanal ortam oluşturun ve bağımlılıkları yükleyin:**
   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```
   `python3` sürümü 3.12 veya daha yeni olmalıdır (`python3 --version`); varsayılan sürümünüz daha eskiyse doğrudan `python3.12` kullanın.
3. **`.system/providers.yaml` dosyasını oluşturun** — kurulu CLI'larınıza uyan `cp` komutunu çalıştırın:
   ```sh
   mkdir -p .system
   cp providers.example.yaml .system/providers.yaml         # varsayılan (codex + claude)
   # cp providers.claude-only.yaml .system/providers.yaml   # yalnızca claude
   # cp providers.codex-only.yaml .system/providers.yaml    # yalnızca codex
   # cp providers.agy-only.yaml .system/providers.yaml      # yalnızca agy (iki hesap)
   # cp providers.ollama-local.yaml .system/providers.yaml  # ollama (yerel)
   ```
4. **Panel:** derlenmiş panel `app/static/` içinde hazır gelir; yalnızca arayüzü değiştirirseniz yeniden derleyin: `cd ui && npm install && npm run build && cd ..`
5. **Sağlayıcılarda oturum açın** (yalnızca kullandıklarınız için):
   - **Claude Code:** `claude` komutunu çalıştırıp `/login` kullanın.
   - **Codex:** `CODEX_HOME="$PWD/.system/codex-home" codex login`
   - **Antigravity:** panel açıldıktan sonra (Adım 7) **Settings → Accounts** altından hesabı ekleyin ve orada gösterilen komutu çalıştırın.
   - **Ollama:** `ollama serve` ayrı bir terminalde çalışır (ya da Ollama uygulaması açık olsun).
6. **Kurulumu denetleyin, ardından sunucuyu başlatın:**
   ```sh
   .venv/bin/python -m app doctor     # python, providers.yaml, CLI'lar, panel — sağlayıcı çağrısı yok
   .venv/bin/python -m app serve
   ```
   Sunucu çalışmadan önce `doctor` çıktısında `server not running` `WARN` uyarısı görülmesi normaldir. Port doluysa `.venv/bin/python -m app serve --port <PORT>` kullanabilirsiniz; tüm komutlar için `.venv/bin/python -m app --help` çalıştırın.
7. **<http://127.0.0.1:8765/ui/> adresini açın.** Yeni kurulumda **kurulum sihirbazı** (`#/setup`, kenar çubuğunda **Kurulum**) açılır ve her adımı sizin için denetler; ilk çalışmadan önce **Ayarlar → Hesaplar** altından oturum durumunu denetleyin.
8. **İsteğe bağlı — macOS'ta otomatik başlatma:** `launchd` kurmadan önce ön plandaki `serve` sürecini `Ctrl-C` ile durdurun (aynı port), ardından `deploy/launchd.sh install` çalıştırın. Durdurmak için `deploy/launchd.sh uninstall` kullanın; düz `kill` sonrası süreç `launchd` tarafından yeniden başlatılır.

İsteğe bağlı yerel Mac uygulaması: `cd ../apps/mac && make install` (bkz. [apps/mac/README.md](apps/mac/README.md)).

---

## Kurulum sihirbazı

Panel kurulumunuzu `python -m app doctor` ile aynı denetimlerle (salt okur, sağlayıcı çağrısı yok) denetler ve her adımı durumu ve kopyalanabilir komutuyla gösterir.

![Kurulum sihirbazı: Python, providers.yaml, sağlayıcı CLI'ları, oturum açma, panel ve ilk çalışma, her biri durumuyla](docs/images/setup-wizard.png)

| Genel bakış | Ayarlar |
|---|---|
| ![Tıklanabilir durum kartları, son çalışmalar ve hızlı başlangıç ile genel bakış](docs/images/panel-overview.png) | ![Sekmelere bölünmüş ayarlar: takım, hesaplar, modeller, görünüm, entegrasyonlar, telefon](docs/images/settings-tabs.png) |

<sub>Görüntüler sahte sağlayıcılı test fikstüründen. Aydınlık ve koyu tema sisteminizi ya da **Ayarlar → Görünüm**'ü izler.</sub>

---

## İlk çalışmanız

1. **Genel Bakış** ekranında **Yeni çalışma** düğmesine tıklayın.
2. Proje klasörünü seçin (git deposuysa **Ayrı kopyada çalış** kutusunu işaretleyin).
3. Görevi yazın ve takımı onaylayın (**Gönder**).
4. Yazar, inceleyici ve düzeltme adımları **Çalışmalar** ekranında akar; ayrı kopyada bittiğinde **Ana klasöre getir** düğmesine tıklayın.

---

## Nasıl çalışır

1. Bir proje klasörü ve bir ekip (yazar + inceleyici) seçip görevi yazarsınız.
2. **Yazar** model, yazma yetkili profille değişikliği yapar.
3. Onayladığınız **testler ve lint önce koşar** (macOS'ta ağsız korumalı alanda); sonuçlar inceleyiciye kanıt olarak gider.
4. **Farklı, bağımsız bir inceleyici** model farkı (diff) **salt okunur** araçlarla denetler ve bulguları raporlar.
5. Yazar bulguları giderir; sonuçtan memnun olduğunuzda kabul edersiniz.

İlk çalıştırmalar için **yalıtılmış kopya** (ayrı git worktree) önerilir: siz **Ana klasöre getir** düğmesine tıklayana kadar ana klasörünüze dokunulmaz.

---

## Size sormadan ne çalışır

- Ekibi ve sağlayıcı araç izinlerini **siz onaylarsınız**.
- **Yazar → inceleyici devri** varsayılan olarak otomatiktir (`require_handover_approval: false`).
- **Döngü (loop)** açıksa turlar otomatik devam eder.
- Bir sağlayıcının kotası bittiğinde **yalnızca zincirdeki bir sonraki sağlayıcı** devralır — yapılandırdığınız zincirin dışına çıkılmaz.

---

## Veri akışı ve güvenlik

- Sunucu **yalnızca 127.0.0.1 üzerinde** dinler — ancak **kodunuz ve istemleriniz seçtiğiniz bulut sağlayıcılara gider**. Yalnızca **Ollama** yerel kalır.
- **Telefon erişimi**, **kendi Tailscale ağınız (tailnet)** üzerinden geçer. Yeni cihazlar yalnızca görüntüleyebilir ve onay verebilir; durdurma/görev gönderme yetkisi Mac üzerinden cihaz bazında açılmalıdır.
- **İnceleyiciler salt okunurdur.**
- **İstemler hiçbir zaman kabuktan (shell) geçmez.**
- Ayrıntılar ve güvenlik açığı bildirimi: [SECURITY.md](SECURITY.md).

---

## Platform desteği

| Özellik | macOS | Linux |
| --- | :---: | :---: |
| Sunucu + web paneli | ✓ | ✓ |
| launchd ile otomatik başlatma | ✓ | — (yalnızca macOS) |
| İnceleme öncesi ağsız korumalı alanda testler | ✓ | — (Linux'ta yok) |
| Önizleme korumalı alanı (isteğe bağlı) | ✓ | — (yalnızca macOS) |
| Keychain ile Antigravity hesap yalıtımı | ✓ | — |
| Mac uygulaması | ✓ | — |
| Tailscale ile telefon erişimi | ✓ | ✓ |

---

## Sorumluluk

Birden fazla abonelik hesabı kullanmak ve bunlar arasında otomatik yedek geçişi yapmak, her sağlayıcının hizmet koşullarıyla sınırlandırılmış olabilir. Kullandığınız her sağlayıcının koşullarını kontrol edin.

---

## Belgeler

- [Kullanım Kılavuzu (Türkçe)](docs/KULLANIM-KILAVUZU.md) — kurulum, sağlayıcı kurulumu, iş akışları, telefon eşleştirme, güvenlik modeli, sorun giderme, SSS.
- [User Guide (English)](docs/USER-GUIDE.md) — İngilizce kullanım kılavuzu.
- [Operatör Rehberi](OPERATOR-REHBERI.md) — günlük işletim, yedekleme/geri yükleme, sorun giderme.
- [Mimari Doküman](AI-Orkestrasyon-Mimari-Dokumani.md) — durum makineleri, güvenlik sınırları, tasarım kararları.
- [CONTRIBUTING.md](CONTRIBUTING.md) — testleri çalıştırma, dallar, yazar → inceleyici akışı.
- [SECURITY.md](SECURITY.md) — güvenlik açığı bildirimi ve tehdit modeli.
- [CHANGELOG.md](CHANGELOG.md) — sürüm notları.
- [Sunucu README](ai-orchestrator/README.md) · [Mac uygulaması README](apps/mac/README.md)

---

## Lisans

[MIT](LICENSE) — `Copyright (c) 2026 PromptBridge contributors`.
