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

    def test_captured_verbatim_notices_zero_leak_and_classify_network(self):
        # Verbatim fixtures from capture/capture-notes.md.
        post_eof = (
            '\n⚠ API error (attempt 1): request failed: Post '
            '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF\n'
        )
        stream_interrupted = (
            "\n⚠ The stream was interrupted. Please continue the task you were working on.\n"
        )
        wild_wrapped = (
            '⚠ API error (attempt 1): request failed: Post\n'
            '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": unexpected EOF\n'
        )
        for name, raw in (
            ("post_eof", post_eof),
            ("stream_interrupted", stream_interrupted),
            ("wild_wrapped", wild_wrapped),
        ):
            for use_agent_guard in (False, True):
                with self.subTest(name=name, agent_guard=use_agent_guard):
                    guard = _AgentTextGuard() if use_agent_guard else RetryNoticeGuard()
                    out = guard.feed(raw) + guard.feed("", final=True)
                    self.assertEqual(out, "")
                    retries = guard.retries if use_agent_guard else guard
                    self.assertEqual(retries.error_code, "NETWORK")

        # Continuation line arriving in separate chunks (both at line break and mid-URL).
        header, cont = wild_wrapped.split("\n", 1)
        for chunk1, chunk2 in (
            (header + "\n", cont),
            (header + "\n" + cont[:10], cont[10:]),
        ):
            with self.subTest(chunk1=chunk1):
                guard = RetryNoticeGuard()
                out = guard.feed(chunk1) + guard.feed(chunk2) + guard.feed("", final=True)
                self.assertEqual(out, "")
                self.assertEqual(guard.error_code, "NETWORK")

        # Verbatim capture.jsonl replay: POST EOF chunk followed 10s later by "1\n2\n3\n4\n".
        guard = _AgentTextGuard()
        out = (
            guard.feed(post_eof)
            + guard.feed("1\n2\n3\n4\n")
            + guard.feed("5\n6\n")
            + guard.feed("", final=True)
        )
        self.assertEqual(out, "1\n2\n3\n4\n5\n6\n")
        self.assertEqual(guard.retries.error_code, "NETWORK")

    def test_wrapped_multiline_retry_all_splits_and_char_by_char(self):
        for prefix in ("⚠", "⚠️"):
            for scheme in ("https://", "http://"):
                raw = (
                    f"{prefix} API error (attempt 1): request failed: Post\n"
                    f'"{scheme}daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": unexpected EOF\n'
                )
                with self.subTest(prefix=prefix, scheme=scheme):
                    guard = RetryNoticeGuard()
                    self.assertEqual(guard.feed(raw) + guard.feed("", final=True), "")
                    self.assertEqual(guard.error_code, "NETWORK")

                    guard = RetryNoticeGuard()
                    out = "".join(guard.feed(ch) for ch in raw) + guard.feed("", final=True)
                    self.assertEqual(out, "")
                    self.assertEqual(guard.error_code, "NETWORK")

                    answer = "完整正文第一行\n完整正文第二行"
                    payload = raw + answer
                    for split in range(len(payload) + 1):
                        guard = RetryNoticeGuard()
                        out = (
                            guard.feed(payload[:split])
                            + guard.feed(payload[split:])
                            + guard.feed("", final=True)
                        )
                        self.assertEqual(out, answer)
                        self.assertEqual(guard.error_code, "NETWORK")

        # Missing trailing newline on continuation still drops and classifies at final=True.
        no_trailing_nl = (
            '⚠ API error (attempt 1): request failed: Post\n'
            '"https://example.com/v1": unexpected EOF'
        )
        guard = RetryNoticeGuard()
        out = "".join(guard.feed(ch) for ch in no_trailing_nl) + guard.feed("", final=True)
        self.assertEqual(out, "")
        self.assertEqual(guard.error_code, "NETWORK")

    def test_stream_interrupted_notice_splits_vs16_and_preserved_quotes(self):
        for prefix in ("⚠", "⚠️"):
            raw = f"{prefix} The stream was interrupted. Please continue the task you were working on.\n"
            answer = "继续输出的正文"
            payload = raw + answer
            for split in range(len(payload) + 1):
                with self.subTest(prefix=prefix, split=split):
                    guard = RetryNoticeGuard()
                    out = (
                        guard.feed(payload[:split])
                        + guard.feed(payload[split:])
                        + guard.feed("", final=True)
                    )
                    self.assertEqual(out, answer)
                    self.assertEqual(guard.error_code, "NETWORK")

            guard = RetryNoticeGuard()
            out = "".join(guard.feed(ch) for ch in payload) + guard.feed("", final=True)
            self.assertEqual(out, answer)
            self.assertEqual(guard.error_code, "NETWORK")

        # Mid-stream interruption after partial line without trailing newline keeps boundary clean.
        guard = RetryNoticeGuard()
        out = (
            guard.feed("1\n2\n3")
            + guard.feed("\n⚠ The stream was interrupted. Please continue the task you were working on.\n")
            + guard.feed("4\n5")
            + guard.feed("", final=True)
        )
        self.assertEqual(out, "1\n2\n3\n4\n5")
        self.assertEqual(guard.error_code, "NETWORK")

        # Mid-line quotes or incomplete heads are preserved verbatim.
        for text in (
            "用户引用：⚠ The stream was interrupted. Please continue the task you were working on.\n",
            "⚠ The stream was interrupted.\n",
            "The stream was interrupted. Please continue the task you were working on.\n",
        ):
            with self.subTest(text=text):
                guard = RetryNoticeGuard()
                out = "".join(guard.feed(ch) for ch in text) + guard.feed("", final=True)
                self.assertEqual(out, text)
                self.assertEqual(guard.error_code, "")

        for partial in ("⚠ The stream", "⚠️ The stream was interrupted. Please continu"):
            with self.subTest(partial=partial):
                guard = RetryNoticeGuard()
                self.assertEqual(guard.feed(partial), "")
                self.assertEqual(guard.feed("", final=True), partial)
                self.assertEqual(guard.error_code, "")

    def test_continuation_termination_bounds_and_quoted_multiline_preservation(self):
        # Independent body chunk without blank line right after wrapped notice is preserved.
        guard = RetryNoticeGuard()
        out = (
            guard.feed(
                '⚠ API error (attempt 1): request failed: Post\n'
                '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": unexpected EOF\n'
            )
            + guard.feed("独立正文第一行\n独立正文第二行")
            + guard.feed("", final=True)
        )
        self.assertEqual(out, "独立正文第一行\n独立正文第二行")
        self.assertEqual(guard.error_code, "NETWORK")

        # Defensive case: body starting with '"https://' is bounded by max 2 continuation lines.
        defensive = (
            "⚠ API error (attempt 1): 503 capacity\n"
            '"https://first.example.com"\n'
            '"https://second.example.com"\n'
            '"https://third.example.com"\n'
            "后续正常内容"
        )
        for char_mode in (False, True):
            with self.subTest(char_mode=char_mode):
                guard = RetryNoticeGuard()
                if char_mode:
                    out = "".join(guard.feed(ch) for ch in defensive) + guard.feed("", final=True)
                else:
                    out = guard.feed(defensive) + guard.feed("", final=True)
                self.assertEqual(out, '"https://third.example.com"\n后续正常内容')

        # Blank line terminates continuation before a following '"https://' body line.
        blank_stop = '⚠ API error (attempt 1): Post\n\n"https://body.example.com"\n'
        guard = RetryNoticeGuard()
        self.assertEqual(
            "".join(guard.feed(ch) for ch in blank_stop) + guard.feed("", final=True),
            '\n"https://body.example.com"\n',
        )

        # Non-URL line (including partial '"htt' prefix) terminates continuation and emits cleanly.
        non_url = '⚠ API error (attempt 1): Post\n"hello"\n"https://body.example.com"\n'
        guard = RetryNoticeGuard()
        self.assertEqual(
            "".join(guard.feed(ch) for ch in non_url) + guard.feed("", final=True),
            '"hello"\n"https://body.example.com"\n',
        )
        guard = RetryNoticeGuard()
        self.assertEqual(
            guard.feed('⚠ API error (attempt 1): Post\n"htt') + guard.feed("", final=True),
            '"htt',
        )

        # New ⚠ header resets continuation line budget and reclassifies.
        two_alerts = (
            '⚠ API error (attempt 1): Post\n'
            '"https://one.example.com"\n'
            '"https://two.example.com"\n'
            '⚠ API error (attempt 2): request failed: Post\n'
            '"https://three.example.com": EOF\n'
            "正常回复"
        )
        guard = RetryNoticeGuard()
        out = "".join(guard.feed(ch) for ch in two_alerts) + guard.feed("", final=True)
        self.assertEqual(out, "正常回复")
        self.assertEqual(guard.error_code, "NETWORK")

        # Terminal marker immediately after wrapped notice terminates continuation and raises.
        terminal = next(iter(TerminalNoticeGuard.messages))
        wrapped_then_terminal = (
            '⚠ API error (attempt 1): request failed: Post\n'
            '"https://example.com": EOF\n'
            f"⚠ {terminal}."
        )
        guard = _AgentTextGuard()
        with self.assertRaises(AgyError) as caught:
            for ch in wrapped_then_terminal:
                guard.feed(ch)
            guard.feed("", final=True)
        self.assertEqual(caught.exception.code, "NETWORK")

        # 4096B cap during continuation keeps diagnostic bounded and releases later URL lines.
        big_cont_head = (
            "⚠ API error (attempt 1): Post\n"
            + '"https://example.com/'
            + ("x" * 4200)
            + " unexpected eof"
        )
        guard = RetryNoticeGuard()
        out = "".join(
            guard.feed(big_cont_head[i : i + 128])
            for i in range(0, len(big_cont_head), 128)
        )
        self.assertEqual(out, "")
        self.assertLessEqual(len(guard.diagnostic), 4096)
        self.assertLessEqual(len(guard._notice_diagnostic), 4096)
        self.assertEqual(guard.error_code, "UPSTREAM")
        out_tail = guard.feed('\n"https://body.example.com"\n') + guard.feed("", final=True)
        self.assertEqual(out_tail, '"https://body.example.com"\n')
        self.assertEqual(guard.diagnostic, "")
        self.assertEqual(guard.error_code, "UPSTREAM")

        # User quoting the two-line alert in normal body text is preserved verbatim.
        quoted = (
            '报错日志如下：\n'
            '请看 ⚠ API error (attempt 1): request failed: Post\n'
            '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": unexpected EOF\n'
            '以上原文保留。'
        )
        guard = RetryNoticeGuard()
        out = "".join(guard.feed(ch) for ch in quoted) + guard.feed("", final=True)
        self.assertEqual(out, quoted)
        self.assertEqual(guard.error_code, "")
