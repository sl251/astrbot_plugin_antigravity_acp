"""Retain one native ACP prompt across the host runner's tool-response turns."""

import asyncio
import copy

from .errors import AgyError
from .messages import canonical_calls
from .transport import Event


def _continuation_exchange(messages, baseline, call, text):
    """Authenticate the pending exchange independently of host history rewrites.

    Only the located result is sent to the retained ACP prompt. Older prose may
    change or disappear, but tool records must belong to the original history
    or an exchange already accepted by this turn; foreign results are rejected.
    Inputs have already been normalized and checked for closed, unique IDs.
    """
    expected = canonical_calls(
        [{"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}]
    )
    calls_at, results_at = [], []
    for index, message in enumerate(messages):
        calls_at.extend(
            index for item in message.get("tool_calls", []) if item["id"] == call.id
        )
        if message.get("role") == "tool" and message.get("tool_call_id") == call.id:
            results_at.append(index)
    if (
        calls_at != [len(messages) - 2]
        or results_at != [len(messages) - 1]
        or messages[-2]
        != {"role": "assistant", "content": text, "tool_calls": expected}
    ):
        raise AgyError(
            "TOOL_HISTORY", "工具续轮缺少唯一、完整且匹配的已审计调用/结果。"
        )

    known_calls = {
        item["id"]: item
        for message in baseline
        for item in message.get("tool_calls", [])
    }
    known_results = {
        message["tool_call_id"]: message
        for message in baseline
        if message["role"] == "tool"
    }
    for message in messages[:-2]:
        if any(
            item != known_calls.get(item["id"])
            for item in message.get("tool_calls", [])
        ) or (
            message["role"] == "tool"
            and message != known_results.get(message.get("tool_call_id"))
        ):
            raise AgyError("TOOL_HISTORY", "工具续轮混入了未审计或被改写的工具记录。")
    return messages[-2:]


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
        exchange = _continuation_exchange(
            request.messages, self.request.messages, call, "".join(self.segment)
        )
        self.waiting["broker"].resolve(call.id, exchange[1]["content"])
        self.request.consumed_text += "".join(self.segment)
        # Track what the retained ACP prompt actually saw. If the host rewrote
        # its prefix, the next user turn must rebuild instead of falsely reusing
        # a session whose context never received those edits.
        self.request.messages.extend(copy.deepcopy(exchange))
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
