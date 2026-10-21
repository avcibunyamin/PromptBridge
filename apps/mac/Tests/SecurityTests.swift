import Foundation

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

func expectEqual<T: Equatable>(_ actual: T, _ expected: T, _ message: String) {
    if actual != expected {
        fputs("FAIL: \(message): got \(actual), expected \(expected)\n", stderr)
        exit(1)
    }
}

func testNavigation() {
    let click = NavigationFrame.main(userActivated: true), redirect = NavigationFrame.main(userActivated: false)
    // main frame: only the panel; the browser opens only for a clicked link
    expectEqual(navigationDecision(for: URL(string: "http://127.0.0.1:8765/ui/"), frame: redirect), .allow, "conductor origin allowed")
    expectEqual(navigationDecision(for: URL(string: "http://127.0.0.1:8766/ui/"), frame: click), .openExternally, "clicked other port opens externally")
    expectEqual(navigationDecision(for: URL(string: "http://localhost:8765/ui/"), frame: click), .openExternally, "localhost is not trusted")
    expectEqual(navigationDecision(for: URL(string: "https://127.0.0.1:8765/ui/"), frame: click), .openExternally, "https origin is not app origin")
    expectEqual(navigationDecision(for: URL(string: "https://example.com/"), frame: redirect), .cancel, "unrequested redirect never opens the browser")
    expectEqual(navigationDecision(for: URL(string: "file:///tmp/x.html"), frame: click), .cancel, "file URLs cancelled")
    expectEqual(navigationDecision(for: URL(string: "about:blank"), frame: redirect), .allow, "about blank allowed")
    // sub frame (preview iframe): a local dev server port only, never the browser
    expectEqual(navigationDecision(for: URL(string: "http://127.0.0.1:5173/"), frame: .sub), .allow, "preview port allowed in iframe")
    expectEqual(navigationDecision(for: URL(string: "http://127.0.0.1:8765/ui/"), frame: .sub), .cancel, "panel never framed")
    expectEqual(navigationDecision(for: URL(string: "http://localhost:5173/"), frame: .sub), .cancel, "localhost iframe cancelled")
    expectEqual(navigationDecision(for: URL(string: "https://127.0.0.1:5173/"), frame: .sub), .cancel, "https iframe cancelled")
    expectEqual(navigationDecision(for: URL(string: "http://127.0.0.1/"), frame: .sub), .cancel, "iframe without explicit port cancelled")
    expectEqual(navigationDecision(for: URL(string: "https://example.com/"), frame: .sub), .cancel, "external iframe cancelled, no browser")
    expectEqual(navigationDecision(for: URL(string: "about:blank"), frame: .sub), .allow, "blank iframe allowed")
    // new window (target=_blank / window.open): the browser, never an in-app window
    expectEqual(navigationDecision(for: URL(string: "http://127.0.0.1:5173/"), frame: .newWindow), .openExternally, "preview opens in browser")
    expectEqual(navigationDecision(for: URL(string: "https://example.com/"), frame: .newWindow), .openExternally, "external new window opens in browser")
    expectEqual(navigationDecision(for: URL(string: "file:///tmp/x.html"), frame: .newWindow), .cancel, "file new window cancelled")
    expectEqual(navigationDecision(for: URL(string: "about:blank"), frame: .newWindow), .cancel, "blank new window cancelled")
    expectEqual(navigationDecision(for: nil, frame: .sub), .cancel, "missing URL cancelled")
}

func testBridgeOrigin() {
    let ok = WebOrigin(scheme: "http", host: "127.0.0.1", port: 8765)
    let badPort = WebOrigin(scheme: "http", host: "127.0.0.1", port: 8766)
    let badHost = WebOrigin(scheme: "http", host: "localhost", port: 8765)
    let badScheme = WebOrigin(scheme: "https", host: "127.0.0.1", port: 8765)
    expect(isAllowedBridgeMessage(name: "selectFolder", isMainFrame: true, origin: ok), "valid selectFolder message")
    expect(isAllowedBridgeMessage(name: "agentStatus", isMainFrame: true, origin: ok), "valid agentStatus message")
    expect(!isAllowedBridgeMessage(name: "selectFolder", isMainFrame: false, origin: ok), "iframe rejected")
    expect(!isAllowedBridgeMessage(name: "selectFolder", isMainFrame: true, origin: badPort), "bad port rejected")
    expect(!isAllowedBridgeMessage(name: "selectFolder", isMainFrame: true, origin: badHost), "localhost rejected")
    expect(!isAllowedBridgeMessage(name: "selectFolder", isMainFrame: true, origin: badScheme), "bad scheme rejected")
    expect(!isAllowedBridgeMessage(name: "unknown", isMainFrame: true, origin: ok), "unknown message rejected")
}

func testPathJavaScriptEncoding() throws {
    let path = "/Users/test/O\"Connor\nline\\next\u{2028}x"
    let js = try folderSelectedJavaScript(path: path)
    expect(js.contains("\\\"Connor"), "quote escaped")
    expect(js.contains("\\nline"), "newline escaped")
    expect(js.contains("\\\\next"), "backslash escaped")
    expect(js.contains("\\u2028x"), "unicode line separator escaped")
    expect(!js.contains("detail: \"\(path)\""), "raw path not interpolated")
}

func testInstallRootValidationRejectsFakeRoot() {
    let fake = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    try? FileManager.default.createDirectory(at: fake, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: fake) }
    switch validateInstallRoot(rootPath: fake.path) {
    case .failure(.missingScript):
        break
    default:
        fputs("FAIL: fake root should be rejected before execution\n", stderr)
        exit(1)
    }
}

func testInstallRootValidationAcceptsTemporaryRepo() {
    let root = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent(UUID().uuidString)
    let orchestrator = root.appendingPathComponent("ai-orchestrator")
    let deploy = orchestrator.appendingPathComponent("deploy")
    let app = orchestrator.appendingPathComponent("app")
    try? FileManager.default.createDirectory(at: deploy, withIntermediateDirectories: true)
    try? FileManager.default.createDirectory(at: app, withIntermediateDirectories: true)
    FileManager.default.createFile(atPath: app.appendingPathComponent("__init__.py").path, contents: Data())
    let script = deploy.appendingPathComponent("launchd.sh")
    FileManager.default.createFile(atPath: script.path, contents: Data("#!/bin/sh\nexit 0\n".utf8))
    try? FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: script.path)
    _ = run("/usr/bin/git", ["-C", root.path, "init"])
    defer { try? FileManager.default.removeItem(at: root) }

    switch validateInstallRoot(rootPath: root.path) {
    case .success(let value):
        expectEqual(value.scriptPath, script.path, "script path")
        expectEqual(value.workingDirectory, orchestrator.path, "working directory")
    case .failure(let error):
        fputs("FAIL: temporary repo should be accepted: \(error)\n", stderr)
        exit(1)
    }

    try? FileManager.default.setAttributes([.posixPermissions: 0o757], ofItemAtPath: script.path)
    switch validateInstallRoot(rootPath: root.path) {
    case .failure(.scriptWorldWritable):
        break
    default:
        fputs("FAIL: world-writable script should be rejected immediately\n", stderr)
        exit(1)
    }
}

@discardableResult
func run(_ executable: String, _ arguments: [String]) -> Int32 {
    let task = Process()
    task.executableURL = URL(fileURLWithPath: executable)
    task.arguments = arguments
    task.standardOutput = Pipe()
    task.standardError = Pipe()
    do {
        try task.run()
        task.waitUntilExit()
        return task.terminationStatus
    } catch {
        return -1
    }
}

@main
struct TestRunner {
    static func main() {
        do {
            testNavigation()
            testBridgeOrigin()
            try testPathJavaScriptEncoding()
            testInstallRootValidationRejectsFakeRoot()
            testInstallRootValidationAcceptsTemporaryRepo()
            print("mac security tests ok")
        } catch {
            fputs("FAIL: \(error)\n", stderr)
            exit(1)
        }
    }
}
