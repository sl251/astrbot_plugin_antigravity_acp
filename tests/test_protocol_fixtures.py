"""Contracts replaying sanitized observed AGY 1.5.2 fixture shapes."""

import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from agy_provider.config import Settings
from agy_provider.tools import ToolBroker, ToolSnapshot
from agy_provider.transport import AcpClient

ROOT = Path(__file__).with_name("fixtures")


def fixture(name):
    return json.loads((ROOT / (name + ".json")).read_text())


class ObservedFixtureTests(unittest.IsolatedAsyncioTestCase):
    def snapshot(self):
        return ToolSnapshot(
            SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="echo_probe",
                        description="Echo",
                        active=True,
                        handler=lambda: None,
                        parameters={
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    )
                ]
            )
        )

    async def test_observed_tools_call_roundtrip_and_notification_names(self):
        raw = fixture("mcp_tools_call_request")[0]["data"]
        broker = ToolBroker(self.snapshot(), Settings(enable_tools=True))
        broker.session_id = raw["params"]["_meta"]["antigravity.google/conversation_id"]
        self.addAsyncCleanup(broker.close)
        task = asyncio.create_task(broker.dispatch(raw))
        call = await asyncio.wait_for(broker.ready.get(), 1)
        broker.handoff(call)
        self.assertEqual(call.name, "echo_probe")
        self.assertEqual(call.arguments, {"message": "BRIDGE-7731"})
        broker.resolve(call.id, '{"echo":"BRIDGE-7731","marker":"BRIDGE-ECHO-OK"}')
        result = await task
        self.assertFalse(result["isError"])
        for update in fixture("tool_call_notifications"):
            update = copy.deepcopy(update)
            update["name"] = update["name"].replace("mcp_echo_", "mcp_astrbot_", 1)
            broker.observe(update)
        self.assertEqual(
            next(iter(broker.notifications.values())), ("echo_probe", "completed")
        )

    async def test_observed_initialize_and_agent_chunks(self):
        initialized = fixture("acp_initialize_result")
        self.assertEqual(initialized["protocolVersion"], 1)
        self.assertTrue(initialized["agentCapabilities"]["promptCapabilities"]["image"])
        client = AcpClient(Settings())
        client.session_id, client.active = "fixture-session", True
        chunks = fixture("agent_final_message_chunks")
        for update in chunks["chunks"]:
            client._on_update({"sessionId": client.session_id, "update": update})
        answer = "".join(
            client.events.get_nowait().text for _ in range(client.events.qsize())
        )
        self.assertEqual(answer, chunks["assembled"])

    def test_observed_stdio_descriptor(self):
        params = fixture("session_new_with_mcp_servers")["params"]
        server = params["mcpServers"][0]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(set(server), {"name", "type", "command", "args", "env"})
        self.assertIsInstance(server["env"], dict)
