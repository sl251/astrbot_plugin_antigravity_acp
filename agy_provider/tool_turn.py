"""Retain one native ACP prompt across the host runner's tool-response turns."""

import asyncio
import copy

from .errors import AgyError
from .messages import canonical_calls
from .transport import Event


class ToolTurn:
    def __init__(
        self, runtime, request, key, model, effort, snapshot, signal, approver=None
    ):
        self.runtime, self.request, self.key = runtime, request, key
        self.model, self.effort, self.snapshot, self.signal = (
            model,
            effort,
            snapshot,
            signal,
        )
        self.queue = asyncio.Queue(maxsize=128)
        self.resume = asyncio.Event()
        self.waiting = None
        self.segment = []
        self.finished = False
        self.approver = approver
        self.task = asyncio.create_task(self._run(), name="agy:tool-turn")
        self.watcher = asyncio.create_task(self._watch(), name="agy:tool-abort")

    async def _watch(self):
        if self.signal is not None:
            await self.signal.wait()
            self.task.cancel()

    async def _put(self, item):
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            raise AgyError("BACKPRESSURE", "工具轮消费者过慢。") from None

    async def _approve(self, call, broker, client):
        approval = asyncio.create_task(
            self.approver(call.name, copy.deepcopy(call.arguments), call.id)
        )
        failed = asyncio.create_task(client.failure_event.wait())
        try:
            try:
                async with asyncio.timeout_at(
                    call.created_at + self.runtime.settings.tool_approval_timeout
                ):
                    done, _ = await asyncio.wait(
                        (approval, failed, call.future),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
            except TimeoutError:
                broker.reject(call, "TOOL_APPROVAL_TIMEOUT")
                return False
            if failed in done:
                raise client.failed
            if call.future.done():
                if call.future.cancelled():
                    raise AgyError("TOOL_CANCELLED", "审批中的 MCP 调用已取消。")
                return False
            try:
                approved = approval.result()
            except Exception:
                approved = False
            if approved is not True:
                broker.reject(call, "TOOL_APPROVAL_DENIED")
                return False
            return True
        finally:
            approval.cancel()
            failed.cancel()
            await asyncio.gather(approval, failed, return_exceptions=True)

    async def _run(self):
        source = self.runtime.stream(
            self.request, self.key, self.model, self.effort, self.snapshot
        )
        try:
            async for event in source:
                if event.kind == "text":
                    self.segment.append(event.text)
                if event.kind == "tools":
                    call, broker = event.result["call"], event.result["broker"]
                    if self.approver is not None and not await self._approve(
                        call, broker, event.result["client"]
                    ):
                        continue
                    self.waiting = event.result
                    self.resume.clear()
                    await self._put(
                        Event("tools", result=event.result, text="".join(self.segment))
                    )
                    # EOF, host cancellation and deadlines all end retained waits.
                    resumed = asyncio.create_task(self.resume.wait())
                    failed = asyncio.create_task(
                        event.result["client"].failure_event.wait()
                    )
                    try:
                        async with asyncio.timeout(
                            self.runtime.settings.tool_approval_timeout
                            + self.runtime.settings.tool_execution_timeout
                        ):
                            done, _ = await asyncio.wait(
                                (resumed, failed, event.result["call"].future),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if failed in done:
                                raise event.result["client"].failed
                            if not self.resume.is_set():
                                raise AgyError(
                                    "TOOL_TIMEOUT", "工具已取消或超时；不会重放。"
                                )
                    finally:
                        resumed.cancel()
                        failed.cancel()
                        await asyncio.gather(resumed, failed, return_exceptions=True)
                    self.segment.clear()
                    self.waiting = None
                else:
                    await self._put(event)
        except TimeoutError:
            await self._error(AgyError("TOOL_TIMEOUT", "宿主审批/执行未在期限内回填。"))
        except asyncio.CancelledError:
            await self._error(
                AgyError("CANCELLED", "工具轮已取消；已交接调用状态未知。")
            )
        except AgyError as exc:
            await self._error(exc)
        except Exception:
            await self._error(AgyError("TOOL_PROTOCOL", "工具轮异常。"))
        finally:
            try:
                await source.aclose()
            finally:
                self.finished = True
                self.watcher.cancel()
                if self.runtime.turns.get(self.key) is self:
                    del self.runtime.turns[self.key]

    async def _error(self, exc):
        # Discard stale queued tool handoffs, never dispatch after a terminal error.
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(exc)

    def accept(self, request, model, effort, snapshot, signal, approver=None):
        if self.finished or not self.waiting:
            raise AgyError("TOOL_STALE", "没有可续接的工具请求。")
        client = self.waiting["client"]
        if (
            self.task.cancelling()
            or not client.alive
            or client.prompt_terminal
            or asyncio.get_running_loop().time() >= client.prompt_deadline
        ):
            raise AgyError("TOOL_STALE", "原 ACP prompt 已结束，拒绝迟到结果。")
        if (
            signal is not self.signal
            or approver is not self.approver
            or model != self.model
            or effort != self.effort
            or snapshot.fingerprint != self.snapshot.fingerprint
            or request.system_hash != self.request.system_hash
        ):
            raise AgyError("TOOL_CONTEXT", "工具续轮身份、人格、模型或许可发生变化。")
        call = self.waiting["call"]
        base = self.request.messages
        expected_call = canonical_calls(
            [
                {
                    "id": call.id,
                    "function": {"name": call.name, "arguments": call.arguments},
                }
            ]
        )
        tail = request.messages[len(base) :]
        if (
            request.messages[: len(base)] != base
            or len(tail) != 2
            or tail[0]
            != {
                "role": "assistant",
                "content": "".join(self.segment),
                "tool_calls": expected_call,
            }
            or tail[1]["role"] != "tool"
            or tail[1].get("tool_call_id") != call.id
        ):
            raise AgyError(
                "TOOL_HISTORY", "工具续轮必须精确匹配已审计的完整调用/结果。"
            )
        self.waiting["broker"].resolve(call.id, tail[1]["content"])
        self.request.consumed_text += "".join(self.segment)
        self.request.messages = request.messages
        self.resume.set()

    async def events(self):
        keep = completed = False
        try:
            while True:
                item = await self.queue.get()
                if isinstance(item, BaseException):
                    raise item
                if item.kind == "tools":
                    if self.finished:
                        raise AgyError("TOOL_STALE", "工具请求已失效。")
                    item.result["broker"].handoff(item.result["call"])
                    keep = True
                    yield item
                    return
                if item.kind == "done":
                    completed = True
                yield item
                if completed:
                    await self.task
                    return
        finally:
            if not keep:
                await self.close()
                if self.runtime.turns.get(self.key) is self:
                    del self.runtime.turns[self.key]

    async def close(self):
        if not self.task.cancelling():
            self.task.cancel()
        self.watcher.cancel()
        cleanup = asyncio.gather(self.task, self.watcher, return_exceptions=True)
        cancelled = None
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if cleanup.done():
                    break
        if cancelled:
            raise cancelled


async def chat_events(
    runtime, request, key, model, effort, snapshot, signal, approver=None
):
    """Per-entry lock releases on host handoff; retained job owns original lease."""
    if key in runtime.entry_locks:
        raise AgyError("BUSY", "同一聊天入口正在处理。")
    runtime.entry_locks[key] = True
    turn = runtime.turns.get(key)
    try:
        if turn:
            try:
                turn.accept(request, model, effort, snapshot, signal, approver)
            except BaseException:
                await turn.close()
                runtime.turns.pop(key, None)
                raise
        else:
            if request.messages[-1]["role"] == "tool":
                raise AgyError("TOOL_STALE", "进程重载或会话结束后不能回填旧工具结果。")
            if (
                len(runtime.turns)
                >= runtime.settings.max_pending + runtime.settings.max_concurrent
            ):
                raise AgyError("BUSY", "工具轮队列已满。")
            turn = ToolTurn(
                runtime, request, key, model, effort, snapshot, signal, approver
            )
            runtime.turns[key] = turn
        source = turn.events()
        try:
            async for event in source:
                yield event
        finally:
            await source.aclose()
    finally:
        runtime.entry_locks.pop(key, None)
