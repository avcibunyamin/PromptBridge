# Katkıda Bulunma Rehberi / Contributing Guide

PromptBridge (Conductor) projesine katkıda bulunmak istediğiniz için teşekkür ederiz! Bu belge; geliştirme ortamını hazırlama, testleri çalıştırma, dal (branch) yönetimi ve **yazar → inceleyici** inceleme akışını açıklar.

> **English section below:** [Contributing to PromptBridge (English)](#contributing-to-promptbridge-english)

---

## Dış Katkıcılar İçin

PromptBridge projesine dışarıdan katkıda bulunmak için lütfen aşağıdaki adımları izleyin:

1. **Çatal (Fork) ve Dal (Branch):** Depoyu GitHub üzerinde fork'layın ve kendi deponuzda yeni bir özellik veya hata düzeltme dalı oluşturun (`git checkout -b ozellik-adi`).
2. **Testleri Çalıştırma:** Değişikliklerinizi göndermeden önce arka uç ve ön yüz testlerinin başarıyla tamamlandığını doğrulayın:
   - Arka uç testleri:
     ```sh
     cd ai-orchestrator && .venv/bin/python -m unittest discover -s tests
     ```
   - Ön yüz testleri ve derleme:
     ```sh
     cd ui && npm test && npm run build
     ```
     *(Depo kökünden çalıştırmak için: `cd ai-orchestrator/ui && npm test && npm run build`)*
3. **CI:** Pull Request'ler macOS ve Linux'ta otomatik test edilir; `ai-orchestrator/app/static/` içindeki derlenmiş panel temiz bir `npm run build` çıktısıyla aynı olmalıdır, arayüzü değiştirdiyseniz derleme çıktısını da commit edin.
4. **Pull Request:** Değişikliklerinizi `main` dalına yönelik küçük ve tek bir amaca odaklanmış bir Pull Request (PR) olarak açın.
5. **Kod Dili:** Tüm kodlar, fonksiyon/değişken adları, yorumlar ve commit mesajları İngilizce olmalıdır.
6. **Güvenlik Açıkları:** Güvenlik açıklarını kesinlikle herkese açık GitHub Issues veya PR üzerinden bildirmeyin; sorumlu bildirim adımları için [SECURITY.md](SECURITY.md) belgesine bakın.

---

## 1. Geliştirme Ortamı Kurulumu

```sh
cd ai-orchestrator
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p .system && cp providers.example.yaml .system/providers.yaml

cd ui
npm ci
npm run build
cd ..
```

---

## 2. Testleri Çalıştırma

Her değişiklikten sonra hem arka uç (Python/SQLite) hem de ön yüz (React/TypeScript) testlerinin ve tip denetiminin geçtiğinden emin olun.

### Arka Uç Testleri (`ai-orchestrator`)

Birim ve entegrasyon testleri standart `unittest` ve geçici gerçek SQLite veritabanları kullanır; sağlayıcı kotası harcamaz:

```sh
cd ai-orchestrator
.venv/bin/python -m unittest discover -s tests
```

Ayrıntılı çıktı veya tek bir test modülü için:

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest tests.test_store -v
```

> **Not:** `tests/smoke_*.py` dosyaları gerçek sağlayıcı CLI'larını çağırır ve abonelik kotası tüketir. Açıkça gerekmedikçe çalıştırmayın. `tests/preview_*.py` ve `tests/drill_*.py` dosyaları sahte (fake) sağlayıcı ikilileriyle çalışır.

### Ön Yüz Testleri ve Derleme (`ai-orchestrator/ui`)

Panel testlerini çalıştırmak, TypeScript tip kontrolünü (`tsc -b`) doğrulamak ve üretim paketini (`app/static/`) derlemek için:

```sh
cd ai-orchestrator/ui
npm test
npm run build
```

---

## 3. Dal (Branch) ve İnceleme Akışı (Maintainers' Own Workflow)

PromptBridge geliştirme süreci kendi mimarisiyle uyumlu olarak **ayrı çalışma kopyası (worktree / branch)** ve **bağımsız inceleyici** ilkelerine dayanır:

1. **Ayrı Dal / Worktree:**
   - Doğrudan `main` dalında geliştirmeyin. Her özellik veya hata düzeltmesi için ayrı bir dal veya git worktree (`../PromptBridge-<dal>` ya da `~/.conductor/worktrees/...`) açın.
   - `main` çalışma kopyasında çalışan canlı bir sunucu varsa orada `git stash`, `git reset` veya `git checkout` çalıştırmayın; farkları `git diff main...<dal>` ile inceleyin.
2. **Yazar ve Bağımsız İnceleyici Ayrımı:**
   - Kodu yazan (uygulayan) model/geliştirici ile inceleyen (reviewer) farklı olmalıdır.
   - İnceleyici değişiklikleri salt okunur olarak denetler; güvenlik sınırlarını, durum geçişlerini (`app/transitions.py`), SQLite işlem kurallarını (`store.transaction(conn)`) ve test kapsamını kontrol eder.
3. **Mimari ve Güvenlik Kuralları:**
   - Durum geçiş yasallığı yalnızca `app/transitions.py` içinde tanımlanır; `store.set_state` yalnızca CAS + denetim kaydı (audit) yapar.
   - Veritabanı yazımlarında ham `with conn:` yerine her zaman `with store.transaction(conn):` kullanın; her iş parçacığı (thread) kendi bağlantısını açmalıdır.
   - Veritabanı şeması değişiyorsa `app/schema.sql` ve `app/store.py` içindeki sürümü idempotent olarak güncelleyin ve geriye dönük uyumluluk testlerini ekleyin.
   - Güvenlik sınırlarını koruyun: yalnızca `127.0.0.1` dinlemesi, durum değiştiren her uç noktada `mutation_guard`, kabuksuz (`execve`) alt süreç çağrıları ve salt okunur inceleyici profilleri.
4. **Gizlilik ve Kişisel Bilgi Kuralı:**
   - Kod, test veya belgelere gerçek kullanıcı adları, e-posta adresleri, `/Users/<gercek-ad>/` yolları, gerçek hesap adları veya sırlar eklemeyin. Örneklerde her zaman `<hesap>`, `<kullanici>` ve `~/Projeler/...` gibi yer tutucular kullanın.
5. **Kod Dili ve Yerel Kayıtlar:**
   - Kaynak kod, tanımlayıcılar, yorumlar ve commit mesajları İngilizce yazılır; `_isbirligi/` yerel planlama kayıtlarıdır ve `.gitignore` ile depo dışında tutulur.
6. **Kabul ve Birleştirme (Merge):**
   - Bir görev yalnızca tüm testler geçtikten, bağımsız incelemeden onay aldıktan ve proje sahibinin açık kabulünden sonra `main` dalına birleştirilir.

---

## Contributing to PromptBridge (English)

Thank you for your interest in contributing to **PromptBridge (Conductor)**!

### For External Contributors

If you are an external contributor, please follow this workflow:

1. **Fork & Branch:** Fork the repository on GitHub and create a dedicated feature or bugfix branch on your fork (`git checkout -b your-feature-branch`).
2. **Run Tests:** Ensure both backend and frontend test suites pass and the UI builds cleanly before opening a pull request:
   - Backend tests:
     ```sh
     cd ai-orchestrator && .venv/bin/python -m unittest discover -s tests
     ```
   - Frontend tests & build:
     ```sh
     cd ui && npm test && npm run build
     ```
     *(or from the repository root: `cd ai-orchestrator/ui && npm test && npm run build`)*
3. **CI:** Pull requests are tested automatically on macOS and Linux; the built panel in `ai-orchestrator/app/static/` must match a fresh `npm run build`, so commit the build output when you change the UI.
4. **Pull Request:** Open a pull request against the `main` branch. Keep pull requests small and focused on a single change or fix.
5. **Code, Comments, and Commit Messages:** All source code, identifiers, comments, and commit messages must be written in English.
6. **Security Vulnerabilities:** Do **not** report security vulnerabilities as public GitHub issues or PRs. Please refer to [SECURITY.md](SECURITY.md) for private disclosure instructions.

### Running Tests

Before submitting a pull request, verify that both backend and frontend test suites and builds succeed.

1. **Backend tests (`ai-orchestrator`):**
   Runs with Python's standard `unittest` against temporary SQLite databases (consumes zero provider quota):
   ```sh
   cd ai-orchestrator
   .venv/bin/python -m unittest discover -s tests
   ```
   *(Note: `tests/smoke_*.py` invoke real provider CLIs and consume quota—do not run them unless specifically testing live provider integration.)*

2. **Frontend tests and production build (`ai-orchestrator/ui`):**
   Runs unit tests, TypeScript type checking (`tsc -b`), and builds the static bundle into `app/static/`:
   ```sh
   cd ai-orchestrator/ui
   npm test
   npm run build
   ```

### Maintainers' Own Workflow

1. **Isolated Branches / Worktrees:**
   - Develop features and fixes on a dedicated branch or Git worktree (`../PromptBridge-<branch>` or `~/.conductor/worktrees/...`), never directly on `main`.
   - Review diffs with `git diff main...<branch>`.
2. **Author → Reviewer Separation:**
   - The implementer (author) and the reviewer should be independent.
   - Every change is verified against unit/integration tests, TypeScript builds, and architectural invariants before merging into `main`.
3. **Architecture & Security Invariants:**
   - Use `with store.transaction(conn):` for SQLite transactions (never raw `with conn:`), and use one SQLite connection per thread.
   - Keep legal state transition logic inside `app/transitions.py`.
   - Preserve all security boundaries: loopback-only binding (`127.0.0.1`), `mutation_guard` on every mutating HTTP endpoint, no shell interpolation for prompts/arguments, and strictly read-only reviewer profiles.
4. **No Personal Data in Code or Docs:**
   - Never commit real usernames, email addresses, `/Users/<real-name>/` paths, account identifiers, or tokens. Use placeholders such as `<account>`, `<user>`, and `~/Projects/...`.
5. **Code Language and Local Records:**
   - Source code, identifiers, comments, and commit messages are in English; `_isbirligi/` contains local planning records and is excluded from the repository via `.gitignore`.
