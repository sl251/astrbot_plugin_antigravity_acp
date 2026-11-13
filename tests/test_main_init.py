import asyncio
import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

from agy_provider.config import Settings


class FakePluginConfig(dict):
    def __init__(self, initial, *, save_error=None):
        super().__init__(initial)
        self.save_calls = 0
        self.save_error = save_error

    def save_config(self):
        self.save_calls += 1
        if self.save_error is not None:
            raise self.save_error


class FakeDefaultConfig(dict):
    def __init__(self, manager):
        super().__init__(provider=[], provider_sources=[])
        self._manager = manager

    def save_config(self):
        self._manager.save()


class FakeProviderManager:
    def __init__(self, *, save_error=None, legacy_no_lock=False):
        self.reload_lock = asyncio.Lock()
        if not legacy_no_lock:
            self.resource_lock = asyncio.Lock()
        default_conf = FakeDefaultConfig(self)
        self.acm = types.SimpleNamespace(default_conf=default_conf)
        self.providers_config = default_conf["provider"]
        self.provider_sources_config = default_conf["provider_sources"]
        self.inst_map = {}
        self.persisted = []
        self.persisted_sources = []
        self.save_error = save_error

    def save(self):
        if self.save_error is not None:
            raise self.save_error
        self.persisted = deepcopy(self.acm.default_conf["provider"])
        self.persisted_sources = deepcopy(self.acm.default_conf["provider_sources"])

    def get_merged_provider_config(self, config):
        return deepcopy(config)

    async def reload(self, config):
        self.inst_map[config["id"]] = object()

    async def terminate_provider(self, provider_id):
        self.inst_map.pop(provider_id, None)


class MainInitializeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-main-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _load_main_module(self):
        fake_logger = MagicMock()

        class FakeStar:
            def __init__(self, context):
                self.context = context

        class FakeProvider:
            def __init__(self, provider_config, provider_settings):
                self.provider_config = provider_config
                self.provider_settings = provider_settings

        class FakeRunner:
            async def reset(self, provider, request, tool_schema_mode="full"):
                return None

            def step_until_done(self):
                async def empty():
                    if False:
                        yield None

                return empty()

        registry = types.ModuleType("astrbot.core.provider.register")
        registry.provider_cls_map = {}
        registry.provider_registry = []

        def register_provider_adapter(ptype, desc, default_config_tmpl=None, provider_display_name=""):
            def decorator(cls):
                meta = types.SimpleNamespace(
                    type=ptype,
                    desc=desc,
                    default_config_tmpl=default_config_tmpl,
                    provider_display_name=provider_display_name,
                    cls_type=cls,
                )
                registry.provider_cls_map[ptype] = meta
                registry.provider_registry.append(meta)
                return cls

            return decorator

        registry.register_provider_adapter = register_provider_adapter

        def fake_command_group(*a, **kw):
            def decorator(fn):
                fn.command = lambda *ca, **ckw: (lambda f: f)
                return fn

            return decorator

        filter_obj = types.SimpleNamespace(
            command_group=fake_command_group,
            permission_type=lambda *a, **kw: (lambda fn: fn),
            PermissionType=types.SimpleNamespace(ADMIN="admin"),
        )
        astrbot_mod = types.ModuleType("astrbot")
        api_mod = types.ModuleType("astrbot.api")
        api_mod.logger = fake_logger
        event_mod = types.ModuleType("astrbot.api.event")
        event_mod.AstrMessageEvent = object
        event_mod.filter = filter_obj
        star_mod = types.ModuleType("astrbot.api.star")
        star_mod.Context = object
        star_mod.Star = FakeStar
        star_mod.register = lambda *a, **kw: (lambda cls: cls)
        web_mod = types.ModuleType("astrbot.api.web")
        web_mod.request = types.SimpleNamespace(username="", content_type="application/json")
        web_mod.json_response = lambda data, **kw: data
        web_mod.error_response = lambda msg, **kw: {"message": msg}
        provider_api_mod = types.ModuleType("astrbot.api.provider")
        provider_api_mod.LLMResponse = object
        provider_api_mod.Provider = FakeProvider
        core_mod = types.ModuleType("astrbot.core")
        core_provider_mod = types.ModuleType("astrbot.core.provider")
        core_provider_mod.register = registry
        entities_mod = types.ModuleType("astrbot.core.provider.entities")
        entities_mod.TokenUsage = dict
        entities_mod.ProviderType = types.SimpleNamespace(CHAT_COMPLETION="chat_completion")
        utils_mod = types.ModuleType("astrbot.core.utils")
        path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
        path_mod.get_astrbot_plugin_data_path = lambda: str(self.root)
        agent_mod = types.ModuleType("astrbot.core.agent")
        runners_mod = types.ModuleType("astrbot.core.agent.runners")
        loop_mod = types.ModuleType("astrbot.core.agent.runners.tool_loop_agent_runner")
        loop_mod.ToolLoopAgentRunner = FakeRunner

        repo_root = Path(__file__).resolve().parents[1]
        pkg_name = "agy_test_plugin_pkg"
        pkg_mod = types.ModuleType(pkg_name)
        pkg_mod.__path__ = [str(repo_root)]

        stubs = {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.web": web_mod,
            "astrbot.api.provider": provider_api_mod,
            "astrbot.core": core_mod,
            "astrbot.core.provider": core_provider_mod,
            "astrbot.core.provider.register": registry,
            "astrbot.core.provider.entities": entities_mod,
            "astrbot.core.utils": utils_mod,
            "astrbot.core.utils.astrbot_path": path_mod,
            "astrbot.core.agent": agent_mod,
            "astrbot.core.agent.runners": runners_mod,
            "astrbot.core.agent.runners.tool_loop_agent_runner": loop_mod,
            pkg_name: pkg_mod,
        }
        module_patch = patch.dict(sys.modules, stubs)
        module_patch.start()
        self.addCleanup(module_patch.stop)

        # Re-import agy_provider submodules cleanly inside the patched sys.modules scope.
        for name in list(sys.modules):
            if name == "agy_provider" or name.startswith("agy_provider."):
                sys.modules.pop(name, None)

        # Populate package namespace aliases so relative imports in main.py resolve cleanly.
        agy_pkg = importlib.import_module("agy_provider")
        sys.modules[f"{pkg_name}.agy_provider"] = agy_pkg
        for sub in ("config", "errors", "host_compat", "onboarding", "provider"):
            sys.modules[f"{pkg_name}.agy_provider.{sub}"] = importlib.import_module(
                f"agy_provider.{sub}"
            )

        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.main", repo_root / "main.py"
        )
        main_mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.main"] = main_mod
        spec.loader.exec_module(main_mod)
        return main_mod, fake_logger

    def _make_context(self, manager):
        context = types.SimpleNamespace(
            provider_manager=manager,
            registered_web_apis=[],
        )
        context.register_web_api = lambda *args: context.registered_web_apis.append(args)
        return context

    async def test_initialize_repairs_invalid_config_updates_and_saves(self):
        main_mod, fake_logger = self._load_main_module()
        manager = FakeProviderManager()
        config = FakePluginConfig(
            {
                "setup_auto_check": False,
                "tool_max_calls": 129,
                "idle_ttl": "bad-int",
                "max_concurrent": 8,
                "max_sessions": 2,
            }
        )
        plugin = main_mod.AgyProviderPlugin(self._make_context(manager), config=config)
        self.addAsyncCleanup(plugin.terminate)

        await plugin.initialize()

        defaults = Settings()
        self.assertEqual(config["tool_max_calls"], defaults.tool_max_calls)
        self.assertEqual(config["idle_ttl"], defaults.idle_ttl)
        self.assertEqual(config["max_concurrent"], 2)
        self.assertEqual(config.save_calls, 1)
        self.assertTrue(plugin.registered)
        self.assertEqual(plugin.setup_service.settings.tool_max_calls, defaults.tool_max_calls)
        self.assertEqual(plugin.setup_service.settings.idle_ttl, defaults.idle_ttl)
        self.assertEqual(plugin.setup_service.settings.max_concurrent, 2)
        warned = [call.args[1] for call in fake_logger.warning.call_args_list if len(call.args) >= 2]
        self.assertIn("tool_max_calls", warned)
        self.assertIn("idle_ttl", warned)
        self.assertIn("max_concurrent", warned)

    async def test_initialize_save_config_failure_warns_without_blocking(self):
        main_mod, fake_logger = self._load_main_module()
        manager = FakeProviderManager()
        config = FakePluginConfig(
            {
                "setup_auto_check": False,
                "tool_max_calls": 129,
                "idle_ttl": 6000,
            },
            save_error=OSError("disk full"),
        )
        plugin = main_mod.AgyProviderPlugin(self._make_context(manager), config=config)
        self.addAsyncCleanup(plugin.terminate)

        await plugin.initialize()

        defaults = Settings()
        self.assertEqual(config["tool_max_calls"], defaults.tool_max_calls)
        self.assertEqual(config["idle_ttl"], defaults.idle_ttl)
        self.assertEqual(config.save_calls, 1)
        self.assertTrue(plugin.registered)
        self.assertEqual(plugin.setup_service.settings.tool_max_calls, defaults.tool_max_calls)
        self.assertTrue(
            any("修正配置写盘失败" in str(call.args[0]) for call in fake_logger.warning.call_args_list)
        )

    async def test_initialize_binds_existing_provider_source(self):
        main_mod, _ = self._load_main_module()
        manager = FakeProviderManager()
        manager.providers_config.append(
            {"id": "agy_old", "type": "agy_acp", "model": "gemini-test", "enable": True}
        )
        config = FakePluginConfig({"setup_auto_check": False})
        plugin = main_mod.AgyProviderPlugin(self._make_context(manager), config=config)
        self.addAsyncCleanup(plugin.terminate)

        await plugin.initialize()

        self.assertEqual(manager.persisted_sources[0]["id"], "agy_acp")
        self.assertEqual(manager.persisted[0]["provider_source_id"], "agy_acp")

    async def test_initialize_source_save_failure_warns_without_blocking(self):
        main_mod, fake_logger = self._load_main_module()
        manager = FakeProviderManager(save_error=OSError("read-only"))
        manager.providers_config.append(
            {"id": "agy_old", "type": "agy_acp", "model": "gemini-test", "enable": True}
        )
        config = FakePluginConfig({"setup_auto_check": False})
        plugin = main_mod.AgyProviderPlugin(self._make_context(manager), config=config)
        self.addAsyncCleanup(plugin.terminate)

        await plugin.initialize()

        self.assertTrue(plugin.registered)
        self.assertTrue(
            any("模型来源关联保存失败" in str(call.args[0]) for call in fake_logger.warning.call_args_list)
        )

    async def test_initialize_legacy_host_skips_source_binding(self):
        main_mod, _ = self._load_main_module()
        manager = FakeProviderManager(legacy_no_lock=True)
        manager.providers_config.append(
            {"id": "agy_old", "type": "agy_acp", "model": "gemini-test", "enable": True}
        )
        config = FakePluginConfig({"setup_auto_check": False})
        plugin = main_mod.AgyProviderPlugin(self._make_context(manager), config=config)
        self.addAsyncCleanup(plugin.terminate)

        await plugin.initialize()
        self.assertTrue(plugin.registered)
        self.assertNotIn("provider_source_id", manager.providers_config[0])


if __name__ == "__main__":
    unittest.main()
