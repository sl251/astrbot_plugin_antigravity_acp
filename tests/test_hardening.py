"""Deterministic occupancy, failure, cancellation and cost acceptance."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401
from agy_provider.config import Settings
from agy_provider.context_usage import ContextUsage
from agy_provider.errors import AgyError, remote_error
from agy_provider.failure_guard import FailureGuard
from agy_provider.messages import normalize
from agy_provider.notices import RetryNoticeGuard
from agy_provider.process import LaunchInfo, launch_args
from agy_provider.sessions import Runtime
from agy_provider.telemetry import TelemetryTracker
from agy_provider.transport import AcpClient, _AgentTextGuard

FIXTURE = Path(__file__).with_name("fake_cli.py")


class OccupancyTests(unittest.TestCase):
    def test_growth_thresholds_and_unknowns(self):
        for previous, current, detected in ((63999, 100, False), (64000, 32000, False),
                                             (64000, 31999, True), (100000, 110000, False)):
            with self.subTest(previous=previous, current=current):
                state = ContextUsage()
                state.observe({"used": previous, "size": 200000})
                state.observe({"used": current, "size": 200000})
                self.assertEqual(state.suspected_compaction, detected)

    def test_invalid_and_changed_window_break_comparison(self):
        for invalid in ({}, {"used": 0, "size": 200000}, {"used": None, "size": 200000},
                        {"used": True, "size": 200000}, {"used": 1.0, "size": 200000},
                        {"used": 300000, "size": 200000}, {"used": 100, "size": 2**54},
                        {"used": 20000, "size": 300000}):
            with self.subTest(invalid=invalid):
                state = ContextUsage()
                state.observe({"used": 100000, "size": 200000})
                state.observe(invalid)
                state.observe({"used": 10000, "size": 200000})
                self.assertFalse(state.suspected_compaction)

    def test_session_identity_cache_and_model_isolation(self):
        one, two = AcpClient(Settings()), AcpClient(Settings())
        for client in (one, two):
            client.active, client.session_id = True, "one" if client is one else "two"
        def update(client, kind, **values):
            client._on_update({"sessionId": "one", "update": {"sessionUpdate": kind, **values}})
        update(one, "usage_update", used=100000, size=200000)
        update(two, "usage_update", used=10000, size=200000)
        self.assertIsNone(two.context_usage.used)
        update(one, "usage_update", usage={"inputTokens": 10, "cachedReadTokens": 90000, "outputTokens": 1})
        update(one, "usage_update", used=10000, size=200000)
        self.assertFalse(one.context_usage.suspected_compaction)
        update(one, "usage_update", used=100000, size=200000)
        update(one, "current_model_update")
        update(one, "usage_update", used=10000, size=200000)
        self.assertFalse(one.context_usage.suspected_compaction)

    def test_new_config_validation_and_minimal_arguments(self):
        defaults = Settings.from_dict({})
        self.assertEqual(defaults.agy_customizations_mode, "inherit")
        for value in (True, "unsafe", None):
            with self.assertRaises(AgyError):
                Settings.from_dict({"agy_customizations_mode": value})
        for key in ("context_compaction_min_peak", "context_compaction_drop_percent"):
            with self.assertRaises(AgyError):
                Settings.from_dict({key: 0})
        info = LaunchInfo("fake", "1.5.9", False, True)
        home = Path("session")
        self.assertNotIn("--include-default-customizations=false", launch_args(info, home, True))
        self.assertIn("--include-default-customizations=false", launch_args(info, home, True, customizations_mode="minimal"))
        self.assertIn("--agent", launch_args(info, home, customizations_mode="minimal"))
        with self.assertRaises(AgyError) as caught:
            launch_args(LaunchInfo("fake", "old", False), home, True, customizations_mode="minimal")
        self.assertEqual(caught.exception.code, "UNSUPPORTED")


class ErrorStateTests(unittest.TestCase):
    def test_strong_errors_at_every_chunk_boundary(self):
        for message, code in (("Daily quota exhausted", "QUOTA"), ("401 UNAUTHENTICATED", "AUTH"),
                              ("Invalid token", "AUTH")):
            banner = f"⚠ API error (attempt 1): {message}\n"
            for index in range(len(banner) + 1):
                guard = _AgentTextGuard()
                with self.assertRaises(AgyError) as caught:
                    guard.feed(banner[:index])
                    guard.feed(banner[index:])
                    guard.feed("", final=True)
                self.assertEqual(caught.exception.code, code)

    def test_rate_attempts_transient_recovery_and_prose(self):
        def banner(attempt, message="429 RESOURCE_EXHAUSTED"):
            return f"⚠ API error (attempt {attempt}): {message}\n"
        guard = RetryNoticeGuard()
        for char in banner(1) + banner(1):
            guard.feed(char)
        self.assertEqual(guard.fast_fail_code, "")
        for char in banner(2):
            guard.feed(char)
        self.assertEqual(guard.fast_fail_code, "RATE_LIMIT")
        for middle in (banner(2, "503 UNAVAILABLE: No capacity"), "Recovered answer\n"):
            guard = RetryNoticeGuard()
            guard.feed(banner(1) + middle + banner(3))
            self.assertEqual(guard.fast_fail_code, "")
        for prose in ('HTTP 429 is a status code.\n', '> ⚠ API error (attempt 1): Daily quota exhausted\n'):
            self.assertEqual(_AgentTextGuard().feed(prose, final=True), prose)

    def test_explicit_rpc_quota_and_capacity_are_distinct(self):
        self.assertEqual(remote_error("Resource has been exhausted (quota).").code, "QUOTA")
        self.assertEqual(remote_error("429 RESOURCE_EXHAUSTED").code, "RATE_LIMIT")
        self.assertEqual(remote_error("503 No capacity").code, "RATE_LIMIT")


class HardeningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acp-hardening-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake_runtime(self, scenario):
        runtime = Runtime(Settings(auth_bridge=False, request_timeout=3), self.root / scenario)
        self.addAsyncCleanup(runtime.close)
        self.enterContext(patch("agy_provider.sessions.discover", AsyncMock(return_value=LaunchInfo("fake", "1.5.9", False))))
        self.enterContext(patch("agy_provider.sessions.launch_args", return_value=[sys.executable, str(FIXTURE), scenario]))
        return runtime

    async def test_compaction_delivers_and_retires_then_rebuilds(self):
        runtime = self.fake_runtime("context-drop")
        request = normalize([], "first", "system", [], 160000)
        events = [event async for event in runtime.stream(request, "identity", "gemini-test", "low")]
        self.assertEqual(events[-1].text, "complete answer")
        self.assertNotIn("identity", runtime.sessions)
        record = runtime.telemetry.history[-1]
        self.assertTrue(record["upstream_compaction_detected"])
        self.assertIsNone(record["input_other"])
        history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "complete answer"}]
        request = normalize(history, "second", "system", [], 160000)
        _ = [event async for event in runtime.stream(request, "identity", "gemini-test", "low")]
        self.assertIn("upstream_compaction_detected", runtime.telemetry.history[-1]["rebuild_reasons"])
        self.assertFalse(runtime.telemetry.history[-1]["reused"])

    async def test_growth_keeps_live_session(self):
        runtime = self.fake_runtime("context-grow")
        _ = [event async for event in runtime.stream(normalize([], "hi", "", [], 160000), "id", "gemini-test", "low")]
        self.assertTrue(runtime.sessions["id"].client.alive)
        self.assertFalse(runtime.telemetry.history[-1]["upstream_compaction_detected"])

    async def test_fast_fail_sends_cancel_after_failure_and_preserves_usage(self):
        for scenario, code in (("strong-quota", "QUOTA"), ("strong-auth", "AUTH"), ("strong-rate", "RATE_LIMIT")):
            with self.subTest(scenario=scenario):
                log = self.root / f"{scenario}.jsonl"
                client = AcpClient(Settings(auth_bridge=False, request_timeout=3))
                self.addAsyncCleanup(client.close)
                with patch.dict(os.environ, {"AGY_TEST_LOG": str(log)}):
                    await client.start([sys.executable, str(FIXTURE), scenario], self.root)
                await client.new_session(self.root)
                async with asyncio.timeout(2):
                    with self.assertRaises(AgyError) as caught:
                        _ = [event async for event in client.generate("hi")]
                self.assertEqual(caught.exception.code, code)
                # Wait for an observed event, rather than guessing a process delay.
                async with asyncio.timeout(2):
                    while '"cancel"' not in log.read_text(encoding="utf-8"):
                        await asyncio.sleep(0.01)
                self.assertEqual(sum(json.loads(line)["event"] == "cancel" for line in log.read_text().splitlines()), 1)
                self.assertEqual(client.latest_usage["inputTokens"], 150)
                self.assertFalse(client.pending)
                await client.close()
                self.assertTrue(client.reader.done() and client.stderr_reader.done())

    async def test_rate_limit_guard_code_half_open_and_zero_short_circuit(self):
        now = [100.0]
        guard = FailureGuard(clock=lambda: now[0])
        guard.record_failure(("provider", "model"), "fp", "RATE_LIMIT", origin_turn_id="turn", quota_ttl=60)
        check = await guard.check(("provider", "model"), "other")
        self.assertFalse(check.allowed)
        self.assertEqual(check.code, "RATE_LIMIT")
        now[0] += 61
        checks = await asyncio.gather(*(guard.check(("provider", "model"), "other") for _ in range(3)))
        self.assertEqual(sum(check.allowed for check in checks), 1)
        guard.record_success(("provider", "model"))
        self.assertTrue((await guard.check(("provider", "model"), "other")).allowed)

    async def test_telemetry_ttl_capacity_groups_and_idempotence(self):
        now = [0.0]
        tracker = TelemetryTracker(clock=lambda: now[0])
        for index in range(105):
            meter = tracker.start_turn(provider="p", session_key_raw="private-user", mode="tools" if index % 2 else "chat", model="m", effort="low")
            meter.request_sent = True
            if index % 2:
                meter.observe_usage({"usage": {"inputTokens": 100, "outputTokens": 2}}, source="acp_error")
            meter.finish(status="error" if index % 2 else "ok")
            self.assertIsNone(meter.finish(status="ok"))
        summary = tracker.summarize()
        self.assertEqual(summary["total_turns"], 100)
        self.assertEqual(sum(group["turns"] for group in summary["groups"]), 100)
        self.assertEqual(sum(group["cached_read_unknown"] for group in summary["groups"]), 50)
        self.assertFalse(tracker.emitted_lines)
        self.assertNotIn("private-user", json.dumps(list(tracker.history)))
        now[0] = 86401
        self.assertEqual(tracker.summarize()["total_turns"], 0)
