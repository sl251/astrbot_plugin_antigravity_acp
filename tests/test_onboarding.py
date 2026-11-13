import ast
import asyncio
import hashlib
import json
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401 -- install the offline host logger

from agy_provider import onboarding as onboarding_module
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.onboarding import Onboarding, ensure_provider_source

MISSING = object()


class FakeDefaultConfig(dict):
    """Model AstrBotConfig's dict interface and save_config() persistence."""

    def __init__(self, manager):
        super().__init__(provider=[], provider_sources=[])
        self._manager = manager

    def save_config(self):
        self._manager.save()


class FakeManager:
    """Model 4.29.4's resource_lock, acm.default_conf, load-before-save create and save-before-reload update."""

    def __init__(self):
        self.reload_lock = asyncio.Lock()
        self.resource_lock = asyncio.Lock()
        default_conf = FakeDefaultConfig(self)
        self.acm = types.SimpleNamespace(default_conf=default_conf)
        self.providers_config = default_conf["provider"]
        self.provider_sources_config = default_conf["provider_sources"]
        self.persisted = []
        self.persisted_sources = []
        self.inst_map = {}
        self.default = "original"
        self.fail = ""
        self.creates = 0

    def get_merged_provider_config(self, config):
        merged = deepcopy(config)
        source_id = merged.get("provider_source_id", "")
        if source_id:
            source = next(
                (
                    row
                    for row in self.provider_sources_config
                    if row.get("id") == source_id
                ),
                None,
            )
            if source:
                merged = {**source, **merged}
                merged["id"] = config["id"]
        return merged

    def save(self):
        if self.fail == "save":
            raise OSError("private host path must not escape")
        default_conf = self.acm.default_conf
        self.persisted = deepcopy(default_conf["provider"])
        self.persisted_sources = deepcopy(default_conf.get("provider_sources", []))

    async def load_provider(self, config):
        merged = self.get_merged_provider_config(config)
        if not merged.get("enable"):
            return
        self.inst_map[config["id"]] = object()

    async def create_provider(self, config):
        async with self.resource_lock:
            self.creates += 1
            if self.fail == "create":
                raise ValueError("private details")
            npid = config.get("id")
            if not npid:
                raise ValueError("New provider config must have an 'id' field")
            default_conf = self.acm.default_conf
            if any(row.get("id") == npid for row in default_conf["provider"]):
                raise ValueError(f"Provider ID {npid} already exists")
            stored = deepcopy(config)
            default_conf["provider"].append(stored)
            try:
                await self.load_provider(stored)
            except Exception:
                default_conf["provider"].remove(stored)
                raise
            default_conf.save_config()
            self.providers_config = default_conf["provider"]

    async def update_provider(self, provider_id, config):
        async with self.resource_lock:
            npid = config.get("id")
            if not npid:
                raise ValueError("New provider config must have an 'id' field")
            default_conf = self.acm.default_conf
            for row in default_conf["provider"]:
                if row.get("id") == npid and row.get("id") != provider_id:
                    raise ValueError(f"Provider ID {npid} already exists")
            for idx, row in enumerate(default_conf["provider"]):
                if row.get("id") == provider_id:
                    default_conf["provider"][idx] = deepcopy(config)
                    break
            else:
                raise ValueError(f"Provider ID {provider_id} not found")
            default_conf.save_config()
            await self.reload(config)

    async def reload(self, config):
        async with self.reload_lock:
            if self.fail == "reload":
                raise ValueError("private details")
            merged = self.get_merged_provider_config(config)
            if merged.get("enable"):
                self.inst_map[config["id"]] = object()
            else:
                self.inst_map.pop(config["id"], None)
            self.providers_config = self.acm.default_conf["provider"]
            self.provider_sources_config = self.acm.default_conf.get("provider_sources", [])

    async def set_provider(self, provider_id, provider_type, umo=None):
        if provider_id not in self.inst_map:
            raise ValueError(f"Provider {provider_id} does not exist and cannot be set.")
        if self.fail == "default":
            raise OSError("private details")
        self.default = provider_id


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Stub only host imports. Load provider defaults from the shipped source.
        provider = types.ModuleType("agy_provider.provider")
        source = Path(__file__).resolve().parents[1] / "agy_provider/provider.py"
        for node in ast.parse(source.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {
                        "DEFAULT_CONFIG",
                        "PROVIDER_TYPE",
                    }:
                        exec(
                            compile(
                                ast.Module(body=[node], type_ignores=[]),
                                str(source),
                                "exec",
                            ),
                            provider.__dict__,
                        )
        self.request = types.SimpleNamespace(
            username="admin", content_type="application/json"
        )
        web = types.ModuleType("astrbot.api.web")
        web.request = self.request
        web.json_response = lambda data, **kw: {"data": data, "status_code": 200, **kw}
        web.error_response = lambda message, status_code=400, **kw: {
            "message": message,
            "status_code": status_code,
            **kw,
        }
        entities = types.ModuleType("astrbot.core.provider.entities")
        entities.ProviderType = types.SimpleNamespace(CHAT_COMPLETION="chat_completion")
        modules = {
            "agy_provider.provider": provider,
            "astrbot.api.web": web,
            "astrbot.core.provider.entities": entities,
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.manager = FakeManager()
        self.context = types.SimpleNamespace(
            provider_manager=self.manager, registered_web_apis=[]
        )
        self.context.register_web_api = lambda *args: (
            self.context.registered_web_apis.append(args)
        )
        with patch(
            "agy_provider.onboarding.plugin_data_root",
            return_value=Path(self.temp.name),
        ):
            self.service = Onboarding(self.context, Settings())
        self.service.report = {
            "status": "passed",
            "models": [{"id": "gemini-test", "name": "Gemini Test"}],
        }
        self.addAsyncCleanup(self.service.close)
        self.service._check = AsyncMock(return_value={"status": "passed"})

    async def call(self, action, data=MISSING, raw=None):
        payload = {"csrf": self.service.csrf} if data is MISSING else data
        self.request.body = AsyncMock(
            return_value=json.dumps(payload).encode() if raw is None else raw
        )
        self.request.json = AsyncMock(return_value=payload)
        return await self.service.handle(action)

    async def apply(self, effort="default", use_default=False):
        self.service.start_apply("gemini-test", effort, use_default)
        await self.service.job

    async def test_unauthorized_read_and_write(self):
        self.request.username = ""
        for action in (
            "status",
            "check",
            "login",
            "apply",
            "login-code",
            "login-cancel",
        ):
            self.assertEqual((await self.call(action))["status_code"], 401)

    async def test_invalid_nonce_and_payload_types(self):
        for payload in (
            {},
            [],
            None,
            {"csrf": "wrong"},
            {"csrf": "中文"},
            {"csrf": 123},
        ):
            self.assertEqual(
                (await self.call("check", payload, raw=b"{}"))["status_code"], 403
            )

    async def test_content_type_and_body_limit(self):
        self.request.content_type = "text/plain"
        self.assertEqual((await self.call("check"))["status_code"], 400)
        self.request.content_type = "application/json; charset=utf-8"
        self.assertEqual(
            (await self.call("check", raw=b"x" * 16385))["status_code"], 400
        )
        self.assertFalse(self.service.busy)

    async def test_invalid_model_and_apply_fields(self):
        for model in ([], {}, 1, "x" * 1025):
            self.assertEqual(
                (await self.call("check", {"csrf": self.service.csrf, "model": model}))[
                    "status_code"
                ],
                400,
            )
        for effort, default in (
            ([], False),
            ("invalid", False),
            ("high", "true"),
            ("high", 1),
        ):
            with self.assertRaises(AgyError):
                self.service.start_apply("gemini-test", effort, default)
        with self.assertRaises(AgyError):
            self.service.start_apply("not-in-catalog", "default", False)

    async def test_routes_removed_without_touching_other_instances(self):
        self.service.register()
        own = list(self.context.registered_web_apis)
        other = (own[0][0], AsyncMock(), ["GET"], "another instance")
        self.context.registered_web_apis.append(other)
        self.assertEqual(len(own), 6)
        await self.service.close()
        self.assertEqual(self.context.registered_web_apis, [other])
        self.assertEqual((await self.call("status"))["status_code"], 400)

    async def test_nonce_rotates_on_reload(self):
        with patch(
            "agy_provider.onboarding.plugin_data_root",
            return_value=Path(self.temp.name),
        ):
            replacement = Onboarding(self.context, Settings())
        self.assertNotEqual(self.service.csrf, replacement.csrf)
        await replacement.close()

    async def test_mutual_exclusion_and_duplicate_click(self):
        gate = asyncio.Event()
        self.service._check = AsyncMock(side_effect=gate.wait)
        self.service.start_check()
        for action in ("check", "login", "apply"):
            self.assertEqual((await self.call(action))["status_code"], 409)
        await self.service.close()
        self.assertTrue(self.service.job.cancelled())

    async def test_create_repeat_and_restart_from_host_config(self):
        await self.apply()
        provider_id = self.service.result["provider_id"]
        await self.apply()
        self.assertEqual(self.manager.creates, 1)
        self.assertEqual(self.service.result["provider_id"], provider_id)
        self.assertEqual(self.manager.persisted, self.manager.providers_config)
        self.assertEqual(self.manager.default, "original")
        self.assertEqual(self.service.providers()[0]["id"], provider_id)

    async def test_disabled_existing_reenabled(self):
        self.manager.providers_config.append(
            {
                "id": "existing",
                "type": "agy_acp",
                "model": "gemini-test",
                "enable": False,
            }
        )
        await self.apply()
        self.assertEqual(self.manager.creates, 0)
        self.assertTrue(self.manager.persisted[0]["enable"])
        self.assertIn("existing", self.manager.inst_map)

    async def test_id_collision_does_not_overwrite_other_provider(self):
        suffix = hashlib.sha256(b"gemini-test\0default").hexdigest()[:12]
        original = {
            "id": "agy_setup_" + suffix,
            "type": "other",
            "model": "gemini-test",
            "enable": True,
        }
        self.manager.providers_config.append(deepcopy(original))
        await self.apply()
        self.assertEqual(self.manager.providers_config[0], original)
        self.assertEqual(self.service.result["provider_id"], original["id"] + "_2")

    async def test_thought_default_inherits_global_for_validation(self):
        self.service.settings = Settings(thought_level="high")
        await self.apply()
        self.service._check.assert_awaited_once_with("gemini-test", "high")
        self.assertEqual(
            self.manager.providers_config[0]["custom_extra_body"]["agy_thought_level"],
            "default",
        )

    async def test_explicit_efforts_remain_distinct(self):
        for effort in ("low", "medium", "high"):
            await self.apply(effort)
            self.service._check.assert_awaited_with("gemini-test", effort)
        self.assertEqual(self.manager.creates, 3)

    async def test_recheck_failure_never_creates_provider(self):
        self.service._check.return_value = {"status": "blocked", "error_code": "MODEL"}
        await self.apply()
        self.assertEqual(self.service.action_error, "MODEL")
        self.assertIsNone(self.service.result)
        self.assertEqual(self.manager.creates, 0)

    async def test_create_failure_reported_and_retryable(self):
        self.manager.fail = "create"
        await self.apply()
        self.assertEqual(self.service.action_error, "HOST")
        self.assertIsNone(self.service.result)
        self.manager.fail = ""
        await self.apply()
        self.assertIsNotNone(self.service.result)

    async def test_persistence_failure_retry_really_persists(self):
        self.manager.fail = "save"
        await self.apply()
        self.assertEqual(self.service.action_error, "HOST")
        self.assertIsNone(self.service.result)
        self.manager.fail = ""
        await self.apply()
        self.assertIsNotNone(self.service.result)
        self.assertEqual(self.manager.persisted, self.manager.providers_config)
        self.assertEqual(self.manager.creates, 1)

    async def test_default_failure_retains_provider_and_retry_reuses(self):
        self.manager.fail = "default"
        await self.apply(use_default=True)
        self.assertEqual(self.service.action_error, "HOST")
        self.assertFalse(self.service.result["default_selected"])
        self.manager.fail = ""
        await self.apply(use_default=True)
        self.assertTrue(self.service.result["default_selected"])
        self.assertEqual(self.manager.creates, 1)

    async def test_shutdown_waits_for_host_commit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        create = self.manager.create_provider

        async def delayed(config):
            entered.set()
            await release.wait()
            await create(config)

        self.manager.create_provider = delayed
        self.service.start_apply("gemini-test", "default", False)
        await entered.wait()
        closing = asyncio.create_task(self.service.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await closing
        self.assertEqual(self.manager.persisted, self.manager.providers_config)
        self.assertEqual(len(self.manager.persisted), 1)
        self.assertEqual(len(self.manager.persisted_sources), 1)
        self.assertEqual(
            self.manager.persisted[0]["provider_source_id"],
            self.manager.persisted_sources[0]["id"],
        )

    async def test_create_save_failure_retains_loaded_and_retry_reuses(self):
        saves = 0
        original_save = self.manager.save

        def fail_second_save():
            nonlocal saves
            saves += 1
            if saves == 2:
                raise OSError("disk full during create save")
            original_save()

        self.manager.save = fail_second_save
        await self.apply()
        self.assertEqual(self.service.action_error, "HOST")
        self.assertIsNone(self.service.result)
        self.assertEqual(self.manager.persisted, [])
        self.assertEqual(len(self.manager.providers_config), 1)

        await self.apply()
        self.assertIsNotNone(self.service.result)
        self.assertEqual(self.manager.persisted, self.manager.providers_config)
        self.assertEqual(self.manager.creates, 1)

    async def test_ensure_provider_source_idempotent(self):
        first = await ensure_provider_source(self.manager)
        second = await ensure_provider_source(self.manager)
        self.assertEqual(first, "agy_acp")
        self.assertEqual(second, "agy_acp")
        self.assertEqual(len(self.manager.persisted_sources), 1)
        self.assertEqual(len(self.manager.provider_sources_config), 1)

    async def test_ensure_provider_source_preserves_other_sources_and_displaces_collision(self):
        other_same_id = {
            "id": "agy_acp",
            "type": "openai_chat_completion",
            "provider": "openai",
            "provider_type": "chat_completion",
            "enable": True,
        }
        other_provider_type = {
            "id": "agy_acp_2",
            "type": "agy_acp",
            "provider": "agy_acp",
            "provider_type": "embedding",
            "enable": True,
        }
        self.manager.acm.default_conf["provider_sources"].extend(
            [deepcopy(other_same_id), deepcopy(other_provider_type)]
        )
        self.manager.providers_config.append(
            {"id": "legacy_agy", "type": "agy_acp", "model": "gemini-test", "enable": True}
        )

        source_id = await ensure_provider_source(self.manager)
        self.assertEqual(source_id, "agy_acp_3")
        sources = self.manager.persisted_sources
        self.assertEqual(sources[0], other_same_id)
        self.assertEqual(sources[1], other_provider_type)
        self.assertEqual(sources[2]["id"], "agy_acp_3")
        self.assertEqual(self.manager.persisted[0]["provider_source_id"], "agy_acp_3")
        self.assertEqual(self.manager.providers_config, self.manager.persisted)

    async def test_ensure_provider_source_rollback_on_save_failure(self):
        no_source_key = {
            "id": "agy_missing_key",
            "type": "agy_acp",
            "model": "gemini-test",
            "enable": True,
        }
        stale_source = {
            "id": "agy_stale",
            "type": "agy_acp",
            "model": "gemini-test",
            "enable": True,
            "provider_source_id": "deleted_source",
        }
        other_provider = {
            "id": "openai_1",
            "type": "openai",
            "model": "gpt-test",
            "enable": True,
        }
        self.manager.providers_config.extend(
            [no_source_key, stale_source, other_provider]
        )
        self.manager.fail = "save"

        with self.assertRaises(OSError):
            await ensure_provider_source(self.manager)

        self.assertEqual(self.manager.acm.default_conf["provider_sources"], [])
        self.assertNotIn("provider_source_id", no_source_key)
        self.assertEqual(stale_source["provider_source_id"], "deleted_source")
        self.assertNotIn("provider_source_id", other_provider)
        self.assertEqual(self.manager.persisted, [])
        self.assertEqual(self.manager.persisted_sources, [])

    async def test_providers_config_consistent_with_persisted_after_binding(self):
        self.manager.providers_config.append(
            {"id": "agy_existing", "type": "agy_acp", "model": "gemini-other", "enable": True}
        )
        await ensure_provider_source(self.manager)
        self.assertEqual(self.manager.providers_config, self.manager.persisted)
        self.assertEqual(
            self.manager.provider_sources_config, self.manager.persisted_sources
        )
        await self.apply()
        self.assertEqual(self.manager.providers_config, self.manager.persisted)
        self.assertTrue(
            all(row.get("provider_source_id") == "agy_acp" for row in self.manager.persisted)
        )

    async def test_legacy_host_missing_interfaces_skips_source_binding_and_saves(self):
        class LegacyHostManager:
            def __init__(self, *, has_lock=False, default_conf=MISSING):
                if has_lock:
                    self.resource_lock = asyncio.Lock()
                if default_conf is not MISSING:
                    self.acm = types.SimpleNamespace(default_conf=default_conf)
                self.providers_config = []
                self.persisted = []
                self.inst_map = {}

            def get_merged_provider_config(self, config):
                return config

            async def create_provider(self, config):
                self.providers_config.append(deepcopy(config))
                self.inst_map[config["id"]] = object()
                self.persisted = deepcopy(self.providers_config)

            async def update_provider(self, provider_id, config):
                self.providers_config[:] = [
                    deepcopy(config) if row["id"] == provider_id else row
                    for row in self.providers_config
                ]
                self.persisted = deepcopy(self.providers_config)

        bad_conf = types.SimpleNamespace(get=lambda *a: [], save_config=lambda: None)
        no_sources_conf = FakeDefaultConfig(types.SimpleNamespace(save=lambda: None))
        del no_sources_conf["provider_sources"]

        for label, legacy in (
            ("missing_resource_lock", LegacyHostManager(has_lock=False)),
            ("missing_acm", LegacyHostManager(has_lock=True, default_conf=MISSING)),
            ("none_default_conf", LegacyHostManager(has_lock=True, default_conf=None)),
            ("missing_provider_sources_key", LegacyHostManager(has_lock=True, default_conf=no_sources_conf)),
            ("non_dict_default_conf", LegacyHostManager(has_lock=True, default_conf=bad_conf)),
        ):
            with self.subTest(label=label):
                self.context.provider_manager = legacy
                with patch.object(onboarding_module.logger, "warning") as warning:
                    await self.apply()
                self.assertEqual(self.service.action_error, "")
                self.assertIsNotNone(self.service.result)
                self.assertEqual(len(legacy.persisted), 1)
                self.assertNotIn("provider_source_id", legacy.persisted[0])
                warning.assert_called()

    async def test_tools_mode_chat_omits_tool_use_and_allows_separate_tools_instance(self):
        self.service.settings = Settings(enable_tools=True)
        resp_chat = await self.call(
            "apply",
            {
                "csrf": self.service.csrf,
                "model": "gemini-test",
                "effort": "default",
                "tools_mode": "chat",
                "use_default": False,
            },
        )
        self.assertEqual(resp_chat["status_code"], 200)
        await self.service.job
        chat_cfg = self.manager.providers_config[0]
        self.assertEqual(chat_cfg["custom_extra_body"]["agy_tools_mode"], "chat")
        self.assertNotIn("tool_use", chat_cfg["modalities"])

        resp_tools = await self.call(
            "apply",
            {
                "csrf": self.service.csrf,
                "model": "gemini-test",
                "effort": "default",
                "tools_mode": "tools",
                "use_default": False,
            },
        )
        self.assertEqual(resp_tools["status_code"], 200)
        await self.service.job
        self.assertEqual(len(self.manager.providers_config), 2)
        tools_cfg = self.manager.providers_config[1]
        self.assertEqual(tools_cfg["custom_extra_body"]["agy_tools_mode"], "tools")
        self.assertIn("tool_use", tools_cfg["modalities"])
        self.assertNotEqual(chat_cfg["id"], tools_cfg["id"])

        bad = await self.call(
            "apply",
            {
                "csrf": self.service.csrf,
                "model": "gemini-test",
                "effort": "default",
                "tools_mode": "invalid",
                "use_default": False,
            },
        )
        self.assertEqual(bad["status_code"], 400)


if __name__ == "__main__":
    unittest.main()
