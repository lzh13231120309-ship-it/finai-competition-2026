# 衡知金融 Agent 快速评测

这套评测用于在 1–2 天内快速回答一个问题：**当前衡知 Agent 在财务分析、金融计算、文本+表格数值推理上到底做得怎么样，下一步应该先改哪里。**

它不是官方 leaderboard 提交器，而是工程回归/诊断工具。默认只测三套公开 benchmark：

- **SECQUE**：565 道专家编写的 SEC 财报分析题；quick 模式全量运行。测财报理解、比较分析、比率计算、风险和洞察。
- **FinChain**：58 个主题 × 5 个模板。quick 模式每个模板固定生成 1 个可复现样本，共 290 题。测金融公式与多步数值推理。
- **T²-RAGBench**：quick 模式从 FinQA / ConvFinQA / TAT-DQA 各固定抽 100 题，共 300 题。这里先采用 **Oracle Context**（把 benchmark 已给出的正确上下文直接传给 Agent），测文本+表格上的数值推理；**不测 retrieval / MRR@3**。

quick 总计 **1,155** 题；smoke 总计 **51** 题。所有采样、FinChain 随机种子都固定，benchmark 模式强制 temperature=0，便于版本间回归比较。

---

## 0. 最快安装方式（推荐）

本包已经包含自动补丁安装器。把压缩包内容复制到 `finai-competition-2026` 仓库根目录后，不需要手工改 `app.py` 和 `agent.py`，直接运行：

```bat
安装评测补丁.bat
```

安装器会：

1. 检查当前仓库是不是本 README 对应的 `feat-yjm-pr` 代码结构；
2. 只修改 `金融大赛_集成试用版/server/app.py` 和 `agent.py`；
3. 修改前把原文件备份到 `benchmarks/backup/`；
4. 对修改后的两个 Python 文件做语法编译检查；
5. 重复运行时保持幂等，不会重复插入代码。

检查是否已安装：

```bat
安装评测补丁.bat --check
```

如果需要回滚：

```bat
安装评测补丁.bat --restore
```

然后按下面流程使用：

```bat
评测.bat prepare
启动衡知.bat
评测.bat doctor
评测.bat selftest
评测.bat run --suite all --profile smoke --run-id smoke-01
评测.bat score --run smoke-01
```

smoke 正常以后再运行：

```bat
评测.bat run --suite all --profile quick --run-id quick-v1
评测.bat score --run quick-v1
```

> `prepare` 需要联网下载公开 benchmark；实际模型评测需要衡知中已经配置好待测模型/API。SECQUE 的 LLM Judge 是单独的评分步骤，详见后文。

---


## 1. 为什么需要 benchmark_mode

不要直接把这些题扔进普通 `/api/chat`。

衡知正常模式会：

- 根据关键词自动路由金融工作流；例如 FinChain 题目中的 `DCF` 可能触发“上传财务文件 → 核验 → 估值”的正式比赛流程；
- 暴露知识库、Skills、MCP、系统工具；
- 自动给每个任务起标题，额外消耗一次模型请求；
- 使用日常对话 temperature。

benchmark_mode 只做隔离：

- 不触发财报/估值附件工作流；
- 不访问知识库、Skills、MCP、文件系统；
- SECQUE 不给任何工具；FinChain/T² 只允许 `calculator`；
- 不自动命名任务；
- temperature 固定为 0。

正常产品行为不变，因为 `benchmark_mode` 默认是 `false`。

---

## 2. 仓库需要增加/修改什么

目录最终应为：

```text
finai-competition-2026/
├─ benchmarks/
│  ├─ README.md
│  ├─ run_benchmarks.py
│  └─ .gitignore
├─ 评测.bat
└─ 金融大赛_集成试用版/
   └─ server/
      ├─ app.py       # 小改：给 /api/chat 增加 benchmark_mode
      └─ agent.py     # 小改：隔离 benchmark prompt / tools / temperature
```

`benchmarks/data/`、`benchmarks/vendor/`、`benchmarks/results/` 都由脚本自动生成，不提交 Git。

### 2.1 修改 `server/app.py`

文件：

```text
金融大赛_集成试用版/server/app.py
```

#### A. 在 `/api/health` 返回值里增加 benchmark 能力标记

原来：

```python
return {
    'implemented_topics':[1,2,4], 'future_topics':[3,5,6,7],
    "ok": True, "version": app.version, "time": time.time(),
```

改为：

```python
return {
    'implemented_topics':[1,2,4], 'future_topics':[3,5,6,7],
    'benchmark_api': True,
    "ok": True, "version": app.version, "time": time.time(),
```

`doctor` 会检查这个字段，防止你忘记应用 benchmark patch。

#### B. 给 `ChatBody` 增加两个可选字段

原来：

```python
class ChatBody(BaseModel):
    task_id: str
    content: str = ""
    attachments: list[str] = []
    provider: str | None = None
    model: str | None = None
```

改为：

```python
class ChatBody(BaseModel):
    task_id: str
    content: str = ""
    attachments: list[str] = []
    provider: str | None = None
    model: str | None = None
    benchmark_mode: bool = False
    benchmark_tools: list[str] = []
```

这两个字段都有默认值，所以网页端原来的请求完全不受影响。

#### C. 把 benchmark 参数传给 `agent.run_agent`

原来：

```python
async for ev in agent.run_agent(body.task_id, body.content, attachments, cancel_event):
    yield _sse(ev)
```

改为：

```python
async for ev in agent.run_agent(
    body.task_id,
    body.content,
    attachments,
    cancel_event,
    benchmark_mode=body.benchmark_mode,
    benchmark_tools=body.benchmark_tools,
):
    yield _sse(ev)
```

---

### 2.2 修改 `server/agent.py`

文件：

```text
金融大赛_集成试用版/server/agent.py
```

#### A. 增加 benchmark 允许使用的工具白名单

在：

```python
FINANCIAL_NAMES=financial_agent.NAMES | research_agent.NAMES
MAX_TOOL_RESULT = 12000
```

之间/之后加入：

```python
BENCHMARK_TOOL_NAMES = {"calculator"}
```

最终建议写成：

```python
FINANCIAL_NAMES=financial_agent.NAMES | research_agent.NAMES
BENCHMARK_TOOL_NAMES = {"calculator"}
MAX_TOOL_RESULT = 12000
```

#### B. 增加独立 benchmark system prompt

在 `build_system_prompt(...)` 前加入：

```python
def build_benchmark_system_prompt(tool_names: set[str]) -> str:
    tool_note = (
        "No tools are available for this case."
        if not tool_names
        else "The only available tool for this case is calculator. Use it for arithmetic when useful."
    )
    return (
        "You are being evaluated on an offline financial benchmark. "
        "The user's question and any context included in that same message are the complete materials for this case. "
        "Do not assume or use knowledge bases, MCP, web browsing, filesystem access, skills, uploaded-file workflows, "
        "or any other source not explicitly supplied in the current message. "
        "Answer in the language of the question. Be precise with dates, units, signs, percentages, and financial definitions. "
        "If the question requests a final-answer format, follow it exactly. "
        + tool_note
    )
```

这样 benchmark 不会吃到你本地知识库、Skill、MCP 或比赛专用金融附件 workflow。

#### C. 扩展 `run_agent` 的函数签名

原来：

```python
async def run_agent(
    task_id: str, text: str, attachments: list[dict],
    cancel_event: asyncio.Event | None = None,
) -> AsyncGenerator[dict, None]:
```

改为：

```python
async def run_agent(
    task_id: str,
    text: str,
    attachments: list[dict],
    cancel_event: asyncio.Event | None = None,
    benchmark_mode: bool = False,
    benchmark_tools: list[str] | None = None,
) -> AsyncGenerator[dict, None]:
```

#### D. 替换自动命名 / 金融路由 / 工具准备这一段

找到从：

```python
# 自动命名任务
if cfg.get("auto_title", True):
```

开始，一直到 MCP tools 合并结束：

```python
tools = tools + mcp_tools
```

这一整段替换为：

```python
# benchmark 每题都是一次独立机器评测，不额外调用模型生成标题
if not benchmark_mode and cfg.get("auto_title", True):
    t = db.get_task(task_id)
    if t and t["title"] in ("新任务", "", None):
        title = await _auto_title(base_url, api_key, model, text, attachments)
        if title:
            db.touch_task(task_id, title=title)
            yield {"type": "title", "title": title}

requested_benchmark_tools: set[str] = set()

if benchmark_mode:
    requested_benchmark_tools = set(benchmark_tools or [])
    unknown_benchmark_tools = requested_benchmark_tools - BENCHMARK_TOOL_NAMES
    if unknown_benchmark_tools:
        yield {
            "type": "error",
            "error": "benchmark_mode 不允许工具：" + ", ".join(sorted(unknown_benchmark_tools)),
        }
        return

    intent = None
    financial_mode = False
    messages = [{
        "role": "system",
        "content": build_benchmark_system_prompt(requested_benchmark_tools),
    }]
else:
    intent = continuing_financial_intent(task_id, text)
    financial_mode = (
        bool(intent)
        and (cfg.get("tools_enabled", {}) or {}).get("financial_extraction", True)
    )
    messages = [{
        "role": "system",
        "content": build_system_prompt(task_id, financial_mode),
    }]

messages += _history_messages(task_id, int(cfg.get("context_rounds", 20)))
if messages[-1].get("role") != "user":
    messages.append({
        "role": "user",
        "content": _user_content(text, attachments, financial_mode),
    })
else:
    messages[-1] = {
        "role": "user",
        "content": _user_content(text, attachments, financial_mode),
    }

if benchmark_mode:
    tools = [
        t for t in LOCAL_TOOLS
        if t["function"]["name"] in requested_benchmark_tools
    ]
else:
    tools = local_tools(cfg)
    if financial_mode:
        tools = [
            t for t in tools
            if t["function"]["name"] in FINANCIAL_NAMES | {"calculator"}
        ]

mcp_enabled = [
    s["name"]
    for s in (cfg.get("mcp_servers") or [])
    if s.get("enabled")
]
if (
    not benchmark_mode
    and not financial_mode
    and mcp_enabled
    and (cfg.get("tools_enabled", {}) or {}).get("mcp", True)
):
    yield {"type": "status", "text": "正在连接 MCP 服务器…"}
    await mcp_manager.start_enabled(mcp_enabled)
    mcp_tools = await mcp_manager.all_tools(mcp_enabled)
    for t in mcp_tools:
        t.pop("_mcp", None)
    tools = tools + mcp_tools
```

说明：runner 每题都会先新建一个 task，所以 `_history_messages` 只会包含当前 case，不会串题。

#### E. benchmark 强制 `temperature=0`

在 `stream_chat(...)` 调用中，原来：

```python
temperature=float(cfg.get("temperature", 0.3)),
```

改为：

```python
temperature=0.0 if benchmark_mode else float(cfg.get("temperature", 0.3)),
```

#### F. 禁止 benchmark 被模型“误激活”为比赛金融附件 workflow

找到：

```python
if not financial_mode and fname in FINANCIAL_NAMES and (cfg.get('tools_enabled',{}) or {}).get('financial_extraction',True):
```

改为：

```python
if (
    not benchmark_mode
    and not financial_mode
    and fname in FINANCIAL_NAMES
    and (cfg.get("tools_enabled", {}) or {}).get("financial_extraction", True)
):
```

这一步非常重要。FinChain 里会出现 DCF 等金融关键词；普通衡知会把这些词识别成估值任务，如果不隔离，benchmark 会被错误地送进“上传文件 → 核验 → 估值”的比赛流程。

---

### 2.3 新增 `benchmarks/.gitignore`

完整内容：

```gitignore
data/
vendor/
results/
__pycache__/
*.pyc
```

---

### 2.4 新增仓库根目录 `评测.bat`

完整内容：

```bat
@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\Scripts\python.exe"

if not exist "%PY%" (
  echo [ERROR] 未找到 %PY%
  echo 请先在仓库根目录运行 安装环境.bat
  exit /b 1
)

"%PY%" -X utf8 "%ROOT%benchmarks\run_benchmarks.py" %*
exit /b %errorlevel%
```

它直接复用项目自己的 Python 环境，不再维护第二套 benchmark 环境。

---

### 2.5 新增 `benchmarks/run_benchmarks.py`

不要手工从 README 复制长脚本。使用本评测包中已经提供的完整 `run_benchmarks.py`。

该脚本提供五个命令：

```text
prepare   下载 benchmark 数据
doctor    检查衡知服务和 benchmark API
selftest  离线测试 scorer / SSE / 固定采样
run       批量创建 task、调用 /api/chat、保存 prediction
score     计算指标并输出 summary / failures
```

## 3. clone 后第一次准备

仓库仍然建议放在纯英文路径，例如：

```text
C:\FinAI\finai-competition-2026
```

先安装原项目环境：

```bat
安装环境.bat
```

评测脚本复用仓库现有 Python：

```text
runtime/python/Scripts/python.exe
```

不需要另外创建 benchmark 虚拟环境；现有锁定依赖已经包含 `httpx`、`huggingface_hub` 和 `openai`。

然后下载 benchmark 数据：

```bat
评测.bat prepare
```

该命令会：

1. 下载 SECQUE 四个 JSONL；
2. 只下载 T²-RAGBench 的三个 metadata JSONL，**不会下载 2.98 GB PDF 全库**；
3. 下载 FinChain 源码压缩包并解压到 `benchmarks/vendor/finchain`。

如果 FinChain 下载中断或想强制刷新：

```bat
评测.bat prepare --force
```

---

## 4. 启动衡知并检查

先正常启动：

```bat
启动衡知.bat
```

在网页里配置好你真正想测的 provider / model / API key。

另开终端：

```bat
评测.bat doctor
```

PASS 至少说明：

- `http://127.0.0.1:17904` 可访问；
- 当前分支已经带 benchmark API；
- 当前已经选中模型；
- 如果使用本地模型，本地模型 ready。

然后先跑离线自测：

```bat
评测.bat selftest
```

它不调用模型，检查数值提取、FinChain FAC-lite、T² Number Match、SSE 解析和固定采样逻辑。

---

## 5. 先跑 smoke：51 题

第一次一定先跑 smoke：

```bat
评测.bat run --suite all --profile smoke --run-id smoke-01
```

默认 `--workers 1`。确认 API/provider 稳定后再考虑：

```bat
评测.bat run --suite all --profile smoke --run-id smoke-02 --workers 2
```

跑完先评分：

```bat
评测.bat score --run smoke-01
```

注意：如果没有启用 SECQUE LLM judge，SECQUE 会显示 `unscored`，**不会用字符串匹配伪造分数**。FinChain 和 T² 会正常评分。

---

## 6. quick 正式快速评测：1,155 题

确认 smoke 正常后：

```bat
评测.bat run --suite all --profile quick --run-id quick-v1
```

quick 固定构成：

| Suite | 数量 | 快速评测方式 |
|---|---:|---|
| SECQUE | 565 | 全量 |
| FinChain | 290 | 58 topic × 5 template × 1 固定 seed |
| T²-RAGBench | 300 | FinQA / ConvFinQA / TAT-DQA 各固定 100 |
| **合计** | **1,155** | 固定、可复现 |

如果平均每题耗时 10 / 20 / 30 秒，单 worker 纯推理时间约为 3.21 / 6.42 / 9.63 小时；实际时间还会受模型排队、限流、重试影响。

可以分别跑：

```bat
评测.bat run --suite secque  --profile quick --run-id secque-v1
评测.bat run --suite finchain --profile quick --run-id finchain-v1
评测.bat run --suite t2      --profile quick --run-id t2-v1
```

调试时也可以限制前 N 题：

```bat
评测.bat run --suite finchain --profile quick --limit 10 --run-id debug-10
```

---

## 7. 评分指标

### 7.1 SECQUE Accuracy

SECQUE 官方任务是单轮、无工具回答，使用 LLM grader 与金融专家 reference 比较，允许合理四舍五入和轻微措辞差异。

本 runner 使用 SECQUE 公开实现里的同款 grader prompt。要真正得到 SECQUE Accuracy，需要一个 judge API：

CMD：

```bat
set OPENAI_API_KEY=你的key
set SECQUE_JUDGE_MODEL=gpt-5-mini
评测.bat score --run quick-v1 --judge-secque
```

PowerShell：

```powershell
$env:OPENAI_API_KEY="你的key"
$env:SECQUE_JUDGE_MODEL="gpt-5-mini"
.\评测.bat score --run quick-v1 --judge-secque
```

如果你使用 OpenAI-compatible judge 网关，也可以设置：

```bat
set OPENAI_BASE_URL=https://你的兼容地址/v1
set SECQUE_JUDGE_MODEL=你的judge模型名
```

**建议：** 如果只想先看 Agent 自身能力，先把 565 个 prediction 跑完；judge 可以之后单独执行，评分会缓存，不会重新让被测 Agent 回答。

最终关注：

- SECQUE Overall Accuracy
- Analysis Accuracy
- Comparison Accuracy
- Ratio Accuracy
- Risk Accuracy

### 7.2 FinChain FAC-lite

FinChain 官方 ChainEval 同时包含：

- Final Answer Correctness (FAC)
- 中间步骤语义/数值对齐

官方完整 ChainEval 会额外加载 sentence-transformers / Longformer / BERTScore 等依赖。为了 1–2 天快速接入，本 runner **只复现官方最终答案 5% relative tolerance 的核心判定**，并命名为 `FAC-lite`，避免冒充完整 ChainEval。

因此：

```text
FAC-lite 高：最终金融数值推理总体可靠
FAC-lite 低：优先查公式选择、变量口径、calculator 使用和最终数字输出
```

如果后续要写正式论文/对齐 FinChain leaderboard，再安装 FinChain 官方 ChainEval 并用其 `chaineval/evaluate_predictions.py`。

### 7.3 T²-RAGBench Oracle Number Match

quick 模式把该题官方提供的正确 `context` 直接放进 prompt，所以这是：

```text
Oracle Context -> Agent -> Numeric Answer
```

它回答的是：**已经拿到正确财报文字/表格以后，你会不会理解并算对？**

它不回答：**你能不能从几千份文档中检索到正确页面？**

因此这里报告：

- T² Overall Number Match
- FinQA NM
- ConvFinQA NM
- TAT-DQA NM

不要把这个结果写成 “T² RAG retrieval score” 或 “MRR@3”。如果要测 retrieval，需要单独构建/接入文档 corpus 和排名结果。

---

## 8. 输出文件

每次 run 写入：

```text
benchmarks/results/<run-id>/
├─ run.json
├─ predictions.jsonl
├─ secque_judgments.jsonl   # 只有启用 SECQUE judge 后才有
├─ scored.jsonl
├─ summary.json
├─ summary.md
└─ failures.csv
```

### predictions.jsonl

每题包含：

- `case_id`
- `suite`
- `category`
- `question`
- `gold`
- `prediction`
- `status`
- `latency_sec`
- `usage`
- `tool_calls`
- `error`

### summary.md

这是最快看的成绩表。

### failures.csv

建议直接 Excel 打开，逐题看失败原因。SECQUE 如果还没 judge，不会被错误地列为失败。

---

## 9. 如何用分数指导改进

优先按下面诊断：

| 现象 | 先查哪里 |
|---|---|
| SECQUE Ratio 低 | 比率公式、期间、百分比/单位、calculator prompt |
| SECQUE Analysis/Insight 低 | 财务分析 prompt、证据读取、金融解释能力 |
| SECQUE Risk 低 | 风险信号、上下文证据与结论的联系 |
| FinChain FAC-lite 低 | 公式选择、变量抽取、calculator、最终答案格式 |
| T² Oracle NM 低 | 表格/文本理解、多步数值推理；不是 retrieval |
| 三套都不错，但你们真实 PDF 流程差 | MinerU、字段抽取、evidence search、金融工作流编排 |
| smoke 正常、quick 大量 timeout | API 限流、context 长度、max steps、单题 timeout |

最重要的是固定 `run-id`/profile 和同一模型配置，对改动前后做同题比较，例如：

```text
quick-v1
quick-v2
```

不要每次随机换题，否则无法判断回归。

---

## 10. 中断、重试与断点续跑

默认支持断点续跑。

如果：

```bat
评测.bat run --suite all --profile quick --run-id quick-v1
```

跑到一半终止，再执行同一命令，会跳过已经 `done/partial` 的 case；之前报错的 case 会再次尝试。

如果你明确想忽略已有结果重跑：

```bat
评测.bat run --suite all --profile quick --run-id quick-v1 --no-resume
```

注意这会继续追加记录；评分阶段会按 `case_id` 使用最后一次记录。

---

## 11. 并发建议

先使用：

```text
--workers 1
```

如果 provider 明确允许并发，再尝试 2，然后 4：

```bat
评测.bat run --suite all --profile quick --run-id quick-v1 --workers 2
```

不要一开始开很大并发，因为你测到的可能主要是 429 / provider 限流，而不是 Agent 能力。

---

## 12. 两天最现实的执行顺序

**第 1 天**

```bat
安装环境.bat
评测.bat prepare
启动衡知.bat
评测.bat doctor
评测.bat selftest
评测.bat run --suite all --profile smoke --run-id smoke-01
评测.bat score --run smoke-01
```

smoke 通过后立刻：

```bat
评测.bat run --suite all --profile quick --run-id quick-v1
```

**第 2 天**

先得到数值 benchmark：

```bat
评测.bat score --run quick-v1
```

再配置 judge 得到 SECQUE：

```bat
评测.bat score --run quick-v1 --judge-secque
```

然后主要看：

```text
benchmarks/results/quick-v1/summary.md
benchmarks/results/quick-v1/failures.csv
```

抽最常见的 20–30 个失败案例分类，改一个最明确的问题，再跑相同 profile 得到 `quick-v2`。

---

## 13. 重要口径：比赛报告里怎么写

推荐准确写成：

> 我们使用 SECQUE 评估给定 SEC filing context 下的金融分析能力；使用 FinChain 的 290 题固定模板子集，以与官方 FAC 相同的 5% 最终答案容差进行 FAC-lite 快速评估；使用 T²-RAGBench 的 300 题固定子集进行 Oracle-Context Number Match 评估。该快速测试不等价于 FinChain 完整 ChainEval，也不评估 T² 的 retrieval/MRR@3。

这样不会把快速工程测试误写成官方 leaderboard 成绩。

---

## 14. 数据来源与本地目录

prepare 后：

```text
benchmarks/data/secque/      # SECQUE JSONL
benchmarks/data/t2/          # T² metadata only
benchmarks/vendor/finchain/  # FinChain source/templates
benchmarks/results/          # 你们自己的输出
```

这些目录都由 `.gitignore` 排除，避免把大数据、第三方 benchmark 源码和实验输出提交进比赛仓库。

