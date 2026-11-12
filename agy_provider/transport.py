"""ACP v1 newline-delimited JSON-RPC over one owned stdio link."""

import asyncio
import json
import re
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .errors import AgyError, classified_error, remote_error
from .notices import TerminalNoticeGuard
from .process import build_child_env, spawn, stop_process


@dataclass
class Event:
    kind: str
    text: str = ""
    result: dict | None = None


class _AgentTextGuard:
    """Hold only possible standalone CLI failure lines, including split chunks."""

    markers = (
        "Agent execution terminated due to error.",
        "⚠ Agent execution terminated due to error.",
        "⚠️ Agent execution terminated due to error.",
    )

    def __init__(self):
        self.pending = ""
        self.line_start = True
        self.notices = TerminalNoticeGuard()

    def feed(self, text: str, *, final=False) -> str:
        text = self.pending + self.notices.feed(text, final=final)
        self.pending = ""
        output = []
        while text:
            line, newline, rest = text.partition("\n")
            if self.line_start:
                candidate = line.strip()
                if candidate in self.markers and (newline or final):
                    raise AgyError("UPSTREAM", "AGY agent 执行失败，请检查官方 CLI。")
                possible = any(
                    marker.startswith(line.lstrip()) or (candidate == marker)
                    for marker in self.markers
                )
                if not newline and not final and possible:
                    self.pending = line
                    break
            output.append(line + newline)
            self.line_start = bool(newline)
            text = rest
        return "".join(output)


class AcpClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.proc: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.write_lock = asyncio.Lock()
        self.events: asyncio.Queue[Event] = asyncio.Queue(maxsize=128)
        self.session_id: str | None = None
        self.capabilities: dict = {}
        self.failed: AgyError | None = None
        self.failure_event = asyncio.Event()
        self.reader: asyncio.Task | None = None
        self.stderr_reader: asyncio.Task | None = None
        self.active = False
        self.output_bytes = 0
        self.closed = False
        self.close_lock = asyncio.Lock()
        self.tool_broker = None
        self.permission_errors: list[str] = []
        self.prompt_deadline = 0
        self.prompt_terminal = False
        self.text_guard = _AgentTextGuard()
        self.stage = "idle"
        self.diagnostic_epoch = 0
        self.stderr_diagnostics = deque(maxlen=32)

    @property
    def alive(self) -> bool:
        return (
            not self.closed
            and not self.failed
            and self.proc is not None
            and self.proc.returncode is None
        )

    async def start(self, argv: list[str], cwd: Path) -> None:
        async with self.close_lock:
            if self.closed:
                raise AgyError("CLOSED", "AGY 连接已关闭。")
            self.proc = await spawn(argv, cwd, env=build_child_env(self.settings))
            self.reader = asyncio.create_task(self._read(), name="agy:stdout")
            self.stderr_reader = asyncio.create_task(
                self._drain_stderr(), name="agy:stderr"
            )
        try:
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "astrbot-agy-provider", "version": "0.4.0"},
                },
                self.settings.startup_timeout,
            )
            if result.get("protocolVersion") != 1:
                raise AgyError("PROTOCOL", "AGY 未协商 ACP v1。")
            self.capabilities = result.get("agentCapabilities") or {}
        except BaseException:
            await self.close()
            raise

    async def _write(self, obj: dict) -> None:
        if self.failed:
            raise self.failed
        if not self.alive or not self.proc or not self.proc.stdin:
            raise AgyError("TRANSPORT", "AGY 连接已关闭。")
        data = (
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        async with self.write_lock:
            try:
                self.proc.stdin.write(data)
                await asyncio.wait_for(
                    self.proc.stdin.drain(), self.settings.startup_timeout
                )
            except (BrokenPipeError, ConnectionError):
                raise AgyError("TRANSPORT", "AGY 输入管道已关闭。") from None

    async def request(self, method: str, params: dict, timeout: int) -> dict:
        self.stage = (
            method
            if method
            in {
                "initialize",
                "session/new",
                "session/set_config_option",
                "session/prompt",
            }
            else "rpc"
        )
        self.diagnostic_epoch += 1
        self.stderr_diagnostics.clear()
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                result = await future
            if not isinstance(result, dict):
                raise AgyError("PROTOCOL", "ACP 返回了非对象结果。")
            return result
        except TimeoutError:
            raise self._diagnose(
                AgyError("TIMEOUT", f"ACP {self.stage} 超时。")
            ) from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Consume exceptions even when writing failed before awaiting it.
                future.exception()

    async def new_session(self, cwd: Path, mcp_servers: list | None = None) -> dict:
        result = await self.request(
            "session/new",
            {"cwd": str(cwd), "mcpServers": mcp_servers or []},
            self.settings.startup_timeout,
        )
        sid = result.get("sessionId")
        if not isinstance(sid, str) or not sid:
            raise AgyError("PROTOCOL", "AGY 没有返回 sessionId。")
        self.session_id = sid
        return result

    async def set_option(self, option: str, value: str) -> dict:
        return await self.request(
            "session/set_config_option",
            {
                "sessionId": self.session_id,
                "configId": option,
                "value": value,
            },
            self.settings.startup_timeout,
        )

    def _diagnose(self, error: AgyError) -> AgyError:
        # Diagnostic stderr is supporting evidence only, never a success veto.
        # Prefer explicit RPC errors over stderr, including known auth failures.
        if error.code not in {"UPSTREAM", "TRANSPORT", "TIMEOUT"}:
            return error
        priority = (
            "PROXY_AUTH",
            "PROXY",
            "TLS",
            "NETWORK",
            "AUTH",
            "QUOTA",
            "RATE_LIMIT",
            "TIMEOUT",
        )
        codes = {code for code, stage in self.stderr_diagnostics if stage == self.stage}
        for code in priority:
            if code in codes:
                return classified_error(code, stage=self.stage)
        return error

    def _fail(self, error: AgyError) -> None:
        self.failed = self.failed or self._diagnose(error)
        self.failure_event.set()
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(self.failed)

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        invalid_frames = 0
        try:
            while line := await self.proc.stdout.readline():
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    invalid_frames += 1
                    if invalid_frames > 8:
                        raise AgyError(
                            "PROTOCOL", "AGY stdout 不是有效的 ACP JSON-RPC 流。"
                        )
                    continue
                if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
                    raise AgyError("PROTOCOL", "收到无效的 JSON-RPC 帧。")
                if "method" in obj:
                    if "id" in obj:
                        await self._reject_request(obj)
                    elif obj["method"] == "session/update":
                        self._on_update(obj.get("params"))
                    elif obj["method"] in {"session/error", "session/agent_error"}:
                        params = obj.get("params")
                        if self._current_update(params):
                            raise remote_error(params.get("error", params))
                else:
                    request_id = obj.get("id")
                    if type(request_id) is not int:
                        continue
                    future = self.pending.get(request_id)
                    if future and not future.done():
                        if "error" in obj:
                            future.set_exception(
                                self._diagnose(remote_error(obj["error"]))
                            )
                        else:
                            future.set_result(obj.get("result"))
                # readline() may complete synchronously for a whole buffered
                # burst. Give generate() and cancellation/control tasks a turn
                # before accepting another frame. Never await queue space here:
                # that would block RPC replies and permission rejection behind
                # a paused consumer. A truly stalled consumer still hits the
                # bounded queue and fails closed below.
                await asyncio.sleep(0)
            # stdout EOF may be scheduled before the final stderr read. Give
            # the drain a bounded chance to finish without waiting on a live CLI.
            if self.stderr_reader and not self.stderr_reader.done():
                await asyncio.wait({self.stderr_reader}, timeout=0.1)
            self._fail(
                AgyError("TRANSPORT", "AGY 进程退出或关闭了输出管道，请检查 CLI。")
            )
        except asyncio.CancelledError:
            raise
        except AgyError as exc:
            self._fail(exc)
        except Exception:
            self._fail(AgyError("PROTOCOL", "ACP 读取失败或单帧超过 1 MiB。"))

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        # Raw text exists only in this coroutine's bounded classification window;
        # the client retains allowlisted categories and fixed stages, never logs.
        window = b""
        epoch = self.diagnostic_epoch
        while chunk := await self.proc.stderr.read(8192):
            if epoch != self.diagnostic_epoch:
                window = b""
                epoch = self.diagnostic_epoch
            window = (window + chunk)[-16384:]
            text = window.decode("utf-8", "replace")
            for line in text.splitlines():
                # AGY emits this background message even during successful use.
                code = remote_error(line).code
                if code == "AUTH" and re.search(r"not.logged.in", line, re.IGNORECASE):
                    continue
                if code != "UPSTREAM":
                    entry = (code, self.stage)
                    if entry not in self.stderr_diagnostics:
                        self.stderr_diagnostics.append(entry)
            # Carry only the incomplete last line across reads; long lines still
            # have a strict 16 KiB maximum and are never attached to an exception.
            window = window.rsplit(b"\n", 1)[-1] if b"\n" in window else window

    async def _reject_request(self, obj: dict) -> None:
        reply = {"jsonrpc": "2.0", "id": obj["id"]}
        if obj["method"] == "session/request_permission":
            reply["result"] = {"outcome": {"outcome": "cancelled"}}
            if len(self.permission_errors) < 128:
                self.permission_errors.append("TOOL_PERMISSION_DENIED")
        else:
            reply["error"] = {
                "code": -32601,
                "message": "AstrBot chat provider does not offer this capability",
            }
        await self._write(reply)

    def _current_update(self, params: object) -> bool:
        return (
            isinstance(params, dict)
            and params.get("sessionId") == self.session_id
            and self.active
        )

    def _on_update(self, params: object) -> None:
        if not self._current_update(params):
            return
        update = params.get("update", {})
        if not isinstance(update, dict):
            return
        kind = update.get("sessionUpdate")
        if kind in {"error", "agent_error", "session_error", "agent_execution_error"}:
            raise remote_error(update.get("error", update))
        if kind in {"tool_call", "tool_call_update"}:
            # Flush a possible terminal notice before any tool can be handed
            # to the host. Never retry or replay a tool as part of this guard.
            tail = self.text_guard.feed("", final=True)
            if tail:
                try:
                    self.events.put_nowait(Event("text", tail))
                except asyncio.QueueFull:
                    raise AgyError("BACKPRESSURE", "流式消费者过慢。") from None
            if self.tool_broker is not None:
                self.tool_broker.observe(update)
                return
            raise AgyError(
                "TOOLS_DISABLED",
                "AGY 尝试调用内置工具；已中止该轮，请检查 CLI 对无工具 agent 的支持。",
            )
        kinds = {"agent_message_chunk": "text", "agent_thought_chunk": "thought"}
        if kind not in kinds:
            return
        content = update.get("content", {})
        if not isinstance(content, dict) or content.get("type") != "text":
            raise AgyError("UNSUPPORTED", "AGY 返回了首版不支持的非文本内容。")
        text = content.get("text", "")
        if not isinstance(text, str):
            raise AgyError("PROTOCOL", "ACP 文本块格式不正确。")
        self.output_bytes += len(text.encode())
        if self.output_bytes > self.settings.max_output_bytes:
            raise AgyError("OUTPUT_LIMIT", "AGY 本轮输出超过插件字节上限。")
        if kind == "agent_message_chunk":
            text = self.text_guard.feed(text)
        if text:
            try:
                self.events.put_nowait(Event(kinds[kind], text))
            except asyncio.QueueFull:
                raise AgyError(
                    "BACKPRESSURE", "流式消费者过慢，已中止请求以限制内存占用。"
                ) from None

    async def generate(self, text: str | list[dict]) -> AsyncIterator[Event]:
        if self.active:
            raise AgyError("BUSY", "同一 ACP 会话不能同时生成。")
        self.events = asyncio.Queue(maxsize=128)
        self.output_bytes = 0
        self.text_guard = _AgentTextGuard()
        self.active = True
        self.prompt_terminal = False
        self.prompt_deadline = (
            asyncio.get_running_loop().time() + self.settings.request_timeout
        )
        completed = False
        response = asyncio.create_task(
            self.request(
                "session/prompt",
                {
                    "sessionId": self.session_id,
                    "prompt": [{"type": "text", "text": text}]
                    if isinstance(text, str)
                    else text,
                },
                self.settings.request_timeout,
            ),
            name="agy:prompt",
        )

        def prompt_finished(task):
            self.prompt_terminal = True
            if task.cancelled():
                return
            error = task.exception()
            if error is not None and self.tool_broker is not None:
                self._fail(
                    error
                    if isinstance(error, AgyError)
                    else AgyError("TRANSPORT", "工具轮的 ACP prompt 已结束。")
                )
            elif self.tool_broker is not None and self.tool_broker.pending:
                self._fail(
                    AgyError(
                        "TOOL_TERMINATED",
                        "原 ACP prompt 已结束，尚未完成的工具请求已取消。",
                    )
                )

        response.add_done_callback(prompt_finished)
        getter = None
        try:
            while True:
                if self.failed:
                    raise self.failed
                if not self.events.empty():
                    yield self.events.get_nowait()
                    continue
                if response.done():
                    result = await response
                    if result.get("error") is not None:
                        raise self._diagnose(remote_error(result["error"]))
                    if result.get("stopReason") in {
                        "error",
                        "failed",
                        "failure",
                        "agent_error",
                        "execution_error",
                        "internal_error",
                    }:
                        raise self._diagnose(
                            AgyError("UPSTREAM", "AGY agent 执行失败，请检查官方 CLI。")
                        )
                    remaining = self.text_guard.feed("", final=True)
                    if remaining:
                        yield Event("text", remaining)
                    completed = True
                    yield Event("done", result=result)
                    return
                getter = asyncio.create_task(self.events.get())
                await asyncio.wait(
                    (getter, response), return_when=asyncio.FIRST_COMPLETED
                )
                if getter.done():
                    event = getter.result()
                    getter = None
                    yield event
                else:
                    getter.cancel()
                    await asyncio.gather(getter, return_exceptions=True)
                    getter = None
        finally:
            self.active = False
            if getter is not None:
                getter.cancel()
                await asyncio.gather(getter, return_exceptions=True)
            if not completed:
                try:
                    await asyncio.wait_for(
                        self._write(
                            {
                                "jsonrpc": "2.0",
                                "method": "session/cancel",
                                "params": {"sessionId": self.session_id},
                            }
                        ),
                        2,
                    )
                except (TimeoutError, AgyError, OSError):
                    pass
            response.cancel()
            await asyncio.gather(response, return_exceptions=True)

    async def close(self) -> None:
        async with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self._fail(AgyError("CLOSED", "AGY 会话已关闭。"))
            try:
                if self.proc:
                    await stop_process(self.proc)
            finally:
                tasks = [task for task in (self.reader, self.stderr_reader) if task]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
