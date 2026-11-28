"""Real socket/stdio relay acceptance; no AGY or host credentials."""

import asyncio
import json
import os
import secrets
import sys
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from agy_provider.bridge_transport import BridgeTransport, FRAME_LIMIT, TOKEN_ENV, connect, encode, proof, read_frame
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.process import spawn, stop_process
from agy_provider.tools import ToolBroker, ToolSnapshot
from tests.test_b07_b08_regressions import _png_data_url


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def make_broker(self, *, tcp=True):
        snapshot = ToolSnapshot(SimpleNamespace(tools=[SimpleNamespace(
            name="echo", active=True, description="offline test", parameters={
                "type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"],
                "additionalProperties": False})]))
        broker = ToolBroker(snapshot, Settings(tool_approval_timeout=1, tool_execution_timeout=1))
        broker.session_id, broker.turn_active = "session", True
        self.addAsyncCleanup(broker.close)
        # Exercise Windows transport on every platform, plus native POSIX below.
        broker.transport = BridgeTransport(tcp=tcp)
        config = await broker.start()
        return broker, config

    def request(self, ident=1, **values):
        return {"id": ident, "method": "tools/call", "params": {
            "name": "echo", "arguments": {"value": values.get("value", "hello")},
            "_meta": {"antigravity.google/conversation_id": values.get("session", "session"),
                      "antigravity.google/root_conversation_id": "session"}}}

    async def rpc(self, endpoint, token, request):
        reader, writer = await connect(endpoint, request, token)
        try:
            return await read_frame(reader, 16 * FRAME_LIMIT)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_tcp_tool_handoff_result_and_duplicate(self):
        broker, config = await self.make_broker()
        endpoint, token = config["args"][-1], config["env"][TOKEN_ENV]
        self.assertNotIn(token, " ".join(config["args"]))
        self.assertEqual(broker.server.sockets[0].getsockname()[0], "127.0.0.1")
        result = asyncio.create_task(self.rpc(endpoint, token, self.request()))
        call = await asyncio.wait_for(broker.ready.get(), 2)
        broker.handoff(call)
        broker.resolve(call.id, "中文结果 🦊")
        self.assertEqual((await result)["result"]["content"][0]["text"], "中文结果 🦊")
        duplicate = await self.rpc(endpoint, token, self.request())
        self.assertEqual(duplicate["error"]["message"], "TOOL_DUPLICATE")
        self.assertEqual(broker.audit[call.id], "completed")

    async def test_broker_resolve_with_image_attachments_and_frame_budget(self):
        for tcp in ((True, False) if os.name == "posix" else (True,)):
            with self.subTest(tcp=tcp):
                broker, config = await self.make_broker(tcp=tcp)
                proc = await spawn([config["command"], *config["args"]], env={**os.environ, **config["env"]})
                self.addAsyncCleanup(stop_process, proc)
                proc.stdin.write(encode({"jsonrpc": "2.0", **self.request()}) + b"\n")
                await proc.stdin.drain()
                call = await asyncio.wait_for(broker.ready.get(), 2)
                broker.handoff(call)

                main = {"type": "text", "text": "结果"}
                label = {"type": "text", "text": "图片"}
                image = {"type": "image", "mimeType": "image/png", "data": _png_data_url().split(",")[1]}
                for attachments, code in (
                    ({}, "TOOL_RESULT"),
                    ([{"type": "image", "mimeType": "image/webp", "data": "AA=="}], "TOOL_RESULT"),
                    ([{"type": "text", "text": 1}], "TOOL_RESULT"),
                    ([{**label, "text": "x" * broker.settings.tool_result_bytes}], "TOOL_RESULT_LIMIT"),
                    ([{**image, "data": "A" * (15 * FRAME_LIMIT)}], "TOOL_RESULT_LIMIT"),
                ):
                    with self.assertRaises(AgyError) as caught:
                        broker.resolve(call.id, main["text"], attachments=attachments)
                    self.assertEqual(caught.exception.code, code)
                    self.assertFalse(call.future.done())
                    self.assertEqual(broker.result_bytes, 0)

                broker.resolve(call.id, main["text"], attachments=[label, image])
                reply = await asyncio.wait_for(read_frame(proc.stdout), 3)
                self.assertEqual(reply["result"], {"content": [main, label, image], "isError": False})
                self.assertEqual(broker.result_bytes, len((main["text"] + label["text"]).encode()))
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 3)
                self.assertEqual(proc.returncode, 0)

    async def test_wrong_key_fake_server_and_instances_do_not_dispatch(self):
        one, config1 = await self.make_broker()
        two, config2 = await self.make_broker()
        self.assertNotEqual(config1["args"][-1], config2["args"][-1])
        for token in ("0" * 64, config2["env"][TOKEN_ENV]):
            with self.assertRaises(ValueError):
                await self.rpc(config1["args"][-1], token, self.request())
        self.assertTrue(one.ready.empty() and two.ready.empty())

    async def test_replayed_envelope_rejected_on_new_challenge(self):
        broker, config = await self.make_broker()
        port, token = int(config["args"][-1][4:]), config["env"][TOKEN_ENV]
        envelope = None
        for replay in (False, True):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(encode({"version": 1, "nonce": secrets.token_hex(32)}) + b"\n")
            await writer.drain()
            challenge = await read_frame(reader)
            request = {"method": "tools/list"}
            if not replay:
                envelope = {"version": 1, "request": request, "mac": proof(token, challenge["nonce"], request)}
            writer.write(encode(envelope) + b"\n")
            await writer.drain()
            if replay:
                self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            else:
                self.assertIn("tools", (await read_frame(reader))["result"])
            writer.close()
            await writer.wait_closed()

    async def test_invalid_frames_and_handshake_timeout_are_bounded(self):
        broker, config = await self.make_broker()
        port = int(config["args"][-1][4:])
        for frame in (b"not json\n", b"[]\n", b'{"version":true,"nonce":"x"}\n', b"x" * (FRAME_LIMIT + 1) + b"\n"):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(frame)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            writer.close()
            await writer.wait_closed()
        self.assertTrue(broker.ready.empty())
        with patch("agy_provider.bridge_transport.HANDSHAKE_TIMEOUT", 0.05):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
            writer.close()
            await writer.wait_closed()

    async def test_idle_wrong_session_and_schema_rejected(self):
        broker, config = await self.make_broker()
        endpoint, token = config["args"][-1], config["env"][TOKEN_ENV]
        broker.turn_active = False
        self.assertEqual((await self.rpc(endpoint, token, self.request()))["error"]["message"], "TOOL_STALE")
        broker.turn_active = True
        self.assertEqual((await self.rpc(endpoint, token, self.request(session="other")))["error"]["message"], "TOOL_SESSION")
        self.assertEqual((await self.rpc(endpoint, token, self.request(value=123)))["error"]["message"], "TOOL_ARGUMENTS")
        self.assertTrue(broker.ready.empty())

    async def test_approval_rejection_timeout_and_connection_cancel(self):
        broker, config = await self.make_broker()
        endpoint, token = config["args"][-1], config["env"][TOKEN_ENV]
        task = asyncio.create_task(self.rpc(endpoint, token, self.request()))
        call = await asyncio.wait_for(broker.ready.get(), 2)
        broker.reject(call, "TOOL_APPROVAL_DENIED")
        self.assertTrue((await task)["result"]["isError"])
        response = await self.rpc(endpoint, token, self.request(2))
        self.assertEqual(response["result"]["content"][0]["text"], "TOOL_APPROVAL_TIMEOUT")
        task = asyncio.create_task(self.rpc(endpoint, token, self.request(3)))
        call = await asyncio.wait_for(broker.ready.get(), 2)
        broker.handoff(call)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await broker.close()
        self.assertFalse(broker.pending or broker.tasks)
        self.assertEqual(broker.audit[call.id], "unknown")
        self.assertEqual(broker.transport.token, "")
        with self.assertRaises(AgyError):
            broker.resolve(call.id, "late result")

    async def test_stdio_relay_unicode_and_shutdown(self):
        broker, config = await self.make_broker()
        env = {**os.environ, **config["env"], "PYTHONIOENCODING": "ascii"}
        # Force a hostile default encoding to exercise the relay wire contract.
        proc = await spawn([config["command"], *config["args"]], env=env)
        self.addAsyncCleanup(stop_process, proc)
        async def rpc(request):
            proc.stdin.write(encode({"jsonrpc": "2.0", **request}) + b"\n")
            await proc.stdin.drain()
            return await asyncio.wait_for(read_frame(proc.stdout), 3)
        initialized = await rpc({"id": 10, "method": "initialize"})
        self.assertIn("tools", initialized["result"]["capabilities"])
        task = asyncio.create_task(rpc(self.request(value="中文 🦊")))
        call = await asyncio.wait_for(broker.ready.get(), 2)
        broker.handoff(call)
        broker.resolve(call.id, "返回中文 🦊")
        self.assertEqual((await task)["result"]["content"][0]["text"], "返回中文 🦊")
        proc.stdin.close()
        await asyncio.wait_for(proc.wait(), 3)
        self.assertEqual(proc.returncode, 0)

    @unittest.skipUnless(os.name == "posix", "Unix sockets require POSIX")
    async def test_native_posix_socket_permissions_and_list(self):
        broker = ToolBroker(ToolSnapshot(), Settings())
        self.addAsyncCleanup(broker.close)
        config = await broker.start()
        self.assertEqual(broker.path.stat().st_mode & 0o777, 0o600)
        response = await self.rpc(config["args"][-1], "", {"method": "tools/list"})
        self.assertEqual(response, {"result": {"tools": []}})
        path = broker.path
        await broker.close()
        self.assertFalse(path.exists())
