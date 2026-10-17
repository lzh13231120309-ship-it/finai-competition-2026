"""Real subprocess regressions: responsive execution and process-tree cleanup."""

import asyncio
import json
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server import sysops


async def run_async(command, **kwargs):
    # Before the async implementation exists, exercise the old blocking path.
    # This makes the red run reproduce the user-visible event-loop stall.
    runner = getattr(sysops, "run_command_async", None)
    if runner is None:
        return sysops.run_command(command, **kwargs)
    return await runner(command, **kwargs)


def python_command(source):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class CommandCancellationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.logs = patch.object(sysops.config, "LOG_DIR", self.root)
        self.logs.start()

    def tearDown(self):
        self.logs.stop()
        self.temp.cleanup()

    def records(self):
        path = self.root / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_command_keeps_event_loop_responsive(self):
        task = asyncio.create_task(run_async(python_command("import time; time.sleep(.6)")))
        start = time.monotonic()
        await asyncio.sleep(.03)
        delay = time.monotonic() - start
        result = await task
        self.assertLess(delay, .25, "command execution blocked the event loop")
        self.assertIn("退出码 0", result)

    def process_tree_command(self):
        marker = self.root / "child.pid"
        child = (
            "import os, signal, time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(20)"
        )
        parent = (
            "import subprocess, sys; "
            f"subprocess.run([sys.executable, '-c', {child!r}])"
        )
        return python_command(parent), marker

    async def wait_for_child(self, marker):
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertTrue(marker.exists(), "subprocess did not start")
        return int(marker.read_text())

    async def wait_until_gone(self, pid):
        deadline = time.monotonic() + .5
        while alive(pid) and time.monotonic() < deadline:
            await asyncio.sleep(.01)
        self.assertFalse(alive(pid), "descendant survived command cleanup")

    async def test_task_cancel_kills_descendant_that_ignores_terminate(self):
        command, marker = self.process_tree_command()
        task = asyncio.create_task(run_async(command, timeout=1))
        pid = None
        try:
            pid = await self.wait_for_child(marker)
            start = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertLess(time.monotonic() - start, .5)
            await self.wait_until_gone(pid)
            self.assertTrue(any(row.get("cancelled") for row in self.records()))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if pid and alive(pid):
                os.kill(pid, signal.SIGKILL)

    async def test_timeout_kills_descendant_and_records_timeout(self):
        command, marker = self.process_tree_command()
        task = asyncio.create_task(run_async(command, timeout=1))
        pid = None
        try:
            pid = await self.wait_for_child(marker)
            start = time.monotonic()
            result = await task
            self.assertIn("超过 1 秒", result)
            self.assertLess(time.monotonic() - start, 1.5)
            await self.wait_until_gone(pid)
            self.assertEqual(self.records()[-1]["timeout"], 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if pid and alive(pid):
                os.kill(pid, signal.SIGKILL)

    async def test_cancel_during_timeout_cleanup_still_propagates_cancellation(self):
        marker = self.root / "terminate.received"
        source = (
            "import signal, time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, lambda *_: Path({str(marker)!r}).touch()); "
            "time.sleep(20)"
        )
        task = asyncio.create_task(run_async(python_command(source), timeout=1))
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(.002)
            self.assertTrue(marker.exists(), "timeout cleanup did not send SIGTERM")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(self.records()[-1].get("cancelled"))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_async_output_cwd_environment_and_audit_are_preserved(self):
        command = python_command(
            "import os, sys; print(os.getcwd()); "
            "print(os.environ['FINAGENT_DATA_DIR']); "
            "print('stderr-marker', file=sys.stderr); sys.exit(3)"
        )
        result = await run_async(command, cwd=str(self.root))
        self.assertIn("退出码 3", result)
        self.assertIn(str(self.root.resolve()), result)
        self.assertIn(str(sysops.config.DATA_DIR), result)
        self.assertIn("stderr-marker", result)
        self.assertEqual(self.records()[-1]["exit"], 3)

    async def test_async_rejects_high_risk_commands_without_execution(self):
        marker = self.root / "forbidden"
        result = await run_async(f"sudo touch {shlex.quote(str(marker))}")
        self.assertIn("已拒绝执行", result)
        self.assertFalse(marker.exists())
        self.assertEqual(self.records()[-1]["action"], "run_command_blocked")

    def test_sync_public_function_still_returns_output(self):
        result = sysops.run_command(python_command("print('sync-marker')"), cwd=str(self.root))
        self.assertIn("退出码 0", result)
        self.assertIn("sync-marker", result)


if __name__ == "__main__":
    unittest.main()
