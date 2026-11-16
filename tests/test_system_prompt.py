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


# --- AstrBot v4.30.0-alpha.1 (commit 6a782b7) fixtures, extracted mechanically ---
# computer_use.py _build_local_mode_prompt() Windows output incl. AppContainer tail.
WINDOWS_430_PWSH = (
    "Local computer tools are available subject to your role's file and execu"
    'tion permissions. Current operating system: Windows. The runtime shell i'
    's PowerShell 7 (pwsh.exe). Use PowerShell 7-compatible syntax and cmdlet'
    's, and do not assume a full Unix userland or GNU utilities are available'
    '. Local shell commands automatically return a managed session when they '
    'outlive the initial wait. Use `astrbot_shell_session` to list, poll, wri'
    'te raw text or complete lines to, interrupt, or terminate those sessions'
    '. Use its `write_line` action for line-oriented programs so the session '
    'receives a real line feed. Do not add `&`, `nohup`, or another detachmen'
    't wrapper for ordinary long-running commands. Restricted Windows executi'
    'on uses AppContainer. PowerShell starts at Workspace:\\, a PSDrive mapped'
    ' to the real workspace. Use relative paths or Workspace:\\ paths with cmd'
    'lets; native programs need relative or actual filesystem paths, not PSDr'
    'ive paths. An InitializeDefaultDrives warning can occur because ancestor'
    ' directories are inaccessible; it does not imply the workspace command f'
    'ailed. Access-denied errors on absolute paths can be retried with a work'
    'space-relative path or astrbot_execute_python. Python/runtime files are '
    'read-only; use the workspace for temporary files and caches. Network cap'
    'abilities do not guarantee localhost access, including local proxy ports'
    '. Host proxy environment variables are not inherited. Console interrupts'
    ' are unavailable; use session terminate when needed. Job limits cover CP'
    'U time, process count and committed memory; per-file size and open-handl'
    'e limits are unavailable. Missing startup authorization requires restart'
    'ing AstrBot; tool calls never request administrator access or retry with'
    'out isolation.'
)
WINDOWS_430_PS51 = (
    "Local computer tools are available subject to your role's file and execu"
    'tion permissions. Current operating system: Windows. The runtime shell i'
    's Windows PowerShell 5.1 (powershell.exe). Use Windows PowerShell 5.1-co'
    'mpatible syntax and cmdlets; do not use PowerShell 7-only syntax or assu'
    'me Unix commands like cat/ls/grep are available. Local shell commands au'
    'tomatically return a managed session when they outlive the initial wait.'
    ' Use `astrbot_shell_session` to list, poll, write raw text or complete l'
    'ines to, interrupt, or terminate those sessions. Use its `write_line` ac'
    'tion for line-oriented programs so the session receives a real line feed'
    '. Do not add `&`, `nohup`, or another detachment wrapper for ordinary lo'
    'ng-running commands. Restricted Windows execution uses AppContainer. Pow'
    'erShell starts at Workspace:\\, a PSDrive mapped to the real workspace. U'
    'se relative paths or Workspace:\\ paths with cmdlets; native programs nee'
    'd relative or actual filesystem paths, not PSDrive paths. An InitializeD'
    'efaultDrives warning can occur because ancestor directories are inaccess'
    'ible; it does not imply the workspace command failed. Access-denied erro'
    'rs on absolute paths can be retried with a workspace-relative path or as'
    'trbot_execute_python. Python/runtime files are read-only; use the worksp'
    'ace for temporary files and caches. Network capabilities do not guarante'
    'e localhost access, including local proxy ports. Host proxy environment '
    'variables are not inherited. Console interrupts are unavailable; use ses'
    'sion terminate when needed. Job limits cover CPU time, process count and'
    ' committed memory; per-file size and open-handle limits are unavailable.'
    ' Missing startup authorization requires restarting AstrBot; tool calls n'
    'ever request administrator access or retry without isolation.'
)


OPEN_LOCAL = '<execution_environment runtime="local">'
CLOSE_TAG = "</execution_environment>"


class Astr30ExecutionEnvTagTests(unittest.TestCase):
    """Tag-based host block removal per docs/429-prompt-investigation.md P0."""

    def _host_block(self, body, open_tag=OPEN_LOCAL):
        return f"{open_tag}\n{body}\n{CLOSE_TAG}".format(open_tag=open_tag, body=body, CLOSE_TAG=CLOSE_TAG)

    def test_full_local_block_with_host_sections_is_removed_entirely(self):
        block = self._host_block("## Shell and sessions\n" + LINUX + "\n## Workspace\n工作区说明")
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                sep = newline
                system = "人格开头" + sep + sep + block.replace("\n", sep) + sep + sep + "结束人格"
                expected = "人格开头" + sep + sep + sep + "结束人格"
                self.assertEqual(filter_host_capabilities(system), expected)
        self.assertEqual(filter_host_capabilities(block), "")

    def test_execution_policy_marker_alone_qualifies_host_block(self):
        block = self._host_block("## Execution policy\n禁止联网")
        system = "前\n" + block + "\n后"
        self.assertEqual(filter_host_capabilities(system), "前\n后")

    def test_windows_430_appcontainer_full_texts_are_removed(self):
        for block in (WINDOWS_430_PWSH, WINDOWS_430_PS51):
            with self.subTest(block=block[:60]):
                system = "人格\n" + block + "\n结尾"
                self.assertEqual(filter_host_capabilities(system), "人格\n\n结尾")
                self.assertEqual(filter_host_capabilities(block), "")

    def test_windows_430_block_inside_tag_is_removed_with_tag(self):
        block = self._host_block("## Shell and sessions\n" + WINDOWS_430_PWSH + "\n## Workspace\n说明")
        self.assertEqual(filter_host_capabilities("p\n" + block + "\nq"), "p\nq")

    def test_partial_tag_without_close_is_kept(self):
        system = "前\n" + OPEN_LOCAL + "\n## Shell and sessions\n内容未闭合"
        with patch("agy_provider.system_prompt.logger.debug") as debug:
            self.assertEqual(filter_host_capabilities(system), system)
            self.assertIn("tag_partial", str(debug.call_args))

    def test_nested_tag_is_kept(self):
        system = (
            "前\n" + OPEN_LOCAL + "\n外层\n"
            '<execution_environment runtime="other">\n' + CLOSE_TAG + "\n尾行"
        )
        with patch("agy_provider.system_prompt.logger.debug") as debug:
            self.assertEqual(filter_host_capabilities(system), system)
            self.assertIn("tag_nested", str(debug.call_args))

    def test_unknown_runtime_tag_is_kept(self):
        system = (
            '前\n<execution_environment runtime="remote">\n远程说明\n'
            + CLOSE_TAG + "\n后"
        )
        with patch("agy_provider.system_prompt.logger.debug") as debug:
            self.assertEqual(filter_host_capabilities(system), system)
            joined = str(debug.call_args)
            self.assertIn("tag_variant", joined)
            self.assertIn("tag_orphan_close", joined)

    def test_persona_same_tag_without_host_sections_is_kept(self):
        block = self._host_block("你是猫娘，会使用 shell 拟声词")
        system = "人格\n" + block + "\n约束"
        with patch("agy_provider.system_prompt.logger.debug") as debug:
            self.assertEqual(filter_host_capabilities(system), system)
            self.assertIn("tag_unverified", str(debug.call_args))
            self.assertNotIn(system, str(debug.call_args))

    def test_orphan_close_tag_is_kept(self):
        system = "人格\n" + CLOSE_TAG + "\n说明"
        self.assertEqual(filter_host_capabilities(system), system)

    def test_mixed_blocks_only_genuine_host_block_removed(self):
        genuine = self._host_block("## Workspace\n说明")
        persona = self._host_block("人格内容块")
        system = "前\n" + genuine + "\n中\n" + persona + "\n后"
        self.assertEqual(
            filter_host_capabilities(system),
            "前\n中\n" + persona + "\n后",
        )

    def test_exact_4294_line_inside_unverified_tag_still_removed(self):
        block = self._host_block(LINUX)
        system = "人格\n" + block + "\n尾"
        # Known exact sentence fails closed even inside an unverified tag.
        self.assertNotIn("astrbot_shell_session", filter_host_capabilities(system))
        self.assertIn("人格", filter_host_capabilities(system))

    def test_rewritten_body_inside_tag_without_exact_sentence_is_removed(self):
        # docs/429-prompt-investigation.md §6.1: rewritten tool text (contains
        # astrbot_shell_session but different wording) must still be removed.
        body = (
            "## Shell and sessions\n"
            "宿主提供 astrbot_shell_session 托管会话，改写过措辞的工具段。"
        )
        block = self._host_block(body)
        cleaned = filter_host_capabilities("人格\n" + block + "\n结尾")
        self.assertNotIn("astrbot_shell_session", cleaned)
        self.assertNotIn("<execution_environment", cleaned)
        self.assertIn("人格", cleaned)

    def test_stray_unclosed_persona_tag_does_not_swallow_real_host_block(self):
        stray = OPEN_LOCAL + "\n人格未闭合块内容"
        real = self._host_block("## Workspace\n说明")
        system = "开头\n" + stray + "\n" + real + "\n结尾"
        cleaned = filter_host_capabilities(system)
        self.assertIn("人格未闭合块内容", cleaned)
        self.assertNotIn("## Workspace", cleaned)
        # The stray persona open tag itself stays (fail open), but the genuine
        # host block behind it must still be removed.
        self.assertEqual(cleaned.count("<execution_environment"), 1)
        self.assertNotIn(CLOSE_TAG, cleaned)
        self.assertIn("结尾", cleaned)

    def test_indented_or_inline_tag_lines_are_not_matched(self):
        for inline in (
            "    " + OPEN_LOCAL,
            "示例 " + OPEN_LOCAL + " 引用",
            "说明 </execution_environment> 用法",
            "人格提到 <execution_environment runtime=\"local\"> 字样",
        ):
            with self.subTest(inline=inline[:40]):
                system = "前\n" + inline + "\n后"
                self.assertEqual(filter_host_capabilities(system), system)

    def test_workspace_notes_heading_is_not_host_section_marker(self):
        block = self._host_block("## Workspace Notes\n人格自己的笔记")
        system = "人格\n" + block + "\n约束"
        self.assertEqual(filter_host_capabilities(system), system)

    def test_windows_fixture_byte_fidelity(self):
        # Fixtures were extracted mechanically from upstream source; guard
        # against accidental retyping regressions via length + spot checks.
        self.assertEqual(len(WINDOWS_430_PWSH), 1742)
        self.assertEqual(len(WINDOWS_430_PS51), 1789)
        self.assertIn(" Restricted Windows execution uses AppContainer. ", WINDOWS_430_PWSH)
        self.assertTrue(WINDOWS_430_PWSH.endswith("retry without isolation."))
        self.assertIn("Workspace:\\", WINDOWS_430_PWSH)
        self.assertTrue(WINDOWS_430_PWSH.startswith(START))


if __name__ == "__main__":
    unittest.main()
