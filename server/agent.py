"""Agent 核心：工具定义、提示词编排、流式执行循环。"""

import ast
import asyncio
import json
import math
import operator
import time
from pathlib import Path
from typing import AsyncGenerator

from . import config, db, knowledge, parsers, skills, sysops
from .llm import LLMError, complete, stream_chat
from .mcp_client import MCPManager, manager as mcp_manager

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

def build_system_prompt(task_id: str) -> str:
    cfg = config.load()
    parts = [cfg.get("system_prompt") or "你是一个专业、严谨的 AI 智能体助手。"]

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
    if (cfg.get("tools_enabled", {}) or {}).get("system_access"):
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


def _user_content(text: str, attachments: list[dict]) -> object:
    if not attachments:
        return text
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
    if not api_key and provider != "ollama":
        yield {"type": "error",
               "error": f"尚未填写「{provider}」的 API Key，请点击右上角设置 → 模型 → 填入并保存。"}
        return

    from .providers import resolve
    base_url, _ = resolve(provider, base_url)

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

    messages = [{"role": "system", "content": build_system_prompt(task_id)}]
    messages += _history_messages(task_id, int(cfg.get("context_rounds", 20)))
    if messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": _user_content(text, attachments)})
    else:
        messages[-1] = {"role": "user", "content": _user_content(text, attachments)}

    tools = local_tools(cfg)
    mcp_enabled = [s["name"] for s in (cfg.get("mcp_servers") or []) if s.get("enabled")]
    if mcp_enabled and (cfg.get("tools_enabled", {}) or {}).get("mcp", True):
        yield {"type": "status", "text": "正在连接 MCP 服务器…"}
        await mcp_manager.start_enabled(mcp_enabled)
        mcp_tools = await mcp_manager.all_tools(mcp_enabled)
        for t in mcp_tools:
            t.pop("_mcp", None)
        tools = tools + mcp_tools
    if tools:
        yield {"type": "tools_ready", "count": len(tools)}

    max_steps = int(cfg.get("agent_max_steps", 50))
    hard_cap = max_steps + 30  # 软上限到点后自动续跑，硬上限才真正停
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    full_text_parts: list[str] = []

    step = 0
    while step < hard_cap:
        if cancel_event and cancel_event.is_set():
            yield {"type": "cancelled"}
            return
        step += 1
        yield {"type": "step", "n": step, "max": hard_cap}

        step_text: list[str] = []
        step_reason: list[str] = []
        tool_calls: list[dict] = []
        try:
            async for ev in stream_chat(
                base_url, api_key, model, messages, tools,
                temperature=float(cfg.get("temperature", 0.3)),
                max_tokens=int(cfg.get("max_tokens") or 0) or None,
            ):
                if cancel_event and cancel_event.is_set():
                    break
                if ev["type"] == "text":
                    step_text.append(ev["text"])
                    yield {"type": "delta", "text": ev["text"]}
                elif ev["type"] == "reasoning":
                    step_reason.append(ev["text"])
                    yield {"type": "reasoning", "text": ev["text"]}
                elif ev["type"] == "tool_calls":
                    tool_calls = ev["tool_calls"]
                elif ev["type"] == "usage":
                    for k in total_usage:
                        total_usage[k] += int((ev["usage"] or {}).get(k) or 0)
        except LLMError as e:
            msg = f"模型调用失败：{e}"
            db.add_message(task_id, "assistant", f"⚠️ {msg}")
            yield {"type": "error", "error": msg}
            return
        except Exception as e:
            msg = f"运行出错：{type(e).__name__}: {e}"
            db.add_message(task_id, "assistant", f"⚠️ {msg}")
            yield {"type": "error", "error": msg}
            return

        content = "".join(step_text)
        if content:
            full_text_parts.append(content)

        if not tool_calls:
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
            yield {"type": "tool_start", "name": fname, "args": args,
                   "id": call.get("id", ""), "step": step + 1}
            t0 = time.time()
            if MCPManager.is_mcp_tool(fname):
                parsed = MCPManager.parse_tool_name(fname)
                result = await mcp_manager.call(parsed[0], parsed[1], args)
            else:
                result = await run_local_tool(fname, args, task_id)
            ms = int((time.time() - t0) * 1000)
            result = result[:MAX_TOOL_RESULT]
            db.add_message(task_id, "tool", result,
                           tool_calls={"tool_call_id": call.get("id", ""), "name": fname})
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            yield {"type": "tool_end", "name": fname, "ms": ms,
                   "ok": not result.startswith("错误："),
                   "preview": result[:600], "id": call.get("id", "")}

        # 软上限到点：不结束会话，注入提示自动续跑（模型仍可继续调工具）
        if step >= max_steps:
            yield {"type": "status",
                   "text": f"已连续执行 {step} 步，自动继续（上限 {hard_cap} 步）…"}
            messages.append({"role": "user", "content":
                f"（系统提示：本任务已连续执行 {step} 步工具调用。请继续完成未竟事项；"
                "如果信息已足够得出结论，请直接输出最终回答，不要再调用工具。）"})

    note = (f"已连续执行 {hard_cap} 步仍未完成，为保护资源已暂停。"
            "请发送「继续」接续当前进度（上下文完整保留）。")
    db.add_message(task_id, "assistant", note)
    yield {"type": "done", "message": {"role": "assistant", "content": note},
           "usage": total_usage, "text": "".join(full_text_parts)}


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
