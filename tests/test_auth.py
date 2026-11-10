"""Core file-only authentication bridge regressions with fake credentials."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agy_provider.auth import bridge_auth
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.process import make_home


class AuthBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.official_home = self.root / "official"
        parent = self.official_home / ".gemini" / "antigravity-cli"
        parent.mkdir(parents=True, mode=0o700)
        self.token = parent / "antigravity-oauth-token"
        self.token.write_bytes(b"fake-credential-only")
        self.token.chmod(0o600)
        self.isolated = self.root / "isolated"
        (self.isolated / "agy").mkdir(parents=True)
        self.home_patch = patch(
            "agy_provider.auth.Path.home", return_value=self.official_home
        )
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)

    def test_creates_only_token_symlink_without_reading_contents(self):
        with patch.object(Path, "open", side_effect=AssertionError("must not read")):
            bridge_auth(self.isolated)
        link = self.isolated / "agy" / self.token.name
        self.assertTrue(link.is_symlink())
        self.assertEqual(Path(os.readlink(link)), self.token)
        self.assertFalse((self.isolated / ".gemini").exists())

    def test_missing_token_reports_auth_without_private_details(self):
        self.token.unlink()
        with self.assertRaises(AgyError) as caught:
            make_home(self.root / "sessions", "", settings=Settings())
        self.assertEqual(caught.exception.code, "AUTH")
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertEqual(list((self.root / "sessions").iterdir()), [])

    def test_session_cleanup_keeps_official_token(self):
        home = make_home(self.root / "sessions", "", settings=Settings())
        self.assertTrue((home / "agy" / self.token.name).is_symlink())
        original = self.token.stat()
        shutil.rmtree(home)
        self.assertTrue(self.token.is_file())
        self.assertEqual(self.token.stat().st_ino, original.st_ino)
        self.assertEqual(self.token.stat().st_size, original.st_size)
