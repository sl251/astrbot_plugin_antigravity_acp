"""Exact AstrBot 4.29.4 host capability filtering, without live CLI access."""

import asyncio
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401
from tests.test_tools_mode import _load_provider_module, _tool

from agy_provider.config import Settings
from agy_provider.messages import digest
from agy_provider.process import LaunchInfo, launch_args as real_launch_args
from agy_provider.system_prompt import filter_host_capabilities


# Source fixture: _build_local_mode_prompt(), AstrBot tag v4.29.4.
START = "Local computer tools are available subject to your role's file and execution permissions. "
END = (
    "Local shell commands automatically return a managed session when they "
    "outlive the initial wait. Use `astrbot_shell_session` to list, poll, "
    "write raw text or complete lines to, interrupt, or terminate those sessions. "
    "Use its `write_line` action for line-oriented programs so the session receives "
    "a real line feed. Do not add `&`, `nohup`, or another detachment wrapper for "
    "ordinary long-running commands."
)
LINUX = START + (
    "Current operating system: Linux. "
    "The runtime shell is Unix-like. Use POSIX-compatible shell commands. "
) + END
WINDOWS_PWSH = START + (
    "Current operating system: Windows. "
    "The runtime shell is PowerShell 7 (pwsh.exe). "
    "Use PowerShell 7-compatible syntax and cmdlets, and do not "
    "assume a full Unix userland or GNU utilities are available. "
) + END
WINDOWS_PS = START + (
    "Current operating system: Windows. "
    "The runtime shell is Windows PowerShell 5.1 (powershell.exe). "
    "Use Windows PowerShell 5.1-compatible syntax and cmdlets; do not use "
    "PowerShell 7-only syntax or assume Unix commands like cat/ls/grep are available. "
) + END


class HostCapabilityFilterTests(unittest.TestCase):
    def test_exact_variants_preserve_surrounding_persona_and_line_endings(self):
        self.assertEqual(len((LINUX + "\n").encode()), 596)
        for block in (LINUX, WINDOWS_PWSH, WINDOWS_PS):
            for newline in ("\n", "\r\n"):
                with self.subTest(block=block[:130], newline=newline):
                    source = f"人格与 shell 说明{newline}{block}{newline}其他约束"
                    expected = f"人格与 shell 说明{newline}{newline}其他约束"
                    self.assertEqual(filter_host_capabilities(source), expected)
            self.assertEqual(filter_host_capabilities(block), "")
        self.assertEqual(filter_host_capabilities(LINUX + "\n" + LINUX), "\n")

    def test_unknown_partial_and_quoted_blocks_remain_unchanged(self):
        for source in (
            START,
            LINUX.replace("managed session", "new managed session"),
            LINUX.replace("Linux", "FutureOS"),
            "用户引用：" + LINUX,
            LINUX + " 人格补充",
            "前文\u2028" + LINUX,
        ):
            with self.subTest(source=source[:100]):
                with patch("agy_provider.system_prompt.logger.debug") as debug:
                    self.assertEqual(filter_host_capabilities(source), source)
                    debug.assert_called_once()
                    self.assertNotIn(source, str(debug.call_args))
        with patch("agy_provider.system_prompt.logger.debug") as debug:
            self.assertEqual(filter_host_capabilities("人格 shell/browser 能力说明"), "人格 shell/browser 能力说明")
            debug.assert_not_called()


class HostCapabilityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_tools_and_structured_system_reach_home_filtered(self):
        with tempfile.TemporaryDirectory(prefix="agy-filter-") as temp:
            root = Path(temp)
            stubs = _load_provider_module(root)
            previous = sys.modules.pop("agy_provider.provider", None)
            try:
                with patch.dict(sys.modules, stubs):
                    mod = importlib.import_module("agy_provider.provider")
                    for tools in (False, True):
                        for structured in (False, True):
                            with self.subTest(tools=tools, structured=structured):
                                mod.configure(Settings(
                                    auth_bridge=False, enable_tools=tools,
                                    session_reuse_mode="structured" if structured else "strict",
                                ))
                                provider = mod.AgyProvider({
                                    "id": f"filter-{tools}-{structured}", "model": "gemini-test",
                                    "custom_extra_body": {"agy_tools_mode": "tools" if tools else "chat"},
                                }, {})
                                stable = "原有人格\n" + LINUX + "\n保留说明"
                                cleaned = "原有人格\n\n保留说明"
                                transient = "用户记忆；不要修改"
                                raw = stable + "\n\n" + transient if structured else stable
                                parts = {"version": 1, "source": "test", "stable_system": stable,
                                         "transient_data": transient} if structured else None
                                homes = []

                                def launch(info, home, use_default_agent=False, *, customizations_mode="inherit"):
                                    if use_default_agent:
                                        path = home / "workspace" / "AGENTS.md"
                                    else:
                                        path = home / "config" / "plugins" / "astrbot-chat" / "agents" / "astrbot-chat.md"
                                    homes.append(path.read_text(encoding="utf-8"))
                                    args = real_launch_args(info, home, use_default_agent,
                                                            customizations_mode=customizations_mode)
                                    return [sys.executable, str(Path(__file__).with_name("fake_cli.py")), "echo", *args[1:]]

                                captured = []
                                original_normalize = mod.normalize

                                def capture(*args, **kwargs):
                                    request = original_normalize(*args, **kwargs)
                                    captured.append(request)
                                    return request

                                try:
                                    with (
                                        patch.object(mod, "normalize", side_effect=capture),
                                        patch("agy_provider.sessions.discover", AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.9", False))),
                                        patch("agy_provider.sessions.launch_args", side_effect=launch),
                                    ):
                                        response = await provider.text_chat(
                                            prompt=LINUX, system_prompt=raw, session_id="test",
                                            func_tool=SimpleNamespace(tools=[_tool("echo")]) if tools else None,
                                            abort_signal=asyncio.Event(), agy_context_parts=parts,
                                        )
                                    self.assertEqual(response.completion_text, "reply:1")
                                    self.assertEqual(len(homes), 1)
                                    self.assertIn(cleaned, homes[0])
                                    self.assertNotIn(START, homes[0])
                                    request = captured[-1]
                                    self.assertEqual(request.system, cleaned)
                                    self.assertEqual(request.system_hash, digest(cleaned))
                                    self.assertEqual(request.system_bytes, len(cleaned.encode()))
                                    self.assertEqual(request.prompt_context.stable_system, cleaned)
                                    self.assertEqual(request.system_structured, structured)
                                    self.assertEqual(request.messages[-1]["content"], LINUX)
                                    if structured:
                                        self.assertEqual(request.transient_data, transient)
                                        self.assertEqual(parts["stable_system"], stable)
                                finally:
                                    await provider.terminate()
                    mod.configure(Settings())
            finally:
                sys.modules.pop("agy_provider.provider", None)
                if previous is not None:
                    sys.modules["agy_provider.provider"] = previous


if __name__ == "__main__":
    unittest.main()
