"""Exercise the shipped command bodies without importing a running AstrBot."""

import ast
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agy_provider.errors import AgyError
from agy_provider.onboarding import next_step


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        commands = [
            node
            for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in {"setup", "use", "doctor"}
        ]
        for node in commands:
            node.decorator_list = []
        namespace = {
            "AstrMessageEvent": object,
            "AgyError": AgyError,
            "next_step": next_step,
            "asyncio": asyncio,
            "json": __import__("json"),
        }
        exec(
            compile(ast.Module(body=commands, type_ignores=[]), str(path), "exec"),
            namespace,
        )
        self.commands = namespace
        self.manager = types.SimpleNamespace(set_provider=AsyncMock())
        service = types.SimpleNamespace(
            _available=lambda: None,
            busy=False,
            login=types.SimpleNamespace(busy=False),
            report={"status": "passed"},
            providers=lambda: [
                {"id": "enabled", "model": "gemini-test", "enabled": True},
                {"id": "disabled", "model": "gemini-test", "enabled": False},
            ],
        )
        self.plugin = types.SimpleNamespace(
            setup_service=service,
            stopping=False,
            context=types.SimpleNamespace(provider_manager=self.manager),
            doctor_lock=asyncio.Lock(),
        )
        self.event = types.SimpleNamespace(
            is_admin=lambda: True,
            plain_result=lambda text: text,
            unified_msg_origin="target-session",
        )
        entities = types.ModuleType("astrbot.core.provider.entities")
        entities.ProviderType = types.SimpleNamespace(CHAT_COMPLETION="chat_completion")
        module_patch = patch.dict(
            sys.modules, {"astrbot.core.provider.entities": entities}
        )
        module_patch.start()
        self.addCleanup(module_patch.stop)

    async def collect(self, name, *args):
        return [
            item async for item in self.commands[name](self.plugin, self.event, *args)
        ]

    async def test_all_commands_deny_non_admin_direct_invocation(self):
        self.event.is_admin = lambda: False
        for name in ("setup", "use", "doctor"):
            self.assertEqual(await self.collect(name), [])
        self.manager.set_provider.assert_not_awaited()

    async def test_use_only_changes_target_session(self):
        result = await self.collect("use", "enabled")
        self.manager.set_provider.assert_awaited_once_with(
            "enabled", "chat_completion", "target-session"
        )
        self.assertIn("已切换", result[0])

    async def test_disabled_non_agy_and_unknown_provider_denied(self):
        for provider_id in ("disabled", "other-provider", "", "unknown"):
            result = await self.collect("use", provider_id)
            self.assertIn("CONFIG", result[0])
        self.manager.set_provider.assert_not_awaited()

    async def test_stopping_plugin_rejects_commands(self):
        self.plugin.stopping = True
        for name in ("setup", "use", "doctor"):
            self.assertTrue(await self.collect(name))
        self.manager.set_provider.assert_not_awaited()

    async def test_setup_explains_native_entry_without_login_material(self):
        result = (await self.collect("setup"))[0]
        self.assertIn("首次设置", result)
        self.assertIn("enabled", result)
        self.assertNotIn("accounts.google.com", result)
