"""MCP startup cancellation owns its subprocess without stopping shared clients."""

import asyncio
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.mcp_client import MCPClient


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class MCPCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    async def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            await asyncio.sleep(.005)
        self.assertTrue(predicate(), "subprocess did not reach the expected state")

    def sleeping_client(self):
        marker = self.root / "server.pid"
        source = (
            "import os, signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(20)"
        )
        return MCPClient("temporary-test-server", sys.executable, ["-u", "-c", source]), marker

    async def emergency_cleanup(self, client, proc=None):
        proc = proc or client.proc
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        for task in (client._reader_task, client._stderr_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_initialize_kills_server_and_finishes_readers(self):
        client, marker = self.sleeping_client()
        startup = asyncio.create_task(client.start())
        proc = None
        try:
            await self.wait_for(marker.exists)
            proc = client.proc
            readers = [client._reader_task, client._stderr_task]
            self.assertTrue(all(readers))
            start = time.monotonic()
            startup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await startup
            self.assertLess(time.monotonic() - start, .7)
            self.assertFalse(alive(int(marker.read_text())), "cancelled startup left its server running")
            self.assertIsNone(client.proc)
            self.assertFalse(client._ready)
            self.assertTrue(all(task.done() for task in readers))
            self.assertIsNone(client._reader_task)
            self.assertIsNone(client._stderr_task)
            self.assertFalse(client._pending)
        finally:
            if not startup.done():
                startup.cancel()
                await asyncio.gather(startup, return_exceptions=True)
            await self.emergency_cleanup(client, proc)

    async def test_cancel_while_spawn_handle_is_pending_does_not_lose_process(self):
        client, marker = self.sleeping_client()
        original = asyncio.create_subprocess_exec
        spawned, release = asyncio.Event(), asyncio.Event()
        processes = []

        async def delayed_spawn(*args, **kwargs):
            proc = await original(*args, **kwargs)
            processes.append(proc)
            spawned.set()
            await release.wait()
            return proc

        with patch.object(asyncio, "create_subprocess_exec", delayed_spawn):
            startup = asyncio.create_task(client.start())
            try:
                await spawned.wait()
                await self.wait_for(marker.exists)
                startup.cancel()
                await asyncio.sleep(.01)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await startup
                self.assertFalse(alive(processes[0].pid), "cancellation lost the spawned process handle")
                self.assertIsNone(client.proc)
                self.assertIsNone(client._reader_task)
                self.assertIsNone(client._stderr_task)
            finally:
                release.set()
                if not startup.done():
                    startup.cancel()
                    await asyncio.gather(startup, return_exceptions=True)
                await self.emergency_cleanup(client, processes[0] if processes else None)

    async def test_cancel_tool_wait_keeps_ready_shared_server_running(self):
        marker = self.root / "tool.received"
        source = "\n".join([
            "import json, sys, time",
            "from pathlib import Path",
            "for line in sys.stdin:",
            " request=json.loads(line)",
            " method=request.get('method')",
            " if method=='tools/call':",
            f"  Path({str(marker)!r}).touch()",
            "  time.sleep(20)",
            " if 'id' not in request: continue",
            " result={'tools':[{'name':'wait','inputSchema':{'type':'object'}}]} if method=='tools/list' else {}",
            " print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)",
        ])
        client = MCPClient("shared-test-server", sys.executable, ["-u", "-c", source])
        request = None
        try:
            await client.start()
            proc = client.proc
            request = asyncio.create_task(client.call_tool("wait", {}))
            await self.wait_for(marker.exists)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertIs(client.proc, proc)
            self.assertTrue(client.running)
            self.assertTrue(client._ready)
            self.assertFalse(client._pending)
        finally:
            if request and not request.done():
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await self.emergency_cleanup(client)


if __name__ == "__main__":
    unittest.main()
