import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.login import LoginFlow
from agy_provider.process import LaunchInfo

URL = "https://accounts.google.com/o/oauth2/auth?state=fake-only&code_challenge=test"


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.verify = AsyncMock(return_value={"status": "passed"})
        self.flow = LoginFlow(Settings(), Path(self.temp.name), self.verify)
        self.addAsyncCleanup(self.flow.close)

    async def test_fragmented_stdout_and_ansi_delimiter(self):
        for delimiter in (b"\n", b"\x1b[0m", b"", b'"'):
            stream = asyncio.StreamReader()
            ready = asyncio.Event()
            task = asyncio.create_task(self.flow._drain(stream, ready))
            self.flow.url = ""
            for char in URL.encode():
                stream.feed_data(bytes([char]))
                await asyncio.sleep(0)
                self.assertFalse(ready.is_set())
            stream.feed_data(delimiter)
            stream.feed_eof()
            await task
            self.assertTrue(ready.is_set())
            self.assertEqual(self.flow.url, URL)

    async def test_long_output_and_untrusted_hosts(self):
        for text in (
            "x" * 100000,
            "https://evil.test/a\n",
            "http://accounts.google.com/a\n",
            "https://accounts.google.com.evil.test/a\n",
            "https://accounts.google.com/" + "a" * 20000 + "\n",
        ):
            ready = asyncio.Event()
            stream = asyncio.StreamReader()
            stream.feed_data(text.encode())
            stream.feed_eof()
            await self.flow._drain(stream, ready)
            self.assertFalse(ready.is_set())

    async def waiting(self):
        self.flow.owner, self.flow.ticket = "admin", "ticket"
        self.flow.state, self.flow.url = "waiting", URL
        self.flow.expires_at = asyncio.get_running_loop().time() + 300
        self.flow.task = asyncio.create_task(asyncio.Event().wait())
        self.flow.proc = SimpleNamespace(
            stdin=SimpleNamespace(write=unittest.mock.Mock(), drain=AsyncMock())
        )

    async def test_snapshot_owner_isolation(self):
        await self.waiting()
        self.assertEqual(self.flow.snapshot("admin")["url"], URL)
        for owner in ("", "another-admin"):
            snapshot = self.flow.snapshot(owner)
            self.assertEqual((snapshot["url"], snapshot["ticket"]), ("", ""))

    async def test_invalid_owner_and_old_tickets(self):
        await self.waiting()
        for owner, ticket in (
            ("other", "ticket"),
            ("admin", "old"),
            ("admin", "中文"),
            ("admin", None),
        ):
            with self.assertRaises(AgyError) as caught:
                await self.flow.cancel(owner, ticket)
            self.assertEqual(caught.exception.code, "LOGIN_EXPIRED")
            self.assertTrue(self.flow.busy)

    async def test_authorization_code_validation(self):
        await self.waiting()
        for code in (None, "", "  ", "a\nb", "a\rb", "a\x00b", "a\x7fb", "a" * 4097):
            with self.assertRaises(AgyError) as caught:
                await self.flow.submit("admin", "ticket", code)
            self.assertEqual(caught.exception.code, "CONFIG")
        self.flow.proc.stdin.write.assert_not_called()

    async def test_submit_once_and_clear_url(self):
        await self.waiting()
        await self.flow.submit("admin", "ticket", " fake-code ")
        self.flow.proc.stdin.write.assert_called_once_with(b"fake-code\n")
        self.assertEqual((self.flow.state, self.flow.url), ("verifying", ""))
        with self.assertRaises(AgyError):
            await self.flow.submit("admin", "ticket", "again")

    async def run_cli(self, exit_code=0, report=None, support=True, hang=False):
        if report:
            self.verify.return_value = report
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        # The URL is deliberately delivered on stderr, not stdout.
        stdout.feed_eof()
        stderr.feed_data((URL + "\n").encode())
        stderr.feed_eof()
        exited = asyncio.Event()
        if not hang:
            exited.set()

        async def wait():
            await exited.wait()
            return exit_code

        proc = SimpleNamespace(stdout=stdout, stderr=stderr, wait=wait)
        stop = AsyncMock()
        with (
            patch(
                "agy_provider.login.discover",
                AsyncMock(return_value=LaunchInfo("fake-agy", "1.5.2", False)),
            ),
            patch(
                "agy_provider.login.capture",
                AsyncMock(return_value="--login" if support else ""),
            ),
            patch("agy_provider.login.spawn", AsyncMock(return_value=proc)) as spawn,
            patch("agy_provider.login.stop_process", stop),
        ):
            self.flow.start("admin")
            if hang:
                for _ in range(50):
                    if self.flow.state == "waiting":
                        break
                    await asyncio.sleep(0)
                self.assertEqual(self.flow.state, "waiting")
                await self.flow.cancel("admin", self.flow.ticket)
            else:
                await self.flow.task
            if support:
                stop.assert_awaited_once_with(proc)
                self.assertEqual(
                    spawn.call_args.args[0], ["fake-agy", "acp", "--login"]
                )
                self.assertFalse(Path(spawn.call_args.args[1]).exists())
            self.assertEqual(list(Path(self.temp.name).iterdir()), [])
            self.assertEqual(
                (self.flow.url, self.flow.ticket, self.flow.proc), ("", "", None)
            )

    async def test_zero_exit_requires_acp_verification(self):
        await self.run_cli(report={"status": "blocked", "error_code": "AUTH"})
        self.assertEqual((self.flow.state, self.flow.error), ("failed", "AUTH"))
        self.verify.assert_awaited_once()

    async def test_success(self):
        await self.run_cli()
        self.assertEqual(self.flow.state, "complete")

    async def test_nonzero_exit(self):
        await self.run_cli(exit_code=1)
        self.assertEqual((self.flow.state, self.flow.error), ("failed", "AUTH"))
        self.verify.assert_not_awaited()

    async def test_unsupported_login(self):
        await self.run_cli(support=False)
        self.assertEqual((self.flow.state, self.flow.error), ("failed", "UNSUPPORTED"))

    async def test_cancel_reaps_process_and_temporary_directory(self):
        await self.run_cli(hang=True)
        self.assertEqual(self.flow.state, "cancelled")

    async def test_total_timeout(self):
        with patch("agy_provider.login.discover", AsyncMock(side_effect=TimeoutError)):
            self.flow.start("admin")
            await self.flow.task
        self.assertEqual((self.flow.state, self.flow.error), ("failed", "TIMEOUT"))

    async def test_url_wait_timeout_reaps_process(self):
        original_wait = asyncio.wait

        async def no_link(tasks, **kwargs):
            if kwargs.get("timeout") == 45:
                return set(), set(tasks)
            return await original_wait(tasks, **kwargs)

        with patch("agy_provider.login.asyncio.wait", no_link):
            await self.run_cli()
        self.assertEqual((self.flow.state, self.flow.error), ("failed", "LOGIN_URL"))
