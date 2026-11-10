"""Runtime/host-turn integration using real ACP, MCP and IPC subprocess pipes."""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import digest, normalize
from agy_provider.process import LaunchInfo
from agy_provider.sessions import Runtime
from agy_provider.tool_turn import chat_events
from agy_provider.tools import ToolSnapshot

FAKE = Path(__file__).with_name("fake_tool_acp.py")


def snapshot(description="Echo from host"):
    return ToolSnapshot(
        SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="echo_probe",
                    description=description,
                    active=True,
                    handler=lambda **kw: kw,
                    parameters={
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                )
            ]
        )
    )


def request(history, *, continuation=False):
    return normalize(
        copy.deepcopy(history),
        None,
        "Test host persona",
        None,
        160000,
        tools=True,
        continuation=continuation,
    )


def exchange(event, result):
    call = event.result["call"]
    return [
        {
            "role": "assistant",
            "content": event.text,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": call.id, "content": result},
    ]


@unittest.skipUnless(os.name == "posix", "Unix MCP bridge")
class ToolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-runtime-test-")
        self.root = Path(self.temp.name)
        self.log = self.root / "events.jsonl"
        self.settings = Settings(
            auth_bridge=False,
            enable_tools=True,
            startup_timeout=5,
            request_timeout=15,
            max_concurrent=2,
            max_sessions=4,
        )
        self.runtimes = []
        self.runtime = self.make_runtime()
        self.snapshot = snapshot()
        self.signal = asyncio.Event()
        self.patch = patch(
            "agy_provider.sessions.launch_args",
            new=lambda _info, _home, use_default_agent=False: [
                sys.executable,
                str(FAKE),
                "--log",
                str(self.log),
            ],
        )
        self.patch.start()

    def make_runtime(self):
        runtime = Runtime(self.settings, self.root)
        runtime.info = LaunchInfo(sys.executable, "1.5.2", False)
        self.runtimes.append(runtime)
        return runtime

    async def asyncTearDown(self):
        await asyncio.gather(*(runtime.close() for runtime in self.runtimes))
        self.patch.stop()
        self.temp.cleanup()
        await asyncio.sleep(0)
        active = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("agy:")
        ]
        self.assertEqual(active, [])

    def records(self, kind=None):
        rows = (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )
        return [row for row in rows if kind is None or row["kind"] == kind]

    async def collect(
        self,
        history,
        *,
        key="group-a",
        continuation=False,
        runtime=None,
        tools=None,
        signal=None,
        approver=None,
    ):
        source = chat_events(
            runtime or self.runtime,
            request(history, continuation=continuation),
            key,
            "gemini-test",
            "default",
            tools or self.snapshot,
            signal if signal is not None else self.signal,
            approver,
        )
        try:
            async with asyncio.timeout(5):
                return [event async for event in source]
        finally:
            await source.aclose()

    async def roundtrip(self, history, text, **options):
        history.append({"role": "user", "content": text})
        events = await self.collect(history, **options)
        tool_events = [event for event in events if event.kind == "tools"]
        self.assertEqual(len(tool_events), 1)
        tool_event = tool_events[0]
        self.assertEqual(tool_event.result["call"].arguments, {"message": text})
        result = "HOST::" + text
        history.extend(exchange(tool_event, result))
        final = await self.collect(history, continuation=True, **options)
        answer = "".join(event.text for event in final if event.kind == "text")
        self.assertEqual(answer, "Answer: " + result)
        self.assertEqual(final[-1].kind, "done")
        history.append({"role": "assistant", "content": answer})
        return tool_event

    async def test_echo_handoff_retains_one_outstanding_prompt(self):
        history = [{"role": "user", "content": "BRIDGE-7731"}]
        events = await self.collect(history)
        self.assertEqual(events[-1].kind, "tools")
        event = events[-1]
        self.assertEqual(len(self.records("prompt_started")), 1)
        self.assertEqual(len(self.records("mcp_call")), 1)
        self.assertEqual(
            sum("result" in row["result"] for row in self.records("mcp_result")), 0
        )
        self.assertEqual(len(self.records("prompt_done")), 0)
        self.assertTrue(self.runtime.sessions["group-a"].busy)
        self.assertFalse(self.runtime.entry_locks)
        history.extend(exchange(event, "BRIDGE-ECHO-OK"))
        final = await self.collect(history, continuation=True)
        self.assertEqual(
            "".join(e.text for e in final if e.kind == "text"), "Answer: BRIDGE-ECHO-OK"
        )
        self.assertEqual(len(self.records("prompt_started")), 1)
        self.assertEqual(len(self.records("mcp_result")), 1)
        self.assertEqual(len(self.records("prompt_done")), 1)
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions["group-a"].busy)

    async def test_twenty_rounds_closed_tool_history_reuses_one_session(self):
        history = []
        for index in range(20):
            await self.roundtrip(history, f"round-{index}")
        self.assertEqual(len(self.records("session_new")), 1)
        self.assertEqual(len(self.records("prompt_started")), 20)
        self.assertEqual(len(self.records("mcp_result")), 20)
        self.assertEqual(
            self.runtime.sessions["group-a"].expected,
            request(history + [{"role": "user", "content": "next"}]).hashes[:-1],
        )

    async def test_continuation_allows_injected_or_compacted_prefix(self):
        original = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current question"},
        ]
        prefixes = [
            [*original[:-1], {"role": "user", "content": "current question\nMemory"}],
            original[-1:],
            [
                {"role": "user", "content": "Summary of older context"},
                {"role": "assistant", "content": "Summary acknowledged"},
                original[-1],
            ],
        ]
        for index, prefix in enumerate(prefixes):
            with self.subTest(prefix=prefix):
                key = f"rebuilt-{index}"
                event = (await self.collect(original, key=key))[-1]
                history = copy.deepcopy(prefix) + exchange(event, "HOST-RESULT")
                final = await self.collect(history, key=key, continuation=True)
                self.assertEqual(
                    "".join(e.text for e in final if e.kind == "text"),
                    "Answer: HOST-RESULT",
                )
                self.assertEqual(event.result["call"].state, "completed")
                # Prefix edits were never sent to the original ACP prompt.
                self.assertEqual(
                    self.runtime.sessions[key].expected[:-1],
                    request(
                        original + exchange(event, "HOST-RESULT"), continuation=True
                    ).hashes,
                )
        self.assertEqual(len(self.records("prompt_started")), 3)
        self.assertEqual(len(self.records("mcp_result")), 3)

    async def test_rewritten_continuation_rebuilds_session_on_next_user_turn(self):
        original = [{"role": "user", "content": "first question"}]
        event = (await self.collect(original))[-1]
        revised = [{"role": "user", "content": "first question\nNew memory"}]
        revised.extend(exchange(event, "first result"))
        final = await self.collect(revised, continuation=True)
        revised.append(
            {
                "role": "assistant",
                "content": "".join(e.text for e in final if e.kind == "text"),
            }
        )
        old_session = event.result["client"].session_id
        await self.roundtrip(revised, "next question")
        self.assertNotEqual(
            self.runtime.sessions["group-a"].client.session_id, old_session
        )
        self.assertEqual(len(self.records("session_new")), 2)

    async def test_continuation_rejects_tampered_calls_and_foreign_tool_records(self):
        prior_exchange = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "old-id",
                        "function": {
                            "name": "echo_probe",
                            "arguments": {"message": "old"},
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "old-id", "content": "old result"},
        ]
        original = [
            {"role": "user", "content": "old question"},
            *prior_exchange,
            {"role": "user", "content": "current question"},
        ]
        for index, mode in enumerate(
            ("name", "args", "id", "text", "foreign", "old-call", "old-result")
        ):
            with self.subTest(mode=mode):
                key = f"tampered-{index}"
                event = (await self.collect(original, key=key))[-1]
                history = copy.deepcopy(original) + exchange(event, "untrusted")
                function = history[-2]["tool_calls"][0]["function"]
                if mode == "name":
                    function["name"] = "another_tool"
                elif mode == "args":
                    function["arguments"] = {"message": "changed"}
                elif mode == "id":
                    history[-2]["tool_calls"][0]["id"] = "foreign-id"
                    history[-1]["tool_call_id"] = "foreign-id"
                elif mode == "text":
                    history[-2]["content"] = "invented assistant text"
                elif mode == "foreign":
                    foreign = copy.deepcopy(prior_exchange)
                    foreign[0]["tool_calls"][0]["id"] = "foreign-id"
                    foreign[1]["tool_call_id"] = "foreign-id"
                    history[-2:-2] = foreign
                elif mode == "old-call":
                    history[1]["tool_calls"][0]["function"]["name"] = "another_tool"
                else:
                    history[2]["content"] = "changed old result"
                with self.assertRaisesRegex(AgyError, "TOOL_HISTORY"):
                    await self.collect(history, key=key, continuation=True)
                self.assertTrue(event.result["broker"].closed)
                self.assertEqual(event.result["call"].state, "unknown")
                self.assertNotIn(key, self.runtime.turns)
        self.assertFalse(
            any("result" in row["result"] for row in self.records("mcp_result"))
        )

    async def test_foreign_conversation_result_cannot_resume_own_pending_call(self):
        first_history = [{"role": "user", "content": "conversation A"}]
        second_history = [{"role": "user", "content": "conversation B"}]
        first = (await self.collect(first_history, key="conversation-a"))[-1]
        second = (await self.collect(second_history, key="conversation-b"))[-1]
        foreign = second_history + exchange(first, "result from A")
        with self.assertRaisesRegex(AgyError, "TOOL_HISTORY"):
            await self.collect(foreign, key="conversation-b", continuation=True)
        self.assertTrue(second.result["broker"].closed)
        self.assertFalse(first.result["broker"].closed)
        final = await self.collect(
            first_history + exchange(first, "own result"),
            key="conversation-a",
            continuation=True,
        )
        self.assertEqual(
            "".join(e.text for e in final if e.kind == "text"), "Answer: own result"
        )

    async def test_incomplete_duplicate_or_misbound_result_never_resumes(self):
        for index, mode in enumerate(
            ("missing", "duplicate", "wrong-id", "wrong-role", "reversed")
        ):
            with self.subTest(mode=mode):
                key = f"invalid-result-{index}"
                original = [{"role": "user", "content": "question"}]
                event = (await self.collect(original, key=key))[-1]
                history = original + exchange(event, "untrusted result")
                if mode == "missing":
                    history.pop()
                elif mode == "duplicate":
                    history.append(copy.deepcopy(history[-1]))
                elif mode == "wrong-id":
                    history[-1]["tool_call_id"] = "foreign-id"
                elif mode == "wrong-role":
                    history[-1]["role"] = "user"
                else:
                    history[-2:] = list(reversed(history[-2:]))
                with self.assertRaisesRegex(AgyError, "TOOL_HISTORY"):
                    await self.collect(history, key=key, continuation=True)
                self.assertFalse(event.result["call"].future.done())
                await self.runtime.turns[key].close()
        self.assertFalse(
            any("result" in row["result"] for row in self.records("mcp_result"))
        )

    async def test_prefix_can_be_compacted_again_between_two_handoffs(self):
        self.runtime.settings = replace(self.settings, tool_max_concurrent=2)
        original = [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "parallel:compaction"},
        ]
        first = (await self.collect(original))[-1]
        first_result = "HOST::" + first.result["call"].arguments["message"]
        compacted = original[-1:] + exchange(first, first_result)
        second = (await self.collect(compacted, continuation=True))[-1]
        self.assertEqual(second.kind, "tools")
        # The next compaction removes even the exchange accepted just above.
        second_result = "HOST::" + second.result["call"].arguments["message"]
        final = await self.collect(
            original[-1:] + exchange(second, second_result), continuation=True
        )
        self.assertEqual(
            "".join(e.text for e in final if e.kind == "text"),
            "Answer: HOST::parallel:compaction/a | HOST::parallel:compaction/b",
        )
        self.assertEqual(len(self.records("prompt_started")), 1)
        self.assertEqual(len(self.records("mcp_result")), 2)

    async def test_schema_change_rebuilds_without_reusing_old_permissions(self):
        history = []
        await self.roundtrip(history, "first")
        old = self.runtime.sessions["group-a"].client.session_id
        await self.roundtrip(
            history, "second", tools=snapshot("Changed permission snapshot")
        )
        self.assertNotEqual(self.runtime.sessions["group-a"].client.session_id, old)
        self.assertEqual(len(self.records("session_new")), 2)

    async def test_reload_closed_history_rebuilds_but_stale_result_cannot_resume(self):
        history = []
        await self.roundtrip(history, "first")
        await self.runtime.close()
        rebuilt = self.make_runtime()
        await self.roundtrip(history, "after-reload", runtime=rebuilt)
        self.assertEqual(len(self.records("session_new")), 2)
        pending_history = [{"role": "user", "content": "pending"}]
        event = (await self.collect(pending_history, runtime=rebuilt, key="pending"))[
            -1
        ]
        pending_history.extend(exchange(event, "late-result"))
        await rebuilt.close()
        last = self.make_runtime()
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            await self.collect(
                pending_history, runtime=last, key="pending", continuation=True
            )
        self.assertEqual(
            sum("result" in row["result"] for row in self.records("mcp_result")), 2
        )

    async def test_history_edit_and_rollback_rebuild_complete_exchange(self):
        history = []
        await self.roundtrip(history, "first")
        prior = copy.deepcopy(history)
        await self.roundtrip(history, "second")
        history[2]["content"] = "edited-tool-result"
        await self.roundtrip(history, "third")
        self.assertEqual(len(self.records("session_new")), 2)
        await self.roundtrip(prior, "rollback")
        self.assertEqual(len(self.records("session_new")), 3)

    async def test_pre_tool_text_is_preserved_once_in_expected_history(self):
        history = []
        event = await self.roundtrip(history, "prefix:first")
        self.assertEqual(event.text, "Before tool. ")
        self.assertEqual(history[1]["content"], "Before tool. ")
        expected = [
            digest(message)
            for message in request(
                history + [{"role": "user", "content": "next"}]
            ).messages[:-1]
        ]
        self.assertEqual(self.runtime.sessions["group-a"].expected, expected)
        await self.roundtrip(history, "next")
        self.assertEqual(len(self.records("session_new")), 1)

    async def test_waiting_host_abort_reaps_process_and_late_result_fails(self):
        history = [{"role": "user", "content": "cancel-me"}]
        event = (await self.collect(history))[-1]
        broker = event.result["broker"]
        proc = event.result["client"].proc
        self.signal.set()
        async with asyncio.timeout(5):
            while proc.returncode is None or self.runtime.callers:
                await asyncio.sleep(0.001)
        self.assertFalse(self.runtime.sessions)
        self.assertEqual(broker.audit[event.result["call"].id], "unknown")
        history.extend(exchange(event, "too-late"))
        with self.assertRaises(AgyError):
            await self.collect(history, continuation=True)
        self.assertEqual(
            sum("result" in row["result"] for row in self.records("mcp_result")), 0
        )

    async def test_acp_eof_while_waiting_host_unblocks_and_reaps(self):
        history = [{"role": "user", "content": "eof"}]
        event = (await self.collect(history))[-1]
        proc = event.result["client"].proc
        proc.kill()
        async with asyncio.timeout(5):
            while self.runtime.callers:
                await asyncio.sleep(0.001)
        self.assertFalse(self.runtime.sessions)
        self.assertTrue(event.result["broker"].closed)
        history.extend(exchange(event, "late"))
        with self.assertRaises(AgyError):
            await self.collect(history, continuation=True)

    async def test_successful_prompt_terminal_rejects_outstanding_tool_result(self):
        history = [{"role": "user", "content": "cancelled-before-host-result"}]
        event = (await self.collect(history))[-1]
        client, broker = event.result["client"], event.result["broker"]
        call = event.result["call"]
        self.assertEqual(len(client.pending), 1)
        # Resolve the real outstanding RPC exactly as _read does for a successful
        # JSON-RPC response. A cancelled prompt is a result, not an RPC error.
        response = next(iter(client.pending.values()))
        response.set_result({"stopReason": "cancelled"})
        async with asyncio.timeout(2):
            while self.runtime.callers:
                await asyncio.sleep(0.001)
        self.assertTrue(client.prompt_terminal)
        self.assertEqual(client.failed.code, "TOOL_TERMINATED")
        self.assertTrue(broker.closed)
        self.assertTrue(call.future.cancelled())
        self.assertEqual(self.runtime.tool_outcomes[call.id], "unknown")
        self.assertIsNotNone(client.proc.returncode)
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions)
        history.extend(exchange(event, "late-host-result"))
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            await self.collect(history, continuation=True)
        self.assertEqual(
            sum("result" in row["result"] for row in self.records("mcp_result")), 0
        )

    async def test_acp_eof_during_approval_cancels_callback_without_waiting_deadline(
        self,
    ):
        self.runtime.settings = replace(self.settings, tool_approval_timeout=30)
        entered, cancelled = asyncio.Event(), asyncio.Event()
        approval_ids = []

        async def approve(_name, _arguments, ident):
            approval_ids.append(ident)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        pending = asyncio.create_task(
            self.collect(
                [{"role": "user", "content": "eof-during-approval"}],
                approver=approve,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            client = self.runtime.sessions["group-a"].client
            # Kill the actual fake ACP process while its MCP request is held and
            # the approval callback is blocked. The normal stdout EOF path runs.
            client.proc.kill()
            with self.assertRaisesRegex(AgyError, "TRANSPORT"):
                await asyncio.wait_for(pending, 2)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(len(approval_ids), 1)
            self.assertEqual(
                self.runtime.tool_outcomes[approval_ids[0]], "not_executed"
            )
            self.assertIsNotNone(client.proc.returncode)
            self.assertFalse(self.runtime.turns)
            self.assertFalse(self.runtime.sessions)
            self.assertEqual(
                sum("result" in row["result"] for row in self.records("mcp_result")), 0
            )
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_approval_denial_exception_and_timeout_never_reach_host_execution(
        self,
    ):
        self.runtime.settings = replace(self.settings, tool_approval_timeout=0.1)
        for mode in ("deny", "raise", "timeout"):
            approvals = []

            async def approve(name, arguments, ident):
                approvals.append((name, arguments, ident))
                if mode == "raise":
                    raise RuntimeError("host rejected approval")
                if mode == "timeout":
                    await asyncio.sleep(1)
                return False

            history = [{"role": "user", "content": mode}]
            events = await self.collect(history, key=mode, approver=approve)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0][0], "echo_probe")
            self.assertEqual(approvals[0][1], {"message": mode})
            self.assertFalse(any(event.kind == "tools" for event in events))
            self.assertEqual(events[-1].kind, "done")
            session = self.runtime.sessions[mode]
            self.assertEqual(list(session.broker.audit.values()), ["not_executed"])
            result = self.records("mcp_result")[-1]["result"]["result"]
            self.assertTrue(result["isError"])
            code = (
                "TOOL_APPROVAL_TIMEOUT" if mode == "timeout" else "TOOL_APPROVAL_DENIED"
            )
            self.assertEqual(result["content"][0]["text"], code)

    async def test_approved_call_executes_once_after_approval(self):
        approvals = []
        executions = []

        async def approve(name, arguments, ident):
            approvals.append((name, arguments, ident))
            return True

        history = [{"role": "user", "content": "approved"}]
        events = await self.collect(history, approver=approve)
        event = events[-1]
        self.assertEqual(event.kind, "tools")
        call = event.result["call"]
        self.assertEqual(approvals, [(call.name, call.arguments, call.id)])
        executions.append(call.id)
        history.extend(exchange(event, "approved-result"))
        final = await self.collect(history, continuation=True, approver=approve)
        self.assertEqual(executions, [call.id])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(
            "".join(e.text for e in final if e.kind == "text"),
            "Answer: approved-result",
        )
        self.assertEqual(len(self.records("prompt_started")), 1)
        self.assertEqual(len(self.records("mcp_result")), 1)

    async def test_host_result_deadline_cleans_up_and_cannot_be_revived(self):
        self.runtime.settings = replace(
            self.settings, tool_approval_timeout=0.1, tool_execution_timeout=0.1
        )
        history = [{"role": "user", "content": "never-return"}]
        event = (await self.collect(history))[-1]
        broker = event.result["broker"]
        async with asyncio.timeout(3):
            while self.runtime.callers:
                await asyncio.sleep(0.001)
        self.assertTrue(broker.closed)
        self.assertEqual(broker.audit[event.result["call"].id], "unknown")
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions)
        history.extend(exchange(event, "late-result"))
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            await self.collect(history, continuation=True)

    async def test_pending_permission_change_cancels_original_and_rejects_late_result(
        self,
    ):
        history = [{"role": "user", "content": "pending-permission"}]
        event = (await self.collect(history))[-1]
        history.extend(exchange(event, "result"))
        changed = snapshot("New permission snapshot")
        with self.assertRaisesRegex(AgyError, "TOOL_CONTEXT"):
            await self.collect(history, continuation=True, tools=changed)
        self.assertTrue(event.result["broker"].closed)
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions)
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            await self.collect(history, continuation=True)

    async def test_lookup_then_echo_reuses_original_prompt_for_two_handoffs(self):
        tools = SimpleNamespace(tools=[])
        for name in ("echo_probe", "lookup_fixture"):
            tools.tools.append(
                SimpleNamespace(
                    name=name,
                    description="Read-only local fixture",
                    active=True,
                    handler=lambda **kw: kw,
                    parameters={
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                        "additionalProperties": False,
                    },
                )
            )
        snap = ToolSnapshot(tools)
        history = [{"role": "user", "content": "multistep:lookup"}]
        first = (await self.collect(history, tools=snap))[-1]
        call = first.result["call"]
        self.assertEqual(call.name, "lookup_fixture")
        fixture = {"fixture-key": "READ-ONLY-FIXTURE-42"}
        value = fixture[call.arguments["message"]]
        history.extend(exchange(first, value))
        second = (await self.collect(history, continuation=True, tools=snap))[-1]
        self.assertEqual(second.kind, "tools")
        self.assertEqual(second.result["call"].name, "echo_probe")
        self.assertEqual(second.result["call"].arguments, {"message": value})
        history.extend(exchange(second, "ECHO::" + value))
        final = await self.collect(history, continuation=True, tools=snap)
        self.assertEqual(
            "".join(e.text for e in final if e.kind == "text"),
            "Answer: ECHO::READ-ONLY-FIXTURE-42",
        )
        self.assertEqual(len(self.records("prompt_started")), 1)
        self.assertEqual(len(self.records("mcp_call")), 2)
        self.assertEqual(len(self.records("mcp_result")), 2)
        self.assertEqual(len(self.records("prompt_done")), 1)

    async def test_two_mcp_calls_outstanding_in_one_prompt_have_distinct_host_ids(self):
        self.runtime.settings = replace(self.settings, tool_max_concurrent=2)
        history = [{"role": "user", "content": "parallel:echo"}]
        first = (await self.collect(history))[-1]
        broker = first.result["broker"]
        async with asyncio.timeout(2):
            while len(broker.pending) != 2:
                await asyncio.sleep(0.001)
        self.assertEqual(len(self.records("mcp_call")), 2)
        self.assertEqual(len(self.records("mcp_result")), 0)
        self.assertEqual(len(self.records("prompt_started")), 1)
        first_call = first.result["call"]
        history.extend(exchange(first, "HOST::" + first_call.arguments["message"]))
        second = (await self.collect(history, continuation=True))[-1]
        second_call = second.result["call"]
        self.assertNotEqual(first_call.id, second_call.id)
        self.assertEqual(
            {first_call.arguments["message"], second_call.arguments["message"]},
            {"parallel:echo/a", "parallel:echo/b"},
        )
        history.extend(exchange(second, "HOST::" + second_call.arguments["message"]))
        final = await self.collect(history, continuation=True)
        self.assertEqual(
            "".join(e.text for e in final if e.kind == "text"),
            "Answer: HOST::parallel:echo/a | HOST::parallel:echo/b",
        )
        self.assertEqual(len(self.records("prompt_done")), 1)
        self.assertEqual(len(self.records("mcp_result")), 2)

    async def test_acp_prompt_deadline_preempts_long_host_wait(self):
        self.runtime.settings = replace(
            self.settings,
            request_timeout=0.2,
            tool_approval_timeout=5,
            tool_execution_timeout=5,
        )
        history = [{"role": "user", "content": "prompt-timeout"}]
        event = (await self.collect(history))[-1]
        async with asyncio.timeout(2):
            while self.runtime.callers:
                await asyncio.sleep(0.001)
        self.assertEqual(event.result["client"].failed.code, "TIMEOUT")
        self.assertTrue(event.result["broker"].closed)
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions)
        history.extend(exchange(event, "late"))
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            await self.collect(history, continuation=True)

    async def test_stop_consuming_ordinary_text_chunk_reaps_retained_turn(self):
        source = chat_events(
            self.runtime,
            request([{"role": "user", "content": "prefix:stop"}]),
            "group-a",
            "gemini-test",
            "default",
            self.snapshot,
            self.signal,
        )
        try:
            first = await asyncio.wait_for(anext(source), 3)
            self.assertEqual(first.kind, "text")
            proc = self.runtime.sessions["group-a"].client.proc
        finally:
            await source.aclose()
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions)
        self.assertFalse(self.runtime.callers)

    async def test_close_stream_after_tool_handoff_retains_until_abort(self):
        source = chat_events(
            self.runtime,
            request([{"role": "user", "content": "retain"}]),
            "group-a",
            "gemini-test",
            "default",
            self.snapshot,
            self.signal,
        )
        event = await asyncio.wait_for(anext(source), 3)
        self.assertEqual(event.kind, "tools")
        proc = event.result["client"].proc
        await source.aclose()
        self.assertIsNone(proc.returncode)
        self.assertIn("group-a", self.runtime.turns)
        self.assertTrue(self.runtime.sessions["group-a"].busy)
        self.signal.set()
        async with asyncio.timeout(3):
            while self.runtime.callers:
                await asyncio.sleep(0.001)
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(self.runtime.turns)
        self.assertFalse(self.runtime.sessions)

    async def test_two_groups_and_two_providers_have_isolated_results(self):
        other = self.make_runtime()
        histories = [[], [], [], []]
        options = [
            {"key": "group-a", "runtime": self.runtime},
            {"key": "group-b", "runtime": self.runtime},
            {"key": "group-a", "runtime": other},
            {"key": "group-b", "runtime": other},
        ]
        await asyncio.gather(
            *(
                self.roundtrip(history, f"unique-{index}", **option)
                for index, (history, option) in enumerate(zip(histories, options))
            )
        )
        self.assertEqual(
            len({row["session"] for row in self.records("session_new")}), 4
        )
        for index, history in enumerate(histories):
            self.assertEqual(history[-1]["content"], f"Answer: HOST::unique-{index}")


if __name__ == "__main__":
    unittest.main()
