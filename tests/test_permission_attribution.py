"""Permission denial explains an empty end_turn without quota/dedupe side effects."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import normalize
from agy_provider.process import _guardrails
from agy_provider.sessions import Runtime
from agy_provider.transport import AcpClient


class PermissionAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def run_turn(
        self, *, deny=True, foreign=False, text="", retry=None, reason="end_turn", tool_name="browser"
    ):
        temp = tempfile.TemporaryDirectory(prefix="agy-permission-")
        self.addCleanup(temp.cleanup)
        settings = Settings(auth_bridge=False)
        runtime = Runtime(settings, Path(temp.name))
        self.addAsyncCleanup(runtime.close)
        client = AcpClient(settings)
        client.proc = SimpleNamespace(returncode=None)
        client.session_id = "current"
        client._write = AsyncMock()
        client.close = AsyncMock()
        request = normalize([], "hello", None, None, 1024)

        async def open_session(session, system, snapshot=None):
            session.client = client
            return {}

        async def prompt(method, params, timeout):
            client.prompt_sent = True
            if deny:
                await client._reject_request({
                    "id": "permission", "method": "session/request_permission",
                    "params": {
                        "sessionId": "foreign" if foreign else "current",
                        "toolCall": {"name": tool_name, "toolCallId": "native", "status": "pending"},
                        "options": [{"optionId": "allow", "kind": "allow_once"}],
                    },
                })
            if text:
                client._on_update({"sessionId": "current", "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                }})
            result = {"stopReason": reason, "usage": {"inputTokens": 12, "outputTokens": 3}}
            if retry:
                result["agy_retry_error"] = retry
            return result

        client.request = AsyncMock(side_effect=prompt)
        error, events = None, []
        with patch.object(runtime, "open", side_effect=open_session), patch(
            "agy_provider.sessions.select", AsyncMock()
        ):
            try:
                events = [event async for event in runtime.stream(
                    request, "session-key", "test-model", "default",
                    provider_id="provider", request_fingerprint="fingerprint",
                )]
            except AgyError as exc:
                error = exc
        return runtime, error, events

    async def test_current_denial_explains_empty_without_cooldown(self):
        runtime, error, _ = await self.run_turn()
        self.assertEqual(error.code, "TOOL_PERMISSION")
        self.assertIn("CLI", str(error))
        check = await runtime.failure_guard.check(
            ("provider", "test-model"), "fingerprint", session_key="session-key"
        )
        self.assertTrue(check.allowed)
        self.assertEqual(runtime.telemetry.history[-1]["error_code"], "TOOL_PERMISSION")

    async def test_unexplained_empty_foreign_denial_and_max_tokens_stay_empty(self):
        for kwargs in ({"deny": False}, {"foreign": True}, {"reason": "max_tokens"}):
            with self.subTest(kwargs=kwargs):
                _, error, _ = await self.run_turn(**kwargs)
                self.assertEqual(error.code, "EMPTY")

    async def test_upstream_retry_classification_takes_precedence(self):
        for code in ("RATE_LIMIT", "QUOTA"):
            with self.subTest(code=code):
                _, error, _ = await self.run_turn(retry=code)
                self.assertEqual(error.code, code)

    async def test_denial_does_not_override_successful_text(self):
        _, error, events = await self.run_turn(text="正常答复")
        self.assertIsNone(error)
        self.assertEqual(events[-1].text, "正常答复")

    def test_guardrails_explicitly_disallow_native_web_and_browser_tools(self):
        for mode in (False, True):
            text = _guardrails(mode)
            self.assertIn("web search or browser", text)
            self.assertIn("Do not use read_url or execute_url", text)
        chat_guard = _guardrails(False)
        self.assertIn("Do not call any declared tool or function", chat_guard)
        for token in ("update_steps", "ask_question", "swarm", "send_message"):
            self.assertIn(token, chat_guard)

    def test_no_tools_unknown_tool_call_sets_no_retry_and_no_fallback(self):
        client = AcpClient(Settings(auth_bridge=False))
        client.active = True
        client.session_id = "chat-s1"
        with self.assertRaises(AgyError) as caught:
            client._on_update({
                "sessionId": "chat-s1",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "c-unknown",
                    "name": "future_unregistered_tool",
                    "status": "in_progress",
                },
            })
        self.assertEqual(caught.exception.code, "TOOLS_DISABLED")
        self.assertTrue(caught.exception.no_retry)
        self.assertTrue(caught.exception.no_fallback)
        self.assertNotIn("无工具 agent", str(caught.exception))

    async def test_no_tools_unknown_permission_denial_message_matches_chat_context(self):
        _, error, _ = await self.run_turn(tool_name="future_unregistered_tool")
        self.assertEqual(error.code, "TOOL_PERMISSION")
        self.assertNotIn("改用纯聊天模式", str(error))
        self.assertIn("未启用工具", str(error))

    async def test_provider_chat_mode_builtin_denied_notice_omits_mcp_suggestion(self):
        import importlib
        import sys
        from agy_provider.builtin_tools import BuiltinDenied
        from tests.test_tools_mode import _load_provider_module

        temp = tempfile.TemporaryDirectory(prefix="agy-chat-notice-")
        self.addCleanup(temp.cleanup)
        stubs = _load_provider_module(Path(temp.name))
        with patch.dict(sys.modules, stubs):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            mod.configure(Settings(enable_tools=False, auth_bridge=False))
            self.addCleanup(mod.configure, Settings())
            provider = mod.AgyProvider({"id": "chat-notice", "model": "gemini-test"}, {})
            self.addAsyncCleanup(provider.terminate)

            async def fail_builtin(*args, **kwargs):
                if False:
                    yield None
                raise BuiltinDenied()

            with patch.object(provider.runtime, "stream", side_effect=fail_builtin):
                resp = await provider.text_chat(prompt="hello", session_id="s1")
            self.assertIn("受限的 CLI 内置工具", resp.completion_text)
            self.assertNotIn("AstrBot 已启用的工具", resp.completion_text)
        sys.modules.pop("agy_provider.provider", None)


if __name__ == "__main__":
    unittest.main()
