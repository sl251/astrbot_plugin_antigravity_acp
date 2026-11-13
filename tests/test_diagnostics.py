import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError, remote_error
from agy_provider.onboarding import environment, next_step
from agy_provider.process import capture, spawn
from agy_provider.transport import AcpClient


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def test_wrapper_messages_are_allowlisted_and_redacted(self):
        for text in (
            "Refusing auth bridge outside AGY plugin session directory",
            "Invalid isolated ACP directories",
            "Unexpected existing auth symlink",
            "Unexpected existing isolated auth file; refusing overwrite",
        ):
            error = remote_error(text + " /private/user TOKEN-FAKE")
            self.assertEqual(error.code, "CLI_WRAPPER")
            self.assertNotIn("TOKEN-FAKE", str(error))
            self.assertNotIn("/private/user", str(error))

    def test_unrelated_wrapper_mentions_do_not_imply_directory_refusal(self):
        self.assertEqual(remote_error("wrapper exited unexpectedly").code, "UPSTREAM")

    def test_unexpected_eof_variants_map_to_network_with_word_boundaries(self):
        for text in (
            "UNAVAILABLE: unexpected EOF",
            "stream closed: unexpected  eof",
            "transport failed: unexpected_eof",
            "read error: unexpected-eof",
            'Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF',
            '": EOF',
            "⚠ The stream was interrupted. Please continue the task you were working on.",
        ):
            with self.subTest(text=text):
                self.assertEqual(remote_error(text).code, "NETWORK")
        for non_match in ("unexpected eofmarker", "nonunexpected eof", "eof"):
            with self.subTest(non_match=non_match):
                self.assertEqual(remote_error(non_match).code, "UPSTREAM")

    def test_retry_notice_relaxes_bare_eof_case_insensitively(self):
        for text in ("eof", "EOF", "read failed: Eof", "⚠ API error (attempt 1): eof"):
            with self.subTest(text=text):
                self.assertEqual(remote_error(text, retry_notice=True).code, "NETWORK")
        self.assertEqual(remote_error("eofmarker", retry_notice=True).code, "UPSTREAM")

    def test_discovery_scope_respects_explicit_command_without_echoing_it(self):
        result = environment(Settings(command="/private/user/native-cli"))
        self.assertIn("显式 command", result["discovery_hint"])
        self.assertNotIn("/private/user", str(result))
        default = environment(Settings())["discovery_hint"]
        self.assertIn("PATH", default)
        self.assertIn(".local/bin", default)
        if os.name == "nt":
            self.assertIn("LOCALAPPDATA", default)

    def test_new_error_hints_are_actionable(self):
        for code, word in (
            ("NOT_INSTALLED", "Docker"),
            ("COMMAND_NOT_FOUND", "command"),
            ("STARTUP_EXIT", "握手"),
            ("CLI_WRAPPER", "原生可执行文件"),
        ):
            self.assertIn(word, next_step({"status": "blocked", "error_code": code}))

    async def test_closed_stdout_without_process_exit_is_not_startup_exit(self):
        client = AcpClient(Settings())
        waiting = asyncio.Event()
        client.proc = SimpleNamespace(wait=waiting.wait, returncode=None)
        error = await client._transport_error(AgyError("TRANSPORT", "closed pipe"))
        self.assertEqual(error.code, "TRANSPORT")
        self.assertIsNone(error.exit_code)

    async def test_stdin_failure_collects_wrapper_evidence_and_reaps_child(self):
        client = AcpClient(Settings())
        client.proc = SimpleNamespace(wait=AsyncMock(return_value=23), returncode=23)
        client._write = AsyncMock(side_effect=AgyError("TRANSPORT", "closed pipe"))

        async def evidence():
            await asyncio.sleep(0)
            client.stderr_diagnostics.append(("CLI_WRAPPER", "initialize"))

        client.stderr_reader = asyncio.create_task(evidence())
        with self.assertRaises(AgyError) as caught:
            await client.request("initialize", {}, 5)
        self.assertEqual(
            (caught.exception.code, caught.exception.exit_code), ("CLI_WRAPPER", 23)
        )
        self.assertFalse(client.pending)
        self.assertTrue(client.stderr_reader.done())

    async def test_missing_interpreter_is_launch_failure_for_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "existing-cli"
            binary.touch()
            with patch(
                "agy_provider.process.asyncio.create_subprocess_exec",
                AsyncMock(side_effect=FileNotFoundError),
            ):
                with self.assertRaises(AgyError) as caught:
                    await spawn([str(binary)])
        self.assertEqual(caught.exception.code, "LAUNCH")

    async def test_version_help_failure_keeps_only_exit_code_and_classification(self):
        proc = SimpleNamespace(
            returncode=17,
            communicate=AsyncMock(return_value=(b"", b"private-path FAKE-PRIVATE")),
        )
        with (
            patch("agy_provider.process.spawn", AsyncMock(return_value=proc)),
            patch("agy_provider.process.stop_process", AsyncMock()) as stop,
        ):
            with self.assertRaises(AgyError) as caught:
                await capture(["fake-agy", "--version"], 5)
        self.assertEqual(
            (caught.exception.code, caught.exception.exit_code), ("UPSTREAM", 17)
        )
        self.assertNotIn("FAKE-PRIVATE", str(caught.exception))
        stop.assert_awaited_once_with(proc)

    def test_explicit_rpc_error_and_other_stages_keep_precedence(self):
        client = AcpClient(Settings())
        client.stage = "session/new"
        client.stderr_diagnostics.append(("CLI_WRAPPER", "session/new"))
        self.assertEqual(
            client._diagnose(AgyError("AUTH", "authentication required")).code, "AUTH"
        )
        client.stage = "initialize"
        self.assertEqual(
            client._diagnose(AgyError("TRANSPORT", "closed pipe")).code, "TRANSPORT"
        )
