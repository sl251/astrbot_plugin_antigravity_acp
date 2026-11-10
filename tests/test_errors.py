"""Core upstream authentication classification and redaction regressions."""

import unittest

from agy_provider.errors import remote_error


class ErrorClassificationTests(unittest.TestCase):
    def test_407_proxy_auth_takes_priority_over_google_auth(self):
        error = remote_error(
            "407 proxy authentication required credential private-secret"
        )
        self.assertEqual(error.code, "PROXY_AUTH")
        self.assertNotIn("private-secret", str(error))

    def test_401_is_google_auth_and_redacted(self):
        error = remote_error("401 authentication required account@example.com")
        self.assertEqual(error.code, "AUTH")
        self.assertNotIn("account@example.com", str(error))

    def test_unknown_server_error_is_not_auth(self):
        error = remote_error({"code": -32000, "message": "unknown private-secret"})
        self.assertEqual(error.code, "UPSTREAM")
        self.assertNotIn("private-secret", str(error))
