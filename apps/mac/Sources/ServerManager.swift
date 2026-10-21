import Foundation
import AppKit
import Combine

class ServerManager: ObservableObject {
    @Published var isServerRunning: Bool = false
    @Published var isInstalled: Bool = false
    @Published var errorMessage: String = ""
    
    private let bundleId = conductorLaunchdLabel
    private var timer: Timer?
    
    init() {
        checkStatus()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.checkStatus()
        }
    }
    
    func checkStatus() {
        // 1. Check if the server is reachable via HTTP
        let url = URL(string: "http://127.0.0.1:8765/health")!
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.0
        
        let task = URLSession.shared.dataTask(with: request) { data, response, error in
            let isRunning = (error == nil && (response as? HTTPURLResponse)?.statusCode == 200)
            
            DispatchQueue.main.async {
                if isRunning != self.isServerRunning {
                    self.isServerRunning = isRunning
                }
                if !isRunning {
                    self.checkLaunchdStatus()
                }
            }
        }
        task.resume()
    }
    
    private func checkLaunchdStatus() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = ["list", bundleId]
        
        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe() // Ignore stderr
        
        do {
            try task.run()
            task.waitUntilExit()
            self.isInstalled = (task.terminationStatus == 0)
        } catch {
            self.isInstalled = false
        }
    }
    
    func startServer() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        task.arguments = ["start", bundleId]
        
        do {
            try task.run()
            task.waitUntilExit()
            if task.terminationStatus != 0 {
                self.errorMessage = "Sunucu başlatılamadı (Hata: \(task.terminationStatus))"
            } else {
                self.errorMessage = ""
                // Wait a bit, then check status again
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                    self.checkStatus()
                }
            }
        } catch {
            self.errorMessage = "Sunucu başlatılırken hata oluştu: \(error.localizedDescription)"
        }
    }
    
    func installServer() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Lütfen PromptBridge proje ana klasörünü seçin."
        
        panel.begin { response in
            if response == .OK, let url = panel.url {
                switch validateInstallRoot(rootPath: url.path) {
                case .failure(let error):
                    self.errorMessage = "Kurulum doğrulanamadı: \(error.description)"
                case .success(let validation):
                    self.confirmAndInstall(validation: validation)
                }
            }
        }
    }

    private func confirmAndInstall(validation: InstallRootValidation) {
        let alert = NSAlert()
        alert.messageText = "PromptBridge ajanını kur"
        alert.informativeText = "Kök: \(validation.rootPath)\nBetik: \(validation.scriptPath)\n\nBu işlem launchd ajanını kurar."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Kur")
        alert.addButton(withTitle: "Vazgeç")
        let response = alert.runModal()
        guard response == .alertFirstButtonReturn else { return }
        runInstallScript(rootPath: validation.rootPath)
    }

    private func runInstallScript(rootPath: String) {
        let validation: InstallRootValidation
        switch validateInstallRoot(rootPath: rootPath) {
        case .failure(let error):
            self.errorMessage = "Kurulum doğrulanamadı: \(error.description)"
            return
        case .success(let value):
            validation = value
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: validation.scriptPath)
        task.arguments = ["install"]
        task.currentDirectoryURL = URL(fileURLWithPath: validation.workingDirectory)

        do {
            try task.run()
            task.waitUntilExit()
            if task.terminationStatus == 0 {
                UserDefaults.standard.set(validation.rootPath, forKey: "PromptBridgeRoot")
                self.errorMessage = ""
                self.isInstalled = true
                self.checkStatus()
            } else {
                self.errorMessage = "Kurulum başarısız oldu (Hata: \(task.terminationStatus))"
            }
        } catch {
            self.errorMessage = "Kurulum çalıştırılamadı: \(error.localizedDescription)"
        }
    }
}
