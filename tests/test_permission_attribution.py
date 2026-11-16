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
    async def run_turn(self, *, deny=True, foreign=False, text="", retry=None, reason="end_turn"):
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
                        "toolCall": {"name": "browser", "toolCallId": "native", "status": "pending"},
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
            self.assertIn("web search or browser", _guardrails(mode))


if __name__ == "__main__":
    unittest.main()
