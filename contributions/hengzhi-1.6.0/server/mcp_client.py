"""MCP（Model Context Protocol）客户端 —— stdio 传输实现。

不依赖任何第三方 MCP 库，直接按 JSON-RPC 2.0 协议与 MCP 服务器进程通信：
    启动子进程 → initialize → notifications/initialized → tools/list → tools/call
消息采用 MCP stdio 传输标准的「按行分隔 JSON」格式。

设计取舍：服务器按需懒启动、失败不影响主程序（只会把错误状态展示在设置页），
这样即使某个 MCP 服务器配置有误，聊天功能依然可用。
"""

import asyncio
import json
import os
import shutil
import signal
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CALL_TIMEOUT = 60.0
START_TIMEOUT = 30.0

# MCP 服务器会把正常进度日志写到 stderr（FastMCP 每处理一个请求都会打一行
# "Processing request of type CallToolRequest"）。这些不是错误，绝不能写进
# client.error，否则界面会在服务完全健康时显示一条红色假报错，看起来就像
# "连上了却不能用"。只有真正像错误的行才写 error，其余进 log_tail。
_BENIGN_PREFIXES = ("processing request of type", "info:", "debug:", "warning:")
_ERROR_MARKERS = ("error", "traceback", "exception", "failed", "fatal",
                  "not found", "refused", "denied", "cannot", "unable to")


def _looks_like_error(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low or low.startswith(_BENIGN_PREFIXES):
        return False
    if "incompletefielddefinitionwarning" in low:
        return False
    return any(marker in low for marker in _ERROR_MARKERS)


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None,
                 env: dict | None = None, cwd: str | None = None):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict] = []
        self.status = "未启动"
        self.error = ""
        self.log_tail = ""            # 服务器 stderr 的最后一行（诊断用，不是错误）
        self._ready = False           # initialize 握手是否已完成
        self._stopping = False
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self.server_info: dict = {}

    # ------------------------------------------------------------- 生命周期

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        """启动服务器并完成 initialize 握手。

        关键：方法返回时 `_ready` 一定为 True。旧实现只要进程 spawn 成功就
        `running=True`，调用方据此判断"已启动"并立刻去 tools/list，而握手尚未
        完成时服务器会拒绝该请求 —— 工具就"凭空消失"了。现在用 _lock 把整个
        握手串行化，且只有握手成功才置 `_ready`。
        """
        async with self._lock:
            if self.running and self._ready and not self._stopping:
                return
            self._stopping = False
            if self.running:            # 上一次启动中途失败 / 残留进程
                await self._terminate_proc()
            exe = shutil.which(self.command) or self.command
            env = os.environ.copy()
            env.update({k: str(v) for k, v in self.env.items()})
            self._ready = False
            self.status = "启动中"
            # Cancellation must not discard an already-created process before
            # asyncio returns its handle to us.
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                exe, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env, cwd=self.cwd or None, start_new_session=True,
            ))
            try:
                self.proc = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                await self._cancel_startup(spawn)
                raise
            except FileNotFoundError:
                self.status = "启动失败"
                self.error = f"找不到可执行文件：{self.command}（请检查是否已安装，或填写绝对路径）"
                raise MCPError(self.error)
            except Exception as e:
                self.status = "启动失败"
                self.error = f"{type(e).__name__}: {e}"
                raise MCPError(self.error)

            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            try:
                init = await asyncio.wait_for(self.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
                    "clientInfo": {"name": "local-finance-agent", "version": "1.0.0"},
                }), timeout=START_TIMEOUT)
                self.server_info = (init or {}).get("serverInfo", {}) or {}
                await self.notify("notifications/initialized", {})
                await self.refresh_tools()
                self._ready = True
                self.status = "已连接"
                self.error = ""
                # 握手期 stderr 里的噪声（pydantic 警告等）不应长期留在 error 上
                if self.error and not _looks_like_error(self.error):
                    self.error = ""
            except asyncio.CancelledError:
                await self._cancel_startup(spawn, self._reader_task, self._stderr_task)
                raise
            except asyncio.TimeoutError:
                self.status = "启动超时"
                self.error = "服务器未在 30 秒内完成 initialize 握手"
                await self._terminate_proc()
                raise MCPError(self.error)
            except MCPError:
                self.status = "启动失败"
                await self._terminate_proc()
                raise
            except Exception as e:
                self.status = "启动失败"
                self.error = f"{type(e).__name__}: {e}"
                await self._terminate_proc()
                raise MCPError(self.error)

    async def _cancel_startup(self, spawn, reader=None, stderr=None):
        """Own cancelled startup resources; ready shared clients never use this."""
        async def cleanup():
            try:
                proc = await spawn
            except Exception:
                self.status = "已停止"
                return
            self._stopping = True
            tasks = [task for task in (reader, stderr) if task is not None]
            for task in tasks:
                task.cancel()
            # Joining readers first prevents their EOF handler from changing
            # the status or pending requests after a subsequent startup.
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._reader_task is reader:
                self._reader_task = None
            if self._stderr_task is stderr:
                self._stderr_task = None
            if self.proc is proc:
                self.proc = None
            self._ready = False
            self.tools = []
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            await asyncio.sleep(.1)
            # Descendants may remain after the server itself exits.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=.2)
            except asyncio.TimeoutError:
                pass
            self._stopping = False
            self.status = "已停止"
            self.error = ""

        owned_cleanup = asyncio.create_task(cleanup())
        while not owned_cleanup.done():
            try:
                await asyncio.shield(owned_cleanup)
            except asyncio.CancelledError:
                continue
        await owned_cleanup

    async def ensure_ready(self, timeout: float = START_TIMEOUT):
        """供 tools/list、tools/call 等入口复用的就绪保证。"""
        if self.running and self._ready:
            return
        try:
            await asyncio.wait_for(self.start(), timeout=timeout + 5)
        except asyncio.TimeoutError:
            self.status = "启动超时"
            self.error = self.error or "等待 MCP 服务器就绪超时"
            raise MCPError(self.error)

    async def _terminate_proc(self):
        """只负责杀掉进程，不改变对外状态（供 start/stop 复用）。"""
        proc, self.proc = self.proc, None
        self._ready = False
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self.tools = []

    async def stop(self):
        async with self._lock:
            self._stopping = True
            for t in (self._reader_task, self._stderr_task):
                if t and not t.done():
                    t.cancel()
            self._reader_task = self._stderr_task = None
            await self._terminate_proc()
            self._stopping = False
            self.status = "已停止"

    # ------------------------------------------------------------- 通信

    async def _read_loop(self):
        assert self.proc and self.proc.stdout
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            if text.startswith("Content-Length"):
                # 兼容 LSP 风格帧（少数实现）：跳过头部另读正文
                try:
                    length = int(text.split(":")[1].strip())
                    await self.proc.stdout.readline()
                    body = await self.proc.stdout.readexactly(length)
                    text = body.decode("utf-8", errors="replace")
                except Exception:
                    continue
            try:
                msg = json.loads(text)
            except Exception:
                continue
            mid = msg.get("id")
            if mid is not None and mid in self._pending:
                fut = self._pending.pop(mid)
                if not fut.done():
                    if "error" in msg:
                        fut.set_exception(MCPError(str(msg["error"].get("message", msg["error"]))))
                    else:
                        fut.set_result(msg.get("result"))
        # 进程结束，唤醒所有等待者
        self._ready = False
        if not self._stopping:
            self.status = "已断开"
            if not self.error:
                self.error = "MCP 服务器进程已退出"
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("MCP 服务器连接已断开"))
        self._pending.clear()

    async def _drain_stderr(self):
        assert self.proc and self.proc.stderr
        while True:
            try:
                line = await self.proc.stderr.readline()
            except (asyncio.CancelledError, Exception):
                break
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()[:400]
            if not text:
                continue
            self.log_tail = text                 # 永远保留，供诊断展示
            if _looks_like_error(text):
                self.error = text                # 只有真错误才污染 error

    async def _send(self, payload: dict):
        if not self.proc or not self.proc.stdin:
            raise MCPError("MCP 服务器未启动")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        # 并发调用时不同请求的 JSON 行不能交错写入，否则整条流报废
        async with self._write_lock:
            self.proc.stdin.write(data)
            try:
                await self.proc.stdin.drain()
            except Exception as e:
                raise MCPError(f"写入失败：{e}") from e

    async def notify(self, method: str, params: dict):
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(self, method: str, params: dict, timeout: float = CALL_TIMEOUT) -> Any:
        self._id += 1
        mid = self._id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise MCPError(f"调用 {method} 超时（{timeout:.0f}s）")
        finally:
            self._pending.pop(mid, None)

    # ------------------------------------------------------------- 工具

    async def refresh_tools(self) -> list[dict]:
        res = await self.request("tools/list", {})
        self.tools = (res or {}).get("tools", []) or []
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        await self.ensure_ready()
        if not self.tools:
            await self.refresh_tools()
        res = await self.request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        return res or {}

    def status_dict(self) -> dict:
        return {
            "name": self.name, "command": self.command, "args": self.args, "env": self.env,
            "status": self.status, "error": self.error, "log_tail": self.log_tail,
            "ready": self._ready,
            "running": self.running,
            "server_info": self.server_info,
            "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:200],
                       "schema": t.get("inputSchema") or {}} for t in self.tools],
        }

    def health(self) -> str:
        """给出一句人话结论，供界面/模型直接展示，避免用户对着一堆状态猜。"""
        if self.running and self._ready and self.tools:
            return f"正常：已连接，{len(self.tools)} 个工具可用"
        if self.running and self._ready:
            return "已连接，但该服务器没有暴露任何工具"
        if self.status == "启动中":
            return "正在启动并握手…"
        if self.error:
            return f"异常：{self.error}"
        return self.status


class MCPManager:
    """按配置管理多个 MCP 服务器，工具统一暴露给 Agent。"""

    TOOL_PREFIX = "mcp"

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}
        # 最近一次 all_tools 里没能提供的服务器及原因；空 = 全部正常。
        # 存在的意义：旧实现对失败 `except: continue` 静默吞掉，工具凭空消失
        # 而用户和模型都不知道，只能笼统地感觉"这个 MCP 用不了"。
        self.last_issues: dict[str, str] = {}

    def sync_from_config(self, servers: list[dict]):
        wanted = {s.get("name"): s for s in (servers or []) if s.get("name")}
        for name in list(self.clients):
            if name not in wanted:
                client = self.clients.pop(name)
                asyncio.create_task(client.stop())
        for name, cfg in wanted.items():
            c = self.clients.get(name)
            if c is None:
                self.clients[name] = MCPClient(
                    name=name, command=cfg.get("command", ""),
                    args=cfg.get("args", []), env=cfg.get("env", {}),
                    cwd=cfg.get("cwd") or None,
                )
            else:
                if (c.command != cfg.get("command", "") or c.args != list(cfg.get("args", []))
                        or c.env != dict(cfg.get("env", {}))):
                    c.command = cfg.get("command", "")
                    c.args = list(cfg.get("args", []))
                    c.env = dict(cfg.get("env", {}))
                    c.status = "配置已变更，需重启"
                    c.tools = []

    def get(self, name: str) -> MCPClient | None:
        return self.clients.get(name)

    async def start_enabled(self, enabled_names: list[str]) -> dict:
        result = {}
        for name in enabled_names:
            c = self.clients.get(name)
            if not c:
                result[name] = "未配置"
                continue
            if c.running and c._ready:
                result[name] = c.status
                continue
            try:
                await c.ensure_ready()
            except Exception as e:
                if not c.error:
                    c.error = f"{type(e).__name__}: {e}"
            result[name] = c.status
        return result

    async def stop_all(self):
        for c in self.clients.values():
            await c.stop()

    async def all_tools(self, enabled_names: list[str]) -> list[dict]:
        """收集启用的 MCP 工具，转换成 OpenAI function calling 格式。

        与旧实现的关键差别：任何一步失败都会把原因写进 client.error 与
        manager.last_issues 并记录到日志，不再静默丢弃工具。
        """
        async def collect(name):
            c = self.clients.get(name)
            if not c:
                return [], "未在配置中找到该服务器"
            if not c.running or not c._ready:
                try:
                    await c.ensure_ready()
                except Exception as e:
                    reason = c.error or f"{type(e).__name__}: {e}"
                    c.error = reason
                    return [], f"启动失败：{reason}"
            if not c.tools:
                try:
                    await c.refresh_tools()
                except Exception as e:
                    reason = c.error or f"{type(e).__name__}: {e}"
                    c.error = reason
                    return [], f"读取工具列表失败：{reason}"
            if not c.tools:
                return [], "已连接但未返回任何工具"
            out = []
            for t in c.tools:
                tname = t.get("name")
                if not tname:
                    continue
                schema = dict(t.get("inputSchema") or {"type": "object", "properties": {}})
                schema.setdefault("type", "object")
                out.append({
                    "type": "function",
                    "function": {
                        "name": f"{self.TOOL_PREFIX}__{name}__{tname}",
                        "description": f"[MCP:{name}] {(t.get('description') or tname)[:900]}",
                        "parameters": schema,
                    },
                    "_mcp": {"server": name, "tool": tname},
                })
            return out, None

        # Different servers are independent; keep configured tool order stable.
        # Cancel siblings as well if the caller cancels discovery.
        names = list(dict.fromkeys(enabled_names))
        pending = [asyncio.create_task(collect(name)) for name in names]
        try:
            results = await asyncio.gather(*pending)
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.last_issues = {name: issue for name, (_, issue) in zip(names, results) if issue}
        return [tool for tools, _ in results for tool in tools]

    def issues_text(self) -> str:
        """把 last_issues 汇总成一行，便于塞进对话或界面提示。"""
        return "；".join(f"{n}：{r}" for n, r in self.last_issues.items())

    @staticmethod
    def parse_tool_name(full_name: str) -> tuple[str, str] | None:
        parts = (full_name or "").split("__", 2)
        if len(parts) == 3 and parts[0] == MCPManager.TOOL_PREFIX:
            return parts[1], parts[2]
        return None

    async def call(self, server: str, tool: str, arguments: dict) -> str:
        c = self.clients.get(server)
        if not c:
            return f"错误：MCP 服务器 {server} 未配置"
        try:
            res = await c.call_tool(tool, arguments)
        except MCPError as e:
            return f"错误：{e}"
        except Exception as e:
            return f"错误：{type(e).__name__}: {e}"
        return _render_mcp_result(res)

    @staticmethod
    def is_mcp_tool(full_name: str) -> bool:
        return MCPManager.parse_tool_name(full_name) is not None


def _render_mcp_result(res: dict) -> str:
    if not isinstance(res, dict):
        return str(res)
    content = res.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            if item.get("type") == "text":
                parts.append(item.get("text") or "")
            elif item.get("type") == "image":
                parts.append(f"[图片返回，{item.get('mimeType', 'image')}]")
            elif item.get("type") == "resource":
                r = item.get("resource") or {}
                parts.append(r.get("text") or f"[资源 {r.get('uri', '')}]")
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False)[:4000])
    if res.get("isError"):
        parts.insert(0, "（工具返回了错误）")
    structured = res.get("structuredContent")
    if structured and not parts:
        parts.append(json.dumps(structured, ensure_ascii=False)[:4000])
    text = "\n".join(p for p in parts if p)
    return text[:12000] if text else "(工具无返回内容)"


manager = MCPManager()
