"""Offline ACP subprocess regression tests; no AGY account required."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.transport import AcpClient

FAKE_ACP = r"""
import json
import sys

def send(obj):
    obj['jsonrpc'] = '2.0'
    print(json.dumps(obj), flush=True)

for line in sys.stdin:
    obj = json.loads(line)
    method = obj.get('method')
    if method == 'initialize':
        send({'id': obj['id'], 'result': {'protocolVersion': 1}})
    elif method == 'session/new':
        send({'id': obj['id'], 'result': {'sessionId': 'test'}})
    elif method == 'session/prompt':
        mode, count = obj['params']['prompt'][0]['text'].split(':')
        frames = []
        for i in range(int(count)):
            kind = 'agent_thought_chunk' if mode == 'mixed' and i % 2 else 'agent_message_chunk'
            frames.append({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
                'sessionId': 'test', 'update': {'sessionUpdate': kind,
                'content': {'type': 'text', 'text': str(i) + ','}}}})
        if mode == 'permission':
            frames.append({'jsonrpc': '2.0', 'id': 'permission',
                           'method': 'session/request_permission', 'params': {}})
        else:
            frames.append({'jsonrpc': '2.0', 'id': obj['id'],
                           'result': {'stopReason': 'end_turn'}})
        sys.stdout.write('\n'.join(json.dumps(x) for x in frames) + '\n')
        sys.stdout.flush()
        if mode == 'permission':
            reply = json.loads(sys.stdin.readline())
            if reply.get('result', {}).get('outcome', {}).get('outcome') == 'cancelled':
                send({'id': obj['id'], 'result': {'stopReason': 'end_turn'}})
"""


class TransportRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-transport-test-")
        self.path = Path(self.temp.name)
        self.fake = self.path / "fake_acp.py"
        self.fake.write_text(FAKE_ACP)
        self.client = AcpClient(
            Settings.from_dict({"startup_timeout": 5, "request_timeout": 10})
        )
        await self.client.start([sys.executable, str(self.fake)], self.path)
        await self.client.new_session(self.path)

    async def asyncTearDown(self):
        await self.client.close()
        self.assertIsNotNone(self.client.proc.returncode)
        await asyncio.sleep(0)
        tasks = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name().startswith("agy:")
        ]
        self.assertEqual(tasks, [])
        self.temp.cleanup()

    async def test_fast_consumer_handles_200_buffered_frames(self):
        events = [event async for event in self.client.generate("text:200")]
        self.assertEqual(len(events), 201)
        self.assertEqual(
            "".join(e.text for e in events[:-1]), "".join(f"{i}," for i in range(200))
        )
        self.assertEqual(events[-1].kind, "done")

    async def test_fast_consumer_preserves_2000_mixed_chunks(self):
        events = [event async for event in self.client.generate("mixed:2000")]
        self.assertEqual(len(events), 2001)
        for i, event in enumerate(events[:-1]):
            self.assertEqual(event.kind, "thought" if i % 2 else "text")
            self.assertEqual(event.text, f"{i},")
        self.assertEqual(events[-1].kind, "done")

    async def test_real_slow_consumer_remains_bounded(self):
        stream = self.client.generate("text:2000")
        await anext(stream)
        # Deliberately stop consuming until the reader hits its queue bound.
        async with asyncio.timeout(5):
            while self.client.failed is None:
                await asyncio.sleep(0.001)
        self.assertEqual(self.client.failed.code, "BACKPRESSURE")
        self.assertLessEqual(self.client.events.qsize(), 128)
        with self.assertRaisesRegex(AgyError, "BACKPRESSURE"):
            await anext(stream)
        await stream.aclose()
        self.assertFalse(self.client.pending)
        self.assertFalse(self.client.active)

    async def test_permission_response_not_blocked_by_paused_consumer(self):
        stream = self.client.generate("permission:100")
        first = await anext(stream)
        # The reader must reject permissions and settle the prompt while the
        # consumer is paused, rather than awaiting a drain of the event queue.
        async with asyncio.timeout(5):
            while self.client.pending:
                await asyncio.sleep(0.001)
        rest = [event async for event in stream]
        self.assertEqual(len(rest), 100)
        self.assertEqual(first.text, "0,")
        self.assertEqual(rest[-1].kind, "done")


if __name__ == "__main__":
    unittest.main()
