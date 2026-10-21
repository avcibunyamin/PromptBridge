import Foundation
import Darwin

let conductorScheme = "http"
let conductorHost = "127.0.0.1"
let conductorPort = 8765
let conductorOrigin = "\(conductorScheme)://\(conductorHost):\(conductorPort)"
let conductorLaunchdLabel = "com.promptbridge.conductor"

struct WebOrigin {
    let scheme: String
    let host: String
    let port: Int
}

enum NavigationDecision: Equatable {
    case allow
    case openExternally
    case cancel
}

func isConductorOrigin(scheme: String?, host: String?, port: Int?) -> Bool {
    guard let scheme, let host, let port else { return false }
    return scheme == conductorScheme && host == conductorHost && port == conductorPort
}

func isAllowedBridgeMessage(name: String, isMainFrame: Bool, origin: WebOrigin) -> Bool {
    guard isMainFrame else { return false }
    guard name == "selectFolder" || name == "agentStatus" else { return false }
    return isConductorOrigin(scheme: origin.scheme, host: origin.host, port: origin.port)
}

/// Where a navigation happens (TB-13a). A sub frame is the project preview iframe; a new window is
/// `target=_blank` or `window.open`.
enum NavigationFrame: Equatable {
    case main(userActivated: Bool)
    case sub
    case newWindow
}

/// TB-11a/TB-13a: the app shows only the Conductor panel; its preview iframe may show a local dev server on
/// another 127.0.0.1 port (it cannot reach the bridge: main frame + 8765 only). The default browser opens
/// only for a link the user clicked or a new window, never from a sub frame or an unrequested redirect.
func navigationDecision(for url: URL?, frame: NavigationFrame = .main(userActivated: true)) -> NavigationDecision {
    guard let url else { return .cancel }
    if url.scheme == "about" && url.absoluteString == "about:blank" {
        return frame == .newWindow ? .cancel : .allow
    }
    let web = url.scheme == "http" || url.scheme == "https"
    switch frame {
    case .main(let userActivated):
        if isConductorOrigin(scheme: url.scheme, host: url.host, port: url.port) { return .allow }
        return web && userActivated ? .openExternally : .cancel
    case .sub:
        return isPreviewOrigin(scheme: url.scheme, host: url.host, port: url.port) ? .allow : .cancel
    case .newWindow:
        return web ? .openExternally : .cancel
    }
}

/// A local dev server: http on 127.0.0.1 with an explicit port other than Conductor's.
func isPreviewOrigin(scheme: String?, host: String?, port: Int?) -> Bool {
    guard let port else { return false }
    return scheme == conductorScheme && host == conductorHost && port != conductorPort && (1...65535).contains(port)
}

func folderSelectedJavaScript(path: String) throws -> String {
    let data = try JSONSerialization.data(withJSONObject: path, options: [.fragmentsAllowed])
    guard var encoded = String(data: data, encoding: .utf8) else {
        throw SecurityError.invalidPathEncoding
    }
    encoded = encoded
        .replacingOccurrences(of: "\u{2028}", with: "\\u2028")
        .replacingOccurrences(of: "\u{2029}", with: "\\u2029")
    return "window.dispatchEvent(new CustomEvent('folderSelected', { detail: \(encoded) }));"
}

enum SecurityError: Error, Equatable {
    case invalidPathEncoding
}

enum InstallValidationError: Error, Equatable, CustomStringConvertible {
    case missingScript
    case missingAppPackage
    case gitRootMismatch(String)
    case gitUnavailable
    case scriptNotOwnedByUser
    case scriptWorldWritable
    case scriptNotExecutable
    case invalidAttributes

    var description: String {
        switch self {
        case .missingScript: return "launchd.sh bulunamadı."
        case .missingAppPackage: return "PromptBridge uygulama paketi doğrulanamadı."
        case .gitRootMismatch(let root): return "Git kökü seçilen klasörle eşleşmiyor: \(root)"
        case .gitUnavailable: return "Git kökü doğrulanamadı."
        case .scriptNotOwnedByUser: return "launchd.sh bu kullanıcıya ait değil."
        case .scriptWorldWritable: return "launchd.sh dünya-yazılabilir."
        case .scriptNotExecutable: return "launchd.sh çalıştırılabilir değil."
        case .invalidAttributes: return "launchd.sh izinleri okunamadı."
        }
    }
}

struct InstallRootValidation {
    let rootPath: String
    let scriptPath: String
    let workingDirectory: String
}

func validateInstallRoot(rootPath: String) -> Result<InstallRootValidation, InstallValidationError> {
    let rootURL = URL(fileURLWithPath: rootPath).standardizedFileURL
    let root = rootURL.path
    let orchestrator = rootURL.appendingPathComponent("ai-orchestrator")
    let script = orchestrator.appendingPathComponent("deploy/launchd.sh")
    let appPackage = orchestrator.appendingPathComponent("app/__init__.py")

    guard FileManager.default.fileExists(atPath: script.path) else {
        return .failure(.missingScript)
    }
    guard FileManager.default.fileExists(atPath: appPackage.path) else {
        return .failure(.missingAppPackage)
    }
    guard verifyGitRoot(root: root) else {
        return .failure(.gitUnavailable)
    }
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: script.path),
          let owner = attrs[.ownerAccountID] as? NSNumber,
          let mode = attrs[.posixPermissions] as? NSNumber else {
        return .failure(.invalidAttributes)
    }
    guard owner.uint32Value == getuid() else {
        return .failure(.scriptNotOwnedByUser)
    }
    guard (mode.intValue & 0o002) == 0 else {
        return .failure(.scriptWorldWritable)
    }
    guard FileManager.default.isExecutableFile(atPath: script.path) else {
        return .failure(.scriptNotExecutable)
    }
    return .success(InstallRootValidation(rootPath: root, scriptPath: script.path, workingDirectory: orchestrator.path))
}

private func verifyGitRoot(root: String) -> Bool {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/git")
    task.arguments = ["-C", root, "rev-parse", "--show-toplevel"]
    let pipe = Pipe()
    task.standardOutput = pipe
    task.standardError = Pipe()
    do {
        try task.run()
        task.waitUntilExit()
    } catch {
        return false
    }
    guard task.terminationStatus == 0 else { return false }
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    guard let output = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) else {
        return false
    }
    return URL(fileURLWithPath: output).standardizedFileURL.path == URL(fileURLWithPath: root).standardizedFileURL.path
}

func launchdAgentIsLoaded(label: String = conductorLaunchdLabel) -> Bool {
    let uid = getuid()
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/bin/launchctl")
    task.arguments = ["print", "gui/\(uid)/\(label)"]
    task.standardOutput = Pipe()
    task.standardError = Pipe()
    do {
        try task.run()
        task.waitUntilExit()
        return task.terminationStatus == 0
    } catch {
        return false
    }
}
