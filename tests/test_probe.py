"""Small preflight contracts; no installed CLI, account or model calls."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError, remote_error
from agy_provider.probe import capability_summary, probe
from agy_provider.process import LaunchInfo


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_auth_required_is_redacted(self):
        for error in (
            "Authentication required: private-example",
            {"code": -32000, "message": "private-example"},
        ):
            with self.subTest(error=error):
                classified = remote_error(error)
                self.assertEqual(classified.code, "AUTH")
                self.assertNotIn("private-example", str(classified))
        self.assertEqual(remote_error({"code": -32603}).code, "UPSTREAM")
        self.assertEqual(remote_error("403 forbidden").code, "UPSTREAM")

    def test_capability_allowlist(self):
        result = capability_summary(
            {
                "private-example": "hidden",
                "mcpCapabilities": {"http": "private-example", "sse": True},
                "promptCapabilities": {"image": True, "audio": 1},
            }
        )
        self.assertEqual(result["mcpCapabilities"], {"http": False, "sse": True})
        self.assertTrue(result["promptCapabilities"]["image"])
        self.assertFalse(result["promptCapabilities"]["audio"])
        self.assertNotIn("private-example", json.dumps(result))
        self.assertEqual(capability_summary([]), capability_summary({}))

    async def test_missing_cli_reports_discovery_blocker(self):
        with patch(
            "agy_provider.probe.discover",
            side_effect=AgyError("NOT_INSTALLED", "private-example"),
        ):
            report = await probe(Settings())
        self.assertEqual(report["stage"], "discovery")
        self.assertEqual(report["error_code"], "NOT_INSTALLED")
        self.assertNotIn("private-example", json.dumps(report))

    async def test_session_probe_cleans_up_without_generation(self):
        for failure in (None, AgyError("AUTH", "private-example")):
            with self.subTest(failure=failure):
                client = AsyncMock()
                client.capabilities = {"promptCapabilities": {"image": True}}
                client.new_session.side_effect = failure
                with (
                    patch(
                        "agy_provider.probe.discover",
                        return_value=LaunchInfo("/fake/agy", "private-example", False),
                    ),
                    patch("agy_provider.probe.AcpClient", return_value=client),
                ):
                    report = await probe(Settings())
                client.close.assert_awaited_once()
                client.generate.assert_not_called()
                home = client.start.call_args.args[1].parent
                self.assertFalse(home.exists())
                self.assertFalse(report["model_prompt_sent"])
                self.assertEqual(report["status"], "blocked" if failure else "passed")
                self.assertNotIn("private-example", json.dumps(report))
