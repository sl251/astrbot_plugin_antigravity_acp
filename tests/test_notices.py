import unittest
from unittest.mock import Mock

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.notices import RetryNoticeGuard, TerminalNoticeGuard
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

    def test_retry_notice_single_line_and_vs16_discarded(self):
        for prefix in ("⚠", "⚠️"):
            line = (
                f"{prefix} API error (attempt 1): UNAVAILABLE (code 503): "
                "No capacity available for model gemini-2.5-pro on the server"
            )
            for candidate in (line, f"  {line}\n", f"{line}\r\n"):
                with self.subTest(prefix=prefix, candidate=candidate):
                    guard = RetryNoticeGuard()
                    out = guard.feed(candidate) + guard.feed("", final=True)
                    self.assertEqual(out, "")
                    self.assertEqual(guard.error_code, "QUOTA")

    def test_retry_notice_split_across_chunks_never_leaks(self):
        for prefix in ("⚠", "⚠️"):
            line = f"{prefix} API error (attempt 12): unexpected EOF while streaming\n"
            answer = "最终回答内容"
            payload = line + answer
            for split in range(len(payload) + 1):
                with self.subTest(prefix=prefix, split=split):
                    guard = RetryNoticeGuard()
                    out = guard.feed(payload[:split]) + guard.feed(payload[split:]) + guard.feed("", final=True)
                    self.assertEqual(out, answer)
                    self.assertEqual(guard.error_code, "NETWORK")

            guard = RetryNoticeGuard()
            chunks = [guard.feed(ch) for ch in payload] + [guard.feed("", final=True)]
            self.assertEqual("".join(chunks), answer)
            self.assertEqual(guard.error_code, "NETWORK")

    def test_retry_notice_followed_by_normal_text_preserves_answer(self):
        guard = RetryNoticeGuard()
        raw = (
            "开场白\n"
            "⚠ API error (attempt 1): UNAVAILABLE (code 503): No capacity available\n"
            "⚠️ API error (attempt 2): unexpected eof\n"
            "完整正文第一行\n完整正文第二行"
        )
        out = "".join(guard.feed(ch) for ch in raw) + guard.feed("", final=True)
        self.assertEqual(out, "开场白\n完整正文第一行\n完整正文第二行")
        self.assertEqual(guard.error_code, "NETWORK")

    def test_midline_retry_notice_and_non_colon_lines_preserved(self):
        samples = (
            "用户引用：⚠ API error (attempt 1): UNAVAILABLE (code 503): busy\n",
            "请看 ⚠️ API error (attempt 2): unexpected eof 怎么排查",
            "⚠ API error (attempt 1) without colon\n",
        )
        for text in samples:
            with self.subTest(text=text):
                for split in (0, len(text) // 2, text.find("⚠")):
                    guard = RetryNoticeGuard()
                    out = guard.feed(text[:split]) + guard.feed(text[split:]) + guard.feed("", final=True)
                    self.assertEqual(out, text)
                    self.assertEqual(guard.error_code, "")

    def test_final_flushes_pending_retry_prefix_as_normal_text(self):
        for partial in (
            "⚠",
            "⚠️",
            "⚠ API error (attempt ",
            "⚠ API error (attempt 1",
            "⚠ API error (attempt 1)",
            "  ⚠️ API error (attempt 99)",
        ):
            with self.subTest(partial=partial):
                guard = RetryNoticeGuard()
                self.assertEqual(guard.feed(partial), "")
                self.assertEqual(guard.feed("", final=True), partial)
                self.assertEqual(guard.error_code, "")

    def test_retry_notice_diagnostic_bounded_at_4096(self):
        guard = RetryNoticeGuard()
        long_line = "⚠ API error (attempt 1): " + ("x" * 5000) + " unexpected eof"
        out = "".join(guard.feed(long_line[i : i + 128]) for i in range(0, len(long_line), 128))
        self.assertEqual(out, "")
        self.assertLessEqual(len(guard.diagnostic), 4096)
        self.assertEqual(guard.error_code, "UPSTREAM")
        # Finishing line and receiving a new retry line resets diagnostic and reclassifies.
        self.assertEqual(guard.feed("\n⚠ API error (attempt 2): unexpected eof\n"), "")
        self.assertEqual(guard.diagnostic, "")
        self.assertEqual(guard.error_code, "NETWORK")

    def test_retry_and_terminal_notices_compose_in_agent_text_guard(self):
        terminal = next(iter(TerminalNoticeGuard.messages))
        text = f"⚠️ API error (attempt 1): UNAVAILABLE (code 503): capacity\n⚠ {terminal}."
        for split in range(len(text) + 1):
            with self.subTest(split=split):
                guard = _AgentTextGuard()
                with self.assertRaises(AgyError) as caught:
                    self.assertEqual(guard.feed(text[:split]), "")
                    guard.feed(text[split:])
                    guard.feed("", final=True)
                self.assertEqual(caught.exception.code, "NETWORK")

        # Ambiguous warning prefix split before a legacy terminal failure marker.
        marker_text = "⚠ API error (attempt 1): 503 capacity\n⚠ Agent execution terminated due to error."
        guard = _AgentTextGuard()
        for ch in marker_text:
            self.assertEqual(guard.feed(ch), "")
        with self.assertRaises(AgyError) as caught:
            guard.feed("", final=True)
        self.assertEqual(caught.exception.code, "UPSTREAM")

        # Retry line followed by normal text passes cleanly through _AgentTextGuard.
        normal = "⚠ API error (attempt 1): unexpected eof\n正常回复"
        guard = _AgentTextGuard()
        out = "".join(guard.feed(ch) for ch in normal) + guard.feed("", final=True)
        self.assertEqual(out, "正常回复")
        self.assertEqual(guard.retries.error_code, "NETWORK")
