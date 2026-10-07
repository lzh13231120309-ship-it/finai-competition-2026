# 衡知 · macOS 运行说明

本仓库原部署脚本以 Windows 为目标（`安装环境.ps1`、`启动衡知.bat` 等）。本次改动让同一套代码可以在 macOS（Apple Silicon / Intel）上运行，**业务逻辑、提示词、金融计算与交付流程均未改动**。

## 一、怎么用

### 1. 前置条件

- macOS 13 或更高版本
- Python 3.13（64 位）
  - 方式一：从 <https://www.python.org/downloads/macos/> 下载安装
  - 方式二：`brew install python@3.13`
- 仓库放在**纯英文路径**下，例如 `~/FinAI/finai-competition-2026`  
  （本地模型对非 ASCII 路径兼容性差，这一点与 Windows 版要求一致）

### 2. 桌面应用（推荐，双击即用）

仓库自带一个 macOS 原生桌面应用，把上面那套服务包成了一个普通 App：不用开终端，也不用手动敲脚本。

**构建：**

```bash
cd macos/HengzhiDesktop
./build_app.sh                      # 产物：~/Applications/衡知.app
./build_app.sh /Applications/衡知.app   # 也可以指定位置
```

构建只需命令行工具（`xcode-select --install`），不需要完整 Xcode。

**使用：**

双击 `衡知.app` 即可。应用会自动完成：显示启动画面 → 拉起本地推理服务与 Web 服务 → 就绪后把界面装进原生窗口。

| 操作        | 方式                                    |
| --------- | ------------------------------------- |
| 重新载入界面    | `⌘R`                                  |
| 放大 / 缩小   | `⌘+` / `⌘-`                           |
| 在浏览器中打开   | `⇧⌘O`                                 |
| 下载 Word 交付 | 点界面上的下载按钮，文件存到「下载」文件夹，存完自动在访达里高亮     |
| 退出        | `⌘Q`，或直接关窗口（关窗即退出，后台服务一并停掉）           |

**注意：** `.app` 里只有窗口，不含任何金融逻辑，所有能力仍由仓库里的 Python 代码提供。所以**不要把 `.app` 单独拷给别人**——它记录的是本机仓库的路径。要在别的机器上用，在那台机器上重新跑一次 `build_app.sh`。

**想放进「启动台」**：把 `衡知.app` 拖到「应用程序」文件夹即可。

### 3. 三个双击就能用的脚本（备选）

| 脚本             | 对应 Windows 版            | 作用                                        |
| -------------- | ----------------------- | ----------------------------------------- |
| `安装环境.command` | `安装环境.bat` / `安装环境.ps1` | 建虚拟环境、装依赖、下载 MinerU 资源与 Qwen3-4B 模型、跑安装检查 |
| `启动衡知.command` | `启动衡知.bat`              | 拉起本地推理服务与集成版 Web 服务，并打开浏览器                |
| `停止衡知.command` | `停止衡知.bat`              | 安全停止上面两个服务                                |
| `运行检查.command` | `运行检查.bat`              | 单独跑一次依赖与模型指纹检查                            |

首次使用按 `安装环境.command` → 启动应用（或 `启动衡知.command`）的顺序执行即可。

> 桌面应用和 `启动衡知.command` 可以混用：如果服务已经在跑，打开 `.app` 会直接连上去，不会重复启动，退出时也不会去动它。

### 4. 命令行用法（可选）

```bash
./安装环境.command                 # 完整安装（含模型下载，约数 GB）
./安装环境.command --skip-models   # 只装依赖，跳过模型下载
./启动衡知.command
./停止衡知.command
```

## 二、本次改了什么

### 代码改动（仅 2 处，不影响功能）

1. **`金融大赛_集成试用版/local_reasoning.py`** — `executable_path()` 原先硬编码 `llama-server.exe`。  
   Windows 版 `mineru-llama-cpp` 的二进制叫 `llama-server.exe`，macOS / Linux 版叫 `llama-server`（无扩展名）。  
   现按 `os.name` 判断返回对应文件名。
2. **`scripts/check_install.py`** — 报错文案 `未找到llama-server.exe` 改为打印实际查找路径，便于跨平台排查。

### 新增文件

**macOS 部署脚本：** `安装环境.command`、`启动衡知.command`、`停止衡知.command`、`运行检查.command`，以及本说明。

**桌面应用：** `macos/HengzhiDesktop/`，含 `Sources/main.swift`（Swift 源码）、`Info.plist`、`build_app.sh`（构建脚本）、`make_icon.py`（图标生成）。

**桌面版后台入口：** `金融大赛_集成试用版/desktop_service.py`。它和 `start_integrated.py` 逻辑相同，差别是不打开浏览器、前台常驻，并额外带一个父进程看门狗（见第五节）。

Windows 的 `.bat` / `.ps1` 脚本、命令行版 `start_integrated.py` **原样保留**，两个平台可以共存，互不影响。

### 本来就已经跨平台的部分（无需改动）

- `server/sysops.py` — 已用 `IS_WINDOWS` 与 `os.name == "nt"` 分支，废纸篓 / 回收站文案、受保护路径集合均已按平台区分
- `server/agent.py` — 提示词已有 macOS 分支（含 PingFang SC 字体、`~/Library` 保护路径等）
- `server/config.py`、`第一阶段_金融提取/finance_extract/app.py`、`start_integrated.py` — 路径与子进程调用方式均为跨平台写法

## 三、移植时在 macOS 上实测过的事实

测试机型：Apple Silicon（arm64），Python 3.13.9，macOS 26。

| 项目                                           | 结果                                                                                                                       |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `mineru-llama-cpp` macOS arm64 (cp313) wheel | 官方提供，含 `llama-server` 与 **Metal GPU 加速库** `libggml-metal.so`                                                             |
| 推理引擎能否运行                                     | `llama-server --version` 输出 `built with AppleClang ... for Darwin arm64`，原生运行                                            |
| 依赖清单 `requirements-lock.txt`                 | 140+ 个包全部装上，含 `mineru` 4.0.10、`mineru-llama-cpp` 0.1.2、`torch` 2.14.1、`onnxruntime` 1.30.0、`opencv-python` 5.0.0.93      |
| 其中 `jieba`（无官方 wheel，需源码编译）                  | 自动编译成功                                                                                                                   |
| 其中 `win32_setctime`（Windows 专用包）             | 可正常安装，在 macOS 上是无害的空操作                                                                                                   |
| 单元测试 `金融大赛_集成试用版/tests` + `第一阶段_金融提取/tests`  | **132 项全部通过**                                                                                                            |
| `运行检查.command`                               | 通过：MinerU-4_models_onnx、MinerU2.5-Pro-2605-1.2B-GGUF、Qwen 指纹三项全部就绪                                                       |
| GPU 探测 `llama-server --list-devices`         | `MTL0: Apple M5 (12124 MiB)` + `BLAS: Accelerate`，Metal 加速可用                                                             |
| `启动衡知.command` 完整启动                          | 成功：本地推理服务与 Web 服务均就绪，模型 2.3 秒加载完成                                                                                        |
| 集成版 Web 服务                                   | 主页 HTTP 200；`/finance/api/health` 返回 `{"ok":true,"local_only":true,"mineru_version":"4.0.10","uploads_to_github":false}` |
| 真实推理调用                                       | 通过 `/v1/chat/completions` 提问，模型正常返回结果（Qwen3-4B，Q4_K_M，16384 上下文）                                                         |
| `停止衡知.command`                               | 身份匹配时正确停止并清理 pid；身份不匹配时拒绝关闭；有提取任务未结束时拒绝关闭                                                                                |
| 桌面应用构建 `macos/HengzhiDesktop/build_app.sh`       | 成功产出 `~/Applications/衡知.app`（arm64 可执行文件 172 KB，整套 636 KB，含 .icns 图标与 ad-hoc 签名）                                    |
| 桌面应用启动                                       | 启动画面 → 4～16 秒就绪 → 原生窗口内加载完整界面（`/`、静态资源、`/finance/` 面板、任务轮询全部 HTTP 200）                                                |
| 桌面应用内真实推理                                    | 通过界面提问，Qwen3-4B 正常返回                                                                                                      |
| 桌面应用退出（正常，`⌘Q` / SIGTERM）                    | 应用、uvicorn、llama-server 全部退出，17904 与 17907 端口释放，pid 文件清理干净                                                              |
| 桌面应用被强杀（`kill -9`，模拟崩溃）                       | 看门狗 1 秒内察觉父进程消失并自行清理，不留孤儿进程，日志留下记录                                                                                      |

安装完成后 `runtime/` 目录约 5.9 GB（含虚拟环境约 1.6 GB、模型约 4.3 GB）。桌面应用本体只有 636 KB。

## 四、桌面应用的几个设计点

**为什么是原生窗口而不是「快捷方式打开浏览器」。** 需求是「像普通 Mac 软件那样用」，所以窗口用 `WKWebView` 承载，界面代码一行没改，仍由 `server/app.py` 提供。菜单栏、`⌘Q`、`⌘V` 粘贴、文件下载都按 macOS 的习惯走。

**下载必须自己接管。** 交付的 Word 由服务端以 `Content-Disposition: attachment` 返回，前端再用 `<a download>` 触发。`WKWebView` 默认不会保存这类响应，必须实现 `WKNavigationDelegate` 的两个策略回调返回 `.download`，再配 `WKDownloadDelegate` 指定落盘位置。两条触发路径都做了覆盖，否则「下载 Word」会点了没反应。文件统一存到「下载」文件夹，重名自动加 `-1`、`-2` 后缀，不会静默覆盖已有文件。

**退出要连后台一起收干净。** 后台服务是应用的子进程，退出时应用发 `SIGTERM`，由 `desktop_service.py` 按「先 Web 服务、后推理服务」的顺序清理。这里有两处容易踩的坑，都已处理：

- 应用被强杀或崩溃时 `SIGTERM` 发不出去，所以 `desktop_service.py` 里加了一个看门狗线程，靠 `os.getppid()` 的变化察觉父进程已死，自行清理，避免留下孤儿进程一直占着 17904 / 17907 端口；
- 看门狗里**必须先置退出标志、再打日志**。父进程一死，stdout 那条管道的读端就关了，写日志会抛 `BrokenPipeError`；如果打印排在置位之前，线程会当场死掉，清理永远不会发生（这个顺序问题实测踩到过，现已修正并把标准流改接到日志文件）。

**应用收到 `SIGTERM` 也走正常退出流程。** 默认情况下 `kill` 掉一个 Cocoa 应用，`applicationWillTerminate` 不会执行，后台服务就成了孤儿。应用内用 `DispatchSourceSignal` 接管了 `SIGTERM` / `SIGINT`，所以命令行 `kill` 它也能干净收尾。

**没有改动任何现有文件的行为。** 桌面应用只是新增的入口：`start_integrated.py`、`.bat` / `.ps1` 脚本、`server/` 下的业务代码全部保持原样。`.app` 里记录的仓库路径由构建脚本写入 `Info.plist`，因此 `.app` 可以被拖到任意位置，但**不能脱离仓库单独拷给别人用**。

## 五、与 Windows 版的差异与注意点

1. **启动方式**：推荐用桌面应用 `衡知.app`——没有终端窗口，关掉窗口即退出并连带停掉后台服务。若走脚本方式，双击 `.command` 会开一个终端窗口，关掉窗口**不影响**已启动的后台服务，要停止得用 `停止衡知.command`。
2. **推理加速**：Windows 版通过 `--list-devices` 探测 Vulkan/NVIDIA 显卡；macOS 上该探测无匹配，llama.cpp 会自动走 **Metal**，无需额外参数。脚本中的 `-ngl 99`（全部层卸载到 GPU）在 Apple Silicon 上同样生效。
3. **线程数**：`local_reasoning.py` 中 `--threads 6` 是为原 Windows 机器设定的。由于 `-ngl 99` 已把计算放到 GPU，一般无需调整；若你的 Mac 核心数差异较大且想微调，可改这个数值。
4. **首次运行模型下载**：Qwen3-4B 模型约 2.5 GB，MinerU 资源另需数 GB，安装脚本会校验官方 SHA256 指纹，不匹配则拒绝启用。
5. **Gatekeeper**：仓库通过 `git clone` 获得时 `.command` 不会被隔离，可直接双击。若从 ZIP 下载后提示"来自身份不明的开发者"，在「系统设置 → 隐私与安全性」中点击「仍要打开」，或在终端执行：
   ```bash
   chmod +x *.command
   xattr -d com.apple.quarantine *.command
   ```
6. **不再支持的点**：无。macOS 与 Windows 功能一致，包括本机文件操作（删除一律走废纸篓）、审计日志、单份 Word 交付。

## 六、团队协作提示

按 `AGENTS.md` 约定，这些改动建议提交在独立分支上（例如 `feature/macos-deployment`），经另一人审查后再合并，不要直接推到 `main`。
