// A native one-window host for the anki-assistant web UI.
//
// A browser tab — even a chromeless Chrome `--app` window — belongs to the browser:
// the Dock shows the browser's icon and the browser's running dot. Hosting the page in
// a WKWebView inside an ordinary .app bundle is what gives it an identity of its own.
//
// The app owns nothing but the window. Starting Anki and the server is still
// `scripts/launch.sh serve`, whose path `scripts/install_app.sh` stamps into Info.plist
// so edits to the shell script keep taking effect without a rebuild.
//
// Built by scripts/install_app.sh; not meant to be run loose.

import Cocoa
import WebKit

let appURL = URL(string: "http://127.0.0.1:5070/")!

/// Path to scripts/launch.sh, stamped into Info.plist at install time.
let launcher = Bundle.main.object(forInfoDictionaryKey: "AnkiAssistantLauncher") as? String ?? ""

final class Controller: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private let status = NSTextField(labelWithString: "Démarrage…")

    // MARK: launch

    func applicationDidFinishLaunching(_: Notification) {
        buildMenu()
        buildWindow()
        startServer { [self] failure in
            if let failure {
                // launch.sh raises its own alert; this only keeps the window from lying.
                status.stringValue = "Le serveur n'a pas démarré.\n\(failure)"
            } else {
                status.isHidden = true
                web.isHidden = false
                web.load(URLRequest(url: appURL))
            }
        }
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Anki Assistant"
        window.center()
        window.setFrameAutosaveName("AnkiAssistantWindow")  // survives a quit

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()  // localStorage keeps the chosen theme
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isInspectable = true  // right-click > Inspect Element, as in a browser
        web.isHidden = true  // until the server answers, so no error page flashes

        let root = NSView()
        root.addSubview(web)
        root.addSubview(status)
        web.translatesAutoresizingMaskIntoConstraints = false
        status.translatesAutoresizingMaskIntoConstraints = false
        status.alignment = .center
        status.maximumNumberOfLines = 0
        NSLayoutConstraint.activate([
            web.topAnchor.constraint(equalTo: root.topAnchor),
            web.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            web.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            web.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            status.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            status.centerYAnchor.constraint(equalTo: root.centerYAnchor),
        ])
        window.contentView = root
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Run `launch.sh serve` off the main thread; hand back stderr when it fails.
    private func startServer(_ done: @escaping (String?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/bin/bash")
            task.arguments = [launcher, "serve"]
            let errors = Pipe()
            task.standardOutput = FileHandle.nullDevice
            task.standardError = errors
            do {
                try task.run()
            } catch {
                return DispatchQueue.main.async { done("\(error)") }
            }
            let text = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)
            task.waitUntilExit()
            let failure = task.terminationStatus == 0
                ? nil
                : (text?.isEmpty == false ? text : "code \(task.terminationStatus)")
            DispatchQueue.main.async { done(failure) }
        }
    }

    // MARK: navigation

    /// Keep the window on the local server. Everything else — the `obsidian://` and
    /// `file://` source links the review UI emits — belongs to whichever app owns it.
    func webView(
        _: WKWebView,
        decidePolicyFor action: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if url.scheme == "about" || (url.scheme == "http" && (url.host == "127.0.0.1" || url.host == "localhost")) {
            decisionHandler(.allow)
        } else {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        }
    }

    /// `target="_blank"` asks for a second window; hand the URL to the system instead.
    func webView(
        _: WKWebView,
        createWebViewWith _: WKWebViewConfiguration,
        for action: WKNavigationAction,
        windowFeatures _: WKWindowFeatures
    ) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_: WKWebView, didFail _: WKNavigation!, withError error: Error) {
        status.stringValue = "\(error.localizedDescription)"
        status.isHidden = false
    }

    // MARK: lifecycle

    func applicationShouldTerminateAfterLastWindowClosed(_: NSApplication) -> Bool { true }

    @objc func reload() { web.reload() }

    // MARK: menu

    /// A bundle gets no menus for free, and without an Edit menu ⌘C/⌘V are dead in the
    /// chat box — AppKit routes those shortcuts through menu items, not the web view.
    private func buildMenu() {
        let name = "Anki Assistant"
        let bar = NSMenu()

        let app = NSMenu()
        app.addItem(withTitle: "À propos de \(name)", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Masquer \(name)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(withTitle: "Quitter \(name)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let edit = NSMenu(title: "Édition")
        edit.addItem(withTitle: "Annuler", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Rétablir", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Couper", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copier", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Coller", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Tout sélectionner", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let view = NSMenu(title: "Présentation")
        view.addItem(withTitle: "Recharger", action: #selector(reload), keyEquivalent: "r")
        view.addItem(withTitle: "Plein écran", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
            .keyEquivalentModifierMask = [.command, .control]

        for menu in [app, edit, view] {
            let item = NSMenuItem()
            item.submenu = menu
            bar.addItem(item)
        }
        NSApp.mainMenu = bar
    }
}

let controller = Controller()
let app = NSApplication.shared
app.setActivationPolicy(.regular)  // a Dock icon and a running dot
app.delegate = controller
app.run()
