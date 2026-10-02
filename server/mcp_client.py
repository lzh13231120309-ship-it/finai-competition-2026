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
from typing import Any

PROTOCOL_VERSION = "2024-11-05"
CALL_TIMEOUT = 60.0
START_TIMEOUT = 30.0


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
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.server_info: dict = {}

    # ------------------------------------------------------------- 生命周期

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self):
        async with self._lock:
            if self.running:
                return
            exe = shutil.which(self.command) or self.command
            env = os.environ.copy()
            env.update({k: str(v) for k, v in self.env.items()})
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    exe, *self.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env, cwd=self.cwd or None,
                )
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
                self.status = "已连接"
                self.error = ""
            except asyncio.TimeoutError:
                self.status = "启动超时"
                self.error = "服务器未在 30 秒内完成 initialize 握手"
                await self.stop()
                raise MCPError(self.error)

    async def stop(self):
        async with self._lock:
            for t in (self._reader_task, self._stderr_task):
                if t and not t.done():
                    t.cancel()
            self._reader_task = self._stderr_task = None
            proc, self.proc = self.proc, None
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self.status = "已停止"
            self.tools = []

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
        self.status = "已断开"
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
            # 仅保留最后一行作为诊断信息
            self.error = line.decode("utf-8", errors="replace").strip()[:400]

    async def _send(self, payload: dict):
        if not self.proc or not self.proc.stdin:
            raise MCPError("MCP 服务器未启动")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
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
        await self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise MCPError(f"调用 {method} 超时（{timeout:.0f}s）")

    # ------------------------------------------------------------- 工具

    async def refresh_tools(self) -> list[dict]:
        res = await self.request("tools/list", {})
        self.tools = (res or {}).get("tools", []) or []
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        if not self.running:
            await self.start()
        res = await self.request("tools/call", {"name": tool_name, "arguments": arguments or {}})
        return res or {}

    def status_dict(self) -> dict:
        return {
            "name": self.name, "command": self.command, "args": self.args, "env": self.env,
            "status": self.status, "error": self.error, "running": self.running,
            "server_info": self.server_info,
            "tools": [{"name": t.get("name"), "description": (t.get("description") or "")[:200],
                       "schema": t.get("inputSchema") or {}} for t in self.tools],
        }


class MCPManager:
    """按配置管理多个 MCP 服务器，工具统一暴露给 Agent。"""

    TOOL_PREFIX = "mcp"

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}

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
                continue
            if c.running:
                result[name] = c.status
                continue
            try:
                await c.start()
            except Exception:
                pass
            result[name] = c.status
        return result

    async def stop_all(self):
        for c in self.clients.values():
            await c.stop()

    async def all_tools(self, enabled_names: list[str]) -> list[dict]:
        """收集启用的 MCP 工具，转换成 OpenAI function calling 格式。"""
        out = []
        for name in enabled_names:
            c = self.clients.get(name)
            if not c:
                continue
            if not c.running:
                try:
                    await c.start()
                except Exception:
                    continue
            if not c.tools:
                try:
                    await c.refresh_tools()
                except Exception:
                    continue
            for t in c.tools:
                tname = t.get("name")
                if not tname:
                    continue
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                if "type" not in schema:
                    schema["type"] = "object"
                out.append({
                    "type": "function",
                    "function": {
                        "name": f"{self.TOOL_PREFIX}__{name}__{tname}",
                        "description": f"[MCP:{name}] {(t.get('description') or tname)[:900]}",
                        "parameters": schema,
                    },
                    "_mcp": {"server": name, "tool": tname},
                })
        return out

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
