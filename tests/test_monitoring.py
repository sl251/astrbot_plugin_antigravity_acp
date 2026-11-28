"""Monitoring contract, host identity gate and route lifetime (offline)."""

import importlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401
from tests.test_onboarding import FakeManager
from tests.test_tools_mode import _load_provider_module
from agy_provider.config import Settings
from agy_provider.context_usage import ContextUsage
from agy_provider.monitoring import Monitoring
from agy_provider.sessions import Session
from agy_provider.transport import AcpClient


class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-monitor-")
        self.addCleanup(self.temp.cleanup)
        previous = sys.modules.pop("agy_provider.provider", None)
        self.addCleanup(self.restore_provider, previous)
        modules = _load_provider_module(Path(self.temp.name))
        self.request = types.SimpleNamespace(username="panel-owner")
        modules["astrbot.api.web"] = types.SimpleNamespace(
            request=self.request,
            json_response=lambda data, **kw: dict(data=data, status_code=200, **kw),
            error_response=lambda message, **kw: dict(message=message, **kw),
        )
        stub = patch.dict(sys.modules, modules)
        stub.start()
        self.addCleanup(stub.stop)
        self.provider = importlib.import_module("agy_provider.provider")
        self.provider.configure(Settings(cost_log_enabled=False, auth_bridge=False))
        self.manager = FakeManager()
        self.manager.acm.default_conf["dashboard"] = {"username": "panel-owner"}
        self.manager.acm.default_conf["agent_runner"] = {
            "runner_type": "local",
            "config": {"model": {"provider_id": "one", "fallback_provider_ids": []}},
        }
        self.context = types.SimpleNamespace(
            provider_manager=self.manager, registered_web_apis=[],
        )
        self.context.register_web_api = lambda *row: self.context.registered_web_apis.append(row)
        self.service = Monitoring(self.context)
        self.addAsyncCleanup(self.service.close)

    @staticmethod
    def restore_provider(previous):
        sys.modules.pop("agy_provider.provider", None)
        if previous is not None:
            sys.modules["agy_provider.provider"] = previous

    def instance(self, identity, *, active=True, history_id=None):
        config = {"id": identity, "type": "agy_acp", "enable": True,
                  "model": "gemini-test", "custom_extra_body": {"agy_tools_mode": "chat"}}
        if history_id:
            config["agy_history_id"] = history_id
        self.manager.providers_config.append(config)
        if not active:
            return None
        inst = self.provider.AgyProvider(config, {})
        self.manager.inst_map[identity] = inst
        self.addAsyncCleanup(inst.terminate)
        return inst

    async def test_detailed_statistics_survive_runtime_and_store_restart(self):
        from agy_provider.telemetry import daily_snapshot, failure_snapshot, start_diagnostics, stop_diagnostics

        start_diagnostics(Path(self.temp.name))
        self.addCleanup(stop_diagnostics)
        old = self.instance("one", history_id="a" * 32)
        meter = old.runtime.telemetry.start_turn(
            provider="one", session_key_raw="private", model="gemini-test", mode="chat", effort="low")
        meter.request_sent = True
        meter.finish(status="error", error_code="TIMEOUT")
        await old.terminate()
        start_diagnostics(Path(self.temp.name))
        config = self.manager.providers_config[0]
        new = self.provider.AgyProvider({**config, "model": "gemini-new"}, {})
        self.addAsyncCleanup(new.terminate)
        self.manager.inst_map["one"] = new
        row, = (await self.service.handle())["data"]["instances"]
        self.assertEqual(row["history_storage"], "available")
        self.assertEqual(row["summary"]["total_turns"], 1)
        self.assertEqual(row["recent_turns"][0]["model"], "gemini-test")
        self.assertTrue(row["recent_turns"][0]["historical"])
        self.assertIsNone(row["summary"]["total_output"])
        self.assertIsNone(row["latest_context"]["context_used"])
        self.assertEqual(row["pool"]["active_sessions"], 0)
        self.assertEqual(daily_snapshot()["days"][0]["requests"], 1)
        self.assertEqual(len(failure_snapshot()["failure_events"]), 1)
        self.assertEqual(new.runtime.telemetry.summarize()["candidate_turns"], 1)
        self.assertFalse(new.runtime.telemetry.tombstones)

        next_turn = new.runtime.telemetry.start_turn(
            provider="one", session_key_raw="private", model="gemini-new", mode="chat", effort="low")
        next_turn.request_sent = True
        next_turn.reused = True
        next_turn.finish(status="ok")
        next_turn.finish(status="ok")
        row, = (await self.service.handle())["data"]["instances"]
        self.assertEqual(row["summary"]["reuse_rate"], 0.5)
        self.assertEqual([t["model"] for t in row["recent_turns"]], ["gemini-new", "gemini-test"])
        self.assertEqual(daily_snapshot()["days"][0]["requests"], 2)
        self.assertEqual(len(failure_snapshot()["failure_events"]), 1)

        for changed in ({"agy_history_id": "b" * 32}, {"id": "two"}, {"agy_history_id": None}):
            other = self.provider.AgyProvider({**config, **changed}, {})
            self.addAsyncCleanup(other.terminate)
            self.assertEqual(other.runtime.telemetry.monitor_summary()["total_turns"], 0)

    async def test_status_contract_allowlist_and_instance_isolation(self):
        one, two = self.instance("one"), self.instance("two")
        self.instance("configured", active=False)
        now = 100.0
        one.runtime.telemetry.clock = lambda: now
        secret = "PRIVATE_PROMPT_SYSTEM_SESSION_TOOL_ARGUMENT"
        for _ in range(25):
            meter = one.runtime.telemetry.start_turn(
                provider="one", session_key_raw=secret, model="gemini-test", mode="chat", effort="low")
            meter.request_sent = True
            record = meter.finish(status="error", error_code="RATE_LIMIT")
            record.update(prompt=secret, system=secret, tool_schema={"private": secret},
                          arguments=secret, native_session_tag=secret)
            record["cache_fields_present"]["private"] = secret
            record["rebuild_reasons"] = ["cold_start", secret]
            record["content_429_candidate"] = {
                "system_hash8": "1234abcd", "system_bytes": 42,
                "attempt": "2", "has_output": False, "system": secret,
            }
        usage = ContextUsage()
        usage.observe({"used": 100, "size": 1000})
        client = types.SimpleNamespace(alive=True, context_usage=usage, close=AsyncMock())
        one.runtime.sessions[secret] = Session(client=client, model="gemini-test")
        # Any accidental request from monitor fails the contract, even when a
        # configured instance lacks a runtime. It must never initialize one.
        with (
            patch("agy_provider.sessions.Runtime.launch_info", side_effect=AssertionError("AGY request")),
            patch("agy_provider.transport.AcpClient.request", side_effect=AssertionError("AGY request")),
            patch("agy_provider.probe.probe", side_effect=AssertionError("probe")),
            patch("agy_provider.telemetry.logger.info") as log,
        ):
            response = await self.service.handle()
            self.assertEqual(response["status_code"], 200)
            self.assertEqual(response["headers"]["Cache-Control"], "no-store")
            data = response["data"]
            self.assertEqual(
                set(data),
                {
                    "schema_version",
                    "routing",
                    "failure_events",
                    "failure_retention",
                    "sampled_at",
                    "window",
                    "instances",
                },
            )
            self.assertEqual(data["schema_version"], 2)
            self.assertIsNotNone(datetime.fromisoformat(data["sampled_at"]).tzinfo)
            self.assertEqual(data["window"], {"hours": 24, "max_turns_per_instance": 100})
            a, b, c = data["instances"]
            self.assertEqual(
                set(a),
                {
                    "provider_id",
                    "provider_tag",
                    "model",
                    "enabled",
                    "tools_mode",
                    "effective_mode",
                    "tools_enabled",
                    "runtime_state",
                    "pool",
                    "summary",
                    "latest_context",
                    "history_storage",
                    "recent_turns",
                },
            )
            self.assertEqual(set(a["pool"]), {"active_sessions", "max_sessions", "live_processes", "active_tool_turns"})
            self.assertEqual(a["pool"]["live_processes"], 1)
            self.assertEqual(set(a["summary"]), {
                "total_turns", "request_sent_turns", "success_turns", "failed_turns", "reuse_rate",
                "cache_token_ratio", "unavailable_usage_turns", "reported_usage_turns",
                "missing_cache_field_turns", "total_input_other", "total_input_cached", "total_output", "rebuild_reasons"})
            self.assertEqual(a["summary"]["request_sent_turns"], 25)
            self.assertEqual(a["summary"]["unavailable_usage_turns"], 25)
            self.assertEqual(a["summary"]["failed_turns"], 25)
            self.assertIsNone(a["summary"]["total_input_other"])
            self.assertIsNone(a["summary"]["total_input_cached"])
            self.assertIsNone(a["summary"]["total_output"])
            self.assertEqual(a["latest_context"]["context_used"], 100)
            self.assertEqual(a["latest_context"]["context_window_size"], 1000)
            self.assertEqual(a["latest_context"]["observed_at"], usage.observed_at)
            self.assertEqual(b["summary"]["total_turns"], 0)
            self.assertEqual(b["recent_turns"], [])
            self.assertEqual(b["latest_context"], {
                "context_used": None, "context_window_size": None, "observed_at": None,
                "source": "not_reported", "scope": "latest_live_session", "model": "gemini-test"})
            self.assertEqual(c["runtime_state"], "configured")
            self.assertIsNone(c["pool"])
            self.assertIsNone(c["summary"])
            turns = a["recent_turns"]
            self.assertEqual(len(turns), 20)
            self.assertEqual(set(turns[0]), {
                "recorded_at", "model", "negotiated_model", "mode", "status", "error_code", "elapsed_ms",
                "input_other", "input_cached", "output", "usage_status", "cache_fields_present",
                "context_used", "context_window_size", "observed_at", "reused", "rebuild_reasons",
                "prompt_bytes", "replay_bytes", "omitted_messages", "content_429_candidate",
                "turn_id", "origin_turn_id", "logical_request_tag", "attempt", "cli_retry_attempt",
                "chunk_seen", "body_seen", "body_streamed", "tool_handed_off", "tool_result_supplied",
                "body_bytes", "thought_bytes", "stop_reason", "permission_denial_count",
                "filtered_notice_count", "fallback_allowed", "fallback_reason",
            })
            self.assertEqual(turns[0]["content_429_candidate"], {
                "system_hash8": "1234abcd", "system_bytes": 42, "has_output": False, "attempt": 2})
            encoded = json.dumps(data)
            for forbidden in (secret, '"prompt"', '"system"', '"session_key"', '"native_session_tag"', '"tool_schema"', '"arguments"'):
                self.assertNotIn(forbidden, encoded)
            turns[0]["cache_fields_present"]["cachedReadTokens"] = True
            self.assertFalse(one.runtime.telemetry.history[-1]["cache_fields_present"]["cachedReadTokens"])
            self.assertEqual(len(one.runtime.telemetry.safe_recent_turns(10000)), 20)
            # Model change, dead client and sample invalidation cannot resurrect
            # old context from a turn record. Reading never fabricates a sample.
            for change in (lambda: setattr(one.runtime.sessions[secret], "model", "old-model"),
                           lambda: setattr(client, "alive", False),
                           lambda: usage.observe({})):
                one.runtime.sessions[secret].model = "gemini-test"
                client.alive = True
                change()
                fresh = (await self.service.handle())["data"]["instances"][0]["latest_context"]
                self.assertIsNone(fresh["context_used"])
                self.assertIsNone(fresh["context_window_size"])
                self.assertIsNone(fresh["observed_at"])
            # Upstream model changes can precede another valid usage sample;
            # its model is no longer attributable to our selected model.
            upstream = AcpClient(Settings())
            upstream.active, upstream.session_id = True, "private-session"
            for update in ({"sessionUpdate": "current_model_update"},
                           {"sessionUpdate": "usage_update", "used": 50, "size": 500}):
                upstream._on_update({"sessionId": upstream.session_id, "update": update})
            client.context_usage = upstream.context_usage
            self.assertIsNone((await self.service.handle())["data"]["instances"][0]["latest_context"]["observed_at"])
            now += 86401
            expired = (await self.service.handle())["data"]["instances"][0]
            self.assertEqual(expired["recent_turns"], [])
            self.assertEqual(expired["summary"]["total_turns"], 0)
            log.assert_not_called()

    async def test_permissions_require_configured_dashboard_admin(self):
        # Host facts: dashboard login authenticates the configured username;
        # v1 plugin-scope API keys enter with username="api_key:<id>". See plan §3.
        for username, expected in ((None, 401), ("", 401), ("ordinary-user", 403),
                                   ("api_key:123", 403), ("panel-owner", 200)):
            self.request.username = username
            response = await self.service.handle()
            self.assertEqual(response["status_code"], expected)
            self.assertEqual(response["headers"]["Cache-Control"], "no-store")
            if expected != 200:
                code = "UNAUTHENTICATED" if expected == 401 else "FORBIDDEN"
                self.assertEqual(response["data"], {"code": code})
                self.assertTrue(response["message"].startswith(f"[AGY:{code}] "))
        self.manager.acm.default_conf.pop("dashboard")
        self.assertEqual((await self.service.handle())["status_code"], 403)

    async def test_quota_and_daily_get_are_local_and_admin_only(self):
        from agy_provider.quota import QuotaService
        from agy_provider.telemetry import start_diagnostics, stop_diagnostics
        start_diagnostics(Path(self.temp.name))
        self.addCleanup(stop_diagnostics)
        self.service.quota = QuotaService(Settings())
        self.service.quota.refresh = AsyncMock()
        self.service.register()
        for path, handler, methods, _ in self.context.registered_web_apis:
            self.assertEqual(methods, ["GET"])
            if path.endswith("/status"):
                continue
            for who, code in ((None, 401), ("api_key:123", 403), ("ordinary", 403), ("panel-owner", 200)):
                self.request.username = who
                response = await handler()
                self.assertEqual(response["status_code"], code)
                self.assertEqual(response["headers"]["Cache-Control"], "no-store")
                if code == 200 and "/daily/" in path:
                    self.assertEqual(response["data"]["days"], [])
                    self.assertEqual(response["data"]["range"]["days"], int(path.rsplit("/", 1)[-1]))
        self.service.quota.refresh.assert_not_awaited()

    async def test_route_lifecycle_only_removes_owned_get_handler(self):
        self.service.register()
        route, handler, methods, _ = self.context.registered_web_apis[0]
        self.assertEqual(route, "/astrbot_plugin_antigravity_acp/monitor/status")
        self.assertEqual(methods, ["GET"])
        replacement = (route, object(), ["GET"], "other")
        self.context.registered_web_apis.append(replacement)
        await self.service.close()
        await self.service.close()
        self.assertEqual(self.context.registered_web_apis, [replacement])
        response = await handler()
        self.assertEqual(response["status_code"], 503)
        self.assertEqual(response["data"], {"code": "CLOSED"})
        self.assertTrue(response["message"].startswith("[AGY:CLOSED] "))

    async def test_unavailable_daily_storage_has_distinct_bridge_code(self):
        with patch("agy_provider.monitoring.daily_snapshot", side_effect=OSError("path")):
            response = await self.service.handle("daily/90")
        self.assertEqual(response["status_code"], 503)
        self.assertEqual(response["data"], {"code": "STORAGE_UNAVAILABLE"})
        self.assertTrue(response["message"].startswith("[AGY:STORAGE_UNAVAILABLE] "))
        self.assertNotIn("path", str(response))

    async def test_privacy_regression_safe_recent_turns_and_monitor_status(self):
        inst = self.instance("priv")
        now = 200.0
        inst.runtime.telemetry.clock = lambda: now
        secrets_by_kind = {
            "prompt": "SENSITIVE_PROMPT_BODY_请勿泄露",
            "system": "SENSITIVE_SYSTEM_PROMPT_人格原文",
            "session_key": "SENSITIVE_SESSION_KEY_RAW_qq_98765",
            "native_session_tag": "SENSITIVE_NATIVE_SESSION_TAG_xyz",
            "tool_args": "SENSITIVE_TOOL_ARGS_SECRET_cmd",
            "tool_schema": "SENSITIVE_TOOL_SCHEMA_SECRET_desc",
            "candidate_system": "SENSITIVE_CANDIDATE_SYSTEM_leak",
        }
        meter = inst.runtime.telemetry.start_turn(
            provider="priv",
            session_key_raw=secrets_by_kind["session_key"],
            mode="chat",
            model="gemini-test",
            effort="low",
            system_bytes=120,
            system_hash8="abcdef01",
        )
        meter.set_native_session("raw-native-session-id")
        meter.request_sent = True
        record = meter.finish(status="error", error_code="RATE_LIMIT")
        record.update(
            prompt=secrets_by_kind["prompt"],
            system=secrets_by_kind["system"],
            session_key=secrets_by_kind["session_key"],
            native_session_tag=secrets_by_kind["native_session_tag"],
            tool_args={"cmd": secrets_by_kind["tool_args"]},
            arguments=secrets_by_kind["tool_args"],
            tool_calls_detail=[{"name": "shell", "arguments": secrets_by_kind["tool_args"]}],
            tool_schema={"description": secrets_by_kind["tool_schema"]},
        )
        record["cache_fields_present"]["extra_secret"] = secrets_by_kind["tool_args"]
        record["content_429_candidate"] = {
            "system_hash8": "abcdef01",
            "system_bytes": 120,
            "has_output": False,
            "attempt": "2",
            "system": secrets_by_kind["candidate_system"],
            "prompt": secrets_by_kind["prompt"],
        }
        inst.runtime.sessions[secrets_by_kind["session_key"]] = Session(
            client=types.SimpleNamespace(alive=True, context_usage=ContextUsage(), close=AsyncMock()),
            model="gemini-test",
        )

        expected_turn_keys = {
            "recorded_at", "model", "mode", "status", "error_code", "elapsed_ms",
            "input_other", "input_cached", "output", "usage_status", "cache_fields_present",
            "context_used", "context_window_size", "observed_at", "reused", "rebuild_reasons",
            "prompt_bytes", "replay_bytes", "omitted_messages", "content_429_candidate",
            "negotiated_model", "turn_id", "origin_turn_id", "logical_request_tag",
            "attempt", "cli_retry_attempt", "chunk_seen", "body_seen", "body_streamed",
            "tool_handed_off", "tool_result_supplied", "body_bytes", "thought_bytes",
            "stop_reason", "permission_denial_count", "filtered_notice_count",
            "fallback_allowed", "fallback_reason",
        }
        projected = inst.runtime.telemetry.safe_recent_turns()
        self.assertEqual(len(projected), 1)
        self.assertEqual(set(projected[0]), expected_turn_keys)
        self.assertEqual(set(projected[0]["cache_fields_present"]), {"cachedReadTokens", "cachedWriteTokens"})
        self.assertEqual(
            set(projected[0]["content_429_candidate"]),
            {"system_hash8", "system_bytes", "has_output", "attempt"},
        )

        response = await self.service.handle()
        self.assertEqual(response["status_code"], 200)
        api_turns = response["data"]["instances"][0]["recent_turns"]
        self.assertEqual(len(api_turns), 1)
        self.assertEqual(set(api_turns[0]), expected_turn_keys)

        for encoded in (
            json.dumps(projected, ensure_ascii=False),
            json.dumps(response["data"], ensure_ascii=False),
        ):
            for secret_value in secrets_by_kind.values():
                self.assertNotIn(secret_value, encoded)
            for forbidden_key in (
                '"prompt"', '"system"', '"session_key"', '"native_session_tag"',
                '"tool_args"', '"arguments"', '"tool_calls_detail"', '"tool_schema"',
            ):
                self.assertNotIn(forbidden_key, encoded)

    async def test_context_null_without_usage_update_and_valid_sample_with_observed_at(self):
        inst = self.instance("ctx")
        client = AcpClient(Settings())
        client.active, client.session_id = True, "ctx-session"
        client.proc = types.SimpleNamespace(returncode=None)
        client.close = AsyncMock()
        inst.runtime.sessions["conv-ctx"] = Session(client=client, model="gemini-test")

        # Token usage updates or zero/invalid context values must keep context_window_size null.
        for update in (
            None,
            {"sessionUpdate": "usage_update", "usage": {"inputTokens": 200, "outputTokens": 50}},
            {"sessionUpdate": "usage_update", "used": 0, "size": 0},
        ):
            if update is not None:
                client._on_update({"sessionId": client.session_id, "update": update})
            ctx = (await self.service.handle())["data"]["instances"][0]["latest_context"]
            self.assertIsNone(ctx["context_used"])
            self.assertIsNone(ctx["context_window_size"])
            self.assertIsNone(ctx["observed_at"])
            self.assertEqual(ctx["source"], "not_reported")

        # Real ACP usage_update populates used, size, observed_at and source.
        client._on_update({
            "sessionId": client.session_id,
            "update": {"sessionUpdate": "usage_update", "used": 42000, "size": 200000},
        })
        ctx = (await self.service.handle())["data"]["instances"][0]["latest_context"]
        self.assertEqual(ctx["context_used"], 42000)
        self.assertEqual(ctx["context_window_size"], 200000)
        self.assertEqual(ctx["observed_at"], client.context_usage.observed_at)
        self.assertIsNotNone(datetime.fromisoformat(ctx["observed_at"]).tzinfo)
        self.assertEqual(ctx["source"], "acp_usage_update")

    async def test_recent_turns_empty_limit_truncation_and_non_enum_convergence(self):
        inst = self.instance("bounds")
        telemetry = inst.runtime.telemetry
        now = 300.0
        telemetry.clock = lambda: now

        # Empty history boundaries.
        self.assertEqual(telemetry.safe_recent_turns(), [])
        self.assertEqual(telemetry.safe_recent_turns(0), [])
        empty_summary = telemetry.monitor_summary()
        self.assertEqual(empty_summary["total_turns"], 0)
        self.assertEqual(empty_summary["success_turns"], 0)
        self.assertEqual(empty_summary["failed_turns"], 0)
        self.assertIsNone(empty_summary["reuse_rate"])
        self.assertIsNone(empty_summary["cache_token_ratio"])
        self.assertIsNone(empty_summary["total_input_other"])
        self.assertIsNone(empty_summary["total_input_cached"])
        self.assertIsNone(empty_summary["total_output"])
        self.assertEqual(empty_summary["rebuild_reasons"], [])

        empty_resp = (await self.service.handle())["data"]["instances"][0]
        self.assertEqual(empty_resp["recent_turns"], [])
        self.assertEqual(empty_resp["summary"], empty_summary)

        # Populate 25 turns to test limit=0, negative limit, truncation >20, and newest-first order.
        for idx in range(25):
            meter = telemetry.start_turn(
                provider="bounds", session_key_raw=f"k-{idx}", mode="chat",
                model="gemini-test", effort="low",
            )
            meter.prompt_bytes = idx
            meter.request_sent = True
            meter.finish(status="ok")

        self.assertEqual(telemetry.safe_recent_turns(0), [])
        self.assertEqual(telemetry.safe_recent_turns(-5), [])
        self.assertEqual([r["prompt_bytes"] for r in telemetry.safe_recent_turns(5)], [24, 23, 22, 21, 20])
        for arg in (None, 25, 10000):
            rows = telemetry.safe_recent_turns() if arg is None else telemetry.safe_recent_turns(arg)
            self.assertEqual(len(rows), 20)
            self.assertEqual(rows[0]["prompt_bytes"], 24)
            self.assertEqual(rows[-1]["prompt_bytes"], 5)

        # Non-enum status / error_code / mode / usage_status / reasons / candidate convergence.
        bad_meter = telemetry.start_turn(
            provider="bounds", session_key_raw="k-bad", mode="chat",
            model="x" * 300, effort="low",
        )
        bad_record = bad_meter.finish(status="ok")
        bad_record.update(
            mode="freeform_mode",
            status="weird_status",
            error_code="FREEFORM_UNKNOWN_ERR",
            usage_status="bogus_usage",
            rebuild_reasons=["freeform_reason", "system_changed", "abort"],
            content_429_candidate={
                "system_hash8": "NOT_HEX!",
                "system_bytes": -10,
                "has_output": "truthy-string",
                "attempt": "not-digits",
            },
        )
        converged = telemetry.safe_recent_turns(1)[0]
        self.assertEqual(len(converged["model"]), 256)
        self.assertEqual(converged["mode"], "unknown")
        self.assertEqual(converged["status"], "unknown")
        self.assertEqual(converged["error_code"], "UPSTREAM")
        self.assertEqual(converged["usage_status"], "unavailable")
        self.assertEqual(converged["rebuild_reasons"], ["abort", "system_changed"])
        self.assertEqual(converged["content_429_candidate"], {
            "system_hash8": None, "system_bytes": None, "has_output": False, "attempt": None,
        })

        # Empty string error_code converges to None rather than UPSTREAM.
        bad_record["error_code"] = ""
        self.assertIsNone(telemetry.safe_recent_turns(1)[0]["error_code"])

    async def test_turn_history_endpoint_returns_up_to_100_turns_with_admin_gate_and_privacy(self):
        from agy_provider.telemetry import start_diagnostics, stop_diagnostics

        start_diagnostics(Path(self.temp.name))
        self.addCleanup(stop_diagnostics)
        active_inst = self.instance("hist-active", history_id="c" * 32)
        self.instance("hist-idle", active=False)
        now = 500.0
        active_inst.runtime.telemetry.clock = lambda: now
        secret = "SENSITIVE_PROMPT_SHOULD_NOT_APPEAR"
        for idx in range(105):
            meter = active_inst.runtime.telemetry.start_turn(
                provider="hist-active", session_key_raw=f"sess-{idx}", mode="tools",
                model="gemini-test", effort="low",
            )
            meter.prompt_bytes = idx
            meter.request_sent = True
            meter.observe_usage(
                {"usage": {"inputTokens": 100 + idx, "outputTokens": 20 + idx, "cachedReadTokens": 300}},
                source="acp_terminal",
            )
            rec = meter.finish(status="ok")
            rec["prompt"] = secret

        with (
            patch("agy_provider.sessions.Runtime.launch_info", side_effect=AssertionError("AGY request")),
            patch("agy_provider.transport.AcpClient.request", side_effect=AssertionError("AGY request")),
            patch("agy_provider.probe.probe", side_effect=AssertionError("probe")),
        ):
            status_resp = await self.service.handle("status")
            self.assertEqual(len(status_resp["data"]["instances"][0]["recent_turns"]), 20)

            turns_resp = await self.service.handle("turns")
            self.assertEqual(turns_resp["status_code"], 200)
            self.assertEqual(turns_resp["headers"]["Cache-Control"], "no-store")
            payload = turns_resp["data"]
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["window"], {"hours": 24, "max_turns_per_instance": 100})
            active_row, idle_row = payload["instances"]
            self.assertEqual(active_row["total_turns"], 100)
            self.assertEqual(len(active_row["turns"]), 100)
            self.assertEqual(active_row["turns"][0]["prompt_bytes"], 104)
            self.assertEqual(active_row["turns"][-1]["prompt_bytes"], 5)
            self.assertEqual(active_row["turns"][0]["input_other"], 204)
            self.assertEqual(active_row["turns"][0]["input_cached"], 300)
            self.assertEqual(active_row["turns"][0]["output"], 124)
            self.assertEqual(idle_row["runtime_state"], "configured")
            self.assertEqual(idle_row["turns"], [])
            self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))
