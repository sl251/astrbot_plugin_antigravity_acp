"""Offline provider-to-CLI candidate telemetry acceptance; no live requests."""

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401
from tests.test_tools_mode import _load_provider_module

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import digest
from agy_provider.process import LaunchInfo

FIXTURE = Path(__file__).with_name("fake_cli.py")
SYSTEM = "PRIVATE_SYSTEM_429_人格原文不得落遥测"


class Content429IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, scenario):
        with tempfile.TemporaryDirectory(prefix="agy-content-429-") as temp:
            root = Path(temp)
            previous = sys.modules.pop("agy_provider.provider", None)
            try:
                with patch.dict(sys.modules, _load_provider_module(root)):
                    mod = importlib.import_module("agy_provider.provider")
                    mod.configure(Settings(
                        auth_bridge=False, cost_log_enabled=True, request_timeout=1,
                    ))
                    provider = mod.AgyProvider({
                        "id": "content-429-test", "model": "gemini-test",
                        "custom_extra_body": {"agy_tools_mode": "chat"},
                    }, {})
                    try:
                        with (
                            patch("agy_provider.sessions.discover", AsyncMock(
                                return_value=LaunchInfo("fake", "agy 1.5.9", False))),
                            patch("agy_provider.sessions.launch_args", return_value=[
                                sys.executable, str(FIXTURE), scenario]),
                            patch("agy_provider.telemetry.logger.info") as info,
                        ):
                            with self.assertRaises(AgyError) as caught:
                                await provider.text_chat(
                                    prompt="PRIVATE_PROMPT_429", system_prompt=SYSTEM,
                                    session_id="PRIVATE_SESSION_429",
                                )
                        self.assertEqual(len(provider.runtime.telemetry.history), 1)
                        info.assert_called_once()
                        line = info.call_args.args[0]
                        self.assertNotIn(SYSTEM, line)
                        self.assertNotIn("PRIVATE_PROMPT_429", line)
                        self.assertNotIn("PRIVATE_SESSION_429", line)
                        record = json.loads(line)
                        self.assertEqual(record, provider.runtime.telemetry.history[-1])
                        self.assertEqual(record["event"], "agy.turn")
                        self.assertEqual(record["status"], "error")
                        self.assertEqual(record["error_code"], caught.exception.code)
                        return record
                    finally:
                        await provider.terminate()
            finally:
                sys.modules.pop("agy_provider.provider", None)
                if previous is not None:
                    sys.modules["agy_provider.provider"] = previous

    async def test_candidate_quota_retry_info_and_partial_output(self):
        for scenario, code, has_output in (
            ("content-429", "RATE_LIMIT", False),
            ("content-429-quota", "QUOTA", None),
            ("content-429-retry-info", "RATE_LIMIT", None),
            ("content-429-after-text", "RATE_LIMIT", True),
        ):
            with self.subTest(scenario=scenario):
                record = await self.run_failure(scenario)
                self.assertEqual(record["error_code"], code)
                if has_output is None:
                    self.assertNotIn("content_429_candidate", record)
                else:
                    self.assertEqual(record["content_429_candidate"], {
                        "system_hash8": digest(SYSTEM)[:8],
                        "system_bytes": len(SYSTEM.encode("utf-8")),
                        "has_output": has_output,
                        "attempt": "2",
                    })

    @unittest.expectedFailure
    async def test_multiline_detail_free_notice_fast_fails_without_output(self):
        # Known gap: RetryNoticeGuard only accepts URL continuation lines.
        # Keep the requested literal stream visible without modifying notices.py.
        record = await self.run_failure("content-429-multiline")
        self.assertEqual(record["error_code"], "RATE_LIMIT")
        self.assertEqual(record["content_429_candidate"], {
            "system_hash8": digest(SYSTEM)[:8],
            "system_bytes": len(SYSTEM.encode("utf-8")),
            "has_output": False,
            "attempt": "2",
        })


if __name__ == "__main__":
    unittest.main()
