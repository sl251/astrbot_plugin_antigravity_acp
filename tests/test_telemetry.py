import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import normalize
from agy_provider.process import LaunchInfo, launch_args as real_launch_args
from agy_provider.sessions import Runtime
from agy_provider.telemetry import (
    TelemetryTracker,
    extract_acp_usage,
    format_cost_report,
    pick_primary_reason,
)

FIXTURE = Path(__file__).with_name("fake_cli.py")


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-telemetry-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _capture_info_logs(self):
        records = []

        class _Logger:
            def info(self, msg, *args):
                records.append(msg % args if args else str(msg))

            def warning(self, msg, *args):
                pass

        return records, _Logger()

    async def test_telemetry_end_states_success_empty_and_no_usage_sanitized(self):
        logs, logger_stub = self._capture_info_logs()
        now = 100.0

        def fake_clock():
            return now

        def run_with_scenario(scenario):
            def fake_launch(info, home, use_default_agent=False):
                args = real_launch_args(info, home, use_default_agent)
                return [sys.executable, str(FIXTURE), scenario, *args[1:]]

            return patch("agy_provider.sessions.launch_args", side_effect=fake_launch)

        secret_prompt = "SECRET_PROMPT_DO_NOT_LOG_12345"
        secret_session = "raw-user-qq-987654321"
        secret_token = "sk-secret-api-credential-xyz"

        with (
            patch("agy_provider.telemetry.logger", logger_stub),
            patch("agy_provider.sessions.logger", logger_stub),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
        ):
            # 1) Success with update + terminal usage (must use terminal, not sum!)
            rt_ok = Runtime(
                Settings(auth_bridge=False, cost_log_enabled=True),
                self.root / "rt-ok",
                clock=fake_clock,
            )
            self.addAsyncCleanup(rt_ok.close)
            with run_with_scenario("usage"):
                req = normalize([], secret_prompt, f"sys {secret_token}", [], 160000)
                async for _ in rt_ok.stream(
                    req, secret_session, "gemini-test", "low", provider_id="p1"
                ):
                    pass

            # 2) EMPTY failure with reported usage (input=500, output=0)
            rt_empty = Runtime(
                Settings(auth_bridge=False, cost_log_enabled=True),
                self.root / "rt-empty",
                clock=fake_clock,
            )
            self.addAsyncCleanup(rt_empty.close)
            with run_with_scenario("empty"):
                req = normalize([], secret_prompt, "sys", [], 160000)
                with self.assertRaises(AgyError) as caught:
                    async for _ in rt_empty.stream(
                        req, secret_session, "gemini-test", "low", provider_id="p2"
                    ):
                        pass
                self.assertEqual(caught.exception.code, "EMPTY")

            # 3) Success with NO usage field -> null + unavailable, never coerced to 0!
            rt_no_usage = Runtime(
                Settings(auth_bridge=False, cost_log_enabled=True),
                self.root / "rt-no-usage",
                clock=fake_clock,
            )
            self.addAsyncCleanup(rt_no_usage.close)
            with run_with_scenario("default"):
                req = normalize([], secret_prompt, "sys", [], 160000)
                async for _ in rt_no_usage.stream(
                    req, secret_session, "gemini-test", "low", provider_id="p3"
                ):
                    pass

            # 4) RPC error carrying usage in data -> error state still retains usage!
            rt_err = Runtime(
                Settings(auth_bridge=False, cost_log_enabled=True),
                self.root / "rt-err",
                clock=fake_clock,
            )
            self.addAsyncCleanup(rt_err.close)
            with run_with_scenario("error-with-usage"):
                req = normalize([], secret_prompt, "sys", [], 160000)
                with self.assertRaises(AgyError):
                    async for _ in rt_err.stream(
                        req, secret_session, "gemini-test", "low", provider_id="p4"
                    ):
                        pass

        self.assertEqual(len(logs), 4)
        parsed = [json.loads(line) for line in logs]

        # Must never leak raw prompt, answer, session id, or system secret
        joined = "\n".join(logs)
        self.assertNotIn(secret_prompt, joined)
        self.assertNotIn(secret_session, joined)
        self.assertNotIn(secret_token, joined)
        self.assertNotIn("fake answer", joined)

        # Turn 1: terminal usage (inputTokens=1200, cachedReadTokens=800, output=42)
        r_ok = parsed[0]
        self.assertEqual(r_ok["event"], "agy.turn")
        self.assertEqual(r_ok["status"], "ok")
        self.assertEqual(r_ok["usage_status"], "reported")
        self.assertEqual(r_ok["usage_source"], "acp_terminal")
        self.assertEqual(r_ok["input_other"], 1200)
        self.assertEqual(r_ok["input_cached"], 800)
        self.assertEqual(r_ok["output"], 42)
        self.assertTrue(r_ok["cache_fields_present"]["cachedReadTokens"])
        self.assertIsNotNone(r_ok["session_key"])
        self.assertNotEqual(r_ok["session_key"], secret_session)
        self.assertFalse(r_ok["reused"])
        self.assertEqual(r_ok["rebuild_reasons"], ["cold_start"])
        self.assertEqual(r_ok["primary_reason"], "cold_start")
        self.assertEqual(r_ok["history_relation"], "unknown")

        # Turn 2: EMPTY failure retains reported usage
        r_empty = parsed[1]
        self.assertEqual(r_empty["status"], "error")
        self.assertEqual(r_empty["error_code"], "EMPTY")
        self.assertEqual(r_empty["usage_status"], "reported")
        self.assertEqual(r_empty["input_other"], 500)
        self.assertEqual(r_empty["output"], 0)

        # Turn 3: Missing usage reports None ("null" in JSON) and unavailable
        r_none = parsed[2]
        self.assertEqual(r_none["status"], "ok")
        self.assertEqual(r_none["usage_status"], "unavailable")
        self.assertIsNone(r_none["input_other"])
        self.assertIsNone(r_none["input_cached"])
        self.assertIsNone(r_none["output"])

        # Turn 4: Error with usage retains reported usage even on RPC exception
        r_err = parsed[3]
        self.assertEqual(r_err["status"], "error")
        self.assertEqual(r_err["usage_status"], "reported")
        self.assertEqual(r_err["input_other"], 650)
        self.assertEqual(r_err["input_cached"], 100)
        self.assertEqual(r_err["output"], 5)

    async def test_priority_tombstone_ring_buffer_and_cost_report(self):
        self.assertEqual(
            pick_primary_reason(["effort_changed", "abort", "system_changed"]),
            "abort",
        )
        buckets, cache_present = extract_acp_usage(
            {"usage": {"inputTokens": 300, "outputTokens": 15}}
        )
        self.assertEqual(buckets, {"input_other": 300, "input_cached": 0, "output": 15})
        self.assertFalse(cache_present["cachedReadTokens"])

        t = 10.0
        tracker = TelemetryTracker(cost_log_enabled=False, clock=lambda: t)
        meter = tracker.start_turn(
            provider="p1", session_key_raw="s1", mode="chat", model="m1", effort="low"
        )
        meter.set_decision(
            reused=False,
            reasons=["cold_start"],
            history_relation="unknown",
            prompt_bytes=20,
            replay_bytes=0,
        )
        meter.request_sent = True
        first = meter.finish(status="ok")
        second = meter.finish(status="error", error_code="UPSTREAM")
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # idempotent!

        # Bounded tombstones
        for i in range(1100):
            tracker.record_eviction(f"key-{i}", "pool_evicted")
        self.assertEqual(len(tracker.tombstones), 1024)
        self.assertIsNone(tracker.consume_tombstone("key-0"))
        self.assertEqual(tracker.consume_tombstone("key-1099"), "pool_evicted")
        # Expire after 3600s
        tracker.record_eviction("exp-key", "idle_expired")
        t += 3601.0
        self.assertIsNone(tracker.consume_tombstone("exp-key"))

        report = format_cost_report(
            [
                {
                    "provider_id": "p1",
                    "model": "m1",
                    "tools_mode": "chat",
                    "effective_mode": "chat",
                    "pool": {"active": 1, "busy": 0, "capacity": 16},
                    "summary": tracker.summarize(),
                }
            ],
            {
                "session_reuse_mode": "strict",
                "history_bytes": 160000,
                "cost_log_enabled": False,
                "empty_dedupe_ttl": 30,
                "quota_cooldown": 60,
            },
        )
        self.assertIn("只读，未发送模型请求", report)
        self.assertIn("reuse=strict", report)
        self.assertIn("100.0%", report)  # unavailable_ratio = 1/1

    async def test_precheck_failure_still_emits_not_sent_record(self):
        logs, logger_stub = self._capture_info_logs()

        class _FakeProvider:
            def __init__(self, provider_config, provider_settings, **kwargs):
                self.provider_config = provider_config
                self.provider_settings = provider_settings
                self.model_name = provider_config.get("model", "")

            def get_model(self):
                return self.model_name

            def set_model(self, model_name):
                self.model_name = model_name

            def _ensure_message_to_dicts(self, contexts):
                return list(contexts or [])

        stubs = {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": types.SimpleNamespace(logger=logger_stub),
            "astrbot.api.provider": types.SimpleNamespace(
                LLMResponse=dict, Provider=_FakeProvider
            ),
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.provider": types.ModuleType("astrbot.core.provider"),
            "astrbot.core.provider.entities": types.SimpleNamespace(TokenUsage=dict),
            "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
            "astrbot.core.utils.astrbot_path": types.SimpleNamespace(
                get_astrbot_plugin_data_path=lambda: str(self.root)
            ),
        }
        with (
            patch.dict(sys.modules, stubs),
            patch("agy_provider.telemetry.logger", logger_stub),
        ):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            mod.configure(Settings(cost_log_enabled=True, auth_bridge=False))
            provider = mod.AgyProvider(
                {"id": "agy_pre", "model": "gemini-test"},
                {"cost_log_enabled": True, "auth_bridge": False},
            )
            self.addAsyncCleanup(provider.terminate)
            with self.assertRaises(AgyError) as caught:
                await provider.text_chat(prompt="hi", audio_urls=["a.mp3"])
            self.assertEqual(caught.exception.code, "UNSUPPORTED")
        sys.modules.pop("agy_provider.provider", None)

        self.assertEqual(len(logs), 1)
        rec = json.loads(logs[0])
        self.assertEqual(rec["usage_status"], "not_sent")
        self.assertEqual(rec["input_other"], 0)
        self.assertEqual(rec["error_code"], "UNSUPPORTED")


if __name__ == "__main__":
    unittest.main()
