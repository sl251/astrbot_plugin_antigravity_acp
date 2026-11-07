"""Local subprocess cleanup regressions; no AGY account or dependencies needed."""

import asyncio
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agy_provider.config import Settings
from agy_provider.process import (
    AGENT_NAME,
    LaunchInfo,
    launch_args,
    make_home,
    spawn,
    stop_process,
)
from agy_provider.sessions import Runtime, Session
from agy_provider.transport import AcpClient


class ProcessCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def child(self, ignore_term=False):
        code = (
            "import signal, time\n"
            + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
            + "print('ready', flush=True)\ntime.sleep(60)\n"
        )
        proc = await spawn([sys.executable, "-u", "-c", code])
        self.addAsyncCleanup(self.force_reap, proc)
        assert proc.stdout is not None
        self.assertEqual(await asyncio.wait_for(proc.stdout.readline(), 2), b"ready\n")
        return proc

    async def force_reap(self, proc):
        # A failing regression must not itself leave the deliberately stubborn child.
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.returncode is None:
            proc.kill()
        await asyncio.wait_for(proc.communicate(), 2)

    def short_grace(self, entered):
        real_wait_for = asyncio.wait_for

        async def wait_for(awaitable, timeout):
            if timeout == 3:
                # stop_process has sent TERM and is now waiting for graceful exit.
                entered.set()
                timeout = 0.15
            return await real_wait_for(awaitable, timeout)

        return patch("agy_provider.process.asyncio.wait_for", new=wait_for)

    def assert_reaped(self, proc, baseline):
        self.assertIsNotNone(proc.returncode)
        if os.name == "posix":
            with self.assertRaises(ProcessLookupError):
                os.kill(proc.pid, 0)
        pending = [task for task in asyncio.all_tasks() - baseline if not task.done()]
        self.assertEqual(pending, [], "cleanup returned with orphan asyncio tasks")

    async def test_normal_process_close(self):
        proc = await self.child()
        baseline = asyncio.all_tasks()
        await stop_process(proc)
        self.assert_reaped(proc, baseline)
        if os.name == "posix":
            self.assertEqual(proc.returncode, -signal.SIGTERM)

    async def test_already_exited_process_close_is_idempotent(self):
        proc = await spawn([sys.executable, "-c", "pass"])
        self.addAsyncCleanup(self.force_reap, proc)
        await asyncio.wait_for(proc.communicate(), 2)
        await stop_process(proc)
        await stop_process(proc)
        self.assertEqual(proc.returncode, 0)

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals/process groups")
    async def test_ignoring_term_process_is_killed(self):
        proc = await self.child(ignore_term=True)
        baseline = asyncio.all_tasks()
        with self.short_grace(asyncio.Event()):
            await stop_process(proc)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assert_reaped(proc, baseline)

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals/process groups")
    async def test_cancel_during_term_wait_reaps_before_propagating(self):
        proc = await self.child(ignore_term=True)
        baseline = asyncio.all_tasks()
        entered = asyncio.Event()
        with self.short_grace(entered):
            task = asyncio.create_task(stop_process(proc))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel("caller cancelled")
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(task.cancelled())
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assert_reaped(proc, baseline)

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals/process groups")
    async def test_repeated_cancellation_does_not_orphan_cleanup(self):
        proc = await self.child(ignore_term=True)
        baseline = asyncio.all_tasks()
        entered = asyncio.Event()
        with self.short_grace(entered):
            task = asyncio.create_task(stop_process(proc))
            await asyncio.wait_for(entered.wait(), 2)
            # Each cancel is delivered while the caller awaits the shielded cleanup.
            async with asyncio.timeout(2):
                for _ in range(4):
                    task.cancel("repeated caller cancellation")
                    await asyncio.sleep(0.005)
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(task.cancelled())
        self.assertGreaterEqual(task.cancelling(), 4)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assert_reaped(proc, baseline)

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals/process groups")
    async def test_runtime_close_during_idle_sweep_reaps_child(self):
        proc = await self.child(ignore_term=True)
        client = AcpClient(Settings())
        client.proc = proc
        session = Session(client=client, used_at=0)
        entered = asyncio.Event()
        real_sleep = asyncio.sleep

        async def fast_sweep_sleep(delay):
            # Only accelerate the sweeper's idle poll, not other scheduler yields.
            await real_sleep(0 if delay == 30 else delay)

        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Settings(idle_ttl=30), Path(root))
            runtime.sessions["idle"] = session
            self.addAsyncCleanup(runtime.close)
            baseline = asyncio.all_tasks()
            with (
                self.short_grace(entered),
                patch("agy_provider.sessions.asyncio.sleep", new=fast_sweep_sleep),
            ):
                runtime.sweeper = asyncio.create_task(runtime._sweep())
                await asyncio.wait_for(entered.wait(), 2)
                # close cancels the sweeper while it is inside session/client close.
                await asyncio.wait_for(runtime.close(), 2)
            self.assertTrue(runtime.sweeper.done())
            self.assertTrue(client.closed)
            self.assertIsNone(session.client)
            self.assertEqual(runtime.sessions, {})
            self.assertEqual(proc.returncode, -signal.SIGKILL)
            self.assert_reaped(proc, baseline)


class HomeLayoutTests(unittest.IsolatedAsyncioTestCase):
    """Agent selection per bridge mode, verified against the AGY 1.5.2 CLI."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_tools_mode_home_uses_workspace_agents_md(self):
        home = make_home(self.root, "PERSONA-X", tools_mode=True)
        agents = home / "workspace" / "AGENTS.md"
        settings = json.loads((home / "agy" / "settings.json").read_text())
        self.assertTrue(agents.is_file())
        self.assertIn("PERSONA-X", agents.read_text())
        self.assertIn("session MCP tools", agents.read_text())
        self.assertIn(
            "never run built-in shell or file tools", agents.read_text().lower()
        )
        self.assertEqual(
            settings, {"toolPermission": "request-review", "agentMode": ""}
        )
        # custom-agent plugin must not exist: with --agent active the CLI hides
        # session MCP tools from the LLM (real-CLI experiments, AGY 1.5.2).
        self.assertFalse((home / "config" / "plugins" / AGENT_NAME).exists())

    def test_plain_mode_home_keeps_restricted_agent(self):
        home = make_home(self.root, "PERSONA-Y", tools_mode=False)
        agent_md = (
            home / "config" / "plugins" / AGENT_NAME / "agents" / f"{AGENT_NAME}.md"
        )
        self.assertTrue(agent_md.is_file())
        self.assertIn("PERSONA-Y", agent_md.read_text())
        self.assertIn("tools: []", agent_md.read_text())
        self.assertFalse((home / "workspace" / "AGENTS.md").exists())

    def test_launch_args_drop_agent_flag_only_for_tools_mode(self):
        info = LaunchInfo("agy", "1.5.2", False)
        home = Path("/tmp/any")
        default = launch_args(info, home, use_default_agent=True)
        restricted = launch_args(info, home, use_default_agent=False)
        self.assertNotIn("--agent", default)
        self.assertIn("astrbot-chat", " ".join(restricted))
        for args in (default, restricted):
            self.assertEqual(args[args.index("--gemini-dir") + 1], str(home))
            self.assertEqual(args[args.index("--app-data-dir") + 1], "agy")


if __name__ == "__main__":
    unittest.main()
