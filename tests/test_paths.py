import ntpath
import os
import shutil
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import astrbot_stub  # noqa: F401 -- install the offline host logger

from agy_provider.config import Settings
from agy_provider.login import LoginFlow
from agy_provider.paths import cleanup_stale_dirs, windows_long_path
from agy_provider.probe import probe
from agy_provider.process import LaunchInfo, launch_args, make_home
from agy_provider.sessions import Runtime, Session


class PathTests(unittest.TestCase):
    def test_non_windows_paths_are_unchanged(self):
        path = Path("relative") / ".." / "data"
        with patch("agy_provider.paths.os", SimpleNamespace(name="posix")):
            self.assertIs(windows_long_path(path), path)

    def test_windows_drive_unc_and_namespace_paths(self):
        # Exercise Windows spelling on every CI platform without changing the
        # real os.name (which would also change pathlib and tempfile behavior).
        windows_os = SimpleNamespace(name="nt", fspath=os.fspath, path=ntpath)
        cases = (
            (r"C:\data", r"\\?\C:\data"),
            ("C:/宿主 dir/old/../data", "\\\\?\\C:\\宿主 dir\\data"),
            (r"\\server\share\old\..\data", r"\\?\UNC\server\share\data"),
            (r"\\?\C:\data", r"\\?\C:\data"),
            (r"\\?\UNC\server\share\data", r"\\?\UNC\server\share\data"),
            (r"\\.\C:\data", r"\\.\C:\data"),
        )
        with (
            patch("agy_provider.paths.os", windows_os),
            patch("agy_provider.paths.Path", PureWindowsPath),
        ):
            for source, expected in cases:
                with self.subTest(source=source):
                    result = windows_long_path(PureWindowsPath(source))
                    self.assertEqual(str(result), expected)
                    self.assertIs(windows_long_path(result), result)

    def test_cleanup_stale_dirs_removes_expired_isolated_dirs_and_preserves_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ns_dead = root / ("a" * 16)
            ns_live = root / ("b" * 16)
            old_rt = ns_dead / "runtime-old"
            active_rt = ns_live / "runtime-active"
            fresh_rt = ns_live / "runtime-fresh"
            old_pre = root / "preflight-old"
            old_login = root / "login-old"
            unrelated = root / "custom-keep"
            for d in (old_rt, active_rt, fresh_rt, old_pre, old_login, unrelated):
                d.mkdir(parents=True)
                (d / "marker.txt").write_text("x", encoding="utf-8")
            # 将陈旧目录和活跃目录时间戳拨回 2 小时前，验证年龄阈值与 active_names 保护
            past = 100000.0
            for d in (old_rt, active_rt, old_pre, old_login, unrelated):
                os.utime(d, (past, past))
            os.utime(fresh_rt, (past + 7000.0, past + 7000.0))

            removed = cleanup_stale_dirs(
                root,
                active_names={"runtime-active"},
                min_age_seconds=3600.0,
                now=past + 7200.0,
            )
            self.assertEqual(removed, 3)
            self.assertFalse(old_rt.exists())
            self.assertFalse(ns_dead.exists())
            self.assertFalse(old_pre.exists())
            self.assertFalse(old_login.exists())
            self.assertTrue(active_rt.exists())
            self.assertTrue(fresh_rt.exists())
            self.assertTrue(unrelated.exists())


@unittest.skipUnless(os.name == "nt", "Requires Windows filesystem semantics")
class WindowsPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.mkdtemp(prefix="agy paths 中文 ")
        if directory.startswith("\\\\?\\UNC\\"):
            directory = "\\\\" + directory[8:]
        elif directory.startswith("\\\\?\\"):
            directory = directory[4:]
        self.base = Path(directory)
        self.addCleanup(shutil.rmtree, windows_long_path(self.base))

    def root_for_agent_length(self, length):
        # mkdtemp's session- prefix plus eight random characters, followed by
        # workspace/AGENTS.md. Keep each component well below its own limit.
        tail = Path("session-xxxxxxxx") / "workspace" / "AGENTS.md"
        target = length - len(str(tail)) - 1
        root = self.base
        if target - len(str(root)) < 2:
            self.skipTest("TEMP is too deep for this exact boundary case")
        while target - len(str(root)) > 48:
            root /= "d" * 32
        return root / ("d" * (target - len(str(root)) - 1))

    def test_relative_paths_are_normalized_before_prefixing(self):
        for source in ("relative/../data", "\\data", "C:relative"):
            with self.subTest(source=source):
                path = Path(source)
                absolute = Path(os.path.abspath(path))
                self.assertEqual(
                    windows_long_path(path), windows_long_path(absolute)
                )
                self.assertNotIn("..", windows_long_path(path).parts)

    def test_agent_files_across_max_path_boundary(self):
        for length in (259, 260, 400, 800):
            with self.subTest(length=length):
                root = self.root_for_agent_length(length)
                home = make_home(root, "test persona")
                agents_md = home / "workspace" / "AGENTS.md"
                self.assertTrue(str(home).startswith("\\\\?\\"))
                prefix_length = len(str(windows_long_path(root))) - len(str(root))
                self.assertEqual(len(str(agents_md)) - prefix_length, length)
                content = agents_md.read_text(encoding="utf-8")
                self.assertIn("test persona", content)
                self.assertIn("No tools are available in this session.", content)
                self.assertFalse((home / "config").exists())
                self.assertTrue((home / "agy" / "settings.json").is_file())
                args = launch_args(LaunchInfo("agy.exe", "1.5.9", False), home)
                self.assertEqual(args[args.index("--gemini-dir") + 1], str(home))
                self.assertNotIn("--agent", args)
                shutil.rmtree(home)
                self.assertFalse(home.exists())

    def test_tools_persona_and_auth_bridge_keep_extended_home(self):
        root = self.root_for_agent_length(600)
        with patch("agy_provider.process.bridge_auth") as bridge:
            home = make_home(root, "tools persona", True, settings=Settings())
        bridge.assert_called_once_with(home)
        self.assertIn(
            "tools persona",
            (home / "workspace" / "AGENTS.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((home / "config").exists())

    def test_write_failure_removes_deep_session(self):
        root = self.root_for_agent_length(600)
        with patch.object(Path, "write_text", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                make_home(root, "test persona")
        self.assertEqual(list(windows_long_path(root).iterdir()), [])

    async def test_runtime_removes_deep_session_and_runtime_root(self):
        root = self.root_for_agent_length(600)
        runtime = Runtime(Settings(auth_bridge=False), root)
        self.addAsyncCleanup(runtime.close)
        home = make_home(runtime.root, "test persona")
        runtime.sessions["test"] = Session(home=home)
        await runtime.close()
        self.assertFalse(home.exists())
        self.assertFalse(runtime.root.exists())

    async def test_probe_custom_root_and_cleanup(self):
        root = self.root_for_agent_length(600)
        client = SimpleNamespace(
            proc=None,
            verified_read=False,
            start=AsyncMock(),
            # probe() treats an empty catalog as NO_MODELS and stays blocked, so
            # the stub must advertise at least one Gemini entry.
            new_session=AsyncMock(
                return_value={
                    "models": {
                        "currentModelId": "gemini-4-hydrogen-high",
                        "availableModels": [
                            {
                                "modelId": "gemini-4-hydrogen-high",
                                "name": "Gemini 4 Hydrogen (High)",
                            }
                        ],
                    }
                }
            ),
            close=AsyncMock(),
            capabilities={},
        )
        with (
            patch(
                "agy_provider.probe.discover",
                AsyncMock(return_value=LaunchInfo("agy.exe", "1.5.9", False)),
            ),
            patch("agy_provider.probe.AcpClient", return_value=client),
        ):
            report = await probe(Settings(auth_bridge=False), root=root)
        self.assertEqual(report["status"], "passed")
        argv, cwd = client.start.call_args.args
        self.assertTrue(argv[argv.index("--gemini-dir") + 1].startswith("\\\\?\\"))
        self.assertTrue(str(cwd).startswith("\\\\?\\"))
        client.new_session.assert_awaited_once_with(cwd)
        self.assertEqual(list(windows_long_path(root).iterdir()), [])

    def test_login_temp_directory_keeps_extended_root(self):
        root = self.root_for_agent_length(600)
        flow = LoginFlow(Settings(), root, AsyncMock())
        flow.root.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix="login-", dir=flow.root) as directory:
            self.assertTrue(directory.startswith("\\\\?\\"))
            self.assertTrue(Path(directory).is_dir())
        self.assertEqual(list(flow.root.iterdir()), [])
