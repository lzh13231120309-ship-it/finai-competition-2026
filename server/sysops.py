"""本机系统操作工具：读写文件、列目录、执行命令、删除（移入回收站）。

安全设计（放权但不失控）：
1. **默认只读**：`tools_enabled.system_access` 为 false 时，本模块的工具完全不会暴露给模型。
2. **删除走回收站**：不提供 rm/del，删除 = 移动到废纸篓（Windows 版为 data/回收站），可反悔；
   系统关键路径直接拒绝。
3. **灾难命令拦截**：rm -rf /、sudo、dd、mkfs、diskutil、shutdown，
   以及 Windows 的 del /s、rd /s /q、format、diskpart、bcdedit、vssadmin 等不可逆操作直接拒绝。
4. **全程审计**：每一次文件写入、命令执行、删除都追加写入 logs/audit.jsonl，可追溯。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config

IS_WINDOWS = os.name == "nt"

# 给模型看的环境示例：路径写法、shell、python 解释器命令随平台变化，
# 否则模型会照抄 macOS 的写法（/Users/xxx/Desktop、python3），在本机全部失败。
if IS_WINDOWS:
    PATH_EXAMPLE = r"C:\Users\Public\Desktop"
    SHELL_NAME = "cmd"
    PYTHON_CMD = "python"
else:
    PATH_EXAMPLE = "/Users/Public/Desktop"
    SHELL_NAME = "shell"
    PYTHON_CMD = "python3"

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

# Windows 上等价的高危操作（原版只挡了类 Unix 命令，在 Windows 上形同虚设）
BLOCKED_CMD_PATTERNS += [
    (r"\b(rd|rmdir)\b[^\r\n]*?/s\b[^\r\n]*?\s[a-zA-Z]:\\?\s*(\*|\.\*)?\s*$", "递归删除整个磁盘/系统目录"),
    (r"\b(rd|rmdir)\b[^\r\n]*?\s[a-zA-Z]:\\(windows|users|program files( \(x86\))?)\\?\s*$", "递归删除系统/用户根目录"),
    (r"\b(del|erase)\b[^\r\n]*?/[sfq]+\b[^\r\n]*?\s[a-zA-Z]:\\(\*\.?\*?)?\s*$", "强制递归删除整个磁盘"),
    (r"\b(del|erase)\b[^\r\n]*?\s[a-zA-Z]:\\(windows|users|program files( \(x86\))?)\\[\*\.\s]*$", "删除系统/用户根目录全部内容"),
    (r"\bformat\s+[a-zA-Z]:", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区操作"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\bvssadmin\s+delete\b|\bwbadmin\s+delete\b", "删除系统备份/卷影副本"),
    (r"\bcipher\s+/w\b", "擦除磁盘剩余空间"),
    (r"\btakeown\b|\bicacls\b[^\r\n]*?/grant", "夺取系统文件所有权"),
    (r"\breg\s+delete\s+HKLM", "删除系统注册表项"),
    (r"\bdel\b[^\r\n]*?%(systemroot|windir|userprofile|appdata)%", "删除系统/用户关键目录"),
    (r"\b(rd|rmdir)\b[^\r\n]*?\s%?(systemroot|windir|userprofile)%?", "删除系统/用户关键目录"),
    (r"\\Windows\\System32\b", "直接操作 System32 目录"),
]

# 禁止移入废纸篓的关键路径（及其本身）
PROTECTED_PATHS = {
    "/", "/System", "/usr", "/bin", "/sbin", "/etc", "/var", "/private",
    "/Applications", "/Library", "/opt", "/tmp", "/Volumes",
    str(Path.home()), str(Path.home() / "Library"),
    str(Path.home() / "Desktop"), str(Path.home() / "Documents"),
    str(Path.home() / "Downloads"),
}
if os.name == "nt":
    PROTECTED_PATHS |= {"C:\\", "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)"}


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


def _decode_output(raw) -> str:
    """子进程输出解码。

    Windows 中文环境下命令输出可能是 UTF-8（Python 等）也可能是 GBK（cmd 内置命令、
    老工具），写死任何一种都会乱码，因此逐个尝试、最后兜底替换。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk", "gb18030", "cp936", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


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
    # 让 python 走 app 自带运行环境，保证 openpyxl / python-docx 等可直接用
    env = dict(os.environ)
    venv_bin = str(Path(sys.executable).parent)
    env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    env["FINAGENT_DATA_DIR"] = str(config.DATA_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # 子进程统一输出 UTF-8，配合下面的宽容解码，中文才不会乱码
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(workdir), env=env,
                           capture_output=True, timeout=timeout)
        out = _decode_output(r.stdout) + (
            ("\n" + _decode_output(r.stderr)) if r.stderr else "")
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


def move_to_trash(path: str) -> str:
    """删除 = 移入回收站（可恢复），并拒绝系统关键路径。"""
    p = _norm(path)
    if not p.exists():
        return f"错误：路径不存在 {p}"
    trash_label = "回收站" if IS_WINDOWS else "废纸篓"
    if str(p) in PROTECTED_PATHS or p in {Path(x) for x in PROTECTED_PATHS}:
        return f"错误：{p} 属于系统/家目录关键路径，禁止删除。"
    home = Path.home()
    if os.name == "nt":
        # 用路径层级判断，避免 "C:\Users\26503abc" 这种同前缀目录绕过限制
        try:
            p.relative_to(home)
            inside_home = True
        except ValueError:
            inside_home = False
        if not inside_home:
            return f"错误：{p} 不在可操作范围内（Windows 版仅限用户主目录 {home}）。"
    elif not (str(p).startswith(str(home)) or str(p).startswith("/tmp") or str(p).startswith("/Volumes")):
        return f"错误：{p} 不在可操作范围内（仅限家目录、/tmp、/Volumes）。"
    if len(p.parts) <= 2 and p.parent == home:
        return f"错误：{p} 是一级目录，禁止删除。"
    if os.name == "nt":
        # Windows 无 ~/.Trash：删除 = 移入数据目录下的「回收站」文件夹，同样可反悔
        trash = config.DATA_DIR / "回收站"
    else:
        trash = home / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / p.name
    if dest.exists():
        dest = trash / f"{p.name}-{time.strftime('%H%M%S')}"
    try:
        shutil.move(str(p), str(dest))
    except Exception as e:
        return f"错误：移入{trash_label}失败（{type(e).__name__}: {e}）"
    audit("move_to_trash", {"path": str(p), "trash_path": str(dest)})
    return f"已移入{trash_label}（可恢复）：{p} → {dest}"


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
                "properties": {"path": {"type": "string", "description": f"目录绝对路径，例如 {PATH_EXAMPLE}"}},
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
            "description": ("在本机执行 shell 命令（Windows 下即 cmd 命令）。用于：用 Python 生成 "
                            "Excel/Word/PPT/PDF/图表"
                            "（已装 openpyxl、python-docx、python-pptx、reportlab、matplotlib、pillow、pypdf）、"
                            f"格式转换、批量处理文件等。命令里请写 {PYTHON_CMD}，它会指向应用自带的运行环境；"
                            "环境未安装 pip，请直接使用上述已装好的库，不要尝试 pip install。"
                            "不可逆的高危命令（sudo、rm -rf /、format、diskpart、shutdown 等）会被拒绝。"),
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
            "description": ("删除文件或文件夹——实际是移入回收站，可以恢复。"
                            "只在用户主目录范围内生效，系统目录与一级目录会被拒绝。"),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "要删除的绝对路径"}},
                "required": ["path"],
            },
        },
    },
]
