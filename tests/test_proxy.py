"""Core proxy configuration and child environment regression checks."""

import json
import os
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.process import PROXY_KEYS, build_child_env


class ProxyTests(unittest.TestCase):
    def test_defaults_match_schema(self):
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text()
        )
        defaults = {key: item["default"] for key, item in schema.items()}
        self.assertEqual(defaults, json.loads(json.dumps(asdict(Settings()))))
        self.assertEqual(Settings.from_dict(defaults), Settings())

    def test_custom_rejects_bad_input_without_echo(self):
        for url in ("", "https://secret@proxy:80", "http://proxy", "http://proxy:80\n"):
            with self.subTest(url=url), self.assertRaises(AgyError) as caught:
                Settings.from_dict({"proxy_mode": "custom", "proxy_url": url})
            self.assertEqual(caught.exception.code, "CONFIG")
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("http://proxy", str(caught.exception))
        self.assertEqual(
            Settings.from_dict(
                {"proxy_mode": "direct", "proxy_url": "old value"}
            ).proxy_mode,
            "direct",
        )

    def test_inherit_preserves_environment(self):
        original = {"HTTP_PROXY": "http://old:80", "NO_PROXY": "*", "HOME": "/fake"}
        with patch.dict(os.environ, original, clear=True):
            result = build_child_env(Settings())
            self.assertEqual(result, original)
            result["HOME"] = "changed"
            self.assertEqual(dict(os.environ), original)

    def test_custom_overrides_six_keys_and_loopback_bypass(self):
        url = "socks5h://localhost:1080"
        settings = Settings.from_dict({"proxy_mode": "custom", "proxy_url": url})
        original = {"HTTP_PROXY": "http://old:80", "NO_PROXY": "*", "HOME": "/fake"}
        with patch.dict(os.environ, original, clear=True):
            result = build_child_env(settings)
            for key in PROXY_KEYS:
                self.assertEqual(result[key], url)
            for key in ("NO_PROXY", "no_proxy"):
                self.assertEqual(result[key], "localhost,127.0.0.1,::1")
            self.assertEqual(result["HOME"], "/fake")
            self.assertEqual(dict(os.environ), original)

    def test_direct_removes_only_six_proxy_keys(self):
        original = dict.fromkeys(PROXY_KEYS, "http://old:80")
        original.update({"NO_PROXY": "*", "HOME": "/fake"})
        with patch.dict(os.environ, original, clear=True):
            result = build_child_env(Settings(proxy_mode="direct"))
            self.assertEqual(result, {"NO_PROXY": "*", "HOME": "/fake"})
            self.assertEqual(dict(os.environ), original)
