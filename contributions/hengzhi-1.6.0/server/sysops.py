"""本机系统操作工具：读写文件、列目录、执行命令、删除（移入废纸篓）。

安全设计（放权但不失控）：
1. **默认只读**：`tools_enabled.system_access` 为 false 时，本模块的工具完全不会暴露给模型。
2. **删除走废纸篓**：不提供 rm，删除 = 移动到 ~/.Trash，可反悔；系统关键路径直接拒绝。
3. **灾难命令拦截**：rm -rf /、sudo、dd、mkfs、diskutil、shutdown 等不可逆操作直接拒绝。
4. **全程审计**：每一次文件写入、命令执行、删除都追加写入 logs/audit.jsonl，可追溯。
"""

import asyncio
import json
import locale
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config

MAX_READ_CHARS = 60000
MAX_CMD_OUTPUT = 20000
MAX_TIMEOUT = 300

# 不可逆的灾难性命令（拦截，不执行）
BLOCKED_CMD_PATTERNS = [
    (r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(/|~|\$HOME|/\*|\*)\s*$", "递归删除根目录/家目录"),
    (r"\bsudo\b", "提权操作 sudo"),
    (r"\bshutdown\b|\breboot\b|\bhalt\b", "关机/重启"),
    (r"\bmkfs\b|\bdiskutil\b|\bnewfs\b", "磁盘格式化/分区操作"),
    (r"\bdd\s+if=.*of=/dev/", "直接写磁盘设备"),
    (r">\s*/dev/disk|>\s*/dev/rdisk", "直接写磁盘设备"),
    (r"\bcsrutil\b|\bspctl\b|\bnvram\b", "修改系统安全设置"),
    (r"\brm\s+-[a-zA-Z]*\s*/System|\brm\s+-[a-zA-Z]*\s*/usr|\brm\s+-[a-zA-Z]*\s*/Applications", "删除系统目录"),
    (r":\(\)\s*\{.*\}\s*;", "fork 炸弹"),
]

# 禁止移入废纸篓的关键路径（及其本身）
PROTECTED_PATHS = {
    "/", "/System", "/usr", "/bin", "/sbin", "/etc", "/var", "/private",
    "/Applications", "/Library", "/opt", "/tmp", "/Volumes",
    str(Path.home()), str(Path.home() / "Library"),
    str(Path.home() / "Desktop"), str(Path.home() / "Documents"),
    str(Path.home() / "Downloads"),
}


# ------------------------------------------------------------------ 基础

def output_dir() -> Path:
    """模型生成文件的默认存放目录。"""
    raw = (config.load().get("default_output_dir") or "").strip()
    p = Path(raw).expanduser() if raw else (Path.home() / "Desktop" / "AI产出")
    p.mkdir(parents=True, exist_ok=True)
    return p


def audit(action: str, detail: dict) -> None:
    """追加一条审计记录（用于比赛要求的「过程可追溯」，也方便事后追责）。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **detail}
        line = json.dumps(rec, ensure_ascii=False)
        with open(config.LOG_DIR / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _expand(p: str) -> Path:
    return Path(str(p)).expanduser()


def _norm(p: str) -> Path:
    """展开并规范化路径（不要求存在）。"""
    path = _expand(p)
    try:
        return path.resolve()
    except Exception:
        return path.absolute()


def _too_big(n: int) -> str:
    return f"（内容过长，仅显示前 {n} 字符）"


# ------------------------------------------------------------------ 工具实现

def list_dir(path: str = "") -> str:
    p = _norm(path or str(Path.home()))
    if not p.exists():
        return f"错误：目录不存在 {p}"
    if not p.is_dir():
        return f"错误：不是目录 {p}"
    try:
        entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    except PermissionError as e:
        return f"错误：没有权限读取 {p}（{e}）"
    if not entries:
        return f"{p} 是空目录。"
    lines = [f"目录 {p} 共 {len(entries)} 项："]
    for e in entries[:400]:
        try:
            st = e.stat()
            if e.is_dir():
                lines.append(f"  [目录] {e.name}/")
            else:
                lines.append(f"  [文件] {e.name}  {st.st_size:,} 字节  "
                             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")
        except Exception:
            lines.append(f"  [?] {e.name}")
    if len(entries) > 400:
        lines.append(f"  …（其余 {len(entries) - 400} 项省略）")
    audit("list_dir", {"path": str(p), "count": len(entries)})
    return "\n".join(lines)


def read_file(path: str, offset: int = 0, limit: int = 20000) -> str:
    p = _norm(path)
    if not p.exists():
        return f"错误：文件不存在 {p}"
    if p.is_dir():
        return f"错误：{p} 是目录，请改用 list_dir 工具。"
    limit = min(max(int(limit or 20000), 500), MAX_READ_CHARS)
    offset = max(int(offset or 0), 0)
    size = p.stat().st_size
    if size > 8 * 1024 * 1024:
        return f"错误：文件过大（{size:,} 字节），本工具用于读取文本类文件。"
    if p.suffix.lower() in {".docx", ".xlsx", ".pptx", ".pdf"}:
        from . import parsers
        parsed = parsers.parse_file(p)
        text = parsed.get("text") or parsed.get("error") or "(无文本内容)"
    else:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"错误：无法读取 {p}（{type(e).__name__}: {e}）"
    piece = text[offset:offset + limit]
    tail = "" if offset + limit >= len(text) else \
        f"\n…（共 {len(text)} 字符，已读到 {offset + len(piece)}，可继续用 offset 读取）"
    audit("read_file", {"path": str(p), "offset": offset, "chars": len(piece)})
    return f"【{p} 第 {offset}-{offset + len(piece)} 字符】\n{piece}{tail}"


def write_file(path: str, content: str, overwrite: bool = False, append: bool = False) -> str:
    p = _norm(path)
    if p.exists() and p.is_dir():
        return f"错误：{p} 是目录。"
    if p.exists() and not (overwrite or append):
        return (f"错误：文件已存在 {p}。若确认要覆盖，请把 overwrite 设为 true；"
                f"若想改名另存，请换一个文件名。")
    if p.parent.exists() and not p.parent.is_dir():
        return f"错误：父路径不是目录 {p.parent}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if (append and p.exists()) else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"错误：写入失败（{type(e).__name__}: {e}）"
    size = p.stat().st_size
    audit("write_file", {"path": str(p), "bytes": size, "append": bool(append)})
    verb = "追加写入" if (append and mode == "a") else ("覆盖写入" if overwrite else "创建")
    return f"已{verb}：{p}（{size:,} 字节）"


def run_command(command: str, cwd: str = "", timeout: int = 60) -> str:
    cmd = (command or "").strip()
    if not cmd:
        return "错误：命令为空。"
    for pattern, why in BLOCKED_CMD_PATTERNS:
        if re.search(pattern, cmd, flags=re.IGNORECASE):
            audit("run_command_blocked", {"command": cmd, "reason": why})
            return (f"已拒绝执行：该命令属于不可逆的高危操作（{why}）。"
                    f"如确有需要，请由主人手动在终端执行。")
    timeout = min(max(int(timeout or 60), 1), MAX_TIMEOUT)
    workdir = _norm(cwd) if cwd else Path.home()
    if not workdir.exists():
        return f"错误：工作目录不存在 {workdir}"
    # 让 python / pip 走 app 自带的虚拟环境，保证 openpyxl / python-docx 等可直接用
    env = dict(os.environ)
    venv_bin = str(Path(sys.executable).parent)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    env["FINAGENT_DATA_DIR"] = str(config.DATA_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(workdir), env=env,
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        code = r.returncode
    except subprocess.TimeoutExpired:
        audit("run_command", {"command": cmd, "cwd": str(workdir), "timeout": timeout})
        return f"错误：命令超过 {timeout} 秒未结束，已终止。"
    except Exception as e:
        return f"错误：执行失败（{type(e).__name__}: {e}）"
    ms = int((time.time() - t0) * 1000)
    audit("run_command", {"command": cmd, "cwd": str(workdir), "exit": code, "ms": ms})
    out = out.strip() or "(无输出)"
    if len(out) > MAX_CMD_OUTPUT:
        out = out[:MAX_CMD_OUTPUT] + f"\n…（输出过长已截断，共 {len(out)} 字符）"
    return f"退出码 {code}｜耗时 {ms}ms｜工作目录 {workdir}\n{out}"


async def _stop_command_process(process, output_task) -> None:
    """Stop the session's process group, including children ignoring SIGTERM."""
    def send(sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    send(signal.SIGTERM)
    await asyncio.sleep(.1)
    # A shell may have exited already while its descendants remain alive.
    # Always signal the group rather than checking only process.returncode.
    send(signal.SIGKILL)
    done, pending = await asyncio.wait({output_task}, timeout=.2)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)


async def _finish_command_cleanup(process, output_task) -> bool:
    """Finish cleanup and report cancellation received while it was running."""
    cleanup = asyncio.create_task(_stop_command_process(process, output_task))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    await cleanup
    return cancelled


async def run_command_async(command: str, cwd: str = "", timeout: int = 60) -> str:
    """Execute without blocking the server; task cancellation stops subprocesses."""
    cmd = (command or "").strip()
    if not cmd:
        return "错误：命令为空。"
    for pattern, why in BLOCKED_CMD_PATTERNS:
        if re.search(pattern, cmd, flags=re.IGNORECASE):
            audit("run_command_blocked", {"command": cmd, "reason": why})
            return (f"已拒绝执行：该命令属于不可逆的高危操作（{why}）。"
                    f"如确有需要，请由主人手动在终端执行。")
    timeout = min(max(int(timeout or 60), 1), MAX_TIMEOUT)
    workdir = _norm(cwd) if cwd else Path.home()
    if not workdir.exists():
        return f"错误：工作目录不存在 {workdir}"
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["FINAGENT_DATA_DIR"] = str(config.DATA_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.time()
    process = None
    output_task = None
    # Shield process creation so cancellation cannot lose an already spawned
    # process before its handle is returned by asyncio.
    spawn_task = asyncio.create_task(asyncio.create_subprocess_shell(
        cmd, cwd=str(workdir), env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    ))
    try:
        process = await asyncio.shield(spawn_task)
        output_task = asyncio.create_task(process.communicate())
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output_task), timeout)
    except asyncio.CancelledError:
        if process is None:
            # Finish acquiring the handle even if cancellation raced with spawn.
            while not spawn_task.done():
                try:
                    await asyncio.shield(spawn_task)
                except asyncio.CancelledError:
                    continue
            try:
                process = spawn_task.result()
            except Exception:
                process = None
        if process is not None:
            if output_task is None:
                output_task = asyncio.create_task(process.communicate())
            await _finish_command_cleanup(process, output_task)
        audit("run_command", {"command": cmd, "cwd": str(workdir),
                              "cancelled": True, "ms": int((time.time() - t0) * 1000)})
        raise
    except asyncio.TimeoutError:
        cancelled = await _finish_command_cleanup(process, output_task)
        if cancelled:
            audit("run_command", {"command": cmd, "cwd": str(workdir),
                                  "cancelled": True, "ms": int((time.time() - t0) * 1000)})
            raise asyncio.CancelledError
        audit("run_command", {"command": cmd, "cwd": str(workdir), "timeout": timeout})
        return f"错误：命令超过 {timeout} 秒未结束，已终止。"
    except Exception as e:
        if process is not None:
            cancelled = await _finish_command_cleanup(process, output_task)
            if cancelled:
                audit("run_command", {"command": cmd, "cwd": str(workdir),
                                      "cancelled": True, "ms": int((time.time() - t0) * 1000)})
                raise asyncio.CancelledError
        return f"错误：执行失败（{type(e).__name__}: {e}）"
    encoding = locale.getpreferredencoding(False)
    out = stdout.decode(encoding, errors="replace")
    if stderr:
        out += "\n" + stderr.decode(encoding, errors="replace")
    code = process.returncode
    ms = int((time.time() - t0) * 1000)
    audit("run_command", {"command": cmd, "cwd": str(workdir), "exit": code, "ms": ms})
    out = out.strip() or "(无输出)"
    if len(out) > MAX_CMD_OUTPUT:
        out = out[:MAX_CMD_OUTPUT] + f"\n…（输出过长已截断，共 {len(out)} 字符）"
    return f"退出码 {code}｜耗时 {ms}ms｜工作目录 {workdir}\n{out}"


def move_to_trash(path: str) -> str:
    """删除 = 移入废纸篓（可恢复），并拒绝系统关键路径。"""
    p = _norm(path)
    if not p.exists():
        return f"错误：路径不存在 {p}"
    if str(p) in PROTECTED_PATHS or p in {Path(x) for x in PROTECTED_PATHS}:
        return f"错误：{p} 属于系统/家目录关键路径，禁止删除。"
    home = Path.home()
    if not (str(p).startswith(str(home)) or str(p).startswith("/tmp") or str(p).startswith("/Volumes")):
        return f"错误：{p} 不在可操作范围内（仅限家目录、/tmp、/Volumes）。"
    if len(p.parts) <= 2 and p.parent == home:
        return f"错误：{p} 是一级目录，禁止删除。"
    trash = home / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / p.name
    if dest.exists():
        dest = trash / f"{p.name}-{time.strftime('%H%M%S')}"
    try:
        shutil.move(str(p), str(dest))
    except Exception as e:
        return f"错误：移入废纸篓失败（{type(e).__name__}: {e}）"
    audit("move_to_trash", {"path": str(p), "trash_path": str(dest)})
    return f"已移入废纸篓（可恢复）：{p} → {dest}"


# ------------------------------------------------------------------ 工具定义

SYS_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": ("列出本机上某个目录的内容（文件名、大小、修改时间）。"
                            "path 留空则列出用户主目录。用来了解磁盘上有什么文件。"),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录绝对路径，如 /path/to/output"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": ("读取本机上某个文件的文本内容（支持 txt/md/csv/json 以及 docx/xlsx/pptx/pdf 的文本提取）。"
                            "大文件可用 offset/limit 分段读取。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "offset": {"type": "integer", "description": "从第几个字符开始，默认 0"},
                    "limit": {"type": "integer", "description": "最多读取字符数，默认 20000"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": ("在本机写文件（会在父目录不存在时自动创建）。"
                            "默认不覆盖已存在的文件；确需覆盖时把 overwrite 设为 true。"
                            "生成报告/数据时，除非用户指定了路径，否则写到默认产出目录。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目标文件的绝对路径"},
                    "content": {"type": "string", "description": "文件内容（文本）"},
                    "overwrite": {"type": "boolean", "description": "是否允许覆盖同名文件，默认 false"},
                    "append": {"type": "boolean", "description": "是否追加写入，默认 false"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": ("在本机执行 shell 命令。用于：用 Python 生成 Excel/Word/PPT/PDF/图表"
                            "（已装 openpyxl、python-docx、python-pptx、reportlab、matplotlib、pillow、pypdf）、"
                            "格式转换、批量处理文件等。命令里的 python3 会自动指向应用自带的虚拟环境。"
                            "不可逆的高危命令（sudo、rm -rf /、dd、mkfs、shutdown 等）会被拒绝。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "cwd": {"type": "string", "description": "工作目录，默认用户主目录"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 300"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to_trash",
            "description": ("删除文件或文件夹——实际是移入废纸篓，可以恢复。"
                            "只在家目录/tmp/Volumes 范围内生效，系统与一级目录会被拒绝。"),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "要删除的绝对路径"}},
                "required": ["path"],
            },
        },
    },
]
