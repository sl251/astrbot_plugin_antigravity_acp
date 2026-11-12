import unittest
from unittest.mock import Mock

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.notices import TerminalNoticeGuard
from agy_provider.transport import AcpClient, _AgentTextGuard


class NoticeTests(unittest.TestCase):
    def test_all_chunk_boundaries_and_markers(self):
        for message, code in TerminalNoticeGuard.messages.items():
            for prefix in ("", "⚠ ", "⚠️ Error: "):
                text = prefix + message + "."
                for split in range(len(text) + 1):
                    with self.subTest(prefix=prefix, code=code, split=split):
                        guard = _AgentTextGuard()
                        with self.assertRaises(AgyError) as caught:
                            guard.feed(text[:split])
                            guard.feed(text[split:])
                            guard.feed("", final=True)
                        self.assertEqual(caught.exception.code, code)

    def test_character_chunks_across_newlines(self):
        text = next(iter(TerminalNoticeGuard.messages)).replace(" ", "\n")
        guard = _AgentTextGuard()
        for char in text:
            self.assertEqual(guard.feed(char), "")
        with self.assertRaises(AgyError) as caught:
            guard.feed("", final=True)
        self.assertEqual(caught.exception.code, "NETWORK")

    def test_explanation_and_quoted_notice_preserved(self):
        message = next(iter(TerminalNoticeGuard.messages))
        for text in (
            message + ". Here is how to troubleshoot.",
            '"' + message + '"',
            "Example: " + message,
            "ordinary text\n" + message + ". Explanation follows.",
        ):
            guard = _AgentTextGuard()
            actual = "".join(guard.feed(c) for c in text) + guard.feed("", final=True)
            self.assertEqual(actual, text)

    def test_unmarked_notice_waits_for_end(self):
        guard = _AgentTextGuard()
        self.assertEqual(guard.feed(next(iter(TerminalNoticeGuard.messages)) + "."), "")
        with self.assertRaises(AgyError):
            guard.feed("", final=True)

    def test_candidate_memory_bounded(self):
        guard = TerminalNoticeGuard()
        actual = "".join(guard.feed(" ") for _ in range(20000))
        self.assertLessEqual(len(guard.pending), 4096)
        self.assertEqual(actual + guard.feed("", final=True), " " * 20000)

    def test_legacy_failure_still_rejected(self):
        for marker in _AgentTextGuard.markers:
            guard = _AgentTextGuard()
            for char in marker:
                guard.feed(char)
            with self.assertRaises(AgyError) as caught:
                guard.feed("", final=True)
            self.assertEqual(caught.exception.code, "UPSTREAM")

    def client(self):
        client = AcpClient(Settings())
        client.session_id, client.active = "session", True
        return client

    def update(self, client, kind, text="", sid="session"):
        client._on_update(
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": kind,
                    "content": {"type": "text", "text": text},
                },
            }
        )

    def test_thought_not_classified(self):
        client = self.client()
        text = next(iter(TerminalNoticeGuard.messages))
        self.update(client, "agent_thought_chunk", text)
        event = client.events.get_nowait()
        self.assertEqual((event.kind, event.text), ("thought", text))

    def test_notice_prevents_tool_handoff(self):
        client = self.client()
        client.tool_broker = Mock()
        self.update(
            client, "agent_message_chunk", next(iter(TerminalNoticeGuard.messages))
        )
        with self.assertRaises(AgyError) as caught:
            self.update(client, "tool_call")
        self.assertEqual(caught.exception.code, "NETWORK")
        client.tool_broker.observe.assert_not_called()

    def test_other_session_updates_ignored(self):
        client = self.client()
        self.update(client, "tool_call", sid="old-session")
        self.assertTrue(client.events.empty())

    def test_backpressure_is_bounded(self):
        client = self.client()
        for _ in range(128):
            self.update(client, "agent_message_chunk", "hello")
        with self.assertRaises(AgyError) as caught:
            self.update(client, "agent_message_chunk", "hello")
        self.assertEqual(caught.exception.code, "BACKPRESSURE")

    def test_output_budget(self):
        client = self.client()
        with self.assertRaises(AgyError) as caught:
            self.update(
                client, "agent_message_chunk", "x" * (Settings().max_output_bytes + 1)
            )
        self.assertEqual(caught.exception.code, "OUTPUT_LIMIT")
