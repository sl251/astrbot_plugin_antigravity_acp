import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import (
    TRANSIENT_SYSTEM_NOTICE,
    ConversationInput,
    digest,
    normalize,
    prepare_context,
)
from agy_provider.process import LaunchInfo, launch_args as real_launch_args
from agy_provider.sessions import (
    Runtime,
    SentWindow,
    Session,
    compare_history,
    decide_reuse,
)
from agy_provider.tool_turn import ToolTurn
from agy_provider.tools import PendingCall, ToolSnapshot

FIXTURE = Path(__file__).with_name("fake_cli.py")


def _tool(name, description="desc", properties=None):
    return SimpleNamespace(
        active=True,
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties or {},
            "additionalProperties": False,
        },
    )


def _read_cli_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class SessionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-sessions-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_path = self.root / "cli.jsonl"

    async def _run_turn(self, runtime, request, key="sess-1", model="gemini-test", effort="low"):
        text_parts = []
        done_event = None
        async for event in runtime.stream(request, key, model, effort):
            if event.kind == "text":
                text_parts.append(event.text)
            elif event.kind == "done":
                done_event = event
        return "".join(text_parts), done_event

    async def test_two_rounds_matching_prefix_open_only_once(self):
        settings = Settings(auth_bridge=False, session_reuse_mode="strict")
        runtime = Runtime(settings, self.root / "rt")
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with (
            patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            patch.object(runtime, "open", wraps=runtime.open) as open_spy,
        ):
            req1 = normalize([], "你好", "你是助手", [], settings.history_bytes)
            ans1, _ = await self._run_turn(runtime, req1, key="user-conv-1")
            self.assertEqual(ans1, "reply:1")
            self.assertEqual(open_spy.await_count, 1)

            req2 = normalize(
                [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": ans1},
                ],
                "继续聊聊",
                "你是助手",
                [],
                settings.history_bytes,
            )
            ans2, _ = await self._run_turn(runtime, req2, key="user-conv-1")
            self.assertEqual(ans2, "reply:2")
            self.assertEqual(open_spy.await_count, 1)

        events = _read_cli_log(self.log_path)
        self.assertEqual(sum(1 for e in events if e["event"] == "init"), 1)
        self.assertEqual(sum(1 for e in events if e["event"] == "new"), 1)
        prompts = [e for e in events if e["event"] == "prompt"]
        self.assertEqual(len(prompts), 2)
        self.assertIn("你好", "\n".join(prompts[0]["texts"]))
        self.assertIn("继续聊聊", "\n".join(prompts[1]["texts"]))
        self.assertNotIn("astrbot_history", "\n".join(prompts[1]["texts"]))
        last_record = runtime.telemetry.history[-1]
        self.assertTrue(last_record["reused"])
        self.assertEqual(last_record["history_relation"], "exact")
        self.assertEqual(last_record["replay_bytes"], 0)

    async def test_real_system_change_forces_rebuild_and_records_reason(self):
        settings = Settings(auth_bridge=False, session_reuse_mode="strict")
        runtime = Runtime(settings, self.root / "rt-sys")
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with (
            patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            patch.object(runtime, "open", wraps=runtime.open) as open_spy,
        ):
            req1 = normalize([], "第一问", "系统设定 A", [], settings.history_bytes)
            ans1, _ = await self._run_turn(runtime, req1, key="sys-key")

            req2 = normalize(
                [
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": ans1},
                ],
                "第二问",
                "系统设定 B",
                [],
                settings.history_bytes,
            )
            await self._run_turn(runtime, req2, key="sys-key")
            self.assertEqual(open_spy.await_count, 2)

        turns = list(runtime.telemetry.history)
        self.assertEqual(len(turns), 2)
        self.assertFalse(turns[1]["reused"])
        self.assertIn("system_changed", turns[1]["rebuild_reasons"])

    async def test_strict_ignores_structured_parts_and_structured_validates_contract(self):
        td = "记忆: Alice=cat"
        raw_sys = f"基础设定\n\n{td}"
        trusted = {
            "version": 1,
            "source": "astrbot_ltm",
            "stable_system": "基础设定",
            "transient_data": td,
            "source_revision": "rev-1",
        }
        # strict mode (default) must stay full system
        strict_ctx = prepare_context(raw_sys, trusted_parts=trusted, mode="strict")
        self.assertFalse(strict_ctx.structured)
        self.assertEqual(strict_ctx.stable_system, raw_sys)
        self.assertIsNone(strict_ctx.transient_data)

        # structured mode without trusted_parts -> strict fallback
        no_parts = prepare_context(raw_sys, trusted_parts=None, mode="structured")
        self.assertFalse(no_parts.structured)
        self.assertEqual(no_parts.stable_system, raw_sys)

        # structured mode with inconsistent stable/transient -> strict fallback
        mismatched = prepare_context(
            "未包含的内容",
            trusted_parts={**trusted, "stable_system": "其他"},
            mode="structured",
        )
        self.assertFalse(mismatched.structured)
        self.assertEqual(mismatched.stable_system, "未包含的内容")

        # structured mode with valid split -> split cleanly
        ok_ctx = prepare_context(raw_sys, trusted_parts=trusted, mode="structured")
        self.assertTrue(ok_ctx.structured)
        self.assertEqual(ok_ctx.stable_system, "基础设定")
        self.assertEqual(ok_ctx.transient_data, td)
        self.assertEqual(ok_ctx.source_revision, "rev-1")

    async def test_structured_transient_updates_reuse_and_clear_retract_work(self):
        settings = Settings(auth_bridge=False, session_reuse_mode="structured")
        runtime = Runtime(settings, self.root / "rt-struct")
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with (
            patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            patch.object(runtime, "open", wraps=runtime.open) as open_spy,
        ):
            r1 = normalize(
                [],
                "问题1",
                "稳定人格\n\n瞬态v1",
                [],
                settings.history_bytes,
                trusted_parts={
                    "version": 1,
                    "source": "rag",
                    "stable_system": "稳定人格",
                    "transient_data": "瞬态v1",
                    "source_revision": "r1",
                },
                reuse_mode="structured",
            )
            a1, _ = await self._run_turn(runtime, r1, key="struct-k")
            self.assertEqual(open_spy.await_count, 1)

            # Turn 2: transient_data changes -> REUSES session!
            r2 = normalize(
                [{"role": "user", "content": "问题1"}, {"role": "assistant", "content": a1}],
                "问题2",
                "稳定人格\n\n瞬态v2",
                [],
                settings.history_bytes,
                trusted_parts={
                    "version": 1,
                    "source": "rag",
                    "stable_system": "稳定人格",
                    "transient_data": "瞬态v2",
                    "source_revision": "r2",
                },
                reuse_mode="structured",
            )
            a2, _ = await self._run_turn(runtime, r2, key="struct-k")
            self.assertEqual(open_spy.await_count, 1)

            # Turn 3: transient cleared -> sends clear marker once and reuses
            r3 = normalize(
                [
                    {"role": "user", "content": "问题1"},
                    {"role": "assistant", "content": a1},
                    {"role": "user", "content": "问题2"},
                    {"role": "assistant", "content": a2},
                ],
                "问题3",
                "稳定人格",
                [],
                settings.history_bytes,
                trusted_parts={
                    "version": 1,
                    "source": "rag",
                    "stable_system": "稳定人格",
                    "transient_data": "",
                    "clear": True,
                },
                reuse_mode="structured",
            )
            a3, _ = await self._run_turn(runtime, r3, key="struct-k")
            self.assertEqual(open_spy.await_count, 1)

            # Turn 4: retract -> MUST force rebuild!
            r4 = normalize(
                [
                    {"role": "user", "content": "问题1"},
                    {"role": "assistant", "content": a1},
                    {"role": "user", "content": "问题2"},
                    {"role": "assistant", "content": a2},
                    {"role": "user", "content": "问题3"},
                    {"role": "assistant", "content": a3},
                ],
                "问题4",
                "稳定人格",
                [],
                settings.history_bytes,
                trusted_parts={
                    "version": 1,
                    "source": "rag",
                    "stable_system": "稳定人格",
                    "transient_data": "",
                    "retract": True,
                },
                reuse_mode="structured",
            )
            await self._run_turn(runtime, r4, key="struct-k")
            self.assertEqual(open_spy.await_count, 2)

        prompts = [e for e in _read_cli_log(self.log_path) if e["event"] == "prompt"]
        self.assertEqual(len(prompts), 4)
        self.assertIn("<agy_context_data>", "\n".join(prompts[0]["texts"]))
        self.assertIn('"snapshot": "瞬态v1"', "\n".join(prompts[0]["texts"]))
        self.assertIn('"snapshot": "瞬态v2"', "\n".join(prompts[1]["texts"]))
        self.assertIn('"snapshot": null', "\n".join(prompts[2]["texts"]))
        self.assertIn(
            "system_changed", runtime.telemetry.history[-1]["rebuild_reasons"]
        )
        self.assertIn(TRANSIENT_SYSTEM_NOTICE[:10], TRANSIENT_SYSTEM_NOTICE)

    async def test_compare_history_and_dual_ledgers_under_local_trimming(self):
        h1 = digest({"role": "user", "content": "u1"})
        h2 = digest({"role": "assistant", "content": "a1"})
        h3 = digest({"role": "user", "content": "u2"})
        h4 = digest({"role": "assistant", "content": "a2"})

        self.assertEqual(compare_history([], [h1]), "unknown")
        self.assertEqual(compare_history([h1, h2], [h1, h2]), "exact")
        self.assertEqual(compare_history([h1, h2, h3, h4], [h3, h4]), "host_trim")
        self.assertEqual(compare_history([h1, h2], [h1, h2, h3, h4]), "append_gap")
        self.assertEqual(compare_history([h1, h2], [h1, "edited"]), "rewrite")

        # Dual ledgers: host_expected holds all messages even when sent_window omitted oldest
        settings = Settings(auth_bridge=False, history_bytes=160, session_reuse_mode="strict")
        runtime = Runtime(settings, self.root / "rt-dual")
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with (
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            patch.object(runtime, "open", wraps=runtime.open) as open_spy,
        ):
            r1 = normalize(
                [
                    {"role": "user", "content": "x" * 200},
                    {"role": "assistant", "content": "y" * 200},
                ],
                "短问题",
                "sys",
                [],
                settings.history_bytes,
            )
            a1, _ = await self._run_turn(runtime, r1, key="dual-k")
            session = runtime.sessions["dual-k"]
            self.assertEqual(len(session.host_expected), 4)
            assert session.sent_window is not None
            self.assertGreater(session.sent_window.omitted_messages, 0)
            self.assertLess(len(session.sent_window.hashes), len(session.host_expected))

            # Next turn with intact host history must REUSE!
            r2 = normalize(
                [
                    {"role": "user", "content": "x" * 200},
                    {"role": "assistant", "content": "y" * 200},
                    {"role": "user", "content": "短问题"},
                    {"role": "assistant", "content": a1},
                ],
                "继续",
                "sys",
                [],
                settings.history_bytes,
            )
            await self._run_turn(runtime, r2, key="dual-k")
            self.assertEqual(open_spy.await_count, 1)
            self.assertTrue(runtime.telemetry.history[-1]["reused"])

            # Next turn where host trimmed oldest message -> MUST REBUILD!
            a2 = "reply:2"
            r3 = normalize(
                [
                    {"role": "user", "content": "短问题"},
                    {"role": "assistant", "content": a1},
                    {"role": "user", "content": "继续"},
                    {"role": "assistant", "content": a2},
                ],
                "又一问",
                "sys",
                [],
                settings.history_bytes,
            )
            await self._run_turn(runtime, r3, key="dual-k")
            self.assertEqual(open_spy.await_count, 2)
            last = runtime.telemetry.history[-1]
            self.assertFalse(last["reused"])
            self.assertEqual(last["history_relation"], "host_trim")
            self.assertIn("history_host_trim", last["rebuild_reasons"])

    async def test_no_identity_never_reuses_and_tool_snapshot_sort_invariance(self):
        snap1 = ToolSnapshot(SimpleNamespace(tools=[_tool("zeta"), _tool("alpha")]))
        snap2 = ToolSnapshot(SimpleNamespace(tools=[_tool("alpha"), _tool("zeta")]))
        self.assertEqual(snap1.fingerprint, snap2.fingerprint)
        self.assertEqual([t["name"] for t in snap1.tools], ["alpha", "zeta"])

        empty_snap = ToolSnapshot(SimpleNamespace(tools=[]))
        self.assertEqual(empty_snap.fingerprint, "")
        self.assertEqual(empty_snap.schema_bytes, 0)

        settings = Settings(auth_bridge=False)
        runtime = Runtime(settings, self.root / "rt-anon")
        self.addAsyncCleanup(runtime.close)

        def fake_launch(info, home, use_default_agent=False):
            args = real_launch_args(info, home, use_default_agent)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with (
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            patch.object(runtime, "open", wraps=runtime.open) as open_spy,
        ):
            r1 = normalize([], "q1", "sys", [], settings.history_bytes)
            await self._run_turn(runtime, r1, key=None)
            r2 = normalize(
                [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "reply:1"}],
                "q2",
                "sys",
                [],
                settings.history_bytes,
            )
            await self._run_turn(runtime, r2, key=None)
            self.assertEqual(open_spy.await_count, 2)

        self.assertIn("no_identity", runtime.telemetry.history[-1]["rebuild_reasons"])

    async def test_tool_turn_accept_freezes_context_revision_and_transient(self):
        settings = Settings(auth_bridge=False, enable_tools=True, session_reuse_mode="structured")
        runtime = Runtime(settings, self.root / "rt-turn")
        self.addAsyncCleanup(runtime.close)

        req1 = ConversationInput(
            system="稳定",
            messages=[{"role": "user", "content": "hi"}],
            system_structured=True,
            transient_data="ctx1",
            source_revision="v1",
        )
        snap = ToolSnapshot(SimpleNamespace(tools=[_tool("calc")]))
        signal = asyncio.Event()
        turn = ToolTurn(runtime, req1, "k1", "gemini-test", "low", snap, signal)
        self.addAsyncCleanup(turn.close)
        fake_client = SimpleNamespace(
            alive=True, prompt_terminal=False, prompt_deadline=float("inf")
        )
        turn.waiting = {
            "client": fake_client,
            "call": PendingCall(
                id="call-1",
                name="calc",
                arguments={},
                future=asyncio.get_running_loop().create_future(),
            ),
            "broker": SimpleNamespace(resolve=lambda *a: None),
        }

        changed_rev = ConversationInput(
            system="稳定",
            messages=[
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "calc", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
            ],
            system_structured=True,
            transient_data="ctx1",
            source_revision="v2",
        )
        with self.assertRaises(AgyError) as caught:
            turn.accept(changed_rev, "gemini-test", "low", snap, signal)
        self.assertEqual(caught.exception.code, "TOOL_CONTEXT")

        changed_td = ConversationInput(
            system="稳定",
            messages=changed_rev.messages,
            system_structured=True,
            transient_data="ctx2",
            source_revision="v1",
        )
        with self.assertRaises(AgyError) as caught2:
            turn.accept(changed_td, "gemini-test", "low", snap, signal)
        self.assertEqual(caught2.exception.code, "TOOL_CONTEXT")

    async def test_eviction_tombstones_and_decide_reuse_reasons(self):
        session = Session()
        session.client = SimpleNamespace(alive=True)
        session.model = "gemini-test"
        session.effort = "low"
        session.system_hash = digest("sys")
        session.host_expected = [digest({"role": "user", "content": "u1"})]
        session.sent_window = SentWindow(hashes=list(session.host_expected), omitted_messages=0)
        req = ConversationInput(
            system="sys",
            messages=[
                {"role": "user", "content": "u1"},
                {"role": "user", "content": "u2"},
            ],
        )
        d = decide_reuse(session, req, "gemini-test", "high", ToolSnapshot(None))
        self.assertFalse(d.reuse)
        self.assertEqual(d.history_relation, "exact")
        self.assertEqual(d.reasons, ["effort_changed"])

        d_evict = decide_reuse(
            Session(),
            req,
            "gemini-test",
            "low",
            ToolSnapshot(None),
            tombstone="idle_expired",
        )
        self.assertFalse(d_evict.reuse)
        self.assertEqual(d_evict.reasons, ["idle_expired"])


import asyncio  # noqa: E402

if __name__ == "__main__":
    unittest.main()
