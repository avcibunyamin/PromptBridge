# PromptBridge Mac Uygulaması

PromptBridge / Conductor sunucusu için tasarlanmış, SwiftUI ve WKWebView tabanlı ince bir Mac uygulaması (kabuk).

## Özellikler
- **Yerel Uygulama Deneyimi:** Pencere, Dock simgesi.
- **Sunucu Yönetimi:** Conductor arka planda (launchd) çalışmıyorsa durumu algılar ve kurulum/başlatma seçeneği sunar.
- **Güvenli WebView:** Ağ erişimi sadece `127.0.0.1:8765` ile sınırlıdır; dış bağlantılar varsayılan tarayıcıda açılır.
- **Native Klasör Seçici:** Yeni çalışma ekranında "Klasör Seç" butonu ile macOS'un yerel seçici ekranını (NSOpenPanel) kullanır.

## Kurulum ve Derleme
Bu uygulama, otomatik araçların karmaşıklığını önlemek için Xcode proje dosyası yerine `swiftc` kullanılarak doğrudan bir `Makefile` ile derlenecek şekilde tasarlanmıştır.

```bash
cd apps/mac
make
```

Bu işlem `build/PromptBridge.app` bundle'ını oluşturur.

## Kullanım
Uygulamayı `/Applications` (Uygulamalar) klasörüne kopyalayarak kalıcı olarak kullanabilirsiniz:

```bash
cp -R build/PromptBridge.app /Applications/
```

Uygulamayı açtığınızda sunucu çalışıyorsa doğrudan panele erişirsiniz. Sunucu kapalıysa sizi yönlendirir.

## Mimari ve Güvenlik
- Swift kodu yalnızca 2 mesaj türünü köprüler: `selectFolder` ve `agentStatus`.
- Panele (JS tarafına) gönderilen tek veri, klasör seçildiğinde yoldur (`folderSelected` eventi).
- Dış hiçbir bağımlılık içermez (npm, Rust, vb. yok). Yalnızca sistem bileşenleri kullanılır.
