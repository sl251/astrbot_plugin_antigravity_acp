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
