"""Offline host/MCP contracts based on the 2026-11-07 losvps probe shapes.

These run the actual stdio relay against the actual Unix broker, with a fake
host executor at the handoff boundary. They do not launch AGY or use accounts.
"""

import asyncio
import copy
import json
import os
import unittest
from types import SimpleNamespace

import agy_provider.tools as tools_module
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.process import stop_process
from agy_provider.tools import (
    MAX_SESSION_CALLS,
    ToolBroker,
    ToolSnapshot,
    validate,
    validate_schema,
)

SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}},
    "required": ["message"],
    "additionalProperties": False,
}


def tool(name="echo_probe", *, active=True, handler=True, schema=None):
    return SimpleNamespace(
        name=name,
        description="Echo the supplied message in the host",
        parameters=copy.deepcopy(SCHEMA if schema is None else schema),
        active=active,
        handler=(lambda **kwargs: kwargs) if handler else None,
    )


def snapshot(*tools):
    return ToolSnapshot(SimpleNamespace(tools=list(tools) or [tool()]))


def call_frame(ident=4, message="BRIDGE-7731", session="session-a"):
    # From mcp_tools_call_request.json: raw tool name and both AGY IDs.
    return {
        "jsonrpc": "2.0",
        "id": ident,
        "method": "tools/call",
        "params": {
            "_meta": {
                "antigravity.google/conversation_id": session,
                "antigravity.google/root_conversation_id": session,
                "progressToken": "probe-progress:2",
            },
            "name": "echo_probe",
            "arguments": {"message": message},
        },
    }


def notification(status="in_progress", *, name="mcp_astrbot_echo_probe"):
    # The probe used server name "echo"; the plugin's server is "astrbot".
    return {
        "sessionUpdate": "tool_call" if status == "in_progress" else "tool_call_update",
        "toolCallId": "call_9721941503576335979",
        "name": name,
        "args": {"message": "BRIDGE-7731", "toolSummary": "Echo probe test"},
        "rawInput": {"message": "BRIDGE-7731", "toolSummary": "Echo probe test"},
        "status": status,
        "kind": "other",
        "title": f"Run {name}",
    }


class ToolSchemaTests(unittest.TestCase):
    def test_mapping_reversible_and_active_filtered(self):
        names = ["echo_probe", "中文工具", "agy_reserved", "a.b", "a-b"]
        snap = snapshot(*(tool(name) for name in names), tool("disabled", active=False))
        self.assertEqual(len(snap.to_host), len(names))
        self.assertNotIn("disabled", snap.to_wire)
        self.assertEqual(snap.to_wire["echo_probe"], "echo_probe")
        for name in names:
            wire = snap.to_wire[name]
            self.assertEqual(snap.to_host[wire], name)
            self.assertEqual(snap.notification_name("mcp_astrbot_" + wire), name)
            self.assertLessEqual(len(wire), 64)
        self.assertIsNone(snap.notification_name("Run echo_probe"))
        self.assertIsNone(snap.notification_name("mcp_other_echo_probe"))

    def test_schema_copy_and_permission_change_invalidate_fingerprint(self):
        original = tool()
        old = snapshot(original)
        original.parameters["properties"]["message"]["type"] = "integer"
        changed = snapshot(original)
        self.assertNotEqual(old.fingerprint, changed.fingerprint)
        validate({"message": "hi"}, old.tools[0]["inputSchema"])
        self.assertNotEqual(old.fingerprint, snapshot(tool(active=False)).fingerprint)

    def test_unsupported_tools_skipped_with_reason(self):
        snap = snapshot(
            tool(handler=False),
            tool("good_probe"),
            tool("bad_schema_probe", schema={"type": "object", "$ref": "#/x"}),
            tool("dup_probe"),
            tool("dup_probe"),
            tool("disabled", active=False),
        )
        self.assertEqual(list(snap.to_wire), ["good_probe", "dup_probe"])
        self.assertEqual(len(snap.tools), 2)
        self.assertEqual(len(snap.skipped), 3)
        self.assertTrue(snap.skipped[0].startswith("echo_probe: "))
        self.assertTrue(any("dup_probe" in item for item in snap.skipped))
        self.assertTrue(any("bad_schema_probe" in item for item in snap.skipped))

    def test_all_tools_unusable_equals_no_tools(self):
        snap = snapshot(tool(handler=False))
        self.assertEqual(snap.tools, [])
        self.assertEqual(len(snap.skipped), 1)
        self.assertIn("无可调用宿主 handler", snap.skipped[0])

    def test_handoff_tool_without_handler_is_bridged(self):
        class FakeHandoffTool(SimpleNamespace):
            pass

        original_types = tools_module._HOST_EXECUTABLE_TOOL_TYPES
        try:
            tools_module._HOST_EXECUTABLE_TOOL_TYPES = (FakeHandoffTool,)
            handoff = FakeHandoffTool(**vars(tool("transfer_to_ops", handler=False)))
            snap = snapshot(handoff)
            self.assertEqual(snap.to_wire, {"transfer_to_ops": "transfer_to_ops"})
            self.assertEqual(snap.tools[0]["inputSchema"], SCHEMA)
            self.assertEqual(snap.skipped, [])
        finally:
            tools_module._HOST_EXECUTABLE_TOOL_TYPES = original_types

    def test_mcp_tool_without_handler_is_bridged(self):
        class FakeMCPTool(SimpleNamespace):
            pass

        original_types = tools_module._HOST_EXECUTABLE_TOOL_TYPES
        try:
            tools_module._HOST_EXECUTABLE_TOOL_TYPES = (FakeMCPTool,)
            mcp = FakeMCPTool(**vars(tool("remote_probe", handler=False)))
            snap = snapshot(mcp)
            self.assertEqual(snap.to_wire, {"remote_probe": "remote_probe"})
            self.assertEqual(snap.tools[0]["inputSchema"], SCHEMA)
            self.assertEqual(snap.skipped, [])
        finally:
            tools_module._HOST_EXECUTABLE_TOOL_TYPES = original_types

    def test_gemini_array_items_filled(self):
        snap = snapshot(
            tool(
                "anysearch_probe",
                schema={
                    "type": "object",
                    "properties": {
                        "content_types": {"type": "array", "description": "类型"},
                        "tags": {"type": "array", "items": {"type": "number"}},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            )
        )
        self.assertEqual(snap.skipped, [])
        params = snap.tools[0]["inputSchema"]["properties"]
        self.assertEqual(params["content_types"].get("items"), {"type": "string"})
        self.assertEqual(params["tags"]["items"], {"type": "number"})

    def test_gemini_nested_array_inside_items_object(self):
        snap = snapshot(
            tool(
                "nested_probe",
                schema={
                    "type": "object",
                    "properties": {
                        "entries": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"labels": {"type": "array"}},
                            },
                        }
                    },
                    "additionalProperties": False,
                },
            )
        )
        self.assertEqual(snap.skipped, [])
        entries = snap.tools[0]["inputSchema"]["properties"]["entries"]
        self.assertEqual(
            entries["items"]["properties"]["labels"]["items"], {"type": "string"}
        )

    def test_json_types_and_nested_constraints(self):
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "minimum": 1, "maximum": 3},
                "values": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1, "maxLength": 3},
                },
            },
            "required": ["n", "values"],
            "additionalProperties": False,
        }
        validate_schema(schema)
        validate({"n": 2, "values": ["a", "b"]}, schema)
        for bad in (
            {"n": True, "values": ["a"]},
            {"n": "2", "values": ["a"]},
            {"n": 4, "values": ["a"]},
            {"n": 2, "values": ["a", "a"]},
            {"n": 2, "values": [""]},
            {"n": 2, "values": ["long"]},
            {"n": 2},
            {"n": 2, "values": ["a"], "extra": 1},
        ):
            with (
                self.subTest(bad=bad),
                self.assertRaisesRegex(AgyError, "TOOL_ARGUMENTS"),
            ):
                validate(bad, schema)

    def test_union_enum_const_and_numeric_bool_distinction(self):
        schema = {"anyOf": [{"type": "null"}, {"type": "integer", "enum": [1, 2]}]}
        validate_schema(schema)
        for good in (None, 1, 2):
            validate(good, schema)
        for bad in (True, False, "1", 3):
            with self.subTest(bad=bad), self.assertRaises(AgyError):
                validate(bad, schema)
        with self.assertRaises(AgyError):
            validate(True, {"const": 1})
        with self.assertRaises(AgyError):
            validate(1, {"oneOf": [{"type": "integer"}, {"type": "number"}]})

    def test_non_finite_json_is_rejected_even_in_unconstrained_properties(self):
        for value in (float("nan"), float("inf"), -float("inf")):
            for data in ({"extra": [value]}, {"extra": {"deep": value}}):
                with self.subTest(data=data), self.assertRaises(AgyError):
                    validate(data, {"type": "object"})
            with self.assertRaises(AgyError):
                validate_schema({"type": "number", "default": value})
        for data in ({1: "bad-key"}, {"extra": object()}, {"extra": "\ud800"}):
            with self.subTest(data=type(data)), self.assertRaises(AgyError):
                validate(data, {"type": "object"})

    def test_malformed_schema_types_report_public_error(self):
        for value in ({}, [{}], [None]):
            with self.subTest(value=value), self.assertRaises(AgyError):
                validate_schema({"type": value})

    def test_extreme_integer_fails_without_raw_overflow(self):
        with self.assertRaises(AgyError):
            validate(10**1000, {"type": "integer", "maximum": 100})

    def test_unsupported_schema_fails_closed(self):
        for schema in (
            {"type": "string", "pattern": ".*"},
            {"$ref": "https://example.invalid/schema"},
            {"type": "integer", "multipleOf": 2},
            {"type": "string", "maxLength": -1},
            {"type": "object", "required": "message"},
            {"type": "array", "items": False},
            {"anyOf": []},
        ):
            with (
                self.subTest(schema=schema),
                self.assertRaisesRegex(AgyError, "TOOL_SCHEMA"),
            ):
                validate_schema(schema)


class ToolBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = ToolBroker(snapshot(), Settings(enable_tools=True))
        self.broker.session_id = "session-a"
        self.tasks = []

    async def asyncTearDown(self):
        await self.broker.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def begin(self, request=None, broker=None):
        broker = broker or self.broker
        task = asyncio.create_task(broker.dispatch(request or call_frame()))
        self.tasks.append(task)
        call = await asyncio.wait_for(broker.ready.get(), 2)
        return task, call

    async def test_echo_executes_only_after_host_handoff_and_reports_completed(self):
        task, call = await self.begin()
        self.assertFalse(task.done())
        self.assertEqual(call.state, "not_executed")
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            self.broker.resolve(call.id, "premature")
        self.broker.handoff(call)
        self.assertEqual(call.state, "unknown")
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            self.broker.handoff(call)
        result = json.dumps(
            {"echo": call.arguments["message"], "marker": "BRIDGE-ECHO-OK"}
        )
        self.broker.resolve(call.id, result)
        reply = await task
        self.assertEqual(json.loads(reply["content"][0]["text"])["echo"], "BRIDGE-7731")
        self.assertFalse(reply["isError"])
        self.assertEqual(self.broker.audit[call.id], "completed")
        self.assertFalse(self.broker.pending)
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            self.broker.resolve(call.id, result)

    async def test_wrong_session_root_missing_meta_unknown_and_type_do_not_handoff(
        self,
    ):
        invalid = []
        for key in (
            "antigravity.google/conversation_id",
            "antigravity.google/root_conversation_id",
        ):
            frame = call_frame()
            frame["params"]["_meta"][key] = "other-group"
            invalid.append((frame, "TOOL_SESSION"))
        frame = call_frame()
        del frame["params"]["_meta"]
        invalid.append((frame, "TOOL_SESSION"))
        frame = call_frame(5)
        frame["params"]["name"] = "shell"
        invalid.append((frame, "TOOL_UNKNOWN"))
        invalid.append((call_frame(6, 123), "TOOL_ARGUMENTS"))
        frame = call_frame(7)
        frame["params"]["arguments"] = {"message": "ok", "extra": True}
        invalid.append((frame, "TOOL_ARGUMENTS"))
        for frame, code in invalid:
            with self.subTest(code=code), self.assertRaisesRegex(AgyError, code):
                await self.broker.dispatch(frame)
        self.assertTrue(self.broker.ready.empty())
        self.assertFalse(self.broker.pending)

    async def test_duplicate_id_never_creates_second_call(self):
        task, call = await self.begin()
        for frame in (call_frame(), call_frame(message="changed")):
            with self.assertRaisesRegex(AgyError, "TOOL_DUPLICATE"):
                await self.broker.dispatch(frame)
        self.assertTrue(self.broker.ready.empty())
        self.broker.handoff(call)
        self.broker.resolve(call.id, "done")
        await task
        with self.assertRaisesRegex(AgyError, "TOOL_DUPLICATE"):
            await self.broker.dispatch(call_frame())

    async def test_budget_counts_invalid_attempts(self):
        self.broker.settings = Settings(tool_max_calls=1)
        with self.assertRaisesRegex(AgyError, "TOOL_ARGUMENTS"):
            await self.broker.dispatch(call_frame(message=False))
        with self.assertRaisesRegex(AgyError, "TOOL_BUDGET"):
            await self.broker.dispatch(call_frame(5))
        self.assertTrue(self.broker.ready.empty())

    async def test_oversize_result_and_non_text_are_not_truncated(self):
        self.broker.settings = Settings(tool_result_bytes=8)
        task, call = await self.begin()
        self.broker.handoff(call)
        with self.assertRaisesRegex(AgyError, "TOOL_RESULT_LIMIT"):
            self.broker.resolve(call.id, "图" * 3)
        with self.assertRaisesRegex(AgyError, "TOOL_RESULT"):
            self.broker.resolve(call.id, {"not": "text"})
        self.assertFalse(task.done())
        self.assertEqual(self.broker.result_bytes, 0)
        self.broker.resolve(call.id, "12345678")
        await task
        task2, call2 = await self.begin(call_frame(5))
        self.broker.handoff(call2)
        with self.assertRaisesRegex(AgyError, "TOOL_RESULT_LIMIT"):
            self.broker.resolve(call2.id, "x")
        self.assertFalse(task2.done())

    async def test_concurrency_bound_and_two_sessions_do_not_cross_results(self):
        task, call = await self.begin()
        with self.assertRaisesRegex(AgyError, "TOOL_CONCURRENCY"):
            await self.broker.dispatch(call_frame(5))
        other = ToolBroker(snapshot(), Settings())
        other.session_id = "session-b"
        self.addAsyncCleanup(other.close)
        task2, call2 = await self.begin(call_frame(4, "group-b", "session-b"), other)
        self.broker.handoff(call)
        other.handoff(call2)
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            self.broker.resolve(call2.id, "crossed")
        other.resolve(call2.id, "group-b-result")
        self.broker.resolve(call.id, "group-a-result")
        self.assertEqual((await task)["content"][0]["text"], "group-a-result")
        self.assertEqual((await task2)["content"][0]["text"], "group-b-result")

    async def test_bounded_parallel_calls_resolve_out_of_order(self):
        broker = ToolBroker(snapshot(), Settings(tool_max_concurrent=2))
        broker.session_id = "session-a"
        self.addAsyncCleanup(broker.close)
        task1, call1 = await self.begin(call_frame(4, "one"), broker)
        task2, call2 = await self.begin(call_frame(5, "two"), broker)
        broker.handoff(call1)
        broker.handoff(call2)
        broker.resolve(call2.id, "second")
        self.assertEqual((await task2)["content"][0]["text"], "second")
        self.assertFalse(task1.done())
        broker.resolve(call1.id, "first")
        self.assertEqual((await task1)["content"][0]["text"], "first")

    async def test_cancel_before_and_after_handoff_distinguishes_unknown(self):
        for ident, handed, expected in (
            (4, False, "not_executed"),
            (5, True, "unknown"),
        ):
            task, call = await self.begin(call_frame(ident))
            if handed:
                self.broker.handoff(call)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.broker.audit[call.id], expected)
            with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
                self.broker.resolve(call.id, "late")
        self.assertFalse(self.broker.pending)

    async def test_cancel_unconsumed_call_does_not_poison_ready_queue(self):
        task = asyncio.create_task(self.broker.dispatch(call_frame()))
        self.tasks.append(task)
        async with asyncio.timeout(2):
            while self.broker.ready.empty():
                await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.broker.pending)
        # A subsequent MCP call must get its own queue entry, not the stale call.
        self.assertTrue(self.broker.ready.empty())
        task2, call = await self.begin(call_frame(5, "new"))
        self.assertEqual(call.arguments["message"], "new")
        self.broker.handoff(call)
        self.broker.resolve(call.id, "new-result")
        self.assertEqual((await task2)["content"][0]["text"], "new-result")

    async def test_cancelled_queue_entry_preserves_other_pending_call(self):
        broker = ToolBroker(snapshot(), Settings(tool_max_concurrent=2))
        broker.session_id = "session-a"
        self.addAsyncCleanup(broker.close)
        first = asyncio.create_task(broker.dispatch(call_frame(4, "first")))
        second = asyncio.create_task(broker.dispatch(call_frame(5, "second")))
        self.tasks.extend((first, second))
        async with asyncio.timeout(2):
            while broker.ready.qsize() != 2:
                await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(broker.ready.qsize(), 1)
        call = broker.ready.get_nowait()
        self.assertEqual(call.arguments["message"], "second")
        broker.handoff(call)
        broker.resolve(call.id, "retained")
        self.assertEqual((await second)["content"][0]["text"], "retained")

    async def test_session_id_budget_applies_after_round_counter_reset(self):
        self.broker.seen.update(("int", i) for i in range(MAX_SESSION_CALLS))
        self.broker.count = 0
        with self.assertRaisesRegex(AgyError, "TOOL_BUDGET"):
            await self.broker.dispatch(call_frame(MAX_SESSION_CALLS + 1))
        self.assertTrue(self.broker.ready.empty())
        self.assertEqual(len(self.broker.seen), MAX_SESSION_CALLS)

    async def test_socket_close_error_still_removes_connection_task(self):
        class Reader:
            async def readline(self):
                return b'{"method": "tools/list"}\n'

            async def read(self, _size):
                await asyncio.Future()

        class Writer:
            def write(self, _data):
                pass

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                raise ConnectionResetError("peer reset")

        await self.broker._connection(Reader(), Writer())
        self.assertFalse(self.broker.tasks)

    async def test_timeout_releases_pending_and_rejects_late_result(self):
        self.broker.settings = Settings(
            tool_approval_timeout=0.01, tool_execution_timeout=0.01
        )
        task, call = await self.begin()
        self.broker.handoff(call)
        with self.assertRaisesRegex(AgyError, "TOOL_TIMEOUT"):
            await asyncio.wait_for(task, 2)
        self.assertFalse(self.broker.pending)
        self.assertEqual(self.broker.audit[call.id], "unknown")
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            self.broker.resolve(call.id, "late")

    async def test_notifications_are_display_only_duplicate_and_reverse_order(self):
        self.broker.observe(notification("completed"))
        self.broker.observe(notification("in_progress"))
        self.broker.observe(notification("completed"))
        self.assertEqual(
            self.broker.notifications["call_9721941503576335979"],
            ("echo_probe", "completed"),
        )
        self.assertTrue(self.broker.ready.empty())
        self.assertEqual(self.broker.count, 0)
        with self.assertRaisesRegex(AgyError, "TOOL_PERMISSION"):
            self.broker.observe(notification(name="run_shell_command"))
        with self.assertRaisesRegex(AgyError, "TOOL_PERMISSION"):
            self.broker.observe(
                {"toolCallId": "unknown", "sessionUpdate": "tool_call_update"}
            )


@unittest.skipUnless(os.name == "posix", "Unix MCP bridge")
class McpSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = ToolBroker(snapshot(), Settings())
        self.broker.session_id = "session-a"
        descriptor = await self.broker.start()
        self.path = self.broker.path
        self.proc = await asyncio.create_subprocess_exec(
            descriptor["command"],
            *descriptor["args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def asyncTearDown(self):
        await self.broker.close()
        await stop_process(self.proc)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.broker.tasks)
        self.assertIsNotNone(self.proc.returncode)

    async def send(self, frame):
        self.proc.stdin.write((json.dumps(frame) + "\n").encode())
        await self.proc.stdin.drain()

    async def receive(self):
        raw = await asyncio.wait_for(self.proc.stdout.readline(), 3)
        self.assertTrue(raw, "MCP process unexpectedly closed stdout")
        return json.loads(raw)

    async def rpc(self, ident, method, params=None):
        await self.send(
            {"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}
        )
        result = await self.receive()
        self.assertEqual(result["id"], ident)
        return result

    async def test_probe_handshake_extensions_and_full_echo_ipc(self):
        discovery = await self.rpc(1, "server/discover")
        self.assertEqual(discovery["error"]["code"], -32601)
        initialized = await self.rpc(
            2,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "clientInfo": {"name": "antigravity-client", "version": "v1.0.0"},
                "capabilities": {"roots": {"listChanged": True}},
            },
        )
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-11-25")
        for method in ("notifications/initialized", "notifications/roots/list_changed"):
            await self.send({"jsonrpc": "2.0", "method": method})
        listed = await self.rpc(3, "tools/list")
        self.assertEqual(listed["result"]["tools"][0]["name"], "echo_probe")
        await self.send(call_frame())
        call = await asyncio.wait_for(self.broker.ready.get(), 2)
        self.assertEqual(call.arguments, {"message": "BRIDGE-7731"})
        self.broker.handoff(call)
        result = json.dumps(
            {"echo": call.arguments["message"], "marker": "BRIDGE-ECHO-OK"}
        )
        self.broker.resolve(call.id, result)
        reply = await self.receive()
        self.assertEqual(reply["id"], 4)
        self.assertEqual(reply["result"]["content"][0]["text"], result)
        self.assertEqual(self.broker.audit[call.id], "completed")

    async def test_stdio_duplicate_unknown_and_wrong_session(self):
        invalid = call_frame()
        invalid["params"]["name"] = "unknown"
        await self.send(invalid)
        self.assertEqual((await self.receive())["error"]["message"], "TOOL_UNKNOWN")
        await self.send(invalid)
        self.assertEqual((await self.receive())["error"]["code"], -32600)
        await self.send(call_frame(5, session="other-session"))
        self.assertEqual((await self.receive())["error"]["message"], "TOOL_SESSION")
        self.assertTrue(self.broker.ready.empty())

    async def wait_cleared(self):
        async with asyncio.timeout(3):
            while self.broker.pending or self.broker.tasks:
                await asyncio.sleep(0.001)

    async def test_stdio_eof_cancels_ipc_and_records_unknown_without_orphans(self):
        await self.send(call_frame())
        call = await asyncio.wait_for(self.broker.ready.get(), 2)
        self.broker.handoff(call)
        self.proc.stdin.close()
        await asyncio.wait_for(self.proc.wait(), 3)
        await self.wait_cleared()
        self.assertEqual(self.broker.audit[call.id], "unknown")
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            self.broker.resolve(call.id, "late")

    async def test_mcp_cancel_notification_cancels_not_executed_call(self):
        await self.send(call_frame())
        call = await asyncio.wait_for(self.broker.ready.get(), 2)
        await self.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 4},
            }
        )
        await self.wait_cleared()
        self.assertEqual(self.broker.audit[call.id], "not_executed")
        reply = await self.rpc(5, "ping")
        self.assertEqual(reply["result"], {})


if __name__ == "__main__":
    unittest.main()
