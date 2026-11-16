"""ACP permission is transit to the broker, never host tool approval."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.tools import ToolBroker, ToolSnapshot
from agy_provider.transport import AcpClient, PermissionDenial


class McpPermissionTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        settings = Settings(auth_bridge=False, enable_tools=True)
        snapshot = ToolSnapshot(SimpleNamespace(tools=[
            SimpleNamespace(name=name, active=True, description=name,
                            parameters={"type": "object", "properties": {}})
            for name in ("echo", "other")
        ]))
        broker = ToolBroker(snapshot, settings)
        broker.session_id = "session"
        broker.turn_active = True
        client = AcpClient(settings)
        client.proc = SimpleNamespace(returncode=None)
        client.session_id = "session"
        client.active = True
        client.prompt_deadline = asyncio.get_running_loop().time() + 30
        client.tool_broker = broker
        client._write = AsyncMock()
        client._on_update({"sessionId": "session", "update": {
            "sessionUpdate": "tool_call", "toolCallId": "call-1",
            "name": "mcp_astrbot_echo", "status": "pending",
        }})
        return client, broker

    def permission(self):
        return {"jsonrpc": "2.0", "id": "permission-1",
                "method": "session/request_permission", "params": {
                    "sessionId": "session", "toolCall": {
                        "toolCallId": "call-1", "name": "mcp_astrbot_echo",
                        "status": "pending", "rawInput": {"toolSummary": "Echo"},
                    }, "options": [
                        {"optionId": "once-17", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                    ],
                }}

    async def outcome(self, client, request):
        await client._reject_request(request)
        return client._write.call_args.args[0]["result"]["outcome"]

    async def test_observed_mcp_selects_only_allow_once_without_executing(self):
        client, broker = self.client()
        selected = await self.outcome(client, self.permission())
        self.assertEqual(selected, {"outcome": "selected", "optionId": "once-17"})
        self.assertEqual(client.permission_grants, {"call-1"})
        self.assertEqual(client.permission_errors, [])
        self.assertFalse(broker.pending)
        self.assertFalse(broker.audit)
        self.assertTrue(broker.ready.empty())
        # A repeated CLI approval does not become a persistent grant.
        self.assertEqual(await self.outcome(client, self.permission()), {"outcome": "cancelled"})

    async def test_session_and_lifecycle_must_all_be_current(self):
        cases = [
            lambda c, b, p: p.update(sessionId="foreign"),
            lambda c, b, p: p.pop("sessionId"),
            lambda c, b, p: setattr(c, "session_id", None),
            lambda c, b, p: setattr(c, "session_id", ""),
            lambda c, b, p: setattr(b, "session_id", "foreign"),
            lambda c, b, p: setattr(c, "active", False),
            lambda c, b, p: setattr(c, "closed", True),
            lambda c, b, p: setattr(c, "failed", AgyError("TRANSPORT", "closed")),
            lambda c, b, p: setattr(c.proc, "returncode", 0),
            lambda c, b, p: setattr(c, "prompt_terminal", True),
            lambda c, b, p: setattr(c, "prompt_deadline", 0),
            lambda c, b, p: setattr(b, "closed", True),
            lambda c, b, p: setattr(b, "turn_active", False),
            lambda c, b, p: setattr(c, "tool_broker", None),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                client, broker = self.client()
                request = self.permission()
                mutate(client, broker, request["params"])
                self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})
                self.assertFalse(client.permission_grants)
                self.assertFalse(broker.pending)

    async def test_native_unknown_or_unobserved_tools_stay_denied(self):
        cases = [
            {"toolCallId": "call-1", "name": "run_command", "status": "pending"},
            {"toolCallId": "call-1", "name": "mcp_astrbot_unknown", "status": "pending"},
            {"toolCallId": "call-1", "name": "mcp_other_echo", "status": "pending"},
            {"toolCallId": "unobserved", "name": "mcp_astrbot_echo", "status": "pending"},
            {"toolCallId": "call-1", "name": "mcp_astrbot_other", "status": "pending"},
            {"toolCallId": "call-1", "title": "Run mcp_astrbot_echo", "status": "pending"},
            {"toolCallId": "", "name": "mcp_astrbot_echo", "status": "pending"},
            {"toolCallId": 1, "name": "mcp_astrbot_echo", "status": "pending"},
            {"toolCallId": "call-1", "name": "mcp_astrbot_echo", "status": "completed"},
            None,
        ]
        for call in cases:
            with self.subTest(call=call):
                client, _ = self.client()
                request = self.permission()
                request["params"]["toolCall"] = call
                self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})

    async def test_completed_failed_or_foreign_notifications_do_not_authorize(self):
        for observed in (("echo", "completed"), ("echo", "failed"), ("other", "pending")):
            with self.subTest(observed=observed):
                client, broker = self.client()
                broker.notifications["call-1"] = observed
                self.assertEqual(await self.outcome(client, self.permission()), {"outcome": "cancelled"})

    async def test_options_never_fall_back_to_persistent_or_ambiguous_grants(self):
        cases = [None, {}, [], [None], ["allow"],
                 [{"optionId": "always", "kind": "allow_always"}],
                 [{"optionId": "", "kind": "allow_once"}],
                 [{"optionId": 1, "kind": "allow_once"}],
                 [{"optionId": " ", "kind": "allow_once"}],
                 [{"optionId": "one", "kind": "allow_once"}, {"optionId": "unknown"}],
                 [{"optionId": "one", "kind": "allow_once"},
                  {"optionId": "two", "kind": "allow_once"}],
                 [{"optionId": "same", "kind": "allow_once"},
                  {"optionId": "same", "kind": "reject_once"}]]
        for options in cases:
            with self.subTest(options=options):
                client, _ = self.client()
                request = self.permission()
                request["params"]["options"] = options
                self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})
        client, _ = self.client()
        request = self.permission()
        request["params"]["options"].insert(0, {"optionId": "always", "kind": "allow_always"})
        self.assertEqual(await self.outcome(client, request), {"outcome": "selected", "optionId": "once-17"})

    async def test_grant_is_recorded_before_io_yields(self):
        client, _ = self.client()
        entered, release = asyncio.Event(), asyncio.Event()
        replies = []

        async def write(reply):
            replies.append(reply)
            entered.set()
            await release.wait()

        client._write = write
        first = asyncio.create_task(client._reject_request(self.permission()))
        await entered.wait()
        self.assertEqual(client.permission_grants, {"call-1"})
        second = asyncio.create_task(client._reject_request(self.permission()))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            # Yield via the write coroutine rather than wall-clock sleeps.
            release.set()
            await asyncio.gather(first, second)
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual([r["result"]["outcome"]["outcome"] for r in replies], ["selected", "cancelled"])

    async def test_unhashable_status_and_option_kind_fail_closed(self):
        for invalid in ([], {}, ["pending"], {"kind": "allow_once"}, 1, None):
            with self.subTest(invalid=invalid):
                client, broker = self.client()
                request = self.permission()
                request["params"]["toolCall"]["status"] = invalid
                self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})
                request = self.permission()
                broker.notifications["call-1"] = ("echo", invalid)
                self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})
                broker.notifications["call-1"] = ("echo", "pending")
                request["params"]["options"][0]["kind"] = invalid
                self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})

    async def test_malformed_permission_and_native_capabilities_remain_denied(self):
        for params in (None, [], "invalid"):
            client, _ = self.client()
            request = self.permission()
            request["params"] = params
            self.assertEqual(await self.outcome(client, request), {"outcome": "cancelled"})
        client, _ = self.client()
        await client._reject_request({"jsonrpc": "2.0", "id": 1, "method": "fs/read_text_file", "params": {}})
        self.assertEqual(client._write.call_args.args[0]["error"]["code"], -32601)

    async def test_new_prompt_resets_grants_but_not_host_authorization(self):
        client, broker = self.client()
        await self.outcome(client, self.permission())
        self.assertEqual(client.permission_grants, {"call-1"})
        await self.outcome(client, self.permission())
        self.assertTrue(client.permission_errors)
        # Runtime clears observations before each generate; old IDs cannot carry
        # approval across turns even though the same client/process is reused.
        broker.notifications.clear()
        client.active = False
        async def next_prompt(method, params, timeout):
            self.assertTrue(client.active and broker.turn_active)
            self.assertFalse(client.permission_grants)
            self.assertFalse(client.permission_errors)
            self.assertEqual(await self.outcome(client, self.permission()), {"outcome": "cancelled"})
            client._on_update({"sessionId": "session", "update": {
                "sessionUpdate": "tool_call", "toolCallId": "call-2",
                "name": "mcp_astrbot_echo", "status": "pending",
            }})
            permission = self.permission()
            permission["params"]["toolCall"]["toolCallId"] = "call-2"
            self.assertEqual(await self.outcome(client, permission), {"outcome": "selected", "optionId": "once-17"})
            return {"stopReason": "end_turn"}

        client.request = AsyncMock(side_effect=next_prompt)
        events = [event async for event in client.generate("next")]
        self.assertEqual(events[-1].kind, "done")
        self.assertEqual(client.permission_grants, {"call-2"})
        self.assertFalse(broker.turn_active)
        self.assertEqual(await self.outcome(client, self.permission()), {"outcome": "cancelled"})

    async def test_denial_evidence_is_structured_bounded_and_prompt_scoped(self):
        client, _ = self.client()
        request = self.permission()
        request["params"]["toolCall"] = {
            "name": "browser", "toolCallId": "native-1", "status": "pending",
            "rawInput": {"secret": "must not be retained"},
        }
        await self.outcome(client, request)
        self.assertEqual(client.permission_errors, [PermissionDenial(
            "session/request_permission", "browser", "native-1"
        )])
        client.permission_errors.clear()
        for mutate in (
            lambda: request["params"].update(sessionId="foreign"),
            lambda: request["params"].pop("sessionId"),
        ):
            mutate()
            await self.outcome(client, request)
            self.assertFalse(client.permission_errors)
        request["params"]["sessionId"] = "session"
        for attr, value in (("active", False), ("prompt_terminal", True), ("prompt_deadline", 0)):
            previous = getattr(client, attr)
            setattr(client, attr, value)
            await self.outcome(client, request)
            self.assertFalse(client.permission_errors)
            setattr(client, attr, previous)
        request["params"]["toolCall"]["name"] = "x" * 1024
        request["params"]["toolCall"]["toolCallId"] = "y" * 1024
        for _ in range(130):
            await self.outcome(client, request)
        self.assertEqual(len(client.permission_errors), 128)
        self.assertEqual(len(client.permission_errors[0].tool_name), 256)
        self.assertEqual(len(client.permission_errors[0].tool_call_id), 256)

    async def test_buffered_permission_after_prompt_terminal_is_cancelled(self):
        client, broker = self.client()
        client.active = False
        client.proc.stdout = asyncio.StreamReader()
        prompt_written, permission_answered = asyncio.Event(), asyncio.Event()
        replies = []

        async def write(obj):
            if obj.get("method") == "session/prompt":
                prompt_written.set()
            else:
                replies.append(obj)
                permission_answered.set()

        client._write = write

        async def generate():
            return [event async for event in client.generate("next")]

        reader = asyncio.create_task(client._read())
        generation = asyncio.create_task(generate())
        try:
            await asyncio.wait_for(prompt_written.wait(), 1)
            terminal = {"jsonrpc": "2.0", "id": client.prompt_request_id,
                        "result": {"stopReason": "end_turn"}}
            # Both frames are buffered before reader gets a chance to resume.
            frames = (terminal, self.permission())
            client.proc.stdout.feed_data("".join(json.dumps(f) + "\n" for f in frames).encode())
            await asyncio.wait_for(permission_answered.wait(), 1)
            self.assertEqual(replies[0]["result"]["outcome"], {"outcome": "cancelled"})
            self.assertFalse(client.permission_grants)
            self.assertFalse(client.permission_errors)
            self.assertFalse(broker.turn_active)
            await asyncio.wait_for(generation, 1)
        finally:
            reader.cancel()
            generation.cancel()
            await asyncio.gather(reader, generation, return_exceptions=True)
