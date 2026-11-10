"""Scoped native-runner regressions; fake host, real retained ACP lifecycle."""

import asyncio
import functools
import importlib
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_provider_media_tools import load_provider, tools

from agy_provider.config import Settings
from agy_provider.messages import normalize
from agy_provider.process import LaunchInfo
from agy_provider.sessions import Runtime
from agy_provider.tool_turn import chat_events
from agy_provider.tools import ToolSnapshot


@dataclass
class Request:
    session_id: str = ""
    conversation_id: str | None = None
    model: str = "original-model"
    func_tool: object = field(default_factory=object)
    contexts: list = field(default_factory=list)


class FakeRunner:
    async def reset(
        self,
        provider,
        request,
        run_context=None,
        tool_executor=None,
        agent_hooks=None,
        streaming=False,
        tool_schema_mode="full",
        **kwargs,
    ):
        self.provider, self.req = provider, request
        self.run_context = run_context
        self._abort_signal = asyncio.Event()
        self.tool_schema_mode = tool_schema_mode
        self.seen_kwargs = kwargs
        if tool_schema_mode == "skills_like":
            request.func_tool = "light"

    async def step_until_done(self, max_step):
        try:
            if getattr(self, "error", None):
                raise self.error
            yield "native-result"
            if getattr(self, "block", False):
                self.started.set()
                await asyncio.Event().wait()
        finally:
            self.inner_closed = True


class FakeTurn:
    def __init__(self, signal, runtime=None, key=None):
        self.signal, self.runtime, self.key = signal, runtime, key
        self.closed = 0

    async def close(self):
        await asyncio.sleep(0)
        self.closed += 1
        if self.runtime and self.runtime.turns.get(self.key) is self:
            self.runtime.turns.pop(self.key)


class HostCompatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider_module = load_provider(Path(self.temp.name))
        # No production AstrBot package is installed here. Only the runner seam
        # is fake; OWNER comes from the actual provider source under test.
        self.compat = importlib.import_module("agy_provider.host_compat")
        self.original_reset = FakeRunner.reset
        self.original_step = FakeRunner.step_until_done
        self.compat.install_host_compat(FakeRunner, owner=self.provider_module.OWNER)
        self.provider = SimpleNamespace(
            agy_plugin_owner=self.provider_module.OWNER,
            runtime=SimpleNamespace(turns={}),
            provider_config={"model": "provider-model"},
        )

    async def asyncTearDown(self):
        if hasattr(self, "compat"):
            self.compat.uninstall_host_compat(FakeRunner)
        FakeRunner.reset = (
            self.original_reset if hasattr(self, "original_reset") else FakeRunner.reset
        )
        FakeRunner.step_until_done = (
            self.original_step
            if hasattr(self, "original_step")
            else FakeRunner.step_until_done
        )
        self.temp.cleanup()

    async def reset(self, provider=None, request=None, mode="skills_like"):
        runner = FakeRunner()
        request = request if request is not None else Request()
        await runner.reset(provider or self.provider, request, tool_schema_mode=mode)
        return runner, request

    async def test_non_acp_modes_and_identities_are_unchanged(self):
        for owner in (None, "sl251.agy_provider.v1", "another-plugin"):
            for mode in ("full", "skills_like"):
                provider = SimpleNamespace(agy_plugin_owner=owner)
                req, context = Request(session_id="QQ-original"), object()
                runner = FakeRunner()
                await runner.reset(provider, req, context, tool_schema_mode=mode)
                self.assertIs(runner.provider, provider)
                self.assertIs(runner.req, req)
                self.assertIs(runner.run_context, context)
                self.assertEqual(runner.tool_schema_mode, mode)
                self.assertEqual(req.session_id, "QQ-original")
                source = runner.step_until_done(2)
                self.assertEqual([value async for value in source], ["native-result"])

    async def test_acp_full_schema_preserves_existing_session_and_models(self):
        req = Request(session_id="keep-session", conversation_id="keep-conversation")
        tool_set = req.func_tool
        with self.assertLogs("agy_provider.host_compat", level="INFO") as logs:
            runner, _ = await self.reset(request=req)
        self.assertEqual(runner.tool_schema_mode, "full")
        self.assertIs(runner.req, req)
        self.assertIs(req.func_tool, tool_set)
        self.assertEqual(req.session_id, "keep-session")
        self.assertEqual(req.conversation_id, "keep-conversation")
        self.assertEqual(req.model, "original-model")
        self.assertEqual(self.provider.provider_config["model"], "provider-model")
        self.assertIn("skills_like", " ".join(logs.output))

    async def test_missing_nested_run_ids_are_distinct_stable_and_request_is_not_mutated(
        self,
    ):
        req = Request()
        parent, _ = await self.reset(request=req)
        child, _ = await self.reset(request=req)
        self.assertIsNot(parent.req, req)
        self.assertIsNot(child.req, req)
        self.assertEqual(req.session_id, "")
        self.assertIsNone(req.conversation_id)
        self.assertTrue(parent.req.session_id.startswith("agy-native-run:"))
        self.assertNotEqual(parent.req.session_id, child.req.session_id)
        self.assertIsNot(parent._abort_signal, child._abort_signal)
        identity = parent.req.session_id
        self.assertEqual(
            [x async for x in parent.step_until_done(1)], ["native-result"]
        )
        self.assertEqual(parent.req.session_id, identity)
        self.assertIs(parent.req.func_tool, req.func_tool)
        self.assertIs(parent.req.contexts, req.contexts)

    async def test_existing_conversation_needs_no_synthetic_id(self):
        runner, req = await self.reset(request=Request(conversation_id="existing"))
        self.assertIs(runner.req, req)
        self.assertEqual(req.session_id, "")

    async def test_positional_schema_mode_is_overridden(self):
        runner, req = FakeRunner(), Request()
        await runner.reset(self.provider, req, None, None, None, False, "skills_like")
        self.assertEqual(runner.tool_schema_mode, "full")
        self.assertIsNot(runner.req, req)

    async def test_finally_closes_only_matching_signal_turns(self):
        runner, _ = await self.reset()
        runtime = self.provider.runtime
        own = FakeTurn(runner._abort_signal, runtime, "own")
        sibling = FakeTurn(asyncio.Event(), runtime, "sibling")
        absent = FakeTurn(None, runtime, "absent")
        runtime.turns.update(own=own, sibling=sibling, absent=absent)
        self.assertEqual(
            [x async for x in runner.step_until_done(1)], ["native-result"]
        )
        self.assertEqual(own.closed, 1)
        self.assertEqual(sibling.closed, 0)
        self.assertEqual(absent.closed, 0)
        self.assertNotIn("own", runtime.turns)

    async def test_exception_cleans_and_propagates(self):
        runner, _ = await self.reset()
        own = FakeTurn(runner._abort_signal)
        self.provider.runtime.turns["own"] = own
        runner.error = ValueError("native-error")
        with self.assertRaisesRegex(ValueError, "native-error"):
            _ = [x async for x in runner.step_until_done(1)]
        self.assertEqual(own.closed, 1)
        self.assertTrue(runner.inner_closed)

    async def test_early_generator_close_cleans_inner_and_turn(self):
        runner, _ = await self.reset()
        own = FakeTurn(runner._abort_signal)
        self.provider.runtime.turns["own"] = own
        source = runner.step_until_done(1)
        self.assertEqual(await anext(source), "native-result")
        await source.aclose()
        self.assertEqual(own.closed, 1)
        self.assertTrue(runner.inner_closed)

    async def test_task_cancel_cleans_before_cancel_propagates(self):
        runner, _ = await self.reset()
        own = FakeTurn(runner._abort_signal)
        self.provider.runtime.turns["own"] = own
        runner.block, runner.started = True, asyncio.Event()

        async def consume():
            return [x async for x in runner.step_until_done(1)]

        task = asyncio.create_task(consume())
        await runner.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(own.closed, 1)
        self.assertTrue(runner.inner_closed)

    async def test_captured_signal_does_not_close_new_reset_run(self):
        runner, _ = await self.reset()
        old_signal = runner._abort_signal
        source = runner.step_until_done(1)
        await anext(source)
        await runner.reset(self.provider, Request(), tool_schema_mode="skills_like")
        old, new = FakeTurn(old_signal), FakeTurn(runner._abort_signal)
        self.provider.runtime.turns.update(old=old, new=new)
        await source.aclose()
        self.assertEqual(old.closed, 1)
        self.assertEqual(new.closed, 0)

    async def test_install_uninstall_are_idempotent(self):
        first_reset, first_step = FakeRunner.reset, FakeRunner.step_until_done
        self.compat.install_host_compat(FakeRunner, owner=self.provider_module.OWNER)
        self.assertIs(FakeRunner.reset, first_reset)
        self.assertIs(FakeRunner.step_until_done, first_step)
        self.compat.uninstall_host_compat(FakeRunner)
        self.compat.uninstall_host_compat(FakeRunner)
        self.assertIs(FakeRunner.reset, self.original_reset)
        self.assertIs(FakeRunner.step_until_done, self.original_step)

    async def test_third_party_patch_before_install_is_restored(self):
        self.compat.uninstall_host_compat(FakeRunner)
        original = FakeRunner.reset
        seen = []

        @functools.wraps(original)
        async def third_party(*args, **kwargs):
            seen.append(True)
            return await original(*args, **kwargs)

        FakeRunner.reset = third_party
        self.compat.install_host_compat(FakeRunner, owner=self.provider_module.OWNER)
        runner, _ = await self.reset()
        self.assertEqual(runner.tool_schema_mode, "full")
        self.assertEqual(seen, [True])
        self.compat.uninstall_host_compat(FakeRunner)
        self.assertIs(FakeRunner.reset, third_party)

    async def test_uninstall_keeps_later_third_party_wrappers_and_disables_ours(self):
        reset, step = FakeRunner.reset, FakeRunner.step_until_done

        @functools.wraps(reset)
        async def third_reset(*args, **kwargs):
            return await reset(*args, **kwargs)

        @functools.wraps(step)
        async def third_step(*args, **kwargs):
            async for item in step(*args, **kwargs):
                yield item

        FakeRunner.reset, FakeRunner.step_until_done = third_reset, third_step
        self.compat.uninstall_host_compat(FakeRunner)
        self.assertIs(FakeRunner.reset, third_reset)
        self.assertIs(FakeRunner.step_until_done, third_step)
        runner, req = await self.reset()
        self.assertEqual(runner.tool_schema_mode, "skills_like")
        self.assertIs(runner.req, req)
        own = FakeTurn(runner._abort_signal)
        self.provider.runtime.turns["own"] = own
        _ = [x async for x in runner.step_until_done(1)]
        self.assertEqual(own.closed, 0)
        self.compat.install_host_compat(FakeRunner, owner=self.provider_module.OWNER)
        runner, _ = await self.reset()
        self.assertEqual(runner.tool_schema_mode, "full")
        self.compat.uninstall_host_compat(FakeRunner)
        self.assertIs(FakeRunner.reset, third_reset)

    async def test_non_acp_step_returns_original_generator_unchanged(self):
        runner, _ = await self.reset(provider=SimpleNamespace(agy_plugin_owner="other"))
        source = runner.step_until_done(2)
        self.assertIs(getattr(source, "ag_code"), self.original_step.__code__)
        self.assertEqual(await anext(source), "native-result")
        await source.aclose()

    async def test_repeated_cancel_during_cleanup_still_waits_for_release(self):
        runner, _ = await self.reset()
        entered, released = asyncio.Event(), asyncio.Event()

        class SlowTurn(FakeTurn):
            async def close(self):
                entered.set()
                await released.wait()
                await super().close()

        own = SlowTurn(runner._abort_signal)
        self.provider.runtime.turns["own"] = own

        async def consume():
            return [x async for x in runner.step_until_done(1)]

        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        released.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(own.closed, 1)

    async def test_real_retained_turn_cleanup_releases_slot_and_records_unknown(self):
        from agy_provider import sessions

        settings = Settings(
            auth_bridge=False,
            enable_tools=True,
            request_timeout=20,
            startup_timeout=5,
            queue_timeout=1,
            max_concurrent=1,
            max_sessions=1,
        )
        runtime = Runtime(settings, Path(self.temp.name))
        fake = Path(__file__).with_name("fake_tool_acp.py")
        runtime.info = LaunchInfo(sys.executable, "1.5.2", False)
        self.provider.runtime = runtime
        runner, _ = await self.reset()
        snapshot = ToolSnapshot(tools())
        request = normalize([], "hello", "", None, settings.history_bytes)
        source = chat_events(
            runtime,
            request,
            runner.req.session_id,
            "gemini-test",
            "default",
            snapshot,
            runner._abort_signal,
        )
        try:
            with patch.object(
                sessions,
                "launch_args",
                lambda *args, **kwargs: [
                    sys.executable,
                    str(fake),
                    "--log",
                    str(Path(self.temp.name) / "trace.jsonl"),
                ],
            ):
                events = [event async for event in source]
                handoff = next(event for event in events if event.kind == "tools")
                ident = handoff.result["call"].id
                self.assertEqual(len(runtime.turns), 1)
                self.assertTrue(runtime.slots.locked())
                _ = [x async for x in runner.step_until_done(1)]
                self.assertEqual(runtime.turns, {})
                self.assertFalse(runtime.slots.locked())
                self.assertEqual(runtime.callers, 0)
                self.assertEqual(runtime.tool_outcomes[ident], "unknown")
        finally:
            await source.aclose()
            await runtime.close()


if __name__ == "__main__":
    unittest.main()
