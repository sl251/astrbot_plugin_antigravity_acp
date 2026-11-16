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

    def instance(self, identity, *, active=True):
        config = {"id": identity, "type": "agy_acp", "enable": True,
                  "model": "gemini-test", "custom_extra_body": {"agy_tools_mode": "chat"}}
        self.manager.providers_config.append(config)
        if not active:
            return None
        inst = self.provider.AgyProvider(config, {})
        self.manager.inst_map[identity] = inst
        self.addAsyncCleanup(inst.terminate)
        return inst

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
            self.assertEqual(set(data), {"schema_version", "sampled_at", "window", "instances"})
            self.assertEqual(data["schema_version"], 1)
            self.assertIsNotNone(datetime.fromisoformat(data["sampled_at"]).tzinfo)
            self.assertEqual(data["window"], {"hours": 24, "max_turns_per_instance": 100})
            a, b, c = data["instances"]
            self.assertEqual(set(a), {"provider_id", "model", "enabled", "tools_mode", "effective_mode",
                                     "tools_enabled", "runtime_state", "pool", "summary", "latest_context", "recent_turns"})
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
                "recorded_at", "model", "mode", "status", "error_code", "elapsed_ms",
                "input_other", "input_cached", "output", "usage_status", "cache_fields_present",
                "context_used", "context_window_size", "observed_at", "reused", "rebuild_reasons",
                "prompt_bytes", "replay_bytes", "omitted_messages", "content_429_candidate"})
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
                self.assertNotIn("data", response)
        self.manager.acm.default_conf.pop("dashboard")
        self.assertEqual((await self.service.handle())["status_code"], 403)

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
        self.assertEqual((await handler())["status_code"], 503)
