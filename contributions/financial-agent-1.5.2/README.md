# 金融人工智能竞赛 Agent（独立源码包 1.5.2）

独立运行的金融研究工作台，支持结构化提取、上市公司财务报告分析、估值建模、研报纠错和质量评估工具。本提交用于团队审查，提供工具不代表完成竞赛验收或达到官方评分目标。

## 启动

需要 Python 3.11–3.13。macOS 双击 `启动.command`；Windows 双击 `启动.bat`；或运行：

```bash
python3 scripts/start.py
```

首次启动会联网创建主环境、安装 numpy/scipy 等辅助依赖，并分别创建两个 MCP 独立环境。自动配置文档提取与 Excel 只读复算连接，实际调用工具自检成功后再启动界面；后续启动复用已安装环境。后端只监听 `127.0.0.1`，浏览器打开后在设置中填写自己的模型 API Key。程序不会预填任何作者密钥或配置。退出终端或 Ctrl+C 停止服务。已配置的本地模型可走 Ollama；云端模型需要网络。本地知识和规则引擎不依赖在线检索。

Mac OCR 在安装 Xcode Command Line Tools 时自动从 Swift 源码编译。其他平台可自行安装兼容的 RapidOCR 后端；没有 OCR 时明确报告不可提取，不把空内容当正确结果。

聊天顶部可以选择快速／均衡／深度，默认均衡。官方 DeepSeek 调整推理强度；其他接口仅按执行策略生效。必要的金融核验不因档位而关闭。

## 已随包提供并自动连接的扩展

| 资源 | 启动时行为 |
| --- | --- |
| 37 个 Skills | 从 seed/skills 装载，含上游参考与脚本 |
| 141 篇知识笔记 | 本地复制并索引；通知原文不共享，网络核验笔记提供脱敏方法说明 |
| financial-doc-extractor | 独立 .venv-doc-mcp，自动连接5个文档解析与字段核验工具 |
| fin-excel-audit | 独立 .venv-mcp，自动连接7个只读工具，验证公式复算 |
| numpy / scipy / python-pptx | 安装到项目主环境，支持附带辅助脚本 |

可运行 `python3 scripts/bootstrap.py` 单独准备全部环境；安装失败会明确报错，不把部分可用当成全部完成。安装与连接状态保存在本机 data/install-status.json，不上传。不会读取其他软件的模型、MCP 或技能配置，也不安装进其他AI工具。

组员需要 Python 3.11–3.13（推荐3.13），首次安装须联网并填写自己的API Key或本地模型地址。源代码能力可一致，回复速度和质量仍取决于所选模型、档位与硬件；跨平台扫描OCR依赖接收方系统：macOS建议安装Xcode Command Line Tools，文档插件在Windows需配置Tesseract及中文语言数据。未配置时扫描件功能不可视为同等可用。包里不含模型权重、作者密钥、个人原文与历史结果。

## macOS 原生外壳（在接收方本机构建）

先运行启动脚本安装依赖，再安装 Xcode Command Line Tools 后执行 `zsh macos/build_app.sh`。仅提供外壳和 OCR 源码，不附带开发者本机应用或 Python 环境。生成的应用与本机 Python 安装有关，跨机器分发需要另外构建可重定位的运行环境。原生外壳构建不会自动携带项目目录中的MCP环境；使用本包的启动.command / 启动.bat 可获得本次验证的自动配置流程。

## 测试与边界

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
node --test tests/*.cjs
python3 scripts/check_release.py
```

随包测试在独立临时数据目录运行，不读取接收方真实配置和MCP连接。测试仅使用程序生成的样例；不含真实评测集、私人原文或历史运行结果。模型替身测试不等于真实模型准确率；真实模型开发案例见 docs/发布验收.md。

事实保留公司、期间、单位、币种、口径和证据。未核验项保留待复核，不把疑点称为造假或确定错误；质量评分只覆盖机器可评项。完整报告可导出 Word，CSV/JSON 为后台可追溯产物；单项追问简短回答。没有材料或关键参数时拒绝臆造估值。

## 隐私与目录

`server/`、`web/` 为源码，`seed/skills/`、`seed/knowledge/` 为出厂资源。运行后的 `data/` 是接收方自己的本地数据，已在忽略规则中排除。密钥只由使用者本地配置，云端模型会接收使用者主动分析的内容。系统文件与命令权限默认关闭，可在设置中按需启用。

发布包排除聊天数据库、配置文件、日志、上传材料、历史产物、备份、缓存、个人目录、其他 Agent 的环境与配置。第三方原始许可证见 THIRD_PARTY_NOTICES.md。
