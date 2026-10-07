//  衡知 · macOS 桌面应用
//  ---------------------------------------------------------------
//  作用：把「衡知」金融投研 Agent 的本地 Web 服务包成一个原生窗口，
//        双击 .app 即可使用，不需要打开终端，也不需要手动执行脚本。
//
//  它本身**不含任何金融逻辑**，所有能力仍由仓库里的 Python 代码提供：
//      启动时  → 以子进程方式运行 金融大赛_集成试用版/desktop_service.py
//      就绪后  → 用 WKWebView 加载 http://127.0.0.1:17904/
//      退出时  → 给子进程发 SIGTERM，由它清理 uvicorn 与本地推理服务
//
//  这样 Windows 版脚本、命令行版启动脚本都完全不受影响。
//  ---------------------------------------------------------------

import Cocoa
import WebKit

// MARK: - 路径与常量

enum HZ {
    static let port = 17904
    static let baseURL = URL(string: "http://127.0.0.1:\(port)")!
    static var healthURL: URL { baseURL.appendingPathComponent("finance/api/health") }

    /// 仓库根目录。构建时由 build_app.sh 写入 Info.plist 的 HZRepoRoot。
    /// 之所以写死而不是靠相对路径推断，是因为用户可能把 .app 拖到任意位置
    /// （比如 /Applications），而 Python 环境仍在仓库里。
    static let repoRoot: URL = {
        if let s = Bundle.main.object(forInfoDictionaryKey: "HZRepoRoot") as? String,
           !s.isEmpty {
            return URL(fileURLWithPath: (s as NSString).expandingTildeInPath)
        }
        // 兜底：从 .app/Contents/MacOS/xxx 往上退到仓库根（build/衡知.app 的情形）
        var u = Bundle.main.bundleURL
        for _ in 0..<5 { u.deleteLastPathComponent() }
        return u
    }()

    static var pythonURL: URL { repoRoot.appendingPathComponent("runtime/python/bin/python3") }
    static var serviceURL: URL { repoRoot.appendingPathComponent("金融大赛_集成试用版/desktop_service.py") }
    static var logsDir: URL { repoRoot.appendingPathComponent("金融大赛_集成试用版/data/logs") }
    static var integratedLog: URL { logsDir.appendingPathComponent("integrated.log") }

    /// 图标主色，用于启动画面的渐变。
    static let brandTop = NSColor(calibratedRed: 0.36, green: 0.29, blue: 0.78, alpha: 1)
    static let brandBottom = NSColor(calibratedRed: 0.55, green: 0.36, blue: 0.85, alpha: 1)
}

// MARK: - 健康检查

/// 探测后台服务是否真正可用。
/// 只认 17904 端口返回的 local_only + mineru_version，避免把别人的服务误判成自己的。
func probeService(timeout: TimeInterval = 1.5, _ done: @escaping (Bool) -> Void) {
    var req = URLRequest(url: HZ.healthURL)
    req.timeoutInterval = timeout
    req.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
    URLSession.shared.dataTask(with: req) { data, _, _ in
        var ok = false
        if let data,
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            ok = (obj["local_only"] as? Bool) == true && obj["mineru_version"] != nil
        }
        DispatchQueue.main.async { done(ok) }
    }.resume()
}

// MARK: - 启动画面

final class SplashView: NSView {
    private let iconView = NSImageView()
    private let titleLabel = NSTextField(labelWithString: "衡知")
    private let statusLabel = NSTextField(labelWithString: "正在启动…")
    private let spinner = NSProgressIndicator()
    private let detailLabel = NSTextField(wrappingLabelWithString: "")
    private let buttonRow = NSStackView()

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        build()
    }

    required init?(coder: NSCoder) { fatalError("不使用 nib") }

    private func build() {
        iconView.image = NSApp.applicationIconImage
        iconView.imageScaling = .scaleProportionallyUpOrDown
        iconView.translatesAutoresizingMaskIntoConstraints = false

        titleLabel.font = .systemFont(ofSize: 34, weight: .semibold)
        titleLabel.textColor = .white
        titleLabel.alignment = .center
        titleLabel.translatesAutoresizingMaskIntoConstraints = false

        statusLabel.font = .systemFont(ofSize: 15)
        statusLabel.textColor = NSColor.white.withAlphaComponent(0.9)
        statusLabel.alignment = .center
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isDisplayedWhenStopped = false
        spinner.translatesAutoresizingMaskIntoConstraints = false

        detailLabel.font = .systemFont(ofSize: 12)
        detailLabel.textColor = NSColor.white.withAlphaComponent(0.75)
        detailLabel.alignment = .center
        detailLabel.maximumNumberOfLines = 12
        detailLabel.isHidden = true
        detailLabel.translatesAutoresizingMaskIntoConstraints = false

        buttonRow.orientation = .horizontal
        buttonRow.spacing = 10
        buttonRow.isHidden = true
        buttonRow.translatesAutoresizingMaskIntoConstraints = false

        for v in [iconView, titleLabel, statusLabel, spinner, detailLabel, buttonRow] {
            addSubview(v)
        }

        NSLayoutConstraint.activate([
            iconView.centerXAnchor.constraint(equalTo: centerXAnchor),
            iconView.bottomAnchor.constraint(equalTo: titleLabel.topAnchor, constant: -18),
            iconView.widthAnchor.constraint(equalToConstant: 128),
            iconView.heightAnchor.constraint(equalToConstant: 128),

            titleLabel.centerXAnchor.constraint(equalTo: centerXAnchor),
            titleLabel.centerYAnchor.constraint(equalTo: centerYAnchor, constant: -20),

            statusLabel.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 16),
            statusLabel.centerXAnchor.constraint(equalTo: centerXAnchor),

            spinner.topAnchor.constraint(equalTo: statusLabel.bottomAnchor, constant: 16),
            spinner.centerXAnchor.constraint(equalTo: centerXAnchor),

            detailLabel.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 18),
            detailLabel.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 60),
            detailLabel.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -60),

            buttonRow.topAnchor.constraint(equalTo: detailLabel.bottomAnchor, constant: 16),
            buttonRow.centerXAnchor.constraint(equalTo: centerXAnchor),
        ])
    }

    override func draw(_ dirtyRect: NSRect) {
        // 紫色渐变，与 .app 图标配色呼应
        let g = NSGradient(starting: HZ.brandTop, ending: HZ.brandBottom)
        g?.draw(in: bounds, angle: -90)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        spinner.startAnimation(nil)
    }

    /// 正常进度提示
    func setStatus(_ text: String) {
        statusLabel.stringValue = text
        spinner.startAnimation(nil)
        spinner.isHidden = false
        detailLabel.isHidden = true
        buttonRow.isHidden = true
    }

    /// 出错提示：停下转圈，展示细节，并给出可点的按钮
    func setError(_ title: String, detail: String, buttons: [(String, Selector)]) {
        statusLabel.stringValue = title
        spinner.stopAnimation(nil)
        spinner.isHidden = true

        detailLabel.stringValue = detail
        detailLabel.isHidden = false

        buttonRow.arrangedSubviews.forEach { $0.removeFromSuperview() }
        for (label, action) in buttons {
            let b = NSButton(title: label, target: nil, action: action)
            b.bezelStyle = .rounded
            b.controlSize = .large
            buttonRow.addArrangedSubview(b)
        }
        buttonRow.isHidden = false
    }
}

// MARK: - 下载处理

/// 原生窗口必须自己接管下载，否则界面上的「下载Word」按钮会没反应。
/// 服务端用 Content-Disposition: attachment 触发，前端用 <a download> 触发，
/// 两条路径都要覆盖。
final class DownloadManager: NSObject, WKDownloadDelegate {
    private var destinations: [ObjectIdentifier: URL] = [:]

    func attach(_ download: WKDownload) {
        download.delegate = self
    }

    /// 存到 ~/Downloads，遇到重名自动加 -1、-2 后缀，绝不静默覆盖用户文件。
    func download(_ download: WKDownload,
                  decideDestinationUsing response: URLResponse,
                  suggestedFilename: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let fm = FileManager.default
        let dir = fm.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? fm.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")

        var name = suggestedFilename.trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty { name = "衡知下载文件" }
        // 去掉可能混进来的路径分隔符
        name = name.replacingOccurrences(of: "/", with: "_")

        var target = dir.appendingPathComponent(name)
        if fm.fileExists(atPath: target.path) {
            let base = (name as NSString).deletingPathExtension
            let ext = (name as NSString).pathExtension
            var i = 1
            while fm.fileExists(atPath: target.path) {
                let n = ext.isEmpty ? "\(base)-\(i)" : "\(base)-\(i).\(ext)"
                target = dir.appendingPathComponent(n)
                i += 1
                if i > 999 { break }
            }
        }

        destinations[ObjectIdentifier(download)] = target
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let url = destinations.removeValue(forKey: ObjectIdentifier(download)) else { return }
        // 在访达里高亮刚下载的文件，用户一眼能看到
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        destinations.removeValue(forKey: ObjectIdentifier(download))
        let a = NSAlert()
        a.messageText = "下载失败"
        a.informativeText = error.localizedDescription
        a.alertStyle = .warning
        a.addButton(withTitle: "好")
        a.runModal()
    }
}

// MARK: - 主控制器

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {

    private var window: NSWindow!
    private var splash: SplashView!
    private var webView: WKWebView!

    private var service: Process?
    private var outputPipe: Pipe?
    private var stdoutBuffer = ""

    /// 服务是不是本应用拉起来的。若是用户先跑了「启动衡知.command」，
    /// 我们就只连接、不接管，退出时也不去动它。
    private var ownsService = false
    private var bootDeadline = Date.distantPast
    private var pollTimer: Timer?

    private let downloads = DownloadManager()

    /// 保留信号源引用，否则会被立刻释放掉。
    private var signalSources: [DispatchSourceSignal] = []

    // MARK: 生命周期

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        installSignalHandlers()
        buildMenu()
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)
        boot()
    }

    /// 让 `kill <pid>` 也能走正常的退出流程。
    /// 默认情况下 SIGTERM 会让进程当场死掉，applicationWillTerminate 不会执行，
    /// 后台的 uvicorn 和 llama-server 就成了孤儿。接管信号后就不会了。
    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT] {
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { NSApp.terminate(nil) }
            src.resume()
            signalSources.append(src)
            signal(sig, SIG_IGN)   // 交给 DispatchSource 处理，别再走默认动作
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        pollTimer?.invalidate()
        pollTimer = nil
        guard ownsService, let p = service, p.isRunning else { return }
        // 先礼后兵：SIGTERM 让 desktop_service.py 自己按顺序清理 uvicorn 与推理服务
        p.terminate()
        let deadline = Date().addingTimeInterval(8)
        while p.isRunning && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }

    // MARK: 界面

    private func buildWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1200, height: 820)
        window = NSWindow(contentRect: frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered,
                          defer: false)
        window.title = "衡知"
        window.minSize = NSSize(width: 940, height: 640)
        window.center()
        window.setFrameAutosaveName("HengzhiMainWindow")
        window.titlebarAppearsTransparent = false

        splash = SplashView(frame: frame)
        splash.autoresizingMask = [.width, .height]
        window.contentView = splash

        window.makeKeyAndOrderFront(nil)
    }

    private func buildMenu() {
        let main = NSMenu()

        // 应用菜单
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appItem.submenu = appMenu
        appMenu.addItem(withTitle: "关于衡知", action: #selector(showAbout), keyEquivalent: "")
            .target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏衡知", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = NSMenuItem(title: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(hideOthers)
        appMenu.addItem(withTitle: "显示全部", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出衡知", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        // 编辑菜单——界面上有输入框，缺了它就没法 ⌘V 粘贴、⌘A 全选
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        editItem.submenu = edit
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = NSMenuItem(title: "重做", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        edit.addItem(redo)
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        // 视图菜单
        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let view = NSMenu(title: "视图")
        viewItem.submenu = view
        view.addItem(withTitle: "重新载入", action: #selector(reloadPage), keyEquivalent: "r").target = self
        view.addItem(.separator())
        view.addItem(withTitle: "放大", action: #selector(zoomIn), keyEquivalent: "+").target = self
        view.addItem(withTitle: "缩小", action: #selector(zoomOut), keyEquivalent: "-").target = self
        view.addItem(withTitle: "实际大小", action: #selector(zoomReset), keyEquivalent: "0").target = self
        view.addItem(.separator())
        let openBrowser = NSMenuItem(title: "在浏览器中打开",
                                     action: #selector(openInBrowser),
                                     keyEquivalent: "o")
        openBrowser.keyEquivalentModifierMask = [.command, .shift]
        openBrowser.target = self
        view.addItem(openBrowser)
        view.addItem(withTitle: "打开数据目录", action: #selector(openDataDir), keyEquivalent: "").target = self

        // 窗口菜单
        let winItem = NSMenuItem()
        main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        winItem.submenu = win
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    // MARK: 启动流程

    private func boot() {
        splash.setStatus("正在启动衡知…")

        // 情形一：用户已经跑过「启动衡知.command」，服务就在那儿，直接连
        probeService { [weak self] alive in
            guard let self = self else { return }
            if alive {
                self.ownsService = false
                self.showWeb()
                return
            }
            self.checkEnvironment()
        }
    }

    private func checkEnvironment() {
        let fm = FileManager.default
        guard fm.isExecutableFile(atPath: HZ.pythonURL.path) else {
            fail(title: "还没安装运行环境",
                 detail: """
                 找不到 Python 环境：
                 \(HZ.pythonURL.path)

                 请在仓库目录里双击「安装环境.command」完成安装（约 5.9 GB，需要联网下载模型），
                 装好后再打开衡知。
                 """,
                 buttons: [("打开仓库目录", #selector(self.openRepoDir)), ("重试", #selector(self.retryBoot))])
            return
        }
        guard fm.fileExists(atPath: HZ.serviceURL.path) else {
            fail(title: "缺少后台服务脚本",
                 detail: "找不到：\n\(HZ.serviceURL.path)\n\n仓库文件可能不完整，请重新克隆。",
                 buttons: [("打开仓库目录", #selector(self.openRepoDir)), ("重试", #selector(self.retryBoot))])
            return
        }
        launchService()
    }

    private func launchService() {
        splash.setStatus("正在加载本地模型与解析引擎…")

        let p = Process()
        p.executableURL = HZ.pythonURL
        p.arguments = ["-X", "utf8", HZ.serviceURL.path]
        p.currentDirectoryURL = HZ.repoRoot

        var env = ProcessInfo.processInfo.environment
        // 万一从终端启动带进了工具私有 PYTHONPATH，清掉，避免污染解释器
        env.removeValue(forKey: "PYTHONPATH")
        env["PYTHONUNBUFFERED"] = "1"
        env["FINAGENT_DATA_DIR"] = HZ.repoRoot.appendingPathComponent("金融大赛_集成试用版/data").path
        p.environment = env

        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        outputPipe = pipe

        pipe.fileHandleForReading.readabilityHandler = { [weak self] fh in
            let d = fh.availableData
            guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
            DispatchQueue.main.async { self?.consume(s) }
        }

        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self, self.ownsService else { return }
                // 还没进入就绪状态就退出了 → 报错；已经在用则忽略
                if self.webView == nil {
                    self.fail(title: "后台服务意外退出",
                              detail: "退出码 \(proc.terminationStatus)\n\n\(self.logTail())",
                              buttons: [("查看日志", #selector(self.openIntegratedLog)),
                                        ("重试", #selector(self.retryBoot))])
                }
            }
        }

        do {
            try p.run()
        } catch {
            fail(title: "无法启动后台服务",
                 detail: error.localizedDescription,
                 buttons: [("打开仓库目录", #selector(self.openRepoDir)), ("重试", #selector(self.retryBoot))])
            return
        }

        service = p
        ownsService = true

        // 双保险：既看子进程的 READY 输出，也轮询健康检查
        bootDeadline = Date().addingTimeInterval(300)
        pollTimer?.invalidate()
        pollTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] t in
            guard let self = self else { t.invalidate(); return }
            if Date() > self.bootDeadline {
                t.invalidate()
                self.fail(title: "启动超时",
                          detail: "等待 5 分钟仍未就绪。首次启动需要加载模型，若机器较慢可稍后重试。\n\n\(self.logTail())",
                          buttons: [("查看日志", #selector(self.openIntegratedLog)),
                                    ("重试", #selector(self.retryBoot))])
                return
            }
            probeService(timeout: 1.0) { ok in if ok { t.invalidate(); self.showWeb() } }
        }
    }

    private func consume(_ chunk: String) {
        stdoutBuffer += chunk
        // 逐行判断，避免 READY 粘在别的输出里
        while let nl = stdoutBuffer.firstIndex(of: "\n") {
            let line = String(stdoutBuffer[stdoutBuffer.startIndex..<nl])
            stdoutBuffer.removeSubrange(stdoutBuffer.startIndex...nl)
            handle(line: line)
        }
        if stdoutBuffer.contains("READY") { showWeb() }
    }

    private func handle(line: String) {
        let t = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.contains("READY") { showWeb(); return }
        if t.isEmpty { return }
        // 把服务的提示转成启动画面上的状态文案，用户知道它在忙什么
        if t.contains("服务启动未完成") {
            fail(title: "后台服务启动失败",
                 detail: "\(t)\n\n\(logTail())",
                 buttons: [("查看日志", #selector(openIntegratedLog)), ("重试", #selector(retryBoot))])
        } else {
            splash.setStatus(t.count > 60 ? String(t.prefix(60)) + "…" : t)
        }
    }

    // MARK: 切换到主界面

    private func showWeb() {
        guard webView == nil else { return }
        pollTimer?.invalidate()
        pollTimer = nil

        let cfg = WKWebViewConfiguration()
        cfg.defaultWebpagePreferences.allowsContentJavaScript = true
        cfg.websiteDataStore = .default()

        let wv = WKWebView(frame: window.contentView?.bounds ?? .zero, configuration: cfg)
        wv.autoresizingMask = [.width, .height]
        wv.navigationDelegate = self
        wv.uiDelegate = self
        wv.allowsBackForwardNavigationGestures = true
        wv.allowsMagnification = true

        window.contentView = wv
        webView = wv

        wv.load(URLRequest(url: HZ.baseURL))
        window.title = "衡知"
    }

    // MARK: 出错处理

    private func fail(title: String, detail: String, buttons: [(String, Selector)]) {
        pollTimer?.invalidate()
        pollTimer = nil
        // 界面已经出来了就不再打扰用户
        guard webView == nil else { return }
        splash.setError(title, detail: detail, buttons: buttons)
    }

    private func logTail(lines: Int = 18) -> String {
        guard let text = try? String(contentsOf: HZ.integratedLog, encoding: .utf8) else {
            return "（暂无日志）"
        }
        let all = text.split(separator: "\n", omittingEmptySubsequences: false)
        return all.suffix(lines).joined(separator: "\n")
    }

    // MARK: 菜单动作

    @objc private func showAbout() {
        let a = NSAlert()
        a.messageText = "衡知"
        a.informativeText = """
        本地部署的金融投研 Agent · macOS 桌面版

        全部计算与推理都在本机完成，不上传任何数据。
        仓库目录：
        \(HZ.repoRoot.path)
        """
        a.addButton(withTitle: "好")
        a.runModal()
    }

    @objc private func retryBoot() {
        // 先把可能残留的进程收掉，再重新走一遍启动流程
        if ownsService, let p = service, p.isRunning { p.terminate() }
        service = nil
        ownsService = false
        webView = nil
        window.contentView = splash
        boot()
    }

    @objc private func reloadPage() {
        if let wv = webView { wv.reload() } else { retryBoot() }
    }

    @objc private func zoomIn() { webView?.pageZoom = min((webView?.pageZoom ?? 1) * 1.1, 3.0) }
    @objc private func zoomOut() { webView?.pageZoom = max((webView?.pageZoom ?? 1) / 1.1, 0.5) }
    @objc private func zoomReset() { webView?.pageZoom = 1.0 }

    @objc private func openInBrowser() { NSWorkspace.shared.open(HZ.baseURL) }
    @objc private func openRepoDir() { NSWorkspace.shared.open(HZ.repoRoot) }
    @objc private func openDataDir() {
        NSWorkspace.shared.open(HZ.repoRoot.appendingPathComponent("金融大赛_集成试用版/data"))
    }
    @objc private func openIntegratedLog() {
        let url = HZ.integratedLog
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.open(url)
        } else {
            NSWorkspace.shared.open(HZ.logsDir)
        }
    }

    // MARK: WKNavigationDelegate —— 下载接管

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // 前端用 <a href=... download> 触发，WebKit 会置上这个标志
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        // 服务端用 Content-Disposition: attachment 触发，这是「下载Word」的关键路径
        if let http = navigationResponse.response as? HTTPURLResponse,
           let cd = http.value(forHTTPHeaderField: "Content-Disposition"),
           cd.lowercased().contains("attachment") {
            decisionHandler(.download)
            return
        }
        // MIME 类型窗口渲染不了（xlsx、zip 等）也交给下载
        if !navigationResponse.canShowMIMEType {
            decisionHandler(.download)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView,
                 navigationAction: WKNavigationAction,
                 didBecome download: WKDownload) {
        downloads.attach(download)
    }

    func webView(_ webView: WKWebView,
                 navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        downloads.attach(download)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        let t = webView.title ?? ""
        window.title = t.isEmpty ? "衡知" : t
    }

    func webView(_ webView: WKWebView,
                 didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        fail(title: "界面加载失败",
             detail: error.localizedDescription,
             buttons: [("重试", #selector(retryBoot)), ("在浏览器中打开", #selector(openInBrowser))])
    }

    // MARK: WKUIDelegate —— 让 target=_blank / 弹窗类链接走系统浏览器

    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = "衡知"
        a.informativeText = message
        a.addButton(withTitle: "好")
        a.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = "衡知"
        a.informativeText = message
        a.addButton(withTitle: "确定")
        a.addButton(withTitle: "取消")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }
}

// MARK: - 入口

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
