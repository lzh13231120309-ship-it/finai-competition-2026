// 人工智能金融大赛本地 Agent —— macOS 原生外壳
//
// 纯 AppKit + WKWebView 实现，不依赖 SwiftUI、不打包 Chromium：
//   · 启动时拉起内置的 Python 后端（venv 随应用分发）
//   · 后端就绪后加载界面
//   · 关闭窗口即退出，并回收后端进程
//
// 内存占用来自系统共享的 WebKit 框架，不额外常驻进程。

import Cocoa
import Darwin
import WebKit

// MARK: - 常量

private let kAppTitle = "衡知"
private let kSupportDirName = "FinAgent"
private let kHealthTimeout: Double = 75
private let kPortCandidates = [17899, 17900, 17901, 17902, 17903, 17904, 17905]

// MARK: - 底层工具

/// 在回环地址上挑一个可用端口：优先用候选列表里的固定端口（保持界面状态稳定），
/// 全被占用时退回让系统随机分配。
private func pickFreePort() -> Int {
    for port in kPortCandidates {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        if fd < 0 { continue }
        defer { close(fd) }
        var on: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &on, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let rc = withUnsafePointer(to: &addr) { ptr -> Int32 in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                bind(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        if rc == 0 { return port }
    }
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = 0
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    _ = withUnsafePointer(to: &addr) { ptr in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            bind(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    var len = socklen_t(MemoryLayout<sockaddr_in>.size)
    _ = withUnsafeMutablePointer(to: &addr) { ptr in
        ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            getsockname(fd, sa, &len)
        }
    }
    return Int(UInt16(bigEndian: addr.sin_port))
}

private func join(_ base: String, _ parts: String...) -> String {
    var path = base
    for p in parts { path = (path as NSString).appendingPathComponent(p) }
    return path
}

// MARK: - 启动/错误状态覆盖层

final class StatusView: NSView {

    private let spinner = NSProgressIndicator()
    private let titleLabel = NSTextField(labelWithString: "")
    private let detailLabel = NSTextField(labelWithString: "")
    private let logLabel = NSTextField(labelWithString: "")
    private let retryButton = NSButton(title: "重试", target: nil, action: nil)
    private let openLogButton = NSButton(title: "打开日志", target: nil, action: nil)
    private var buttonRow: NSStackView!

    var onRetry: (() -> Void)?
    private var currentLogPath: String?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        build()
    }

    required init?(coder: NSCoder) { fatalError("not supported") }

    private func build() {
        spinner.style = .spinning
        spinner.controlSize = .large
        spinner.isDisplayedWhenStopped = false

        titleLabel.font = NSFont.systemFont(ofSize: 19, weight: .semibold)
        titleLabel.alignment = .center
        titleLabel.maximumNumberOfLines = 0
        titleLabel.lineBreakMode = .byWordWrapping

        detailLabel.font = NSFont.systemFont(ofSize: 13)
        detailLabel.textColor = .secondaryLabelColor
        detailLabel.alignment = .center
        detailLabel.maximumNumberOfLines = 0
        detailLabel.lineBreakMode = .byWordWrapping

        logLabel.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        logLabel.textColor = .tertiaryLabelColor
        logLabel.alignment = .left
        logLabel.maximumNumberOfLines = 10
        logLabel.lineBreakMode = .byTruncatingHead
        logLabel.isHidden = true
        logLabel.wantsLayer = true

        retryButton.bezelStyle = .rounded
        retryButton.target = self
        retryButton.action = #selector(didTapRetry)
        openLogButton.bezelStyle = .rounded
        openLogButton.target = self
        openLogButton.action = #selector(didTapOpenLog)

        buttonRow = NSStackView(views: [retryButton, openLogButton])
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 10
        buttonRow.isHidden = true

        for v in [spinner, titleLabel, detailLabel, logLabel, buttonRow!] as [NSView] {
            v.translatesAutoresizingMaskIntoConstraints = false
        }

        let stack = NSStackView(views: [spinner, titleLabel, detailLabel, logLabel, buttonRow])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)

        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -24),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: leadingAnchor, constant: 40),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: trailingAnchor, constant: -40),
            logLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 620),
            detailLabel.widthAnchor.constraint(lessThanOrEqualToConstant: 560),
        ])
    }

    func showLoading(title: String, detail: String) {
        spinner.isHidden = false
        spinner.startAnimation(nil)
        titleLabel.stringValue = title
        detailLabel.stringValue = detail
        logLabel.isHidden = true
        logLabel.stringValue = ""
        buttonRow.isHidden = true
        currentLogPath = nil
    }

    func showError(title: String, detail: String, logPath: String?, tail: String? = nil) {
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        titleLabel.stringValue = title
        detailLabel.stringValue = detail
        currentLogPath = logPath
        if let t = tail, !t.isEmpty {
            logLabel.stringValue = t
            logLabel.isHidden = false
        } else {
            logLabel.isHidden = true
        }
        retryButton.isHidden = false
        openLogButton.isHidden = (logPath == nil)
        buttonRow.isHidden = false
    }

    @objc private func didTapRetry() { onRetry?() }

    @objc private func didTapOpenLog() {
        guard let p = currentLogPath else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: p))
    }
}

// MARK: - 应用委托

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
                         WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {

    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusView: StatusView!

    private var serverProcess: Process?
    private var logHandle: FileHandle?
    private var logPath: String?
    private var port: Int = 0
    private var bootStartedAt = Date()
    private var isShuttingDown = false
    private var isReady = false

    // MARK: 生命周期


    // MARK: 窗口缩放 —— 强制同步 WKWebView 尺寸并通知页面
    // macOS 26 上 WKWebView 偶发不随窗口缩小重排，页面比窗口宽导致内容被裁、
    // 顶栏按钮被侧栏盖住。这里每次缩放都显式对齐 frame 并给页面发 resize 事件。
    func windowDidResize(_ notification: Notification) {
        guard let wv = webView, let sup = wv.superview else { return }
        if wv.frame.size != sup.bounds.size {
            wv.frame = sup.bounds
        }
        wv.evaluateJavaScript("window.dispatchEvent(new Event('resize'))", completionHandler: nil)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        boot()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        shutdownServer()
    }

    // MARK: 菜单

    private func buildMenu() {
        let mainMenu = NSMenu()

        // 应用菜单
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于 \(kAppTitle)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 \(kAppTitle)",
                        action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他",
                                         action: #selector(NSApplication.hideOtherApplications(_:)),
                                         keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "全部显示",
                        action: #selector(NSApplication.unhideAllApplications(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 \(kAppTitle)",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        // 编辑菜单 —— 网页里的输入框要靠它才能用 ⌘C/⌘V
        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editItem.submenu = editMenu
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        // 窗口菜单
        let winItem = NSMenuItem()
        mainMenu.addItem(winItem)
        let winMenu = NSMenu(title: "窗口")
        winItem.submenu = winMenu
        winMenu.addItem(withTitle: "最小化",
                        action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "缩放",
                        action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winMenu.addItem(.separator())
        winMenu.addItem(withTitle: "重新载入界面",
                        action: #selector(reloadUI), keyEquivalent: "r")
        NSApp.windowsMenu = winMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func reloadUI() {
        guard isReady else { return }
        webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
    }

    // MARK: 窗口

    private func buildWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1200, height: 800)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered,
                          defer: false)
        window.title = kAppTitle
        window.minSize = NSSize(width: 920, height: 620)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()

        let container = NSView(frame: rect)
        container.autoresizingMask = [.width, .height]

        statusView = StatusView(frame: rect)
        statusView.autoresizingMask = [.width, .height]
        statusView.onRetry = { [weak self] in self?.restart() }
        container.addSubview(statusView)

        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        cfg.defaultWebpagePreferences.allowsContentJavaScript = true

        webView = WKWebView(frame: rect, configuration: cfg)
        webView.autoresizingMask = [.width, .height]
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isHidden = true
        container.addSubview(webView)

        window.contentView = container
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }


    // 必杀兜底：缩放结束后若页面视口与真实宽度不一致（macOS 26 WKWebView 偶发不重排），
    // 直接刷新页面。任务数据都在本地服务端，刷新零损失。
    func windowDidEndLiveResize(_ notification: Notification) {
        guard let wv = webView, let sup = wv.superview else { return }
        wv.frame = sup.bounds
        wv.evaluateJavaScript("window.innerWidth") { inner, _ in
            guard let inner = inner as? Double else { return }
            if abs(inner - wv.frame.width) > 2 {
                wv.reload()
            }
        }
    }

    // MARK: 启动流程

    private func boot() {
        bootStartedAt = Date()
        isReady = false
        statusView.showLoading(title: "正在启动本地服务…",
                               detail: "首次启动需要初始化运行环境，通常几秒钟")

        guard let resourcePath = Bundle.main.resourcePath else {
            statusView.showError(title: "应用资源缺失",
                                 detail: "找不到应用资源目录，请重新安装。", logPath: nil)
            return
        }
        let runtimeDir = join(resourcePath, "runtime")
        let pythonPath = join(runtimeDir, "venv", "bin", "python")

        guard FileManager.default.isExecutableFile(atPath: pythonPath) else {
            statusView.showError(title: "运行环境缺失",
                                 detail: "未找到内置的 Python 运行环境：\n\(pythonPath)",
                                 logPath: nil)
            return
        }

        let dataDir = prepareDataDir(runtimeDir: runtimeDir)
        port = pickFreePort()
        launchServer(runtimeDir: runtimeDir, pythonPath: pythonPath, dataDir: dataDir)
    }

    /// 准备用户数据目录（与代码分离，放在「应用支持」下，卸载应用不影响数据）
    private func prepareDataDir(runtimeDir: String) -> String {
        let fm = FileManager.default
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let dataDir = base.appendingPathComponent(kSupportDirName, isDirectory: true)

        for sub in ["uploads", "knowledge", "logs", "skills"] {
            try? fm.createDirectory(at: dataDir.appendingPathComponent(sub, isDirectory: true),
                                    withIntermediateDirectories: true)
        }

        // 首次运行：把内置示例技能播种到用户数据目录
        let seedSkills = join(runtimeDir, "seed", "skills")
        let destSkills = dataDir.appendingPathComponent("skills", isDirectory: true)
        if fm.fileExists(atPath: seedSkills) {
            let existing = (try? fm.contentsOfDirectory(atPath: destSkills.path)) ?? []
            if existing.isEmpty, let items = try? fm.contentsOfDirectory(atPath: seedSkills) {
                for item in items {
                    try? fm.copyItem(atPath: join(seedSkills, item),
                                     toPath: destSkills.appendingPathComponent(item).path)
                }
            }
        }
        return dataDir.path
    }

    private func launchServer(runtimeDir: String, pythonPath: String, dataDir: String) {
        let lp = join(dataDir, "logs", "server.log")
        logPath = lp
        FileManager.default.createFile(atPath: lp, contents: nil)
        logHandle = FileHandle(forWritingAtPath: lp)
        logHandle?.seekToEndOfFile()

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.arguments = ["-m", "server.launcher",
                          "--host", "127.0.0.1",
                          "--port", String(port),
                          "--log-level", "warning"]
        // The writable data directory stays valid when the app is relocated.
        // Python modules still load from the explicit runtime PYTHONPATH below.
        proc.currentDirectoryURL = URL(fileURLWithPath: dataDir)

        var env = ProcessInfo.processInfo.environment
        env["FINAGENT_DATA_DIR"] = dataDir
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = runtimeDir
        let venvBin = join(runtimeDir, "venv", "bin")
        env["PATH"] = "\(venvBin):\(env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")"
        proc.environment = env

        if let fh = logHandle {
            proc.standardOutput = fh
            proc.standardError = fh
            proc.standardInput = FileHandle.nullDevice
        }

        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self = self, !self.isShuttingDown else { return }
                self.statusView.showError(
                    title: "本地服务已停止",
                    detail: "后端进程意外结束（状态码 \(p.terminationStatus)）。可点击重试重新启动。",
                    logPath: self.logPath,
                    tail: self.tailLog())
            }
        }

        do {
            try proc.run()
            serverProcess = proc
            pollHealth(attempt: 0)
        } catch {
            statusView.showError(title: "无法启动本地服务",
                                 detail: error.localizedDescription,
                                 logPath: lp,
                                 tail: tailLog())
        }
    }

    private func pollHealth(attempt: Int) {
        if Date().timeIntervalSince(bootStartedAt) > kHealthTimeout {
            statusView.showError(title: "启动超时",
                                 detail: "后端在 \(Int(kHealthTimeout)) 秒内没有就绪。以下是最新日志，可据此排查。",
                                 logPath: logPath,
                                 tail: tailLog())
            return
        }
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        req.cachePolicy = .reloadIgnoringLocalCacheData

        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                guard let self = self else { return }
                if ok {
                    self.isReady = true
                    self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                        self.pollHealth(attempt: attempt + 1)
                    }
                }
            }
        }.resume()
    }

    private func restart() {
        shutdownServer()
        isShuttingDown = false
        boot()
    }

    private func shutdownServer() {
        isShuttingDown = true
        guard let p = serverProcess, p.isRunning else {
            serverProcess = nil
            try? logHandle?.close()
            logHandle = nil
            return
        }
        p.terminationHandler = nil
        p.terminate()                       // 先礼：SIGTERM，让 uvicorn 优雅收尾
        let deadline = Date().addingTimeInterval(4)
        while p.isRunning && Date() < deadline {
            usleep(80_000)
        }
        if p.isRunning {
            kill(p.processIdentifier, SIGKILL)   // 后兵
        }
        serverProcess = nil
        try? logHandle?.close()
        logHandle = nil
    }

    private func tailLog(_ lines: Int = 10) -> String {
        guard let lp = logPath,
              let text = try? String(contentsOfFile: lp, encoding: .utf8),
              !text.isEmpty else { return "" }
        let all = text.split(separator: "\n", omittingEmptySubsequences: false)
        let tail = all.suffix(lines).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        return tail.isEmpty ? "" : "最近日志：\n" + tail.joined(separator: "\n")
    }

    // MARK: WKUIDelegate —— JS 的 alert / confirm / prompt
    // 不实现这些方法时 WKWebView 会静默忽略 confirm()（返回 false），
    // 导致界面上所有删除确认按钮看似"点了没反应"。

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "提示"
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "请确认"
        alert.informativeText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert()
        alert.messageText = prompt
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 240, height: 24))
        input.stringValue = defaultText ?? ""
        alert.accessoryView = input
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        alert.runModal()
        completionHandler(input.stringValue)
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isReady else { return }
        statusView.isHidden = true
        webView.isHidden = false
        window.makeFirstResponder(webView)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        guard isReady else { return }
        statusView.showError(title: "界面加载失败",
                             detail: error.localizedDescription,
                             logPath: logPath, tail: tailLog())
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        guard isReady else { return }
        statusView.showError(title: "界面加载失败",
                             detail: error.localizedDescription,
                             logPath: logPath, tail: tailLog())
    }

    // Downloads remain downloads: never navigate the conversation to a workbook.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let url = navigationAction.request.url
        let artifact = url?.host == "127.0.0.1" && url?.port == port && (url?.path.hasPrefix("/api/financial/artifacts/") ?? false)
        decisionHandler(navigationAction.shouldPerformDownload || artifact ? .download : .allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let attachment = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition")?.lowercased().contains("attachment") ?? false
        decisionHandler(attachment || !navigationResponse.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.title = "保存衡知报告"
        panel.nameFieldStringValue = ((suggestedFilename.removingPercentEncoding ?? suggestedFilename) as NSString).lastPathComponent
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.url : nil) }
    }

    func downloadDidFinish(_ download: WKDownload) {
        webView.evaluateJavaScript("toast('文件已保存，可在所选文件夹打开', 'ok')", completionHandler: nil)
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        if (error as NSError).code == NSURLErrorCancelled { return }
        let alert = NSAlert(); alert.messageText = "报告下载未完成"; alert.informativeText = error.localizedDescription
        alert.beginSheetModal(for: window, completionHandler: nil)
    }

    // MARK: WKUIDelegate

    /// 让 target="_blank" 之类的新窗口请求：本机地址就地打开，外部链接交给浏览器
    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url {
            if url.host == "127.0.0.1" || url.host == "localhost" {
                if url.path.hasPrefix("/api/financial/artifacts/") { webView.load(navigationAction.request) }
                else { NSWorkspace.shared.open(url) }
            } else {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }
}

// MARK: - 入口

let application = NSApplication.shared
let appDelegate = AppDelegate()
application.delegate = appDelegate
application.setActivationPolicy(.regular)
application.run()
