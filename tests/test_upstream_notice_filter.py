import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401 -- install the offline host logger

from agy_provider import sessions as sessions_module
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import digest, normalize
from agy_provider.process import LaunchInfo
from agy_provider.sessions import Runtime, strip_upstream_capacity_notices
from agy_provider.transport import AcpClient, Event


def _make_notices(count: int, model: str = "gemini-3.8-flash-low") -> str:
    lines = [
        f"⚠ API error (attempt {i}): UNAVAILABLE (code 503): "
        f"No capacity available for model {model} on the server"
        for i in range(1, count + 1)
    ]
    return "\n" + "\n\n".join(lines)


class UpstreamNoticeFilterTests(unittest.TestCase):
    def test_five_notices_followed_by_answer_keeps_only_answer(self):
        answer = "只要编剧胆子大，明年直接让天理下池子给大伙端茶倒水"
        raw = _make_notices(5) + "\n" + answer
        cleaned, removed = strip_upstream_capacity_notices(raw)
        self.assertEqual(removed, 5)
        self.assertEqual(cleaned, answer)

    def test_only_notices_cleans_to_empty(self):
        raw = _make_notices(5) + "\n\n"
        cleaned, removed = strip_upstream_capacity_notices(raw)
        self.assertEqual(removed, 5)
        self.assertEqual(cleaned, "")

    def test_normal_answer_containing_error_is_preserved(self):
        samples = (
            "这是一个正常回答，里面提到了 error 和 503 状态码。",
            "API error (attempt 1): UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-low on the server",
            "⚠API error (attempt 1): UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-low on the server",
            "⚠ 这是用户自己写的警告：error occurred on the server",
            "第一段\n\n遇到 API error 时请稍后重试。\n\n第二段",
        )
        for text in samples:
            with self.subTest(text=text):
                cleaned, removed = strip_upstream_capacity_notices(text)
                self.assertEqual(removed, 0)
                self.assertEqual(cleaned, text)

    def test_whitespace_variations_and_internal_paragraphs(self):
        raw = (
            "  ⚠️   API error (attempt 1): UNAVAILABLE (code 503): "
            "No capacity available for model gemini-2.5-pro on the server  \n"
            "\n"
            "第一段\n"
            "\n"
            "第二段"
        )
        cleaned, removed = strip_upstream_capacity_notices(raw)
        self.assertEqual(removed, 1)
        self.assertEqual(cleaned, "第一段\n\n第二段")


class SessionAndProviderNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-notice-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def _run_stream(self, chunks: list[str], key: str | None = "conv-1"):
        runtime = Runtime(Settings(), self.root)
        self.addAsyncCleanup(runtime.close)
        request = normalize([], "你好", None, None, 1024)
        fake_client = types.SimpleNamespace(
            alive=True, capabilities={}, close=AsyncMock()
        )

        async def fake_open(session, system, snapshot=None):
            session.client = fake_client
            return {}

        async def fake_tool_events(client, prompt, broker):
            for chunk in chunks:
                yield Event("text", chunk)
            yield Event("done", result={"stopReason": "end_turn"})

        with (
            patch.object(runtime, "open", side_effect=fake_open),
            patch("agy_provider.sessions.select", AsyncMock()),
            patch("agy_provider.sessions.tool_events", side_effect=fake_tool_events),
        ):
            events = [
                event
                async for event in runtime.stream(
                    request, key, "gemini-3.8-flash", "low"
                )
            ]
        return runtime, request, events

    async def test_session_stream_strips_notices_updates_expected_and_logs_count(self):
        answer = "只要编剧胆子大，明年直接让天理下池子给大伙端茶倒水"
        chunks = [_make_notices(5) + "\n", answer]
        with patch.object(sessions_module.logger, "warning") as warning:
            runtime, request, events = await self._run_stream(chunks, key="conv-1")

        done = next(e for e in events if e.kind == "done")
        self.assertEqual(done.text, answer)
        session = runtime.sessions["conv-1"]
        self.assertEqual(
            session.expected,
            request.hashes + [digest({"role": "assistant", "content": answer})],
        )
        warning.assert_called_once()
        message, count = warning.call_args.args
        self.assertEqual(count, 5)
        self.assertNotIn(answer, message)
        self.assertNotIn("UNAVAILABLE", message)

    async def test_session_stream_raises_quota_when_only_notices(self):
        chunks = [_make_notices(5) + "\n"]
        with (
            patch.object(sessions_module.logger, "warning") as warning,
            self.assertRaises(AgyError) as caught,
        ):
            await self._run_stream(chunks, key="conv-only-notices")

        self.assertEqual(caught.exception.code, "QUOTA")
        self.assertIn("AGY 额度不足或容量不可用，请检查官方 CLI。", str(caught.exception))
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], 5)

    async def test_session_stream_keeps_empty_error_without_notices(self):
        with self.assertRaises(AgyError) as caught:
            await self._run_stream(["   \n  "], key="conv-empty")
        self.assertEqual(caught.exception.code, "EMPTY")

    async def test_provider_text_chat_returns_cleaned_answer_and_falls_back_on_quota(self):
        class FakeLLMResponse:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class FakeProvider:
            def __init__(self, provider_config, provider_settings):
                self.provider_config = provider_config
                self.provider_settings = provider_settings
                self._model = ""

            def set_model(self, model):
                self._model = model

            def get_model(self):
                return self._model

            def _ensure_message_to_dicts(self, contexts):
                return list(contexts or [])

        fake_logger = types.SimpleNamespace(warning=lambda *a, **kw: None)
        astrbot_mod = types.ModuleType("astrbot")
        api_mod = types.ModuleType("astrbot.api")
        api_mod.logger = fake_logger
        provider_mod = types.ModuleType("astrbot.api.provider")
        provider_mod.LLMResponse = FakeLLMResponse
        provider_mod.Provider = FakeProvider
        core_mod = types.ModuleType("astrbot.core")
        core_provider_mod = types.ModuleType("astrbot.core.provider")
        entities_mod = types.ModuleType("astrbot.core.provider.entities")
        entities_mod.TokenUsage = dict
        utils_mod = types.ModuleType("astrbot.core.utils")
        path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
        path_mod.get_astrbot_plugin_data_path = lambda: str(self.root)

        stubs = {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.provider": provider_mod,
            "astrbot.core": core_mod,
            "astrbot.core.provider": core_provider_mod,
            "astrbot.core.provider.entities": entities_mod,
            "astrbot.core.utils": utils_mod,
            "astrbot.core.utils.astrbot_path": path_mod,
        }
        with patch.dict(sys.modules, stubs):
            sys.modules.pop("agy_provider.provider", None)
            agy_provider_mod = importlib.import_module("agy_provider.provider")
            provider = agy_provider_mod.AgyProvider(
                {"id": "agy_probe", "model": "gemini-3.8-flash"}, {}
            )
            self.addAsyncCleanup(provider.terminate)

            answer = "只要编剧胆子大，明年直接让天理下池子给大伙端茶倒水"

            async def fake_open(session, system, snapshot=None):
                session.client = types.SimpleNamespace(
                    alive=True, capabilities={}, close=AsyncMock()
                )
                return {}

            async def success_events(client, prompt, broker):
                yield Event("text", _make_notices(5) + "\n")
                yield Event("text", answer)
                yield Event("done", result={"stopReason": "end_turn"})

            with (
                patch.object(provider.runtime, "open", side_effect=fake_open),
                patch("agy_provider.sessions.select", AsyncMock()),
                patch("agy_provider.sessions.tool_events", side_effect=success_events),
            ):
                resp = await provider.text_chat(prompt="你好", session_id="qq-group")
            self.assertEqual(resp.completion_text, answer)

            async def quota_events(client, prompt, broker):
                yield Event("text", _make_notices(5))
                yield Event("done", result={"stopReason": "end_turn"})

            with (
                patch.object(provider.runtime, "open", side_effect=fake_open),
                patch("agy_provider.sessions.select", AsyncMock()),
                patch("agy_provider.sessions.tool_events", side_effect=quota_events),
            ):
                with self.assertRaises(AgyError) as caught:
                    await provider.text_chat(prompt="你好", session_id="qq-group-2")
            self.assertEqual(caught.exception.code, "QUOTA")
        sys.modules.pop("agy_provider.provider", None)

    async def test_acp_client_and_runtime_stream_handle_retry_only_and_retry_then_answer(self):
        fixture = Path(__file__).with_name("fake_cli.py")
        settings = Settings(auth_bridge=False, startup_timeout=5, request_timeout=5)

        # Transport level: retry-only turns yield no text and carry agy_retry_error.
        for scenario, expected_code in (
            ("retry-quota-only", "QUOTA"),
            ("retry-eof-only", "NETWORK"),
            ("retry-multiline-eof-only", "NETWORK"),
            ("retry-post-eof-only", "NETWORK"),
            ("retry-stream-interrupted-only", "NETWORK"),
        ):
            client = AcpClient(settings)
            self.addAsyncCleanup(client.close)
            await client.start([sys.executable, str(fixture), scenario], self.root)
            await client.new_session(self.root)
            events = [event async for event in client.generate("hello")]
            self.assertEqual([e.kind for e in events], ["done"])
            self.assertEqual(events[0].result["agy_retry_error"], expected_code)
            await client.close()

            # Session level: empty body with agy_retry_error raises classified_error.
            runtime = Runtime(settings, self.root)
            self.addAsyncCleanup(runtime.close)
            request = normalize([], "你好", None, None, 1024)
            with (
                patch(
                    "agy_provider.sessions.discover",
                    AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
                ),
                patch(
                    "agy_provider.sessions.launch_args",
                    return_value=[sys.executable, str(fixture), scenario],
                ),
                patch("agy_provider.sessions.select", AsyncMock()),
            ):
                with self.assertRaises(AgyError) as caught:
                    async for _ in runtime.stream(request, "conv-retry", "gemini-test", "low"):
                        pass
                self.assertEqual(caught.exception.code, expected_code)
            await runtime.close()

        # Single-line or wrapped multi-line retry notice followed by normal text keeps only the real answer.
        for scenario in ("retry-then-answer", "retry-multiline-then-answer"):
            runtime = Runtime(settings, self.root)
            self.addAsyncCleanup(runtime.close)
            request = normalize([], "你好", None, None, 1024)
            with (
                patch(
                    "agy_provider.sessions.discover",
                    AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
                ),
                patch(
                    "agy_provider.sessions.launch_args",
                    return_value=[sys.executable, str(fixture), scenario],
                ),
                patch("agy_provider.sessions.select", AsyncMock()),
            ):
                events = [
                    event
                    async for event in runtime.stream(
                        request, "conv-ok", "gemini-test", "low"
                    )
                ]
            done = next(e for e in events if e.kind == "done")
            self.assertEqual(done.text, "真实回复内容")
            await runtime.close()

    async def test_retry_eof_then_terminal_marker_preserves_network_and_leaks_nothing(self):
        fixture = Path(__file__).with_name("fake_cli.py")
        settings = Settings(auth_bridge=False, startup_timeout=5, request_timeout=5)

        client = AcpClient(settings)
        self.addAsyncCleanup(client.close)
        await client.start([sys.executable, str(fixture), "retry-eof-terminal"], self.root)
        await client.new_session(self.root)
        transport_texts = []
        with self.assertRaises(AgyError) as caught_transport:
            async for ev in client.generate("hello"):
                if ev.kind == "text":
                    transport_texts.append(ev.text)
        self.assertEqual(caught_transport.exception.code, "NETWORK")
        self.assertEqual(transport_texts, [])
        await client.close()

        runtime = Runtime(settings, self.root)
        self.addAsyncCleanup(runtime.close)
        request = normalize([], "你好", None, None, 1024)
        emitted_texts = []
        with (
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch(
                "agy_provider.sessions.launch_args",
                return_value=[sys.executable, str(fixture), "retry-eof-terminal"],
            ),
            patch("agy_provider.sessions.select", AsyncMock()),
        ):
            with self.assertRaises(AgyError) as caught_session:
                async for ev in runtime.stream(
                    request, "conv-retry-eof-terminal", "gemini-test", "low"
                ):
                    if ev.kind == "text":
                        emitted_texts.append(ev.text)
            self.assertEqual(caught_session.exception.code, "NETWORK")
            self.assertEqual(emitted_texts, [])
        await runtime.close()
