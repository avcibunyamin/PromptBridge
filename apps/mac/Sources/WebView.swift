import SwiftUI
import WebKit

struct WebView: NSViewRepresentable {
    let url: URL
    
    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        let contentController = WKUserContentController()
        
        // Add script handlers
        contentController.add(context.coordinator, name: "selectFolder")
        contentController.add(context.coordinator, name: "agentStatus")
        configuration.userContentController = contentController
        
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        
        // Load the local UI
        let request = URLRequest(url: url)
        webView.load(request)
        return webView
    }
    
    func updateNSView(_ nsView: WKWebView, context: Context) {
        // Handle updates if necessary
    }
    
    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }
    
    class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        var parent: WebView
        
        init(_ parent: WebView) {
            self.parent = parent
        }
        
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            let frame: NavigationFrame
            if let target = navigationAction.targetFrame {
                frame = target.isMainFrame ? .main(userActivated: navigationAction.navigationType == .linkActivated) : .sub
            } else {
                frame = .newWindow
            }
            switch navigationDecision(for: navigationAction.request.url, frame: frame) {
            case .allow:
                decisionHandler(.allow)
            case .openExternally:
                if let url = navigationAction.request.url {
                    NSWorkspace.shared.open(url)
                }
                decisionHandler(.cancel)
            case .cancel:
                decisionHandler(.cancel)
            }
        }

        /// `window.open` (the panel's "Tarayıcıda aç"): never a window inside the app.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if navigationDecision(for: navigationAction.request.url, frame: .newWindow) == .openExternally,
               let url = navigationAction.request.url {
                NSWorkspace.shared.open(url)
            }
            return nil
        }

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            let origin = message.frameInfo.securityOrigin
            let port = Int(origin.port)
            let allowed = isAllowedBridgeMessage(
                name: message.name,
                isMainFrame: message.frameInfo.isMainFrame,
                origin: WebOrigin(scheme: origin.protocol, host: origin.host, port: port)
            )
            guard allowed else {
                NSLog("PromptBridge ignored WebView message: \(message.name)")
                return
            }
            if message.name == "selectFolder" {
                let panel = NSOpenPanel()
                panel.canChooseFiles = false
                panel.canChooseDirectories = true
                panel.allowsMultipleSelection = false
                panel.message = "Projeyi seçin"
                
                // Keep panel modal to window
                if let window = NSApp.mainWindow {
                    panel.beginSheetModal(for: window) { response in
                        if response == .OK, let url = panel.url {
                            if let js = try? folderSelectedJavaScript(path: url.path) {
                                message.webView?.evaluateJavaScript(js, completionHandler: nil)
                            }
                        }
                    }
                }
            } else if message.name == "agentStatus" {
                let isRunning = launchdAgentIsLoaded()
                let js = "window.dispatchEvent(new CustomEvent('agentStatusReply', { detail: \(isRunning) }));"
                message.webView?.evaluateJavaScript(js, completionHandler: nil)
            }
        }
    }
}
