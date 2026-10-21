import SwiftUI
import WebKit

struct ContentView: View {
    @StateObject private var serverManager = ServerManager()
    
    var body: some View {
        if serverManager.isServerRunning {
            WebView(url: URL(string: "http://127.0.0.1:8765/ui/")!)
        } else {
            VStack(spacing: 20) {
                Text("PromptBridge Sunucusu Kapalı")
                    .font(.title)
                
                if serverManager.isInstalled {
                    Button("Sunucuyu Başlat") {
                        serverManager.startServer()
                    }
                    .padding()
                    .background(Color.accentColor)
                    .foregroundColor(.white)
                    .cornerRadius(8)
                } else {
                    Text("PromptBridge henüz bu Mac'te kurulu değil.")
                        .foregroundColor(.secondary)
                    
                    Button("PromptBridge Klasörünü Seç ve Kur") {
                        serverManager.installServer()
                    }
                    .padding()
                    .background(Color.accentColor)
                    .foregroundColor(.white)
                    .cornerRadius(8)
                }
                
                if !serverManager.errorMessage.isEmpty {
                    Text(serverManager.errorMessage)
                        .foregroundColor(.red)
                        .font(.caption)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .onAppear {
                serverManager.checkStatus()
            }
        }
    }
}
