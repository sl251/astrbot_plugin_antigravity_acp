import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.models import catalog_names, select
from agy_provider.probe import probe
from agy_provider.process import LaunchInfo, discover
from agy_provider.transport import AcpClient

FIXTURE = Path(__file__).with_name("fake_cli.py")


class AcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy test with spaces ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    async def run_probe(self, scenario="probe", **kwargs):
        settings = Settings(
            auth_bridge=False, startup_timeout=0.2 if scenario == "hang" else 5
        )
        with (
            patch(
                "agy_provider.probe.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False)),
            ),
            patch(
                "agy_provider.probe.launch_args",
                return_value=[sys.executable, str(FIXTURE), scenario],
            ),
        ):
            result = await probe(settings, root=self.root, details=True, **kwargs)
        self.assertEqual(list(self.root.iterdir()), [])
        return result

    async def test_probe_real_child_handshake_no_generation(self):
        result = await self.run_probe()
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["model_prompt_sent"])
        self.assertEqual(result["model_count"], 2)
        self.assertEqual(result["efforts"], ["low", "high"])
        self.assertNotIn("must-not-publish", str(result))

    async def test_model_switch_uses_new_efforts(self):
        result = await self.run_probe(model="opaque-id", effort="medium")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["efforts"], ["medium"])
        self.assertEqual(result["selected_model"], "opaque-id")

    async def test_unsupported_effort_never_passes(self):
        result = await self.run_probe(model="opaque-id", effort="high")
        self.assertEqual(
            (result["status"], result["error_code"]), ("blocked", "UNSUPPORTED")
        )

    async def test_removed_model_never_passes(self):
        result = await self.run_probe(model="removed-model")
        self.assertEqual(result["error_code"], "MODEL")

    async def test_auth_failure_redacted(self):
        result = await self.run_probe("auth")
        self.assertEqual(result["error_code"], "AUTH")
        self.assertNotIn("FAKE-PRIVATE", str(result))

    async def test_startup_exit_reports_phase_and_exit_code_without_raw_output(self):
        result = await self.run_probe("startup-exit")
        self.assertEqual(
            (result["error_code"], result["stage"], result["exit_code"]),
            ("STARTUP_EXIT", "initialize", 23),
        )
        self.assertNotIn("FAKE-PRIVATE", str(result))
        self.assertNotIn("/private/test", str(result))

    async def test_zero_exit_before_handshake_is_still_startup_failure(self):
        result = await self.run_probe("startup-zero")
        self.assertEqual(
            (result["error_code"], result["exit_code"]), ("STARTUP_EXIT", 0)
        )

    async def test_legacy_wrapper_refusal_is_classified_across_stderr_chunks(self):
        result = await self.run_probe("wrapper-exit")
        self.assertEqual(
            (result["error_code"], result["exit_code"]), ("CLI_WRAPPER", 23)
        )
        self.assertNotIn("FAKE-PRIVATE", str(result))

    async def test_known_network_evidence_takes_precedence_over_startup_exit(self):
        result = await self.run_probe("network-exit")
        self.assertEqual((result["error_code"], result["exit_code"]), ("NETWORK", 23))

    async def test_success_is_not_vetoed_by_stderr_warning(self):
        result = await self.run_probe("wrapper-warning")
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("error_code", result)

    async def test_explicit_auth_error_is_not_overridden_by_wrapper_stderr(self):
        result = await self.run_probe("auth-with-wrapper-warning")
        self.assertEqual(result["error_code"], "AUTH")

    async def test_exit_after_handshake_is_transport_failure(self):
        result = await self.run_probe("post-init-exit")
        self.assertEqual(
            (result["error_code"], result["stage"], result["exit_code"]),
            ("TRANSPORT", "session/new", 23),
        )

    async def test_explicit_missing_command_does_not_fall_back_or_echo_path(self):
        missing = str(self.root / "private-location" / "missing-agy")
        with patch("agy_provider.process.shutil.which") as which:
            with self.assertRaises(AgyError) as caught:
                await discover(Settings(command=missing))
        self.assertEqual(caught.exception.code, "COMMAND_NOT_FOUND")
        self.assertNotIn(missing, str(caught.exception))
        which.assert_not_called()

    async def test_empty_model_catalog(self):
        result = await self.run_probe("no-models")
        self.assertEqual(result["error_code"], "NO_MODELS")

    async def test_timeout_reaps_actual_child(self):
        result = await self.run_probe("hang")
        self.assertEqual(result["error_code"], "TIMEOUT")

    async def test_text_and_thought_over_real_stdio(self):
        client = AcpClient(
            Settings(auth_bridge=False, startup_timeout=5, request_timeout=5)
        )
        self.addAsyncCleanup(client.close)
        await client.start([sys.executable, str(FIXTURE), "generate"], self.root)
        await client.new_session(self.root)
        events = [event async for event in client.generate("offline fixture only")]
        self.assertEqual([event.kind for event in events], ["thought", "text", "done"])
        self.assertEqual(events[1].text, "fake answer")
        proc = client.proc
        await client.close()
        self.assertIsNotNone(proc.returncode)

    async def test_missing_cli(self):
        with (
            patch("agy_provider.process.shutil.which", return_value=None),
            patch.object(Path, "is_file", return_value=False),
        ):
            with self.assertRaises(AgyError) as caught:
                await discover(Settings())
        self.assertEqual(caught.exception.code, "NOT_INSTALLED")

    async def test_cmd_and_bat_rejected(self):
        for suffix in (".cmd", ".bat"):
            path = self.root / ("agy" + suffix)
            path.touch()
            with self.assertRaises(AgyError) as caught:
                await discover(Settings(command=str(path)))
            self.assertEqual(caught.exception.code, "CONFIG")

    @unittest.skipUnless(os.name == "nt", "Windows default installation path")
    async def test_windows_default_native_executable_with_spaces(self):
        binary = self.root / "agy" / "bin" / "agy.exe"
        binary.parent.mkdir(parents=True)
        binary.touch()
        capture = AsyncMock(
            side_effect=["agy 1.5.19", "--gemini-dir --app-data-dir"]
        )
        with (
            patch.dict(os.environ, {"LOCALAPPDATA": str(self.root)}),
            patch("agy_provider.process.shutil.which", return_value=None),
            patch("agy_provider.process.capture", capture),
        ):
            info = await discover(Settings())
        self.assertEqual(info.binary, str(binary))
        self.assertEqual(capture.call_args_list[0].args[0], [str(binary), "--version"])

    async def test_spawn_uses_hidden_window_on_windows(self):
        from agy_provider.process import spawn

        with patch(
            "agy_provider.process.asyncio.create_subprocess_exec",
            AsyncMock(return_value=SimpleNamespace(pid=999999)),
        ) as create, patch("agy_provider.process.WindowsJob.attach", return_value=None):
            await spawn(["fake-agy", "acp"], self.root)
        kwargs = create.call_args.kwargs
        self.assertEqual(
            kwargs.get("creationflags"), 0x08000000 if os.name == "nt" else None
        )
        self.assertEqual(kwargs["start_new_session"], os.name == "posix")

    async def test_legacy_switch_does_not_reuse_old_efforts(self):
        result = {
            "models": {
                "currentModelId": "gemini-a",
                "availableModels": [{"modelId": "gemini-a"}, {"modelId": "gemini-b"}],
            },
            "configOptions": [{"id": "thought_level", "options": [{"value": "high"}]}],
        }
        client = AcpClient(Settings())
        client.request = AsyncMock(return_value={})
        with self.assertRaises(AgyError) as caught:
            await select(client, result, "gemini-b", "high")
        self.assertEqual(caught.exception.code, "UNSUPPORTED")

    def test_grouped_catalog_filters_non_gemini(self):
        result = {
            "configOptions": [
                {
                    "id": "model",
                    "options": [
                        {
                            "name": "group",
                            "options": [
                                {"value": "opaque", "name": "Gemini"},
                                {"value": "other", "name": "Other"},
                            ],
                        }
                    ],
                }
            ]
        }
        self.assertEqual(catalog_names(result), {"opaque": "Gemini"})

    async def test_discover_caches_by_file_stat_and_populates_login_support(self):
        binary = self.root / ("agy.exe" if os.name == "nt" else "agy")
        binary.write_bytes(b"binary-v1")
        if os.name == "posix":
            binary.chmod(0o755)
        mock_capture = AsyncMock(
            side_effect=[
                "agy 1.5.9",
                "--gemini-dir --app-data-dir --agent --login",
                "agy 1.6.0",
                "--gemini-dir --app-data-dir --agent",
            ]
        )
        settings = Settings(command=str(binary))
        with patch("agy_provider.process.capture", mock_capture):
            first = await discover(settings)
            second = await discover(settings)
            self.assertEqual(first, second)
            self.assertTrue(first.supports_login)
            # 第二次相同 stat 命中进程内缓存，不额外启动子进程
            self.assertEqual(mock_capture.await_count, 2)
            # 文件体积变化后缓存自动失效重测
            binary.write_bytes(b"binary-v2-updated")
            third = await discover(settings)
            self.assertEqual(third.version, "agy 1.6.0")
            self.assertFalse(third.supports_login)
            self.assertEqual(mock_capture.await_count, 4)

    async def test_discover_accepts_help_without_agent_flag(self):
        binary = self.root / ("agy.exe" if os.name == "nt" else "agy")
        binary.write_bytes(b"binary-no-agent")
        if os.name == "posix":
            binary.chmod(0o755)
        mock_capture = AsyncMock(
            side_effect=[
                "agy 1.5.19",
                "--gemini-dir --app-data-dir --include-default-customizations --disable-telemetry --login",
            ]
        )
        with patch("agy_provider.process.capture", mock_capture):
            info = await discover(Settings(command=str(binary)))
        self.assertEqual(info.version, "agy 1.5.19")
        self.assertTrue(info.include_default_customizations)
        self.assertTrue(info.disable_telemetry)
        self.assertTrue(info.supports_login)
        self.assertEqual(mock_capture.await_count, 2)

    async def test_probe_minimal_passes_flag_and_blocks_unsupported_cli(self):
        from agy_provider.process import launch_args as real_launch_args

        seen_args = []

        def fake_launch(info, home, *, customizations_mode="inherit"):
            args = real_launch_args(info, home, customizations_mode=customizations_mode)
            seen_args.append(args)
            return [sys.executable, str(FIXTURE), "probe", *args[1:]]

        minimal_settings = Settings(
            auth_bridge=False, startup_timeout=5, agy_customizations_mode="minimal"
        )
        with (
            patch(
                "agy_provider.probe.discover",
                AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.19", False, True)),
            ),
            patch("agy_provider.probe.launch_args", side_effect=fake_launch),
        ):
            supported = await probe(minimal_settings, root=self.root, details=True)
        self.assertEqual(supported["status"], "passed")
        self.assertEqual(supported["minimal_tools_status"], "flag_available_unverified")
        self.assertEqual(len(seen_args), 1)
        self.assertIn("--include-default-customizations=false", seen_args[0])
        self.assertNotIn("--agent", seen_args[0])

        with patch(
            "agy_provider.probe.discover",
            AsyncMock(return_value=LaunchInfo("fake", "agy 1.5.2", False, False)),
        ):
            unsupported = await probe(minimal_settings, root=self.root, details=True)
        self.assertEqual(unsupported["status"], "blocked")
        self.assertEqual(unsupported["stage"], "initialize")
        self.assertEqual(unsupported["error_code"], "UNSUPPORTED")
        self.assertEqual(unsupported["minimal_tools_status"], "unsupported")
        self.assertEqual(list(self.root.iterdir()), [])
