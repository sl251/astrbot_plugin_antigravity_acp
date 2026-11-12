"""Bounded official CLI login. Authorization material lives only in memory."""

import asyncio
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .errors import AgyError
from .process import build_child_env, capture, discover, spawn, stop_process


class LoginFlow:
    def __init__(self, settings, root: Path, verify):
        self.settings = settings
        self.root = root
        self.verify = verify
        self.task = None
        self.proc = None
        self.owner = ""
        self.ticket = ""
        self.url = ""
        self.state = "idle"
        self.error = ""
        self.expires_at = 0.0
        self.submitted = False

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def snapshot(self, owner):
        own = owner == self.owner
        return {
            "state": self.state, "busy": self.busy, "error_code": self.error,
            "owned": own, "ticket": self.ticket if own and self.busy else "",
            "url": self.url if own and self.state == "waiting" else "",
            "expires_in": max(0, int(self.expires_at - asyncio.get_running_loop().time())) if self.busy else 0,
        }

    def start(self, owner):
        if self.busy:
            raise AgyError("BUSY", "登录正在进行，请完成或取消当前登录。")
        self.owner, self.ticket = owner, secrets.token_urlsafe(24)
        self.state, self.error, self.url = "starting", "", ""
        self.submitted = False
        self.expires_at = asyncio.get_running_loop().time() + 300
        self.task = asyncio.create_task(self._run(), name="agy:login")

    def _assert_owner(self, owner, ticket):
        if (
            not self.busy
            or owner != self.owner
            or not isinstance(ticket, str)
            or not ticket.isascii()
            or not secrets.compare_digest(ticket, self.ticket)
        ):
            raise AgyError("LOGIN_EXPIRED", "此登录已结束或不属于当前管理员，请重新发起。")

    async def submit(self, owner, ticket, code):
        self._assert_owner(owner, ticket)
        if self.state != "waiting" or self.submitted or not self.proc or not self.proc.stdin:
            raise AgyError("BUSY", "当前登录暂不接受授权码。")
        if (
            not isinstance(code, str)
            or not 1 <= len(code.strip()) <= 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in code)
        ):
            raise AgyError("CONFIG", "请输入浏览器显示的单行授权码（最多 4096 字符）。")
        self.submitted = True
        self.state = "verifying"
        self.url = ""
        try:
            self.proc.stdin.write((code.strip() + "\n").encode())
            await asyncio.wait_for(self.proc.stdin.drain(), 5)
        except (OSError, TimeoutError):
            await self.cancel(owner, ticket)
            raise AgyError("AUTH", "授权码未能交给 CLI，请重新登录。") from None

    async def cancel(self, owner, ticket):
        self._assert_owner(owner, ticket)
        await self.close()

    async def close(self):
        if self.busy:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.url = ""
        self.ticket = ""
        if self.state in {"starting", "waiting", "verifying"}:
            self.state = "cancelled"

    async def _drain(self, stream, ready):
        # Parse only complete URLs, including URLs split across pipe reads.
        # Never retain a stderr tail or include CLI output in an exception.
        window = ""
        while chunk := await stream.read(2048):
            window = (window + chunk.decode("utf-8", "replace"))[-16384:]
            self._find_url(window, ready)
        self._find_url(window + "\n", ready)

    def _find_url(self, text, ready):
        if ready.is_set():
            return
        for match in re.finditer(r'''https://accounts\.google\.com/[^\s"'<>\x1b]{1,8192}(?=[\s"'<>\x1b])''', text):
            url = match[0]
            parsed = urlsplit(url)
            if parsed.netloc == "accounts.google.com" and not any(ord(c) < 32 for c in url):
                self.url = url
                self.state = "waiting"
                ready.set()
                return

    async def _run(self):
        readers = []
        waiters = []
        try:
            async with asyncio.timeout_at(self.expires_at):
                info = await discover(self.settings)
                env = build_child_env(self.settings)
                help_text = await capture([info.binary, "acp", "--help"], self.settings.startup_timeout, env=env)
                if "--login" not in help_text:
                    raise AgyError("UNSUPPORTED", "此 CLI 不支持面板登录，请使用终端 agy 完成登录。")
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                with tempfile.TemporaryDirectory(prefix="login-", dir=self.root) as directory:
                    # Do not override HOME or app-data: login and the default
                    # runtime keyring/file bridge must refer to the same user.
                    try:
                        self.proc = await spawn([info.binary, "acp", "--login"], Path(directory), env=env)
                        ready = asyncio.Event()
                        readers = [asyncio.create_task(self._drain(stream, ready)) for stream in (self.proc.stdout, self.proc.stderr)]
                        exited = asyncio.create_task(self.proc.wait())
                        link = asyncio.create_task(ready.wait())
                        waiters = [exited, link]
                        done, _ = await asyncio.wait(waiters, timeout=45, return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            raise AgyError("LOGIN_URL", "CLI 未返回授权链接，请在同一运行环境使用 agy acp --login。")
                        code = await exited
                        self.state, self.url = "verifying", ""
                        if code != 0:
                            raise AgyError("AUTH", "官方登录未成功，请重新登录或使用终端完成。")
                        report = await self.verify()
                        if report.get("status") != "passed":
                            raise AgyError(report.get("error_code", "AUTH"), "登录后 ACP 检查未通过。")
                        self.state = "complete"
                    finally:
                        if self.proc:
                            try:
                                await stop_process(self.proc)
                            finally:
                                self.proc = None
        except asyncio.CancelledError:
            self.state = "cancelled"
            raise
        except TimeoutError:
            self.state, self.error = "failed", "TIMEOUT"
        except AgyError as exc:
            self.state, self.error = "failed", exc.code
        except Exception:
            self.state, self.error = "failed", "LOCAL"
        finally:
            for task in readers + waiters:
                task.cancel()
            await asyncio.gather(*readers, *waiters, return_exceptions=True)
            self.proc, self.url, self.ticket = None, "", ""
