import os
import sys
import tempfile
import unittest
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
            side_effect=["agy 1.5.2", "--gemini-dir --app-data-dir --agent"]
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
            AsyncMock(return_value=object()),
        ) as create:
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
