"""Issue #41：CLI 1.5.18 统一原生工具注册表、分页校验、无 MCP 模式与非搜索拒止提示测试。"""

import asyncio
import copy
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agy_provider import builtin_tools
from agy_provider.builtin_tools import (
    BUILTIN_SPECS,
    BUILTINS,
    KNOWN_BUILTIN_NAMES,
    NON_SEARCH_ABORTED_NOTICE,
    SEARCH_ABORTED_NOTICE,
    BuiltinDenied,
    builtin_notice,
    builtin_tools_configured,
    is_known_builtin,
    read_target,
    supports_read,
    tool_policy,
)
from agy_provider.config import Settings
from agy_provider.process import LaunchInfo, _guardrails, launch_args as real_launch_args
from agy_provider.tools import ToolBroker, ToolSnapshot
from agy_provider.transport import AcpClient
from tests.test_tools_mode import _load_provider_module

FIXTURE = Path(__file__).with_name("fake_cli.py")


class BuiltinRegistryAndVersionTests(unittest.TestCase):
    def test_registry_covers_cli_1518_declared_tools_and_keeps_internal_hidden_from_ui(self):
        self.assertIn("edit_file", BUILTINS)
        for cli_1518_tool in (
            "run_command", "read_file", "edit_file", "write_file",
            "update_steps", "manage_task", "schedule", "ask_question",
            "search_web", "define_subagent", "invoke_subagent",
            "manage_subagents", "send_message", "run_workflow", "swarm",
        ):
            self.assertIn(cli_1518_tool, KNOWN_BUILTIN_NAMES)
            self.assertTrue(is_known_builtin(cli_1518_tool))
        self.assertIn("schedule", BUILTIN_SPECS)
        self.assertFalse(BUILTIN_SPECS["schedule"].ui_visible)
        self.assertNotIn("schedule", BUILTINS)

        policy = tool_policy(Settings(enable_tools=True), "deny", supported=True)
        by_id = {row["id"]: row for row in policy["tools"]}
        self.assertEqual(by_id["edit_file"]["capability"], "unsupported")
        self.assertEqual(by_id["edit_file"]["choices"], ["deny"])
        self.assertEqual(by_id["read_file"]["capability"], "supported")
        self.assertEqual(by_id["read_file"]["choices"], ["deny", "allow_once"])

    def test_supports_read_verified_versions_and_platform_gates(self):
        with patch.object(builtin_tools.sys, "platform", "linux"):
            self.assertTrue(supports_read("1.5.17"))
            self.assertTrue(supports_read("agy 1.5.18"))
            self.assertFalse(supports_read("1.5.16"))
            self.assertFalse(supports_read("1.5.19"))
            self.assertFalse(supports_read("unknown"))
        for plat in ("win32", "darwin"):
            with self.subTest(platform=plat), patch.object(builtin_tools.sys, "platform", plat):
                self.assertFalse(supports_read("1.5.17"))
                self.assertFalse(supports_read("1.5.18"))

    def test_builtin_tools_configured_respects_master_and_platform(self):
        self.assertFalse(builtin_tools_configured(Settings(enable_tools=False, builtin_search_web="allow")))
        self.assertFalse(builtin_tools_configured(Settings(enable_tools=True)))
        self.assertTrue(builtin_tools_configured(Settings(enable_tools=True, builtin_search_web="allow")))
        with patch.object(builtin_tools.sys, "platform", "linux"):
            self.assertTrue(builtin_tools_configured(Settings(enable_tools=True, builtin_read_file="allow_once")))
        with patch.object(builtin_tools.sys, "platform", "win32"):
            self.assertFalse(builtin_tools_configured(Settings(enable_tools=True, builtin_read_file="allow_once")))
            self.assertTrue(builtin_tools_configured(Settings(enable_tools=True, builtin_search_web="allow")))


class NonSearchDenialNoticeTests(unittest.TestCase):
    def test_non_search_builtin_never_reports_search_aborted_notice(self):
        self.assertEqual(builtin_notice("search_web"), SEARCH_ABORTED_NOTICE)
        for name in ("read_file", "edit_file", "write_file", "run_command", "schedule", "invoke_subagent", "swarm"):
            notice = builtin_notice(name)
            self.assertEqual(notice, NON_SEARCH_ABORTED_NOTICE)
            self.assertNotIn("搜索", notice)

        client = AcpClient(Settings())
        client.session_id = "s1"
        client.active = True
        for name in ("read_file", "edit_file", "run_command", "invoke_subagent", "schedule"):
            with self.subTest(name=name), self.assertRaises(BuiltinDenied) as caught:
                client._on_update({
                    "sessionId": "s1",
                    "update": {"sessionUpdate": "tool_call", "toolCallId": "c1", "name": name},
                })
            self.assertEqual(caught.exception.code, "TOOLS_DISABLED")
            self.assertEqual(caught.exception.user_notice, NON_SEARCH_ABORTED_NOTICE)
            self.assertNotIn("搜索", caught.exception.user_notice)

        with self.assertRaises(BuiltinDenied) as caught:
            client._on_update({
                "sessionId": "s1",
                "update": {"sessionUpdate": "tool_call", "toolCallId": "c2", "name": "search_web"},
            })
        self.assertEqual(caught.exception.user_notice, SEARCH_ABORTED_NOTICE)

    def test_broker_rejects_cli_1518_internal_builtins_via_builtin_denied(self):
        broker = ToolBroker(ToolSnapshot(None), Settings(enable_tools=True))
        broker.turn_active = True
        for name in ("edit_file", "ask_question", "update_steps", "manage_task",
                     "schedule", "define_subagent", "invoke_subagent", "run_workflow", "swarm"):
            with self.subTest(name=name), self.assertRaises(BuiltinDenied):
                broker.observe({"toolCallId": f"id-{name}", "name": name})


class NoMcpBuiltinsModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-nomcp-41-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log_path = self.root / "cli.jsonl"

    async def test_search_web_activates_without_host_mcp_and_guards_chat_and_choice_none(self):
        stubs = _load_provider_module(self.root)
        seen_homes = []

        def fake_launch(info, home, *, customizations_mode="inherit"):
            agents_text = (home / "workspace" / "AGENTS.md").read_text(encoding="utf-8")
            seen_homes.append((home, agents_text, (home / "config").exists()))
            args = real_launch_args(info, home, customizations_mode=customizations_mode)
            self.assertNotIn("--agent", args)
            return [sys.executable, str(FIXTURE), "echo", *args[1:]]

        with patch.dict(sys.modules, stubs):
            sys.modules.pop("agy_provider.provider", None)
            mod = importlib.import_module("agy_provider.provider")
            self.addCleanup(sys.modules.pop, "agy_provider.provider", None)
            self.addCleanup(mod.configure, Settings())
            mod.configure(Settings(enable_tools=True, builtin_search_web="allow", auth_bridge=False))

            provider = mod.AgyProvider({"id": "nomcp", "model": "gemini-test"}, {})
            self.addAsyncCleanup(provider.terminate)
            scenario = self.root / "scenario"
            scenario.write_text("builtin-search")

            with (
                patch.dict(os.environ, {"AGY_SCENARIO_FILE": str(scenario), "AGY_TEST_LOG": str(self.log_path)}),
                patch("agy_provider.sessions.discover", AsyncMock(return_value=LaunchInfo("fake", "1.5.18", False))),
                patch("agy_provider.sessions.launch_args", side_effect=fake_launch),
                patch.object(ToolBroker, "start", AsyncMock(side_effect=AssertionError("无 MCP 时不得启动 mcp_bridge"))),
            ):
                # 1. 无宿主 MCP（func_tool=None）仍进入默认 agent 并允许搜索预算计数
                res1 = await provider.text_chat(
                    prompt="search something", session_id="s-nomcp", func_tool=None, abort_signal=asyncio.Event(),
                )
                self.assertEqual(res1.completion_text, "fake answer")
                agents_md = seen_homes[-1][1]
                self.assertFalse(seen_homes[-1][2])
                self.assertIn("No AstrBot MCP tools are active in this session.", agents_md)
                self.assertIn("may use the built-in search_web tool", agents_md)
                self.assertEqual(provider.runtime.telemetry.history[-1]["mode"], "tools")
                self.assertEqual(provider.runtime.telemetry.history[-1]["tool_calls"], 1)

                # 2. 同会话切换 tool_choice="none" 触发会话重建并切换为无工具默认 agent + AGENTS.md
                scenario.write_text("echo")
                res2 = await provider.text_chat(
                    prompt="no tools this turn", session_id="s-nomcp",
                    func_tool=SimpleNamespace(tools=[]), tool_choice="none", abort_signal=asyncio.Event(),
                )
                self.assertEqual(res2.completion_text, "reply:1")
                self.assertIn("No tools are available in this session.", seen_homes[-1][1])
                self.assertFalse(seen_homes[-1][2])
                self.assertEqual(provider.runtime.telemetry.history[-1]["mode"], "chat")
                self.assertIn("tool_hash_changed", provider.runtime.telemetry.history[-1]["rebuild_reasons"])

                # 3. chat 实例即使全局开启 search_web 也保持 chat 模式且不传 --agent
                chat_provider = mod.AgyProvider(
                    {"id": "chat-only", "model": "gemini-test", "custom_extra_body": {"agy_tools_mode": "chat"}},
                    {},
                )
                self.addAsyncCleanup(chat_provider.terminate)
                await chat_provider.text_chat(prompt="hello", session_id="s-chat", func_tool=None)
                self.assertIn("No tools are available in this session.", seen_homes[-1][1])
                self.assertFalse(seen_homes[-1][2])
                self.assertEqual(chat_provider.runtime.telemetry.history[-1]["mode"], "chat")

    def test_guardrails_no_mcp_copy_keeps_strict_prohibitions(self):
        text = _guardrails(True, read_allowed=True, search_allowed=False, has_mcp=False)
        self.assertIn("No AstrBot MCP tools are active in this session.", text)
        self.assertNotIn("session MCP tools exposed by AstrBot", text)
        self.assertIn("request read_file for a top-level .txt file", text)
        self.assertIn("web search or browser", text)


@unittest.skipUnless(sys.platform == "linux", "Scoped builtin reads are verified only on Linux")
class ReadPaginationAndNoMcpLinuxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.file = self.workspace / "lines.txt"
        self.file.write_text("L1\nL2\nL3\n", encoding="utf-8")
        self.file.chmod(0o600)
        settings = Settings(builtin_read_file="allow_once", enable_tools=True, tool_max_calls=2)
        self.broker = ToolBroker(ToolSnapshot(None), settings)
        self.client = AcpClient(settings)
        self.client.verified_read = True
        self.client.tool_broker = self.broker
        self.client.proc = SimpleNamespace(returncode=None)
        self.client.active = self.broker.turn_active = True
        self.client.session_id = self.broker.session_id = "sess_1518"
        self.broker.read_workspace = self.workspace
        self.client._write = AsyncMock()

    def _make_call(self, raw_args, *, ident="call-lines", status="pending", locations=None):
        return {
            "toolCallId": ident,
            "name": "read_file",
            "kind": "read",
            "status": status,
            "rawInput": raw_args,
            "args": raw_args,
            "locations": locations if locations is not None else [{"path": str(self.file)}],
        }

    def test_read_target_allows_valid_pagination_and_rejects_bad_types_or_file_uri(self):
        valid = self._make_call({"Path": str(self.file), "StartLine": 2, "EndLine": 3, "ByteOffset": 0})
        self.assertEqual(read_target(valid, self.workspace), str(self.file))

        invalid_cases = [
            {"Path": str(self.file), "StartLine": True, "EndLine": 3},
            {"Path": str(self.file), "StartLine": 0, "EndLine": 3},
            {"Path": str(self.file), "StartLine": 3, "EndLine": 2},
            {"Path": str(self.file), "ByteOffset": -1},
            {"Path": str(self.file), "Extra": 1},
            {"Path": f"file://{self.file}"},
        ]
        for args in invalid_cases:
            with self.subTest(args=args):
                self.assertIsNone(read_target(self._make_call(args), self.workspace))

    async def test_read_pagination_args_cannot_be_mutated_between_observe_and_permission(self):
        args1 = {"Path": str(self.file), "StartLine": 1, "EndLine": 2}
        self.client.prompt_deadline = asyncio.get_running_loop().time() + 10
        self.client._on_update({"sessionId": "sess_1518", "update": {
            "sessionUpdate": "tool_call", **self._make_call(args1, ident="call-tampered", status="in_progress"),
        }})
        self.client._on_update({"sessionId": "sess_1518", "update": {
            "sessionUpdate": "tool_call_update", **self._make_call(args1, ident="call-tampered", status="pending"),
        }})

        # 篡改 StartLine/EndLine 的权限请求必须被拒绝，且拒绝后该 ID 不可再用原参数二次放行
        tampered = {"Path": str(self.file), "StartLine": 1, "EndLine": 3}
        await self.client._reject_request({
            "id": "p1",
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess_1518",
                "toolCall": self._make_call(tampered, ident="call-tampered", status="pending"),
                "options": [{"optionId": "allow", "kind": "allow_once"}],
            },
        })
        self.assertEqual(self.client._write.call_args.args[0]["result"], {"outcome": {"outcome": "cancelled"}})
        await self.client._reject_request({
            "id": "p1b",
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess_1518",
                "toolCall": self._make_call(copy.deepcopy(args1), ident="call-tampered", status="pending"),
                "options": [{"optionId": "allow", "kind": "allow_once"}],
            },
        })
        self.assertEqual(self.client._write.call_args.args[0]["result"], {"outcome": {"outcome": "cancelled"}})
        self.assertEqual(self.broker.count, 0)

        # 未篡改的新分页调用应成功放行并扣减预算
        self.client._on_update({"sessionId": "sess_1518", "update": {
            "sessionUpdate": "tool_call", **self._make_call(args1, ident="call-valid", status="in_progress"),
        }})
        self.client._on_update({"sessionId": "sess_1518", "update": {
            "sessionUpdate": "tool_call_update", **self._make_call(args1, ident="call-valid", status="pending"),
        }})
        await self.client._reject_request({
            "id": "p2",
            "method": "session/request_permission",
            "params": {
                "sessionId": "sess_1518",
                "toolCall": self._make_call(copy.deepcopy(args1), ident="call-valid", status="pending"),
                "options": [{"optionId": "allow", "kind": "allow_once"}],
            },
        })
        self.assertEqual(self.client._write.call_args.args[0]["result"], {"outcome": {"outcome": "selected", "optionId": "allow"}})
        self.assertEqual(self.broker.count, 1)

    async def test_no_mcp_read_file_1518_attaches_read_workspace_without_bridge_child(self):
        from agy_provider.sessions import Runtime, Session

        settings = Settings(builtin_read_file="allow_once", enable_tools=True, auth_bridge=False)
        runtime = Runtime(settings, self.workspace)
        self.addAsyncCleanup(runtime.close)
        session = Session()
        fake_client = SimpleNamespace(
            verified_read=True,
            session_id="sess-nomcp-1518",
            start=AsyncMock(),
            new_session=AsyncMock(return_value={"models": {"availableModels": []}}),
            close=AsyncMock(),
        )
        with (
            patch("agy_provider.sessions.discover", AsyncMock(return_value=LaunchInfo("fake", "1.5.18", False))),
            patch("agy_provider.sessions.AcpClient", return_value=fake_client),
            patch.object(ToolBroker, "start", AsyncMock(side_effect=AssertionError("无 MCP 时不得启动 mcp_bridge"))),
        ):
            await runtime.open(session, "system", ToolSnapshot(None), allow_builtins=True)
        self.assertIsNotNone(session.broker)
        self.assertEqual(session.broker.read_workspace, (session.home / "workspace").resolve())
        self.assertEqual(fake_client.new_session.call_args.args[1], [])
        self.assertTrue(session.allow_builtins)
        await session.close()


if __name__ == "__main__":
    unittest.main()
