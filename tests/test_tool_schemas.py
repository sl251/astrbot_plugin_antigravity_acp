"""Gemini schema hints must not change the host execution contract."""

import asyncio
import copy
import unittest
from types import SimpleNamespace

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.tools import ToolBroker, ToolSnapshot


def snapshot_for(array_schema):
    schema = {
        "type": "object",
        "properties": {"values": array_schema},
        "required": ["values"],
        "additionalProperties": False,
    }
    tool = SimpleNamespace(
        name="echo", active=True, description="echo", parameters=schema,
    )
    return ToolSnapshot(SimpleNamespace(tools=[tool])), schema


class ToolSchemaTests(unittest.IsolatedAsyncioTestCase):
    def broker(self, snapshot):
        broker = ToolBroker(snapshot, Settings(auth_bridge=False, enable_tools=True))
        broker.session_id = "schema-session"
        broker.turn_active = True
        return broker

    def request(self, values):
        return {
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "echo",
                "arguments": {"values": values},
                "_meta": {
                    "antigravity.google/conversation_id": "schema-session",
                    "antigravity.google/root_conversation_id": "schema-session",
                },
            },
        }

    async def round_trip(self, snapshot, values):
        broker = self.broker(snapshot)
        task = asyncio.create_task(broker.dispatch(self.request(values)))
        try:
            # If validation rejects a value, fail immediately rather than wait
            # on the empty ready queue until the execution budget expires.
            await asyncio.sleep(0)
            if task.done():
                await task
                self.fail("Tool call completed without host handoff")
            call = broker.ready.get_nowait()
            self.assertEqual(call.arguments, {"values": values})
            broker.handoff(call)
            broker.resolve(call.id, "host result")
            result = await asyncio.wait_for(task, 1)
            self.assertEqual(result, {
                "content": [{"type": "text", "text": "host result"}],
                "isError": False,
            })
            self.assertFalse(broker.pending)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_missing_items_accepts_numbers_objects_empty_and_nested_values(self):
        snapshot, _ = snapshot_for({"type": "array"})
        self.assertEqual(
            snapshot.tools[0]["inputSchema"]["properties"]["values"]["items"],
            {"type": "string"},
        )
        for values in ([1, 2.5], [{"name": "object"}], [], [[1], [{"n": 2}]]):
            with self.subTest(values=values):
                await self.round_trip(snapshot, values)

    async def test_nested_array_missing_items_uses_original_contract(self):
        snapshot, _ = snapshot_for({"type": "array", "items": {"type": "array"}})
        self.assertEqual(
            snapshot.tools[0]["inputSchema"]["properties"]["values"]["items"]["items"],
            {"type": "string"},
        )
        await self.round_trip(snapshot, [[1, {"nested": True}], []])

    async def test_explicit_string_items_still_rejects_non_strings(self):
        snapshot, _ = snapshot_for({"type": "array", "items": {"type": "string"}})
        await self.round_trip(snapshot, ["valid"])
        await self.round_trip(snapshot, [])
        for values in ([1], [{}], [["nested"]]):
            with self.subTest(values=values):
                broker = self.broker(snapshot)
                with self.assertRaises(AgyError) as caught:
                    await broker.dispatch(self.request(values))
                self.assertEqual(caught.exception.code, "TOOL_ARGUMENTS")
                self.assertTrue(broker.ready.empty())
                self.assertFalse(broker.pending)

    def test_host_schema_and_model_schema_are_independent_copies(self):
        original_array = {"type": "array", "items": {"type": "array"}}
        before = copy.deepcopy(original_array)
        snapshot, source = snapshot_for(original_array)
        self.assertEqual(source["properties"]["values"], before)
        self.assertEqual(snapshot.to_host_schema["echo"], source)
        source["properties"]["values"]["minItems"] = 3
        self.assertNotIn("minItems", snapshot.to_host_schema["echo"]["properties"]["values"])
        snapshot.tools[0]["inputSchema"]["properties"]["values"]["items"]["items"]["type"] = "number"
        self.assertEqual(snapshot.to_host_schema["echo"]["properties"]["values"], before)

    def test_fingerprint_distinguishes_host_contracts_with_identical_model_schema(self):
        unconstrained, _ = snapshot_for({"type": "array"})
        strings, _ = snapshot_for({"type": "array", "items": {"type": "string"}})
        same, _ = snapshot_for({"type": "array"})
        self.assertEqual(unconstrained.tools, strings.tools)
        self.assertNotEqual(unconstrained.fingerprint, strings.fingerprint)
        self.assertEqual(unconstrained.fingerprint, same.fingerprint)


if __name__ == "__main__":
    unittest.main()
