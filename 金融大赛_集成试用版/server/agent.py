"""Agent 核心：工具定义、提示词编排、流式执行循环。"""

import ast
import asyncio
import json
import math
import operator
import re
import time
from pathlib import Path
from typing import AsyncGenerator

from . import config, db, knowledge, parsers, skills, sysops
from .llm import LLMError, complete, stream_chat, is_local
from .mcp_client import MCPManager, manager as mcp_manager
from . import financial_agent, research_agent, finance_delivery
FINANCIAL_NAMES=financial_agent.NAMES | research_agent.NAMES

MAX_TOOL_RESULT = 12000


# ------------------------------------------------------------------ 工具定义

SAFE_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "floor": math.floor, "ceil": math.ceil, "fabs": math.fabs,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
}
BIN_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def safe_eval(expr: str) -> float | int:
    node = ast.parse(expr, mode="eval").body

    def ev(n):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)):
                return n.value
            raise ValueError("只支持数字常量")
        if isinstance(n, ast.BinOp) and type(n.op) in BIN_OPS:
            return BIN_OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in UNARY_OPS:
            return UNARY_OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name) and n.func.id in SAFE_FUNCS:
                return SAFE_FUNCS[n.func.id](*[ev(a) for a in n.args])
            raise ValueError("不支持的函数")
        if isinstance(n, (ast.Tuple, ast.List)):
            return [ev(e) for e in n.elts]
        raise ValueError(f"不支持的表达式：{type(n).__name__}")

    return ev(node)


LOCAL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": ("执行精确的数学计算。任何涉及数字的运算都必须调用本工具，"
                            "不要心算。支持 + - * / // % ** 与 sqrt/log/exp/abs/round/min/max/sum 等函数。"),
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "数学表达式，例如 (1234-987)/987*100"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": "在本地知识库中检索资料。返回带来源文件名的片段，用于回答需要依据的问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词或问题"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 6"},
                    "kb": {"type": "string", "description": "指定知识库名称，留空则检索全部已启用知识库"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_knowledge_bases",
            "description": "列出当前可用的知识库及其文件数量。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "读取某个技能的完整说明（SKILL.md），按其中的步骤执行任务。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "技能名称"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_attachment",
            "description": "读取本任务中已上传附件的内容（用于文件较长、上下文里只看到摘要时）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "附件文件名"},
                    "offset": {"type": "integer", "description": "从第几个字符开始读取，默认 0"},
                    "limit": {"type": "integer", "description": "最多读取字符数，默认 8000"},
                },
                "required": ["filename"],
            },
        },
    },
]


def local_tools(cfg: dict) -> list[dict]:
    flags = cfg.get("tools_enabled", {}) or {}
    out = []
    if flags.get('financial_extraction', True):
        out.extend(financial_agent.TOOLS)
        out.extend(research_agent.TOOLS)
    for t in LOCAL_TOOLS:
        n = t["function"]["name"]
        if n == "calculator" and not flags.get("calculator", True):
            continue
        if n in ("search_knowledge", "list_knowledge_bases") and not flags.get("knowledge_search", True):
            continue
        if n == "load_skill" and not flags.get("skill_loader", True):
            continue
        if n == "read_attachment" and not flags.get("file_reader", True):
            continue
        out.append(t)
    # 本机操作工具：仅当主人在设置页开启「允许操作本机」时才暴露
    if flags.get("system_access"):
        out.extend(sysops.SYS_TOOLS)
    return out


# ------------------------------------------------------------------ 工具执行

async def run_local_tool(name: str, args: dict, task_id: str) -> str:
    try:
        if name in research_agent.NAMES:
            return await research_agent.dispatch(name,args,task_id)
        if name in financial_agent.NAMES:
            return await financial_agent.dispatch(name, args, task_id)
        if name == "calculator":
            expr = str(args.get("expression", "")).strip()
            if not expr:
                return "错误：表达式为空"
            val = safe_eval(expr)
            return f"{expr} = {val}"
        if name == "search_knowledge":
            res = knowledge.search(str(args.get("query", "")), args.get("kb") or None,
                                   int(args.get("top_k") or 6))
            if res.get("note"):
                return res["note"]
            hits = res.get("hits", [])
            if not hits:
                return "未检索到相关内容。"
            blocks = [f"[{i+1}] 来源：{h['kb']} / {h['file']}（第 {h['chunk_no']+1} 段，相关度 {h['score']}）\n{h['text']}"
                      for i, h in enumerate(hits)]
            return "\n\n".join(blocks)[:MAX_TOOL_RESULT]
        if name == "list_knowledge_bases":
            kbs = knowledge.list_kbs()
            if not kbs:
                return "当前没有知识库。可在设置页新建，或把资料放入 data/knowledge/<知识库名>/ 后重建索引。"
            return "\n".join(
                f"- {k['name']}：{k['files']} 个文件，{k['chunks']} 个分片，"
                f"{'已建索引' if k['indexed'] else '尚未建索引'}，{'启用' if k['enabled'] else '停用'}"
                for k in kbs)
        if name == "load_skill":
            sname = str(args.get("name", "")).strip()
            text = skills.read_skill_text(sname)
            if not text:
                avail = "、".join(s["name"] for s in skills.list_skills()) or "（无）"
                return f"未找到技能「{sname}」。当前可用技能：{avail}"
            return text[:MAX_TOOL_RESULT]
        if name == "read_attachment":
            return _read_attachment(task_id, args)
        if name == "list_dir":
            return sysops.list_dir(str(args.get("path", "")))
        if name == "read_file":
            return sysops.read_file(str(args.get("path", "")),
                                    int(args.get("offset") or 0), int(args.get("limit") or 20000))
        if name == "write_file":
            return sysops.write_file(str(args.get("path", "")), str(args.get("content", "")),
                                     bool(args.get("overwrite")), bool(args.get("append")))
        if name == "run_command":
            return sysops.run_command(str(args.get("command", "")), str(args.get("cwd", "")),
                                      int(args.get("timeout") or 60))
        if name == "move_to_trash":
            return sysops.move_to_trash(str(args.get("path", "")))
    except Exception as e:
        return f"错误：{type(e).__name__}: {e}"
    return f"错误：未知工具 {name}"


def _read_attachment(task_id: str, args: dict) -> str:
    fname = str(args.get("filename", "")).strip()
    offset = max(int(args.get("offset") or 0), 0)
    limit = min(max(int(args.get("limit") or 8000), 1), 40000)
    upload_dir = config.UPLOAD_DIR / task_id
    if not upload_dir.exists():
        return "本任务没有上传过附件。"
    target = None
    for f in upload_dir.iterdir():
        if f.name == fname or f.name.endswith(fname):
            target = f
            break
    if not target:
        names = "、".join(f.name for f in upload_dir.iterdir())
        return f"未找到附件「{fname}」。本任务附件：{names}"
    parsed = parsers.parse_file(target)
    text = parsed.get("text") or "(无文本内容)"
    piece = text[offset:offset + limit]
    tail = "" if offset + limit >= len(text) else f"\n…（还有 {len(text) - offset - limit} 字符，可继续用 offset 读取）"
    return f"【{target.name} 第 {offset}-{offset+len(piece)} 字符】\n{piece}{tail}"


# ------------------------------------------------------------------ 提示词

def build_system_prompt(task_id: str, financial_only: bool = False) -> str:
    cfg = config.load()
    parts = [cfg.get("system_prompt") or "你是一个专业、严谨的 AI 智能体助手。"]
    parts.append('你叫衡知，是金融研究助手。用自然、清晰的中文回应，先解释主要判断，再用少量关键数据支持；区分事实、推断和观点。不要在回复中展示原始JSON、转义符或内部工具术语。')
    if (cfg.get('tools_enabled', {}) or {}).get('financial_extraction', True):
        parts.append(financial_agent.PROMPT)
        parts.append(research_agent.PROMPT)

    kbs = [k for k in knowledge.list_kbs() if k["enabled"] and k["indexed"]]
    parts.append(
        "## 工具使用规范\n"
        "- 需要精确计算时必须调用 calculator，禁止心算数字。\n"
        "- 需要资料依据时调用 search_knowledge，并在回答中标注来源文件名。\n"
        "- 若信息不足，明确说明缺少什么，不要编造数据。"
    )
    catalog = skills.catalog_for_prompt()
    if catalog:
        parts.append(catalog)
    if kbs:
        parts.append("## 可用知识库\n" + "\n".join(f"- {k['name']}（{k['files']} 个文件）" for k in kbs))
    if not financial_only and (cfg.get("tools_enabled", {}) or {}).get("system_access"):
        try:
            out_dir = sysops.output_dir()
        except Exception:
            out_dir = "~/Desktop/AI产出"
        win = sysops.IS_WINDOWS
        if win:
            env_block = (
                "## 本机操作能力（已开启）\n"
                "你可以直接操作主人这台 **Windows 电脑**：读取/写入文件、列目录、执行 cmd 命令、"
                "删除文件（移入回收站）。\n\n"
                f"**默认产出目录**：`{out_dir}`\n"
                "- 生成任何文件（报告、Excel、Word、PPT、PDF、图表）时，除非主人明确指定了其他路径，"
                "一律保存到上述默认产出目录；文件名用「主题-日期」形式，避免覆盖已有文件。\n"
                "- 生成文件后，必须在回答里给出**完整绝对路径**，并简要说明文件里有什么。\n"
                "- 路径必须用 Windows 写法（如 `C:\\Users\\你的用户名\\Desktop\\AI产出\\报告-2026-10-02.xlsx`），"
                "**绝不要写 `/Users/...` 这类 macOS 路径**。\n\n"
                "**生成文件的正确做法**：用 run_command 执行 Python（命令写 `python`，它已指向应用自带环境；"
                "以下库均已安装：openpyxl / xlsxwriter 生成 Excel，python-docx 生成 Word，"
                "python-pptx 生成 PPT，reportlab 生成 PDF，matplotlib 画图表，pillow 处理图片，pypdf 处理 PDF）。\n"
                "- 环境**没有 pip**，不要执行 `pip install`，直接用上面已装好的库。\n"
                "- shell 是 **cmd**，不是 bash：不要用 `ls` / `cat` / `rm` / `open` / `&&` 链式写法以外的 Unix 习惯，"
                "列目录用 dir，读文件用 type 或直接调 read_file 工具。\n"
                "- 含中文的 PDF：reportlab 用 `UnicodeCIDFont('STSong-Light')` 注册字体，否则中文变方块。\n"
                "- 含中文的图表：matplotlib 先设 "
                "`matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'SimSun']` "
                "并设 `axes.unicode_minus = False`。\n"
                "- 复杂内容建议先 write_file 写一个 .py 脚本，再用 run_command 执行，便于排错和复现。\n\n"
                "**纪律**：\n"
                "- 删除文件一律用 move_to_trash（可恢复），绝不使用 del / rd / rm 命令。\n"
                "- 不要写入或删除 `C:\\Windows`、`C:\\Program Files`、`%APPDATA%`、注册表等系统位置。\n"
                "- 覆盖已有文件前，先确认那是你自己刚生成的文件；不确定就先换个文件名。\n"
                "- 命令失败时阅读报错再改，不要反复执行同一个失败命令。"
            )
        else:
            env_block = (
                "## 本机操作能力（已开启）\n"
                "你可以直接操作主人这台 Mac：读取/写入文件、列目录、执行 shell 命令、删除文件（移入废纸篓）。\n\n"
                f"**默认产出目录**：`{out_dir}`\n"
                "- 生成任何文件（报告、Excel、Word、PPT、PDF、图表）时，除非主人明确指定了其他路径，"
                "一律保存到上述默认产出目录；文件名用「主题-日期」形式，避免覆盖已有文件。\n"
                "- 生成文件后，必须在回答里给出**完整绝对路径**，并简要说明文件里有什么。\n\n"
                "**生成文件的正确做法**：用 run_command 调用 Python（命令里的 python3 已指向应用自带环境，"
                "以下库均已安装：openpyxl / xlsxwriter 生成 Excel，python-docx 生成 Word，"
                "python-pptx 生成 PPT，reportlab 生成 PDF，matplotlib 画图表，pillow 处理图片，pypdf 处理 PDF）。\n"
                "- 含中文的 PDF：reportlab 用 `UnicodeCIDFont('STSong-Light')` 注册字体，否则中文变方块。\n"
                "- 含中文的图表：matplotlib 先设 "
                "`matplotlib.rcParams['font.sans-serif'] = ['PingFang SC', 'Heiti SC', 'Arial Unicode MS']` "
                "并设 `axes.unicode_minus = False`。\n"
                "- 复杂内容建议先 write_file 写一个 .py 脚本，再用 run_command 执行，便于排错和复现。\n\n"
                "**纪律**：\n"
                "- 删除文件一律用 move_to_trash（可恢复），绝不使用 rm 命令。\n"
                "- 不要写入或删除 `~/Library`、`/System`、`/usr`、`/Applications` 等系统位置。\n"
                "- 覆盖已有文件前，先确认那是你自己刚生成的文件；不确定就先换个文件名。\n"
                "- 命令失败时阅读报错再改，不要反复执行同一个失败命令。"
            )
        parts.append(env_block)
    parts.append(f"当前时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n\n".join(p for p in parts if p)


def _history_messages(task_id: str, rounds: int) -> list[dict]:
    """按「最近 N 轮用户消息」取历史：从第 N 近的 user 消息起，其后所有消息（含工具往返）全部保留。

    旧实现按固定条数截窗口，工具密集的运行一步就产生好几条消息，
    会把原始用户消息挤出窗口，导致"继续"之后模型完全失忆。
    """
    rows = db.list_messages(task_id)
    if not rows:
        return []
    user_idx = [i for i, m in enumerate(rows) if m["role"] == "user"]
    if not user_idx:
        return []
    rounds = max(int(rounds or 1), 1)
    start = user_idx[-rounds] if len(user_idx) > rounds else 0
    rows = rows[start:]

    out: list[dict] = []
    pending_tool_ids: set[str] = set()
    for m in rows:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or "",
                        "tool_calls": m["tool_calls"]})
            for c in m["tool_calls"]:
                if c.get("id"):
                    pending_tool_ids.add(c["id"])
        elif role == "tool":
            meta = m.get("tool_calls") or {}
            tc_id = meta.get("tool_call_id", "")
            if tc_id and tc_id not in pending_tool_ids:
                continue  # 孤立的 tool 消息（无对应 assistant.tool_calls），丢弃防 400
            out.append({"role": "tool", "tool_call_id": tc_id,
                        "content": m.get("content") or ""})
            pending_tool_ids.discard(tc_id)
        elif role == "user":
            pending_tool_ids.clear()
            if m.get("attachments"):
                names = "、".join(str(a.get("name", "")) for a in m["attachments"] if a.get("name"))
                note = f"\n\n（该轮曾上传附件：{names}；如需再次读取内容，可调用 read_attachment 工具）"
                out.append({"role": "user", "content": (m.get("content") or "") + note})
            else:
                out.append({"role": "user", "content": m.get("content") or ""})
        else:
            out.append({"role": role, "content": m.get("content") or ""})

    # 中断/异常导致 tool_calls 没有结果行：补占位结果，避免厂商协议 400
    filled = {m.get("tool_call_id") for m in out if m["role"] == "tool"}
    fixed: list[dict] = []
    for m in out:
        fixed.append(m)
        if m["role"] == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                cid = c.get("id", "")
                if cid and cid not in filled:
                    fixed.append({"role": "tool", "tool_call_id": cid,
                                  "content": "（该工具调用因运行中断没有返回结果）"})
    return fixed


def _user_content(text: str, attachments: list[dict], financial_only: bool = False) -> object:
    if not attachments:
        return text
    if financial_only or re_financial_task(text):
        # Route images through MinerU; the reasoning model need not support vision.
        return (text or '') + '\n\n本轮附件（请用金融工具读取真实结构及证据）：\n' + '\n'.join(
            f"- {a['name']}（{a.get('size', 0)}字节）" for a in attachments)
    parts: list[dict] = []
    for a in attachments:
        if a.get("kind") == "image" and a.get("data_url"):
            parts.append({"type": "image_url", "image_url": {"url": a["data_url"]}})
    blocks = [parsers.build_attachment_block(a) for a in attachments]
    body = (text or "").strip()
    full = ("\n\n".join(blocks) + ("\n\n" + body if body else "")) if blocks else body
    if not parts:
        return full
    parts.append({"type": "text", "text": full})
    return parts


def re_financial_task(text):
    return bool(financial_intent(text))

def financial_intent(text):
    import re
    positive=re.sub(r'(?:不要|不需要|无需|不做)[^，,。！？；;\n]*','',text or '')
    if any(x in positive for x in ('估值','DCF','现金流折现','可比公司','值多少钱','合理股价','目标价','股价区间','④','第4项')):return 'valuation'
    if any(x in positive for x in ('财报分析','财务报告分析','同比','环比','毛利率','异常信号','②','第2项')):return 'analysis'
    if re.search(r'(分析|解读|看看|解释|判断|评估|比较)',positive) and re.search(r'(财报|财务报表|财务报告|年报|季报|业绩)',positive):return 'analysis'
    if re.search(r'(分析|解读|看看|解释|阅读)',positive) and '公告' in positive:return 'extraction'
    if any(x in positive for x in ('结构化','提取','抽取','质押','中标','股权变动','公告解读','解读上传的金融公告')):return 'extraction'
    return None


def financial_followup(text):
    reply=(text or '').strip()
    return bool(re.fullmatch(r'(?:是|币种(?:是|为)?|用|按)?(?:人民币|CNY|美元|USD|港币|HKD)[。.!！]?',reply,re.I) or re.search(r'^(继续|接着|重新|重算|生成|请继续|详细解释|详细一点|出个报告|给我.*(?:Word|word|报告))',reply))

def continuing_financial_intent(task_id,text):
    direct=financial_intent(text)
    if direct:return direct
    # A short clarification continues the last financial task; unrelated questions do not.
    reply=(text or '').strip()
    if not financial_followup(reply):
        return None
    users=[m for m in db.list_messages(task_id) if m['role']=='user']
    for msg in reversed(users):
        if (msg.get('content') or '').strip()==reply:continue
        found=financial_intent(msg.get('content'))
        if found:return found
        # Do not resurrect an older financial task across an unrelated topic.
        if not financial_followup(msg.get('content')):break
    return None


def compact_financial_context(messages, budget=14000):
    """Compact older model inputs only; SQLite and source/artifact files remain intact."""
    def size():
        return sum(len(str(m.get('content') or '')) for m in messages)
    tool_indices = [i for i, m in enumerate(messages) if m['role'] == 'tool']
    for i in tool_indices[:-1]:
        if size() <= budget:
            break
        try:
            data = json.loads(messages[i]['content'])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        summary = {k: data[k] for k in ('ok', 'error', 'run_id', 'status', 'source', 'total', 'field_count', 'missing_fields', 'downloads', 'attachments', 'remaining_attachment_ids', 'next_offset') if k in data}
        summary['context_compacted'] = True
        summary['notice'] = '完整结果已保存。必要时用当前run_id检索原文或读取字段，不依据压缩记录猜测数值。'
        messages[i]['content'] = json.dumps(summary, ensure_ascii=False)
    for m in messages[:-2]:
        if size() <= budget:
            break
        if m['role'] == 'assistant' and len(m.get('content') or '') > 1200:
            m['content'] = '此前模型文字说明已压缩；事实以已保存的工具结果与当前字段为准。'


# ------------------------------------------------------------------ 主循环

class Cancelled(Exception):
    pass


async def run_agent(
    task_id: str, text: str, attachments: list[dict],
    cancel_event: asyncio.Event | None = None,
) -> AsyncGenerator[dict, None]:
    cfg = config.load()
    provider = cfg.get("provider", "deepseek")
    model = cfg.get("model", "")
    api_key = (cfg.get("api_keys", {}) or {}).get(provider, "")
    base_url = (cfg.get("base_url_overrides", {}) or {}).get(provider) or ""

    if not model:
        yield {"type": "error", "error": "尚未选择模型，请点击左上角模型选择器或进入设置页配置。"}
        return
    from .providers import resolve
    base_url, _ = resolve(provider, base_url)
    if not api_key and not is_local(base_url):
        yield {"type": "error",
               "error": f"尚未填写「{provider}」的 API Key，请点击右上角设置 → 模型 → 填入并保存。"}
        return


    # 记录用户消息
    user_msg = db.add_message(task_id, "user", text or "", attachments=[
        {"name": a["name"], "kind": a["kind"], "size": a["size"]} for a in attachments
    ] or None)
    db.touch_task(task_id)
    yield {"type": "user_saved", "message": user_msg}

    # 自动命名任务
    if cfg.get("auto_title", True):
        t = db.get_task(task_id)
        if t and t["title"] in ("新任务", "", None):
            title = await _auto_title(base_url, api_key, model, text, attachments)
            if title:
                db.touch_task(task_id, title=title)
                yield {"type": "title", "title": title}

    intent=continuing_financial_intent(task_id,text)
    financial_mode = bool(intent) and (cfg.get('tools_enabled', {}) or {}).get('financial_extraction', True)
    messages = [{"role": "system", "content": build_system_prompt(task_id, financial_mode)}]
    messages += _history_messages(task_id, int(cfg.get("context_rounds", 20)))
    if messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": _user_content(text, attachments,financial_mode)})
    else:
        messages[-1] = {"role": "user", "content": _user_content(text, attachments,financial_mode)}

    tools = local_tools(cfg)
    if financial_mode:
        tools = [t for t in tools if t['function']['name'] in FINANCIAL_NAMES | {'calculator'}]
    mcp_enabled = [s["name"] for s in (cfg.get("mcp_servers") or []) if s.get("enabled")]
    if not financial_mode and mcp_enabled and (cfg.get("tools_enabled", {}) or {}).get("mcp", True):
        yield {"type": "status", "text": "正在连接 MCP 服务器…"}
        await mcp_manager.start_enabled(mcp_enabled)
        mcp_tools = await mcp_manager.all_tools(mcp_enabled)
        for t in mcp_tools:
            t.pop("_mcp", None)
        tools = tools + mcp_tools
    if tools:
        yield {"type": "tools_ready", "count": len(tools)}

    max_steps = max(1, min(int(cfg.get("agent_max_steps", 24)), 24))
    hard_cap = max_steps
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    full_text_parts: list[str] = []

    step = 0
    financial_calls = 0
    exported_results = []
    completion_reminders = 0
    financial_phase = 'attachments'
    evidence_attempts = 0
    evidence_searched = False
    repeated_reads = {}
    required_attachment_ids = set()
    exported_attachment_ids = set()
    run_attachment_ids = {}
    current_attachment_names = {a['name'] for a in attachments}
    searched_fields = set()
    active_run_id = None
    research_results=[]
    assumption_ids=[]
    search_attempts=0
    evidence_reads=0
    async def final_delivery(reason=''):
        result=await asyncio.to_thread(finance_delivery.build,task_id,exported_results,research_results,intent,reason)
        db.add_message(task_id,'assistant',result['content']);db.touch_task(task_id)
        return result
    while step < hard_cap or (financial_mode and financial_phase=='finish'):
        if cancel_event and cancel_event.is_set():
            yield {"type": "cancelled"}
            return
        if financial_mode and financial_phase=='finish':
            exported_results=[o for o in exported_results if financial_agent.export_is_current(task_id,o)]
            research_results=[o for o in research_results if research_agent.current(task_id,o)]
            ready=bool(exported_results) and not (required_attachment_ids-exported_attachment_ids)
            if intent in ('analysis','valuation'):ready=ready and any(o['mode']=='analysis' for o in research_results)
            if intent=='valuation':ready=ready and any(o['mode']=='valuation' for o in research_results)
            if not ready:
                yield {'type':'error','error':'输入或字段发生变化，当前成果已失效，请重新核验生成。'};return
            # Keep model-generated tool choices and parameters in the audit, but financial
            # delivery is rendered from artifacts. A second prose pass can change units.
            content=(await final_delivery())['content']
            yield {'type':'delta','text':content}
            yield {'type':'done','message':{'role':'assistant','content':content},'usage':total_usage,'text':content}
            return
        step += 1
        yield {"type": "step", "n": step, "max": hard_cap}

        step_text: list[str] = []
        step_reason: list[str] = []
        tool_calls: list[dict] = []
        step_tools = financial_agent.phase_tools(tools, financial_phase) if financial_mode else tools
        if financial_mode:
            compact_financial_context(messages)
            step_tools = json.loads(json.dumps(step_tools))
            for tool in step_tools:
                props = tool['function']['parameters']['properties']
                if 'run_id' in props and active_run_id:
                    props['run_id']['enum'] = [active_run_id]
                if 'attachment_id' in props and required_attachment_ids - exported_attachment_ids:
                    props['attachment_id']['enum'] = sorted(required_attachment_ids - exported_attachment_ids)
                if 'run_ids' in props and exported_results:
                    props['run_ids']['items']['enum']=sorted({r['run_id'] for r in exported_results})
                if 'assumptions_attachment_id' in props:
                    props['assumptions_attachment_id']['enum']=assumption_ids or ['']
        try:
            choice='required' if financial_mode and financial_phase!='finish' else 'auto'
            if financial_mode and len(step_tools)==1:
                choice={'type':'function','function':{'name':step_tools[0]['function']['name']}}
            request_messages=messages
            if financial_mode:
                guide={'role':'user','content':f"服务端流程状态：{financial_phase}。本阶段工具为{[t['function']['name'] for t in step_tools]}。当前文件ID={active_run_id}；已导出文件ID={[r['run_id'] for r in exported_results]}。必须调用本阶段工具，不要写最终说明或下载链接。缺失或冲突的字段应保留问题并导出其余可靠数据；币种、期间、统计范围属于字段属性，不要作为独立财务指标反复检索。没有JSON方案时估值工具省略方案附件参数。"}
                request_messages=messages+[guide]
            async for ev in stream_chat(
                base_url, api_key, model, request_messages, step_tools,
                temperature=float(cfg.get("temperature", 0.3)),
                max_tokens=min(int(cfg.get('max_tokens') or 2048),1536) if financial_mode else int(cfg.get("max_tokens") or 0) or None,
                tool_choice=choice,
            ):
                if cancel_event and cancel_event.is_set():
                    break
                if ev["type"] == "text":
                    step_text.append(ev["text"])
                    if not financial_mode:yield {"type": "delta", "text": ev["text"]}
                elif ev["type"] == "reasoning":
                    step_reason.append(ev["text"])
                    yield {"type": "reasoning", "text": ev["text"]}
                elif ev["type"] == "tool_calls":
                    tool_calls = ev["tool_calls"]
                elif ev["type"] == "usage":
                    for k in total_usage:
                        total_usage[k] += int((ev["usage"] or {}).get(k) or 0)
        except LLMError as e:
            if financial_mode:
                content=(await final_delivery('研究模型暂时没有完成后续步骤，尚未完成的分析已在报告中说明'))['content']
                yield {'type':'delta','text':content};yield {'type':'done','text':content,'message':{'role':'assistant','content':content},'partial':True,'usage':total_usage};return
            msg = f"模型调用失败：{e}"
            db.add_message(task_id, "assistant", f"⚠️ {msg}")
            yield {"type": "error", "error": msg}
            return
        except Exception as e:
            if financial_mode:
                content=(await final_delivery('本轮处理遇到运行问题，当前仅交付已经核验的部分'))['content']
                yield {'type':'delta','text':content};yield {'type':'done','text':content,'message':{'role':'assistant','content':content},'partial':True,'usage':total_usage};return
            msg = f"运行出错：{type(e).__name__}: {e}"
            db.add_message(task_id, "assistant", f"⚠️ {msg}")
            yield {"type": "error", "error": msg}
            return

        content = "".join(step_text)
        if content:
            full_text_parts.append(content)

        if not tool_calls:
            exported_results = [out for out in exported_results if financial_agent.export_is_current(task_id, out)]
            research_results=[out for out in research_results if research_agent.current(task_id,out)]
            research_ready= intent=='extraction' or (any(o['mode']=='analysis' for o in research_results) and (intent!='valuation' or any(o['mode']=='valuation' for o in research_results)))
            if financial_mode and (not exported_results or required_attachment_ids - exported_attachment_ids or not research_ready):
                if completion_reminders < 2:
                    completion_reminders += 1
                    messages.append({'role': 'assistant', 'content': content})
                    messages.append({'role': 'user', 'content': f'系统核验：当前阶段为{financial_phase}，当前run_id={active_run_id}，尚未导出的附件ID={sorted(required_attachment_ids - exported_attachment_ids)}。本轮未完成；请调用当前提供的工具，不得编造下载链接。若工具或输入不可用，明确说明失败。'})
                    yield {'type': 'status', 'text': '正在检查是否已真正生成结构化成果…'}
                    continue
                msg = '本轮未生成经过核验的结构化文件。请检查附件、模型响应或工具错误；不能将当前文字说明视为提取完成。'
                content=(await final_delivery('当前材料处理尚未完成，不使用模型草稿作为可靠结论'))['content']
                yield {'type':'delta','text':content};yield {'type':'done','text':content,'message':{'role':'assistant','content':content},'partial':True,'usage':total_usage}
                return
            if financial_mode:
                content=(await final_delivery())['content']
                yield {'type':'delta','text':content};yield {'type':'done','text':content,'message':{'role':'assistant','content':content},'usage':total_usage};return
            if exported_results and intent=='extraction':
                delivery = financial_agent.delivery_text(task_id, exported_results)
                content += delivery
                full_text_parts.append(delivery)
                yield {'type': 'delta', 'text': delivery}
            for out in research_results:
                if intent=='valuation' and out['mode']=='analysis':continue
                delivery=research_agent.delivery(task_id,out)
                content+=delivery;full_text_parts.append(delivery)
                yield {'type':'delta','text':delivery}
            db.add_message(task_id, "assistant", content, reasoning="".join(step_reason))
            db.touch_task(task_id)
            done_msg = {"role": "assistant", "content": content}
            yield {"type": "done", "message": done_msg, "usage": total_usage,
                   "text": "".join(full_text_parts)}
            return

        # 保存带工具调用的助手消息
        db.add_message(task_id, "assistant", content,
                       reasoning="".join(step_reason), tool_calls=tool_calls)
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})

        for call in tool_calls:
            fn = call.get("function") or {}
            fname = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except Exception:
                args = {}
            # A model-selected financial tool also activates the verified workflow.
            # Ordinary phrasing must not leave tools available without their prerequisites.
            if not financial_mode and fname in FINANCIAL_NAMES and (cfg.get('tools_enabled',{}) or {}).get('financial_extraction',True):
                intent='valuation' if fname=='build_financial_valuation' else 'analysis' if fname=='analyze_financial_reports' else 'extraction'
                financial_mode=True;financial_phase='attachments'
                tools=[t for t in tools if t['function']['name'] in FINANCIAL_NAMES | {'calculator'}]
                messages[0]['content']=build_system_prompt(task_id,True)
                yield {'type':'finance_mode'}
                yield {'type':'status','text':'正在准备材料与计算依据…'}
            yield {"type": "tool_start", "name": fname, "args": args,
                   "id": call.get("id", ""), "step": step + 1}
            t0 = time.time()
            enabled_names = {t['function']['name'] for t in financial_agent.phase_tools(tools, financial_phase)} if financial_mode else {t['function']['name'] for t in step_tools}
            if fname not in enabled_names:
                message='当前阶段需要先列出附件并完成事实核验，再使用研究工具；请调用当前提供的工具。' if financial_mode and fname in FINANCIAL_NAMES else '当前任务未启用此工具'
                result = json.dumps({'ok': False, 'error': message}, ensure_ascii=False) if fname in FINANCIAL_NAMES else '错误：当前任务未启用此工具'
            elif financial_mode and args.get('run_id') and args['run_id'] != active_run_id:
                result = json.dumps({'ok': False, 'error': '请先完成当前文件的核验与导出，逐个处理附件', 'current_run_id': active_run_id}, ensure_ascii=False)
            elif fname in research_agent.NAMES and set(args.get('run_ids') or []) != {o['run_id'] for o in exported_results}:
                result=json.dumps({'ok':False,'error':'必须使用本轮全部已导出财报ID','required_run_ids':sorted({o['run_id'] for o in exported_results})},ensure_ascii=False)
            elif fname=='build_financial_valuation' and args.get('assumptions_attachment_id') and args['assumptions_attachment_id'] not in assumption_ids:
                result=json.dumps({'ok':False,'error':'估值方案只能使用本轮已上传的JSON方案附件。财报附件不能作为方案；没有JSON方案时请省略该参数或留空，由工具生成有依据的估计与明确标注的情景假设。','allowed_assumptions':assumption_ids},ensure_ascii=False)
            elif fname in FINANCIAL_NAMES and financial_calls >= 24:
                result = json.dumps({'ok': False, 'error': '金融工具调用达到24次上限，请结束并说明未完成项'}, ensure_ascii=False)
            elif MCPManager.is_mcp_tool(fname):
                parsed = MCPManager.parse_tool_name(fname)
                result = await mcp_manager.call(parsed[0], parsed[1], args)
            else:
                if fname in FINANCIAL_NAMES:
                    financial_calls += 1
                result = await run_local_tool(fname, args, task_id)
            ms = int((time.time() - t0) * 1000)
            if fname == 'export_financial_result':
                parsed_export = json.loads(result)
                if parsed_export.get('ok'):
                    exported_results.append(parsed_export)
            if financial_mode and fname in FINANCIAL_NAMES:
                payload = json.loads(result)
                if payload.get('ok'):
                    completion_reminders = 0
                if fname == 'list_financial_attachments' and payload.get('ok') and payload.get('attachments'):
                    selected_attachments = [a for a in payload['attachments'] if not current_attachment_names or a['filename'] in current_attachment_names]
                    required_attachment_ids = {a['attachment_id'] for a in selected_attachments if a.get('role')!='valuation_assumptions'}
                    assumption_ids=[a['attachment_id'] for a in selected_attachments if a.get('role')=='valuation_assumptions']
                    if len(required_attachment_ids)>3:
                        msg='本地模型每轮最多处理3份事实文件，请分批新建任务测试；估值假设JSON另附。当前尚未开始解析这些文件。'
                        db.add_message(task_id,'assistant',msg)
                        yield {'type':'error','error':msg};return
                    if len(assumption_ids)>1 and intent=='valuation':
                        msg='本轮包含多份估值假设JSON，请只上传一份需要使用的方案，避免静默选择参数。'
                        db.add_message(task_id,'assistant',msg)
                        yield {'type':'error','error':msg};return
                    financial_phase = 'extract'
                elif fname == 'extract_financial_document' and payload.get('ok'):
                    active_run_id = payload['run_id']
                    run_attachment_ids[payload['run_id']] = args.get('attachment_id')
                    financial_phase = 'plan' if payload.get('status') == 'done' else 'wait'
                    evidence_searched = False
                    evidence_attempts = 0
                    searched_fields = set()
                    search_attempts=0;evidence_reads=0
                elif fname == 'inspect_financial_result':
                    signature = json.dumps(args, sort_keys=True)
                    repeated_reads[signature] = repeated_reads.get(signature, 0) + 1
                    if payload.get('status') in ('queued','running'):
                        financial_phase='wait'
                    elif financial_phase=='evidence':
                        evidence_reads+=1
                        if evidence_reads>=2 or not payload.get('ok'):financial_phase='validate'
                    elif not payload.get('ok') or repeated_reads[signature] >= 2:
                        financial_phase = 'search'
                    elif financial_phase == 'wait' and payload.get('status') == 'done':
                        financial_phase = 'plan'
                elif fname == 'find_financial_evidence' and payload.get('ok'):
                    search_attempts+=1
                    evidence_searched = True
                    searched_fields.add(args.get('query'))
                    financial_phase = 'evidence' if payload.get('items') and search_attempts<=3 else 'validate'
                elif fname == 'add_evidence_fields':
                    evidence_attempts += 1
                    financial_phase = 'validate' if payload.get('ok') or evidence_attempts >= 2 else 'evidence'
                elif fname == 'validate_financial_result' and payload.get('ok'):
                    unresolved = set(payload.get('missing_fields') or []) - searched_fields
                    financial_phase = 'search' if unresolved and evidence_attempts < 2 and search_attempts<3 else 'export'
                elif fname == 'export_financial_result' and payload.get('ok'):
                    exported_attachment_ids.add(run_attachment_ids.get(payload.get('run_id')))
                    remaining = sorted(required_attachment_ids - exported_attachment_ids)
                    financial_phase = 'extract' if remaining else ('analyze' if intent in ('analysis','valuation') else 'finish')
                    if remaining:
                        payload['remaining_attachment_ids'] = remaining
                        payload['next_actions'] = '本轮还有附件未处理，请用这些ID调用extract_financial_document，逐个核验并导出后再最终回答。'
                        result = json.dumps(payload, ensure_ascii=False)
                    elif intent in ('analysis','valuation'):
                        payload['next_actions']='本轮事实提取已完成。请用全部ID调用analyze_financial_reports；之后若用户要估值则build_financial_valuation。'
                        payload['required_run_ids']=sorted({o['run_id'] for o in exported_results});result=json.dumps(payload,ensure_ascii=False)
                elif fname in research_agent.NAMES and payload.get('ok'):
                    research_results=[r for r in research_results if r['mode']!=payload['mode']]+[payload]
                    financial_phase='value' if fname=='analyze_financial_reports' and intent=='valuation' else 'finish'
            if fname not in FINANCIAL_NAMES:
                result = result[:MAX_TOOL_RESULT]
            db.add_message(task_id, "tool", result,
                           tool_calls={"tool_call_id": call.get("id", ""), "name": fname})
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            yield {"type": "tool_end", "name": fname, "ms": ms,
                   "ok": not result.startswith("错误：") and (fname not in FINANCIAL_NAMES or json.loads(result).get('ok', False)),
                   "preview": result[:600], "id": call.get("id", "")}

        if step == hard_cap - 2:
            yield {"type": "status",
                   "text": f"任务接近执行上限（最多 {hard_cap} 步），正在检查剩余工作…"}
            messages.append({"role": "user", "content":
                f"（系统提示：本任务已连续执行 {step} 步工具调用。请继续完成未竟事项；"
                "如果信息已足够得出结论，请直接输出最终回答，不要再调用工具。）"})

    if financial_mode:
        content=(await final_delivery('本轮达到处理步数上限，尚未完成的内容需要继续处理；不把当前结果标为完整完成'))['content']
        yield {'type':'delta','text':content};yield {'type':'done','text':content,'message':{'role':'assistant','content':content},'partial':True,'usage':total_usage};return
    note = (f"已连续执行 {hard_cap} 步仍未完成，为保护资源已暂停。"
            "请发送「继续」接续当前进度（上下文完整保留）。")
    db.add_message(task_id, "assistant", note)
    yield {"type": "error", "error": note, "usage": total_usage}


async def _auto_title(base_url: str, api_key: str, model: str,
                      text: str, attachments: list[dict]) -> str:
    src = (text or "").strip()
    if not src and attachments:
        src = "分析文件：" + "、".join(a["name"] for a in attachments[:3])
    if not src:
        return ""
    try:
        title = await asyncio.wait_for(complete(
            base_url, api_key, model,
            [{"role": "system", "content": "用不超过 12 个汉字概括用户意图，直接输出标题，不要引号和标点。"},
             {"role": "user", "content": src[:800]}],
            temperature=0.2, max_tokens=32), timeout=10)
        title = title.strip().strip('"“”').split("\n")[0][:20]
        return title or src[:14]
    except Exception:
        return src[:14]
