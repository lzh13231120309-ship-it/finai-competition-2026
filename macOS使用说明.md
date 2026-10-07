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

### 2. 三个双击就能用的脚本

| 脚本 | 对应 Windows 版 | 作用 |
|---|---|---|
| `安装环境.command` | `安装环境.bat` / `安装环境.ps1` | 建虚拟环境、装依赖、下载 MinerU 资源与 Qwen3-4B 模型、跑安装检查 |
| `启动衡知.command` | `启动衡知.bat` | 拉起本地推理服务与集成版 Web 服务，并打开浏览器 |
| `停止衡知.command` | `停止衡知.bat` | 安全停止上面两个服务 |
| `运行检查.command` | `运行检查.bat` | 单独跑一次依赖与模型指纹检查 |

首次使用按 `安装环境.command` → `启动衡知.command` 的顺序执行即可，浏览器会自动打开 <http://127.0.0.1:17904/>。

### 3. 命令行用法（可选）

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

### 新增文件（macOS 部署脚本）

`安装环境.command`、`启动衡知.command`、`停止衡知.command`、`运行检查.command`，以及本说明。

Windows 的 `.bat` / `.ps1` 脚本**原样保留**，两个平台可以共存，互不影响。

### 本来就已经跨平台的部分（无需改动）

- `server/sysops.py` — 已用 `IS_WINDOWS` 与 `os.name == "nt"` 分支，废纸篓 / 回收站文案、受保护路径集合均已按平台区分
- `server/agent.py` — 提示词已有 macOS 分支（含 PingFang SC 字体、`~/Library` 保护路径等）
- `server/config.py`、`第一阶段_金融提取/finance_extract/app.py`、`start_integrated.py` — 路径与子进程调用方式均为跨平台写法

## 三、移植时在 macOS 上实测过的事实

测试机型：Apple Silicon（arm64），Python 3.13.9，macOS 26。

| 项目 | 结果 |
|---|---|
| `mineru-llama-cpp` macOS arm64 (cp313) wheel | 官方提供，含 `llama-server` 与 **Metal GPU 加速库** `libggml-metal.so` |
| 推理引擎能否运行 | `llama-server --version` 输出 `built with AppleClang ... for Darwin arm64`，原生运行 |
| 依赖清单 `requirements-lock.txt` | 140+ 个包全部装上，含 `mineru` 4.0.10、`mineru-llama-cpp` 0.1.2、`torch` 2.14.1、`onnxruntime` 1.30.0、`opencv-python` 5.0.0.93 |
| 其中 `jieba`（无官方 wheel，需源码编译） | 自动编译成功 |
| 其中 `win32_setctime`（Windows 专用包） | 可正常安装，在 macOS 上是无害的空操作 |
| 单元测试 `金融大赛_集成试用版/tests` + `第一阶段_金融提取/tests` | **132 项全部通过** |
| `运行检查.command` | 通过：MinerU-4_models_onnx、MinerU2.5-Pro-2605-1.2B-GGUF、Qwen 指纹三项全部就绪 |
| GPU 探测 `llama-server --list-devices` | `MTL0: Apple M5 (12124 MiB)` + `BLAS: Accelerate`，Metal 加速可用 |
| `启动衡知.command` 完整启动 | 成功：本地推理服务与 Web 服务均就绪，模型 2.3 秒加载完成 |
| 集成版 Web 服务 | 主页 HTTP 200；`/finance/api/health` 返回 `{"ok":true,"local_only":true,"mineru_version":"4.0.10","uploads_to_github":false}` |
| 真实推理调用 | 通过 `/v1/chat/completions` 提问，模型正常返回结果（Qwen3-4B，Q4_K_M，16384 上下文） |
| `停止衡知.command` | 身份匹配时正确停止并清理 pid；身份不匹配时拒绝关闭；有提取任务未结束时拒绝关闭 |

安装完成后 `runtime/` 目录约 5.9 GB（含虚拟环境约 1.6 GB、模型约 4.3 GB）。



## 四、与 Windows 版的差异与注意点

1. **启动方式**：macOS 双击 `.command` 会开一个终端窗口，关掉窗口不影响已启动的后台服务；要停止请用 `停止衡知.command`。

2. **推理加速**：Windows 版通过 `--list-devices` 探测 Vulkan/NVIDIA 显卡；macOS 上该探测无匹配，llama.cpp 会自动走 **Metal**，无需额外参数。脚本中的 `-ngl 99`（全部层卸载到 GPU）在 Apple Silicon 上同样生效。

3. **线程数**：`local_reasoning.py` 中 `--threads 6` 是为原 Windows 机器设定的。由于 `-ngl 99` 已把计算放到 GPU，一般无需调整；若你的 Mac 核心数差异较大且想微调，可改这个数值。

4. **首次运行模型下载**：Qwen3-4B 模型约 2.5 GB，MinerU 资源另需数 GB，安装脚本会校验官方 SHA256 指纹，不匹配则拒绝启用。

5. **Gatekeeper**：仓库通过 `git clone` 获得时 `.command` 不会被隔离，可直接双击。若从 ZIP 下载后提示"来自身份不明的开发者"，在「系统设置 → 隐私与安全性」中点击「仍要打开」，或在终端执行：

   ```bash
   chmod +x *.command
   xattr -d com.apple.quarantine *.command
   ```

6. **不再支持的点**：无。macOS 与 Windows 功能一致，包括本机文件操作（删除一律走废纸篓）、审计日志、单份 Word 交付。

## 五、团队协作提示

按 `AGENTS.md` 约定，这些改动建议提交在独立分支上（例如 `feature/macos-deployment`），经另一人审查后再合并，不要直接推到 `main`。
