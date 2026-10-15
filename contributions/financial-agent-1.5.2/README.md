# 金融人工智能竞赛 Agent（独立源码包 1.5.2）

独立运行的金融研究工作台，支持结构化提取、上市公司财务报告分析、估值建模、研报纠错和质量评估工具。本提交用于团队审查，提供工具不代表完成竞赛验收或达到官方评分目标。

## 启动

需要 Python 3.11–3.13。macOS 双击 `启动.command`；Windows 双击 `启动.bat`；或运行：

```bash
python3 scripts/start.py
```

首次联网创建项目 `.venv` 并安装依赖，后端只监听 `127.0.0.1`，浏览器打开后在设置中填写自己的模型 API Key。程序不会预填任何作者密钥或配置。退出终端或 Ctrl+C 停止服务。已配置的本地模型可走 Ollama；云端模型需要网络。本地知识和规则引擎不依赖在线检索。

Mac OCR 在安装 Xcode Command Line Tools 时自动从 Swift 源码编译。其他平台可自行安装兼容的 RapidOCR 后端；没有 OCR 时明确报告不可提取，不把空内容当正确结果。

聊天顶部可以选择快速／均衡／深度，默认均衡。官方 DeepSeek 调整推理强度；其他接口仅按执行策略生效。必要的金融核验不因档位而关闭。

## 独立 Excel MCP（可选）

执行 `python3 scripts/setup_excel_mcp.py`，创建独立 `.venv-mcp`，安装随包提供的上游源码并配置只读 `fin-excel-audit` 服务。运行主程序后重启或刷新 MCP。不会读取其他软件的模型、MCP 或技能配置。辅助脚本如需 numpy/scipy，安装 requirements-helpers.txt。

## macOS 原生外壳（在接收方本机构建）

先运行启动脚本安装依赖，再安装 Xcode Command Line Tools 后执行 `zsh macos/build_app.sh`。仅提供外壳和 OCR 源码，不附带开发者本机应用或 Python 环境。生成的应用与本机 Python 安装有关，跨机器分发需要另外构建可重定位的运行环境。

## 测试与边界

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
node --test tests/*.cjs
python3 scripts/check_release.py
```

随包测试仅使用程序生成的样例；不含真实评测集、私人原文或历史运行结果。模型替身测试不等于真实模型准确率；真实模型开发案例见 docs/发布验收.md。

事实保留公司、期间、单位、币种、口径和证据。未核验项保留待复核，不把疑点称为造假或确定错误；质量评分只覆盖机器可评项。完整报告可导出 Word，CSV/JSON 为后台可追溯产物；单项追问简短回答。没有材料或关键参数时拒绝臆造估值。

## 隐私与目录

`server/`、`web/` 为源码，`seed/skills/`、`seed/knowledge/` 为出厂资源。运行后的 `data/` 是接收方自己的本地数据，已在忽略规则中排除。密钥只由使用者本地配置，云端模型会接收使用者主动分析的内容。系统文件与命令权限默认关闭，可在设置中按需启用。

发布包排除聊天数据库、配置文件、日志、上传材料、历史产物、备份、缓存、个人目录、其他 Agent 的环境与配置。第三方原始许可证见 THIRD_PARTY_NOTICES.md。
