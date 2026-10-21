# Güvenlik Politikası / Security Policy

Bu belge, PromptBridge (Conductor) projesinde tespit edilen güvenlik açıklarının nasıl bildirileceğini ve sistemin tehdit modelini (threat model) özetler.

> **English section below:** [Security Policy (English)](#security-policy-english)

---

## 1. Güvenlik Açığı Bildirimi (Vulnerability Reporting)

Güvenlik açıklarını **halka açık GitHub Issues** üzerinden **bildirmeyin**.

Bir güvenlik açığı tespit ettiyseniz lütfen aşağıdaki sorumlu bildirim (responsible disclosure) adımlarını izleyin:

1. **Özel Bildirim:** GitHub deposunun **Security → Advisories → Report a vulnerability** (GitHub Özel Güvenlik Bildirimi) özelliğini kullanarak açığı özel olarak bildirin.
2. **Bildirimde Yer Alması Gerekenler:**
   - Etkilenen bileşen veya dosya (ör. `ai-orchestrator/app/api.py`, `preview.py`, `mobile.py`, adaptörler vb.).
   - Açığın türü ve etkisi (ör. `mutation_guard` atlatma, kabuk enjeksiyonu, dizin dışına çıkma / path traversal, korumalı alan dışına çıkma / sandbox escape, yetkisiz durum değişikliği).
   - Gerçek kullanıcı verisi veya gizli anahtar içermeyen, adım adım yeniden üretim (reproduction) talimatları veya kavram kanıtı (PoC).
   - Varsa önerdiğiniz çözüm veya yama.
3. **Yanıt Süreci:** Bildiriminiz incelendikten sonra doğrulama, düzeltme geliştirme ve sürüm yayımı koordine edilir; düzeltme yayınlandıktan sonra güvenlik bülteni paylaşılır.

---

## 2. Tehdit Modeli Özeti (Threat Model Overview)

PromptBridge, yerel bir geliştirici makinesinde birden fazla yapay zeka kodlama CLI aracını (Claude Code, Antigravity `agy`, Codex, Ollama) çalıştıran bir orkestratördür. Tehdit modeli aşağıdaki temel riskleri ve savunma sınırlarını kapsar:

### 2.1 Güvenlik Sınırları ve Savunmalar

| Tehdit / Risk | Savunma Mekanizması |
|---|---|
| **Ağ üzerinden yetkisiz erişim** | Ana sunucu (`127.0.0.1:8765`) ve mobil geçit (`127.0.0.1:8770`) yalnızca yerel loopback arayüzünü dinler; yerel ağa (`0.0.0.0`) açılmaz. |
| **Tarayıcı üzerinden CSRF / Drive-by saldırıları** | Durum değiştiren her HTTP uç noktası (`POST`, `PUT`, `DELETE`) `mutation_guard` ile korunur; yabancı `Origin` / `Referer` başlıkları `403 Forbidden` ile reddedilir. |
| **Komut / Kabuk Enjeksiyonu (Shell Injection)** | Kullanıcı istemleri, dosya yolları ve git argümanları asla bir kabuk (`sh -c`) üzerinden çalıştırılmaz; tüm alt süreçler sabit argüman dizileriyle (`execve`) başlatılır. |
| **İnceleyici ajanın dosya değiştirmesi veya komut çalıştırması** | İnceleyici (`reviewer`) her zaman `READ_ONLY` profilinde çalışır (Claude'da yalnız `Read/Glob/Grep` + `dontAsk`, Codex'te `read-only` sandbox, `agy`'de salt okunur kip ve kapalı komut aracı). |
| **Zararlı depo yapılandırmaları (`.codex`, `.agents`, `.git/config`, kancalar)** | Codex süreçleri izole `.system/codex-home*` alanında çalışır ve hedef klasör her adımdan önce `untrusted` olarak sabitlenir. `agy` hesap başına izole anahtarlık (keychain) içeren `.system/agy-home-<account>` dizinlerinde çalışır ve hedef klasörde `.agents/` veya `.agent/` MCP/hook yapılandırması varsa çalışmayı reddeder (`agy_workspace_config`). Claude `--safe-mode` ile çalışır. GitHub akışı `.git/config` içindeki tehlikeli anahtarları (`core.hooksPath`, `credential.helper`, `filter`, `include`) reddeder ve `.git/hooks/*` değişikliklerini kullanıcıya bildirir. |
| **Proje talimat dosyaları (`CLAUDE.md`, `AGENTS.md`) üzerinden dosya sızdırma** | Claude `--safe-mode` ile çalışır; talimat dosyaları sembolik bağ (symlink), mutlak/`~`/klasör dışı yol, kontrol karakteri, derinlik (en fazla 5) ve boyut (16 KiB) kontrollerinden geçirilir ve oturum başında dondurulur. |
| **Ajanların veya önizlemenin kullanıcı kimliğiyle GitHub'a push yapması** | Alt süreç ortamlarından kimlik bilgisi değişkenleri temizlenir; GitHub commit/push/Draft PR işlemleri yalnızca kullanıcının paneldeki üç aşamalı açık onayıyla (gizli dosya ve `.github/workflows/` uyarılarıyla) yürütülür. |
| **Test ve önizleme komutlarının ev klasörüne veya ağa erişmesi** | İnceleme öncesi test/lint komutları ağsız korumalı alanda, ev klasörüne yazma izni olmadan ve 200 MB / 5 dk sınırıyla çalışır. Önizleme sunucusu açık komut özeti (hash) onayı gerektirir ve isteğe bağlı macOS `sandbox-exec` korumalı alanında `.ssh`, `.netrc`, `.npmrc`, `~/.config/gh`, Keychain ve `.system` erişimi kapatılarak çalıştırılabilir. |
| **Mobil erişim ve cihaz kaybı** | Mobil erişim yalnızca kullanıcının kendi Tailscale (`tailscale serve` HTTPS) ağı üzerinden çalışır. Eşleştirme 5 dakika geçerli, tek kullanımlık 6 haneli kodla yapılır (5 deneme sınırı). Yeni cihazlar varsayılan olarak salt izleme/onay yetkisindedir; "Durdurabilir" ve "Görev verebilir" yetkileri yalnız Mac'ten verilir ve cihaz Mac'ten anında iptal edilebilir. |
| **Denetim izinin (Audit Log) değiştirilmesi** | SQLite `versions` ve `history` tabloları `UPDATE` ve `DELETE` işlemlerini engelleyen veritabanı tetikleyicileri (append-only triggers) ile korunur. |

### 2.2 Kapsam Dışı / Bilinen Sınırlar

- **Yerel Kullanıcı / Root Yetkisi:** Aynı macOS/Linux kullanıcı hesabında tam yetkiyle çalışan diğer süreçler SQLite dosyasına (`.system/state.db`) doğrudan erişebilir; PromptBridge yerel işletim sistemi kullanıcısına karşı bir DRM/yetkilendirme kalkanı değildir.
- **Yazar (`WRITE`) Profilinin Çalışma Klasörü Erişimi:** Yazar rolündeki ajan, görevini yerine getirmek için hedef çalışma klasöründeki dosyaları düzenleme yetkisine sahiptir. Ana proje klasörünüzü korumak için **Ayrı kopyada çalış (git worktree)** özelliğini kullanmanız önerilir.

---

## Security Policy (English)

### 1. Vulnerability Reporting

Please **do not** report security vulnerabilities through **public GitHub Issues**.

If you discover a security vulnerability, please follow these responsible disclosure steps:

1. **Private Disclosure:** Report the vulnerability privately using the repository's **Security → Advisories → Report a vulnerability** (GitHub Security Advisories) feature.
2. **What to Include:**
   - Affected component or file (e.g., `ai-orchestrator/app/api.py`, `preview.py`, `mobile.py`, provider adapters, etc.).
   - Vulnerability class and impact (e.g., `mutation_guard` bypass, command/shell injection, path traversal, sandbox escape, unauthorized state mutation).
   - Step-by-step reproduction instructions or a minimal proof-of-concept (PoC) without real user data or secrets.
   - Suggested fix or patch, if any.
3. **Response Process:** Once your report is reviewed, verification, fix development, and release publication are coordinated; a security advisory is published after the fix is released.

### 2. Threat Model Overview

PromptBridge orchestrates multiple AI coding CLI tools (Claude Code, Antigravity `agy`, Codex, Ollama) on a local developer machine. Its threat model covers the following core risks and defense boundaries:

#### 2.1 Security Boundaries and Defenses

| Threat / Risk | Defense Mechanism |
|---|---|
| **Unauthorized network access** | Both the primary server (`127.0.0.1:8765`) and the mobile gateway (`127.0.0.1:8770`) listen exclusively on the local loopback interface and are never exposed to the local network (`0.0.0.0`). |
| **Browser CSRF / drive-by attacks** | Every state-mutating HTTP endpoint (`POST`, `PUT`, `DELETE`) is protected by `mutation_guard`; foreign `Origin` / `Referer` headers are rejected with `403 Forbidden`. |
| **Command / Shell Injection** | User prompts, file paths, and Git arguments are never executed through a shell (`sh -c`); all subprocesses are spawned with fixed argument arrays (`execve`). |
| **Reviewer agent modifying files or running commands** | The reviewer (`reviewer`) always runs in the `READ_ONLY` profile (only `Read/Glob/Grep` + `dontAsk` in Claude, `read-only` sandbox in Codex, read-only mode and disabled command tool in `agy`). |
| **Malicious repository configs (`.codex`, `.agents`, `.git/config`, hooks)** | Codex processes run in an isolated `.system/codex-home*` directory with the target workspace pinned as `untrusted` before every step. `agy` runs in per-account `.system/agy-home-<account>` directories with isolated keychains and refuses workspaces containing `.agents/` or `.agent/` MCP/hook configs (`agy_workspace_config`). Claude runs with `--safe-mode`. The GitHub flow rejects dangerous keys in `.git/config` (`core.hooksPath`, `credential.helper`, `filter`, `include`) and warns the user about `.git/hooks/*` changes. |
| **File exfiltration via project instruction files (`CLAUDE.md`, `AGENTS.md`)** | Claude runs with `--safe-mode`; instruction files are validated against symlinks, absolute/`~`/out-of-workspace paths, control characters, depth (max 5), and size (16 KiB), and are frozen at session start. |
| **Agents or preview pushing to GitHub with user credentials** | Credential environment variables are stripped from subprocess environments; GitHub commit/push/Draft PR operations are executed only after three-step explicit human confirmation in the panel (with secret-file and `.github/workflows/` warnings). |
| **Test and preview commands accessing home directory or network** | Pre-review test/lint commands run in a network-disabled sandbox without home-directory write access and with 200 MB / 5 min limits. The preview server requires explicit command hash approval and can run inside an optional macOS `sandbox-exec` sandbox blocking access to `.ssh`, `.netrc`, `.npmrc`, `~/.config/gh`, Keychain, and `.system`. |
| **Mobile access and device loss** | Mobile access operates exclusively over the user's own Tailscale (`tailscale serve` HTTPS) network. Pairing uses a single-use 6-digit code valid for 5 minutes (5-attempt limit). New devices default to view/approval-only permissions; "Can stop" and "Can dispatch tasks" permissions are granted only from the Mac, and a device can be revoked immediately from the Mac. |
| **Audit log tampering** | SQLite `versions` and `history` tables are protected by append-only database triggers that block `UPDATE` and `DELETE` operations. |

#### 2.2 Out of Scope / Known Limitations

- **Local User / Root Access:** Other processes running with full privileges under the same macOS/Linux user account can directly access the SQLite file (`.system/state.db`); PromptBridge is not a DRM or authorization shield against the local OS user.
- **Writer (`WRITE`) Profile Workspace Access:** An agent in the writer role is authorized to edit files in the target workspace to fulfill its task. To protect your main project directory, using the **Work in isolated copy (git worktree)** feature is recommended.
