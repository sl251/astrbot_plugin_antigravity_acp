import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from agy_provider.auth import bridge_auth
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.process import PROXY_KEYS, build_child_env, make_home


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows CI may expose TEMP through an 8.3 alias. The fixture's mock
        # HOME must be canonical, just like the production safety check expects.
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "official"
        self.home.mkdir(mode=0o700)
        self.isolated = self.root / "isolated"
        (self.isolated / "agy").mkdir(parents=True)
        home_patch = patch("agy_provider.auth.Path.home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def token(self):
        parent = self.home / ".gemini" / "antigravity-cli"
        parent.mkdir(parents=True, mode=0o700)
        (self.home / ".gemini").chmod(0o700)
        token = parent / "antigravity-oauth-token"
        token.write_text("FAKE-TEST-DATA", encoding="utf-8")
        token.chmod(0o600)
        return token

    def test_missing_file_defers_to_native_auth(self):
        bridge_auth(self.isolated)
        self.assertEqual(list((self.isolated / "agy").iterdir()), [])

    def test_noncanonical_home_is_rejected(self):
        with patch.object(Path, "resolve", return_value=self.root / "other-home"):
            with self.assertRaises(AgyError) as caught:
                bridge_auth(self.isolated)
        self.assertEqual(caught.exception.code, "AUTH_BRIDGE")

    def test_empty_existing_file_rejected(self):
        self.token().write_bytes(b"")
        with self.assertRaises(AgyError) as caught:
            bridge_auth(self.isolated)
        self.assertEqual(caught.exception.code, "AUTH_BRIDGE")

    def test_existing_file_link_errors_fail_closed(self):
        self.token()
        lstat = Path.lstat

        def safe_metadata(path):
            # Reach link creation even on Windows, whose mode bits are synthetic.
            values = list(lstat(path))
            values[0] &= ~0o022
            return os.stat_result(values)

        for error in (PermissionError, OSError, FileNotFoundError):
            with (
                patch.object(Path, "symlink_to", side_effect=error),
                patch.object(Path, "lstat", safe_metadata),
            ):
                with self.assertRaises(AgyError) as caught:
                    bridge_auth(self.isolated)
                self.assertEqual(caught.exception.code, "AUTH_BRIDGE")

    def test_link_and_session_cleanup_preserve_official_file(self):
        token = self.token()
        try:
            bridge_auth(self.isolated)
        except AgyError:
            if os.name == "nt":
                self.skipTest(
                    "Windows file metadata/link permissions rejected; failure path tested separately"
                )
            raise
        link = self.isolated / "agy" / token.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), token)
        link.unlink()
        self.assertEqual(token.read_text(encoding="utf-8"), "FAKE-TEST-DATA")

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode checks")
    def test_unsafe_file_and_parent_modes(self):
        token = self.token()
        for path in (token, token.parent, token.parent.parent):
            original = path.stat().st_mode
            path.chmod(0o777)
            try:
                with self.assertRaises(AgyError):
                    bridge_auth(self.isolated)
            finally:
                path.chmod(original)

    def test_make_home_cleans_up_if_bridge_fails(self):
        with patch(
            "agy_provider.process.bridge_auth",
            side_effect=AgyError("AUTH_BRIDGE", "test"),
        ):
            with self.assertRaises(AgyError):
                make_home(self.root / "sessions", "test", settings=Settings())
        self.assertEqual(list((self.root / "sessions").iterdir()), [])


class ConfigTests(unittest.TestCase):
    def test_schema_defaults_match_runtime(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        defaults = asdict(Settings())
        defaults["media_roots"] = list(defaults["media_roots"])
        self.assertEqual(
            {key: value["default"] for key, value in schema.items()}, defaults
        )

    def test_setup_auto_check_requires_boolean(self):
        for value in ("true", 1, None):
            with self.assertRaises(AgyError):
                Settings.from_dict({"setup_auto_check": value})

    def test_proxy_environments_do_not_mutate_host(self):
        with patch.dict(
            os.environ, {"HTTP_PROXY": "http://old:1234", "NO_PROXY": "host.test"}
        ):
            original = dict(os.environ)
            custom = build_child_env(
                Settings(proxy_mode="custom", proxy_url="http://new:2345")
            )
            self.assertTrue(all(custom[key] == "http://new:2345" for key in PROXY_KEYS))
            self.assertEqual(custom["NO_PROXY"], "localhost,127.0.0.1,::1")
            direct = build_child_env(Settings(proxy_mode="direct"))
            self.assertTrue(all(key not in direct for key in PROXY_KEYS))
            self.assertEqual(build_child_env(Settings()), original)
            self.assertEqual(dict(os.environ), original)

    def test_invalid_proxy_errors_are_redacted(self):
        for url in (
            "http://user:fake-password@localhost",
            "http://localhost:1234/path",
            "https://localhost:1234",
            "http://localhost:1234?secret=fake",
        ):
            with self.assertRaises(AgyError) as caught:
                Settings.from_dict({"proxy_mode": "custom", "proxy_url": url})
            self.assertNotIn("fake-password", str(caught.exception))

    def test_v050_defaults_validation_and_backward_compatibility(self):
        defaults = Settings()
        self.assertEqual(defaults.history_bytes, 160000)
        self.assertFalse(defaults.enable_tools)
        self.assertEqual(defaults.session_reuse_mode, "strict")
        self.assertFalse(defaults.cost_log_enabled)
        self.assertEqual(defaults.empty_dedupe_ttl, 30)
        self.assertEqual(defaults.quota_cooldown, 60)

        custom = Settings.from_dict(
            {
                "enable_tools": True,
                "history_bytes": 160000,
                "session_reuse_mode": "structured",
                "cost_log_enabled": False,
                "empty_dedupe_ttl": 0,
                "quota_cooldown": 300,
            }
        )
        self.assertTrue(custom.enable_tools)
        self.assertEqual(custom.history_bytes, 160000)
        self.assertEqual(custom.session_reuse_mode, "structured")
        self.assertFalse(custom.cost_log_enabled)
        self.assertEqual(custom.empty_dedupe_ttl, 0)
        self.assertEqual(custom.quota_cooldown, 300)

        for bad in (
            {"session_reuse_mode": "auto"},
            {"cost_log_enabled": "yes"},
            {"empty_dedupe_ttl": -1},
            {"empty_dedupe_ttl": 301},
            {"empty_dedupe_ttl": True},
            {"quota_cooldown": -1},
            {"quota_cooldown": 301},
            {"quota_cooldown": False},
        ):
            with self.assertRaises(AgyError):
                Settings.from_dict(bad)

    def test_schema_sliders_match_runtime_limits(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        sliders = {
            key: item["slider"]
            for key, item in schema.items()
            if item.get("type") == "int"
        }
        self.assertEqual(len(sliders), 18)
        for key, slider in sliders.items():
            low, high = slider["min"], slider["max"]
            base = {"max_sessions": 16} if key == "max_concurrent" else {}
            if key == "max_sessions":
                base = {"max_concurrent": 1}
            self.assertEqual(
                getattr(Settings.from_dict({**base, key: low}), key), low
            )
            self.assertEqual(
                getattr(Settings.from_dict({**base, key: high}), key), high
            )
            for bad in (low - 1, high + 1):
                with self.assertRaises(AgyError):
                    Settings.from_dict({**base, key: bad})

    def test_from_dict_strict_rejects_invalid_and_out_of_range_values(self):
        invalid_cases = (
            {"tool_max_calls": "abc"},
            {"tool_max_calls": "16.5"},
            {"tool_max_calls": 16.5},
            {"tool_max_calls": 16.0},
            {"tool_max_calls": True},
            {"tool_max_calls": False},
            {"tool_max_calls": None},
            {"tool_max_calls": 129},
            {"idle_ttl": 6000},
            {"max_concurrent": 99},
            {"max_concurrent": 4, "max_sessions": 2},
        )
        for values in invalid_cases:
            with self.subTest(values=values):
                with self.assertRaises(AgyError) as caught:
                    Settings.from_dict(values)
                self.assertEqual(caught.exception.code, "CONFIG")

    def test_from_dict_repair_restores_defaults_and_reports_callbacks(self):
        defaults = Settings()
        for raw in ("abc", "16.5", 16.5, 16.0, True, False, None, float("inf")):
            repairs = []
            with self.subTest(raw=raw):
                settings = Settings.from_dict(
                    {"tool_max_calls": raw},
                    on_repair=lambda k, v: repairs.append((k, v)),
                )
                self.assertEqual(settings.tool_max_calls, defaults.tool_max_calls)
                self.assertEqual(repairs, [("tool_max_calls", defaults.tool_max_calls)])

        repairs = []
        settings = Settings.from_dict(
            {
                "tool_max_calls": 129,
                "idle_ttl": 6000,
                "max_concurrent": 99,
            },
            on_repair=lambda k, v: repairs.append((k, v)),
        )
        self.assertEqual(settings.tool_max_calls, defaults.tool_max_calls)
        self.assertEqual(settings.idle_ttl, defaults.idle_ttl)
        self.assertEqual(settings.max_concurrent, defaults.max_concurrent)
        self.assertEqual(
            dict(repairs),
            {
                "tool_max_calls": defaults.tool_max_calls,
                "idle_ttl": defaults.idle_ttl,
                "max_concurrent": defaults.max_concurrent,
            },
        )
        self.assertEqual(Settings.from_dict({"tool_max_calls": "16"}).tool_max_calls, 16)

    def test_from_dict_repair_clamps_max_concurrent_to_max_sessions(self):
        repairs = []
        settings = Settings.from_dict(
            {"max_concurrent": 8, "max_sessions": 2},
            on_repair=lambda k, v: repairs.append((k, v)),
        )
        self.assertEqual(settings.max_concurrent, 2)
        self.assertEqual(settings.max_sessions, 2)
        self.assertEqual(repairs, [("max_concurrent", 2)])

        repaired = {}
        settings = Settings.from_dict(
            {"max_concurrent": 99, "max_sessions": 1},
            on_repair=repaired.__setitem__,
        )
        self.assertEqual(settings.max_concurrent, 1)
        self.assertEqual(repaired, {"max_concurrent": 1})

    def test_from_dict_repair_keeps_security_fields_strict(self):
        for bad in (
            {"command": "relative/agy"},
            {"proxy_mode": "invalid"},
            {"proxy_mode": "custom", "proxy_url": "https://127.0.0.1:7890"},
            {"media_roots": ["relative/path"]},
            {"thought_level": "extreme"},
            {"enable_tools": "true"},
            {"setup_auto_check": "yes"},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(AgyError) as caught:
                    Settings.from_dict(bad, on_repair=lambda k, v: None)
                self.assertEqual(caught.exception.code, "CONFIG")