import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.process import LaunchInfo, launch_args as real_launch_args

FIXTURE = Path(__file__).with_name("fake_cli.py")


class _FakeLLMResponse:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


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


def _tool(name):
    return SimpleNamespace(
        active=True,
        name=name,
        description="test tool",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )


def _load_provider_module(root: Path):
    fake_logger = types.SimpleNamespace(
        warning=lambda *a, **kw: None,
        info=lambda *a, **kw: None,
    )
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = fake_logger
    provider_mod = types.ModuleType("astrbot.api.provider")
    provider_mod.LLMResponse = _FakeLLMResponse
    provider_mod.Provider = _FakeProvider
    core_mod = types.ModuleType("astrbot.core")
    core_provider_mod = types.ModuleType("astrbot.core.provider")
    entities_mod = types.ModuleType("astrbot.core.provider.entities")
    entities_mod.TokenUsage = dict
    utils_mod = types.ModuleType("astrbot.core.utils")
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    path_mod.get_astrbot_plugin_data_path = lambda: str(root)
    return {
        "astrbot": astrbot_mod,
        "astrbot.api": api_mod,
        "astrbot.api.provider": provider_mod,
        "astrbot.core": core_mod,
        "astrbot.core.provider": core_provider_mod,
        "astrbot.core.provider.entities": entities_mod,
        "astrbot.core.utils": utils_mod,
        "astrbot.core.utils.astrbot_path": path_mod,
    }


class ToolsModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-tools-mode-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_path = self.root / "cli.jsonl"

    async def test_temporary_user_text_reuses_session_but_real_edits_rebuild(self):
        def text(value):
            return {"type": "text", "text": value}

        with (
            patch.dict(sys.modules, _load_provider_module(self.root)),
            patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
            patch(
                "agy_provider.sessions.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.9", False)),
            ),
            patch(
                "agy_provider.sessions.launch_args",
                return_value=[sys.executable, str(FIXTURE), "echo"],
            ),
        ):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            self.addCleanup(sys.modules.pop, "agy_provider.provider", None)
            self.addCleanup(mod.configure, Settings())
            for mode in ("strict", "structured"):
                with self.subTest(mode=mode):
                    mod.configure(Settings(
                        enable_tools=True, auth_bridge=False, session_reuse_mode=mode,
                    ))
                    provider = mod.AgyProvider({"id": mode, "model": "gemini-test"}, {})
                    self.addAsyncCleanup(provider.terminate)

                    async def chat(contexts, extras=None):
                        return await provider.text_chat(
                            contexts=contexts, extra_user_content_parts=extras,
                            session_id=mode, func_tool=SimpleNamespace(tools=[_tool("echo")]),
                            abort_signal=asyncio.Event(),
                        )

                    # The runner's serialized contexts omit PrivateAttr.
                    # One extra duplicates the prompt, so position matters.
                    reminder = text("<system_reminder>persistent</system_reminder>")
                    extras = [
                        reminder,
                        SimpleNamespace(_no_save=True, model_dump=lambda: text("first")),
                        {**text("temporary snapshot"), "_no_save": True},
                    ]
                    user = {"role": "user", "content": [
                        text("first"), reminder, text("first"), text("temporary snapshot"),
                    ]}
                    first = await chat([user], extras)
                    self.assertEqual(first.completion_text, "reply:1")
                    self.assertNotIn("_no_save", user["content"][-1])

                    history = [
                        {"role": "user", "content": [text("first"), reminder]},
                        {"role": "assistant", "content": [
                            {"type": "think", "think": "reasoning"}, text(first.completion_text),
                        ]},
                    ]
                    second = await chat([*history, {"role": "user", "content": "second"}])
                    self.assertEqual(second.completion_text, "reply:2")
                    self.assertEqual(provider.runtime.telemetry.history[-1]["mode"], "tools")
                    self.assertTrue(provider.runtime.telemetry.history[-1]["reused"])
                    self.assertEqual(provider.runtime.telemetry.history[-1]["history_relation"], "exact")

                    # Edit only the previous user message, preserving its reply.
                    third = await chat([
                        *history,
                        {"role": "user", "content": "edited second"},
                        {"role": "assistant", "content": second.completion_text},
                        {"role": "user", "content": "third"},
                    ])
                    self.assertEqual(third.completion_text, "reply:1")
                    self.assertEqual(
                        provider.runtime.telemetry.history[-1]["primary_reason"], "history_rewrite",
                    )

        events = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        prompts = [event for event in events if event["event"] == "prompt"]
        self.assertEqual(len(prompts), 6)
        for i in (0, 3):
            self.assertIn("temporary snapshot", "".join(prompts[i]["texts"]))
            self.assertEqual(prompts[i + 1]["texts"], ["second"])

    async def test_resolve_tools_mode_truth_table_and_validation(self):
        stubs = _load_provider_module(self.root)
        with patch.dict(sys.modules, stubs):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            resolve = mod.resolve_tools_mode

            self.assertFalse(resolve(False, "inherit", "auto"))
            self.assertFalse(resolve(False, "chat", "auto"))
            self.assertFalse(resolve(False, "tools", "auto"))
            self.assertTrue(resolve(True, "inherit", "auto"))
            self.assertFalse(resolve(True, "chat", "auto"))
            self.assertTrue(resolve(True, "tools", "auto"))
            self.assertFalse(resolve(True, "tools", "none"))
            self.assertFalse(resolve(True, "inherit", "none"))
            with self.assertRaises(AgyError) as caught:
                resolve(True, "invalid-mode", "auto")
            self.assertEqual(caught.exception.code, "CONFIG")
        sys.modules.pop("agy_provider.provider", None)

    async def test_chat_instance_argv_uses_default_agent_and_excludes_tools(self):
        stubs = _load_provider_module(self.root)
        func_tool = SimpleNamespace(tools=[_tool("web_search")])
        signal = asyncio.Event()
        seen_homes = []

        def fake_launch(info, home, *, customizations_mode="inherit"):
            agents_text = (home / "workspace" / "AGENTS.md").read_text(encoding="utf-8")
            seen_homes.append((home, agents_text, (home / "config").exists()))
            args = real_launch_args(info, home, customizations_mode=customizations_mode)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with patch.dict(sys.modules, stubs):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            mod.configure(Settings(enable_tools=True, auth_bridge=False))
            self.addCleanup(mod.configure, Settings())
            provider = mod.AgyProvider(
                {
                    "id": "agy_chat",
                    "model": "gemini-test",
                    "custom_extra_body": {"agy_tools_mode": "chat"},
                },
                {},
            )
            self.addAsyncCleanup(provider.terminate)

            with (
                patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
                patch(
                    "agy_provider.sessions.discover",
                    AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.19", False, True)),
                ),
                patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            ):
                resp = await provider.text_chat(
                    prompt="纯聊天问题",
                    session_id="chat-s1",
                    func_tool=func_tool,
                    abort_signal=signal,
                )
            self.assertEqual(resp.completion_text, "reply:1")

            # chat instance must reject tool call continuation / tool history
            with self.assertRaises(AgyError) as caught:
                await provider.text_chat(
                    prompt=None,
                    session_id="chat-s1",
                    tool_calls_result=SimpleNamespace(
                        tool_calls_result=[
                            SimpleNamespace(
                                tool_call_id="c1",
                                content="done",
                                model_dump=lambda: {
                                    "role": "tool",
                                    "tool_call_id": "c1",
                                    "content": "done",
                                },
                            )
                        ],
                        assistant_message=SimpleNamespace(
                            content="",
                            tool_calls=[],
                            model_dump=lambda: {"role": "assistant", "content": ""},
                        ),
                    ),
                )
            self.assertEqual(caught.exception.code, "TOOLS_DISABLED")

        sys.modules.pop("agy_provider.provider", None)

        events = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        inits = [e for e in events if e["event"] == "init"]
        self.assertEqual(len(inits), 1)
        argv = inits[0]["argv"]
        self.assertNotIn("--agent", argv)
        self.assertNotIn("--include-default-customizations=false", argv)
        self.assertIn("No tools are available in this session.", seen_homes[0][1])
        self.assertIn("Never run built-in", seen_homes[0][1])
        self.assertFalse(seen_homes[0][2])

        # Also verify telemetry recorded mode == "chat" and tool_schema_bytes == 0
        turns = list(provider.runtime.telemetry.history)
        self.assertEqual(turns[0]["mode"], "chat")
        self.assertEqual(turns[0]["tool_schema_bytes"], 0)

    async def test_tools_mode_cannot_bypass_master_enable_tools_or_tool_choice_none(self):
        stubs = _load_provider_module(self.root)
        func_tool = SimpleNamespace(tools=[_tool("calc")])
        signal = asyncio.Event()

        def fake_launch(info, home, *, customizations_mode="inherit"):
            args = real_launch_args(info, home, customizations_mode=customizations_mode)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with patch.dict(sys.modules, stubs):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            mod.configure(Settings(enable_tools=False, auth_bridge=False))
            self.addCleanup(mod.configure, Settings())

            # Case 1: provider sets agy_tools_mode="tools", plugin master switch is False
            p_disabled = mod.AgyProvider(
                {
                    "id": "agy_tools_blocked",
                    "model": "gemini-test",
                    "custom_extra_body": {"agy_tools_mode": "tools"},
                },
                {},
            )
            self.addAsyncCleanup(p_disabled.terminate)

            with (
                patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
                patch(
                    "agy_provider.sessions.discover",
                    AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.19", False, True)),
                ),
                patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            ):
                resp = await p_disabled.text_chat(
                    prompt="需要工具吗",
                    session_id="tools-blocked-s1",
                    func_tool=func_tool,
                    abort_signal=signal,
                    agy_tools_mode="tools",  # attempt request kwarg bypass too
                )
            self.assertEqual(resp.completion_text, "reply:1")
            last = p_disabled.runtime.telemetry.history[-1]
            self.assertEqual(last["mode"], "chat")
            self.assertEqual(last["tool_schema_bytes"], 0)

            # Case 2: master switch is True, provider is "tools", but tool_choice="none"
            mod.configure(Settings(enable_tools=True, auth_bridge=False))
            p_enabled = mod.AgyProvider(
                {
                    "id": "agy_tools_choice_none",
                    "model": "gemini-test",
                    "custom_extra_body": {"agy_tools_mode": "tools"},
                },
                {},
            )
            self.addAsyncCleanup(p_enabled.terminate)
            with (
                patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
                patch(
                    "agy_provider.sessions.discover",
                    AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.19", False, True)),
                ),
                patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            ):
                await p_enabled.text_chat(
                    prompt="本次禁用工具",
                    session_id="choice-none-s1",
                    func_tool=func_tool,
                    tool_choice="none",
                    abort_signal=signal,
                )
            self.assertEqual(p_enabled.runtime.telemetry.history[-1]["mode"], "chat")

            # Case 3: master switch True + empty active func_tool + minimal mode still uses default agent without --agent
            mod.configure(Settings(enable_tools=True, auth_bridge=False, agy_customizations_mode="minimal"))
            p_minimal = mod.AgyProvider(
                {
                    "id": "agy_tools_minimal_chat",
                    "model": "gemini-test",
                    "custom_extra_body": {"agy_tools_mode": "tools"},
                },
                {},
            )
            self.addAsyncCleanup(p_minimal.terminate)
            with (
                patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log_path)}),
                patch(
                    "agy_provider.sessions.discover",
                    AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.19", False, True)),
                ),
                patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
            ):
                await p_minimal.text_chat(
                    prompt="空工具集",
                    session_id="empty-tools-s1",
                    func_tool=SimpleNamespace(tools=[]),
                    abort_signal=signal,
                )
            self.assertEqual(p_minimal.runtime.telemetry.history[-1]["mode"], "chat")

        sys.modules.pop("agy_provider.provider", None)

        events = [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        inits = [e for e in events if e["event"] == "init"]
        self.assertEqual(len(inits), 3)
        for init_event in inits:
            self.assertNotIn("--agent", init_event["argv"])
        self.assertIn("--include-default-customizations=false", inits[-1]["argv"])


if __name__ == "__main__":
    unittest.main()
