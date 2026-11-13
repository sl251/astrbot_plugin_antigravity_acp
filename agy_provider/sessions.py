"""Bounded native process pool; AstrBot history is the source of truth."""

import asyncio
import logging
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .errors import AgyError, classified_error
from .failure_guard import FailureGuard
from .messages import (
    TRANSIENT_SYSTEM_NOTICE,
    ConversationInput,
    attach_transient_data,
    digest,
    prompt_blocks,
)
from .models import catalog, select
from .process import LaunchInfo, discover, launch_args, make_home
from .telemetry import TelemetryTracker, TurnMeter
from .tools import ToolBroker, ToolSnapshot
from .transport import AcpClient, Event

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)

_UNSET = object()

_UPSTREAM_CAPACITY_NOTICE_RE = re.compile(
    r"^⚠\ufe0f?\s+API error \(attempt \d+\): UNAVAILABLE \(code 503\): "
    r"No capacity available for model \S+ on the server$"
)


def strip_upstream_capacity_notices(text: str) -> tuple[str, int]:
    """Remove CLI 503 capacity retry lines and extra blank lines around them."""
    if not text:
        return text, 0
    kept: list[str] = []
    pending_blanks: list[str] = []
    removed = 0
    after_notice = False
    had_blank_before_notice = False

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _UPSTREAM_CAPACITY_NOTICE_RE.match(stripped):
            removed += 1
            if pending_blanks:
                had_blank_before_notice = True
            pending_blanks.clear()
            after_notice = True
        elif stripped == "":
            pending_blanks.append(line)
        else:
            if kept:
                if not after_notice:
                    kept.extend(pending_blanks)
                elif pending_blanks or had_blank_before_notice:
                    kept.append(pending_blanks[0] if pending_blanks else "\n")
            pending_blanks.clear()
            after_notice = False
            had_blank_before_notice = False
            kept.append(line)

    if removed == 0:
        return text, 0
    if not after_notice:
        kept.extend(pending_blanks)
    elif kept:
        kept[-1] = kept[-1].rstrip("\r\n")
    return "".join(kept), removed


@dataclass
class SentWindow:
    hashes: list[str] = field(default_factory=list)
    omitted_messages: int = 0
    window_hash: str = ""


@dataclass
class ReuseDecision:
    reuse: bool
    reasons: list[str]
    history_relation: str  # exact / host_trim / rewrite / append_gap / unknown


def compare_history(expected: list[str], incoming: list[str]) -> str:
    """Compare incoming host history hashes against session host_expected."""
    if not expected:
        return "unknown"
    if expected == incoming:
        return "exact"
    if 0 < len(incoming) < len(expected) and expected[-len(incoming) :] == incoming:
        return "host_trim"
    if len(incoming) > len(expected) and incoming[: len(expected)] == expected:
        return "append_gap"
    return "rewrite"


def classify_rebuild(
    session,
    request: ConversationInput,
    tombstone: object = None,
    *,
    temporary: bool = False,
    model: object = _UNSET,
    effort: object = _UNSET,
    snapshot: object = _UNSET,
) -> list[str]:
    """Collect all matching rebuild reasons in deterministic diagnostic order."""
    incoming = request.hashes[:-1]
    expected = getattr(session, "host_expected", None)
    if expected is None:
        expected = getattr(session, "expected", [])
    history_relation = compare_history(expected, incoming)
    reasons: list[str] = []

    if temporary:
        reasons.append("no_identity")
    elif tombstone:
        if isinstance(tombstone, str):
            reasons.append(tombstone)
        elif isinstance(tombstone, (list, tuple)):
            reasons.extend(str(item) for item in tombstone if item)

    client = getattr(session, "client", None)
    if client is None:
        if not temporary and not tombstone:
            reasons.append("runtime_restart" if incoming else "cold_start")
    else:
        if not getattr(client, "alive", False):
            reasons.append("process_dead")
        if (
            getattr(session, "system_hash", "") != request.system_hash
            or getattr(session, "system_structured", False)
            != getattr(request, "system_structured", False)
            or getattr(request, "force_rebuild", False)
        ):
            reasons.append("system_changed")
        if model is not _UNSET and getattr(session, "model", "") != model:
            reasons.append("model_changed")
        if effort is not _UNSET and getattr(session, "effort", "") != effort:
            reasons.append("effort_changed")
        if snapshot is not _UNSET:
            req_tool_hash = (
                snapshot.fingerprint
                if (snapshot is not None and getattr(snapshot, "tools", None))
                else ""
            )
            if getattr(session, "tool_hash", "") != req_tool_hash:
                reasons.append("tool_hash_changed")
        broker = getattr(session, "broker", None)
        if broker is not None and len(getattr(broker, "seen", ())) >= 4000:
            reasons.append("tool_ledger_limit")
        if history_relation == "host_trim":
            reasons.append("history_host_trim")
        elif history_relation == "rewrite":
            reasons.append("history_rewrite")
        elif history_relation == "append_gap":
            reasons.append("history_append_gap")
        elif history_relation == "unknown" and not reasons:
            reasons.append("cold_start")

    seen: set[str] = set()
    unique: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return unique


def decide_reuse(
    session,
    request: ConversationInput,
    model: str,
    effort: str,
    snapshot: ToolSnapshot | None = None,
    *,
    temporary: bool = False,
    tombstone: object = None,
) -> ReuseDecision:
    expected = getattr(session, "host_expected", None)
    if expected is None:
        expected = getattr(session, "expected", [])
    history_relation = compare_history(expected, request.hashes[:-1])
    reasons = classify_rebuild(
        session,
        request,
        tombstone,
        temporary=temporary,
        model=model,
        effort=effort,
        snapshot=snapshot,
    )
    reuse = not reasons and history_relation == "exact"
    return ReuseDecision(reuse=reuse, reasons=reasons, history_relation=history_relation)


@dataclass
class Session:
    client: AcpClient | None = None
    broker: ToolBroker | None = None
    tool_hash: str = ""
    home: Path | None = None
    system_hash: str = ""
    system_structured: bool = False
    had_transient: bool = False
    turn_counter: int = 0
    model: str = ""
    effort: str = ""
    host_expected: list[str] = field(default_factory=list)
    sent_window: SentWindow | None = None
    busy: bool = False
    used_at: float = field(default_factory=time.monotonic)
    lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def expected(self) -> list[str]:
        return self.host_expected

    @expected.setter
    def expected(self, value: list[str]) -> None:
        self.host_expected = list(value)

    async def close(self) -> None:
        async with self.lifecycle:
            await self.close_unlocked()

    async def close_unlocked(self) -> None:
        try:
            try:
                if self.broker:
                    await self.broker.close()
            finally:
                self.broker = None
                if self.client:
                    await self.client.close()
        finally:
            self.client = None
            if self.home:
                await asyncio.to_thread(shutil.rmtree, self.home, True)
                self.home = None
            self.host_expected.clear()
            self.sent_window = None
            self.system_hash = ""
            self.system_structured = False
            self.had_transient = False
            self.turn_counter = 0
            self.model = ""
            self.effort = ""
            self.tool_hash = ""


class Runtime:
    def __init__(self, settings: Settings, root: Path, *, clock=time.monotonic):
        self.settings = settings
        self.root = root / ("runtime-" + uuid.uuid4().hex)
        self.sessions: dict[str, Session] = {}
        self.key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.pool_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(settings.max_concurrent)
        self.callers = 0
        self.stopped = False
        self.info: LaunchInfo | None = None
        self.info_lock = asyncio.Lock()
        self.models_lock = asyncio.Lock()
        self.model_cache: list[str] = []
        self.model_cache_time = 0.0
        self.sweeper: asyncio.Task | None = None
        self.turns = {}
        self.entry_locks = {}
        self.tool_outcomes: dict[str, str] = {}
        self.telemetry = TelemetryTracker(
            cost_log_enabled=settings.cost_log_enabled, clock=clock
        )
        self.failure_guard = FailureGuard(clock=clock)

    def pool_stats(self) -> dict[str, int]:
        live_processes = sum(
            1
            for s in self.sessions.values()
            if s.client is not None and s.client.alive
        )
        return {
            "active_sessions": len(self.sessions),
            "max_sessions": self.settings.max_sessions,
            "live_processes": live_processes,
            "active_tool_turns": len(self.turns),
        }

    def record_tool_outcome(self, call_id, state):
        self.tool_outcomes[call_id] = state
        while len(self.tool_outcomes) > 256:
            del self.tool_outcomes[next(iter(self.tool_outcomes))]

    async def launch_info(self) -> LaunchInfo:
        async with self.info_lock:
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用或重新加载。")
            if self.info is None:
                self.info = await discover(self.settings)
            return self.info

    @asynccontextmanager
    async def lease(self, key: str, temporary: bool):
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用或重新加载。")
        if self.callers >= self.settings.max_pending + self.settings.max_concurrent:
            raise AgyError("BUSY", "AGY 等待队列已满，请稍后再试。")
        self.callers += 1
        lock, refs = self.key_locks.get(key, (asyncio.Lock(), 0))
        self.key_locks[key] = (lock, refs + 1)
        locked = acquired = False
        session = None
        try:
            try:
                async with asyncio.timeout(self.settings.queue_timeout):
                    await lock.acquire()
                    locked = True
                    await self.slots.acquire()
                    acquired = True
            except TimeoutError:
                raise AgyError("BUSY", "等待 AGY 会话超时，请稍后再试。") from None
            async with self.pool_lock:
                if self.stopped:
                    raise AgyError("CLOSED", "插件已停用或重新加载。")
                if self.sweeper is None:
                    self.sweeper = asyncio.create_task(
                        self._sweep(), name="agy:idle-reaper"
                    )
                session = self.sessions.get(key)
                if session is None:
                    if len(self.sessions) >= self.settings.max_sessions:
                        idle = [(k, s) for k, s in self.sessions.items() if not s.busy]
                        if not idle:
                            raise AgyError("BUSY", "AGY 会话池已满。")
                        old_key, old = min(idle, key=lambda pair: pair[1].used_at)
                        await old.close()
                        del self.sessions[old_key]
                        self.telemetry.record_eviction(old_key, "pool_evicted")
                    session = self.sessions[key] = Session()
                session.busy = True
            yield session
        finally:
            try:
                if session:
                    async with self.pool_lock:
                        session.used_at = time.monotonic()
                        session.busy = False
                        if (
                            temporary
                            or self.stopped
                            or session.client is None
                            or not session.client.alive
                        ):
                            dead_idle = (
                                not temporary
                                and not self.stopped
                                and session.client is not None
                                and not session.client.alive
                                and self.telemetry.peek_tombstone(key) is None
                            )
                            await session.close()
                            if self.sessions.get(key) is session:
                                del self.sessions[key]
                            if dead_idle:
                                self.telemetry.record_eviction(key, "process_dead")
            finally:
                if acquired:
                    self.slots.release()
                if locked:
                    lock.release()
                _, refs = self.key_locks[key]
                if refs == 1:
                    del self.key_locks[key]
                else:
                    self.key_locks[key] = (lock, refs - 1)
                self.callers -= 1

    async def open(self, session: Session, system: str, snapshot=None) -> dict:
        async with session.lifecycle:
            return await self._open_unlocked(session, system, snapshot)

    async def _open_unlocked(
        self, session: Session, system: str, snapshot=None
    ) -> dict:
        await session.close_unlocked()
        info = await self.launch_info()
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用。")
        session.home = make_home(
            self.root,
            system,
            tools_mode=bool(snapshot and snapshot.tools),
            settings=self.settings,
        )
        session.client = AcpClient(self.settings)
        try:
            await session.client.start(
                launch_args(
                    info,
                    session.home,
                    use_default_agent=bool(snapshot and snapshot.tools),
                ),
                session.home / "workspace",
            )
            servers = []
            if snapshot and snapshot.tools:
                session.broker = ToolBroker(snapshot, self.settings)
                session.broker.audit_sink = self.record_tool_outcome
                servers = [await session.broker.start()]
                session.client.tool_broker = session.broker
            result = await session.client.new_session(
                session.home / "workspace", servers
            )
            if session.broker:
                session.broker.session_id = session.client.session_id
            session.tool_hash = (
                snapshot.fingerprint if (snapshot and snapshot.tools) else ""
            )
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用。")
            return result
        except BaseException:
            await session.close_unlocked()
            raise

    async def get_models(self) -> list[str]:
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用。")
        async with self.models_lock:
            if self.model_cache and time.monotonic() - self.model_cache_time < 300:
                return list(self.model_cache)
            async with self.lease(uuid.uuid4().hex, temporary=True) as session:
                result = await self.open(session, "")
                self.model_cache = catalog(result)
                self.model_cache_time = time.monotonic()
                return list(self.model_cache)

    async def stream(
        self,
        request: ConversationInput,
        key: str | None,
        model: str,
        effort: str,
        snapshot: ToolSnapshot | None = None,
        *,
        meter: TurnMeter | None = None,
        provider_id: str = "",
        logical_request_id: str | None = None,
        request_fingerprint: str | None = None,
        continuation: bool = False,
        claimed_half_open: tuple[str, str] | None = None,
    ) -> AsyncIterator[Event]:
        temporary = key is None
        local_meter = meter
        if local_meter is None:
            local_meter = self.telemetry.start_turn(
                provider=provider_id,
                session_key_raw=key,
                mode="tools" if (snapshot and snapshot.tools) else "chat",
                model=model,
                effort=effort,
                system_bytes=request.system_bytes,
                transient_bytes=len((request.transient_data or "").encode()),
                tool_schema_bytes=getattr(snapshot, "schema_bytes", 0) if snapshot else 0,
            )

        async with self.lease(key or uuid.uuid4().hex, temporary) as session:
            completed = False
            failure_exc: BaseException | None = None
            cancelled = False
            try:
                if not continuation and provider_id:
                    check = await self.failure_guard.check_empty_only(
                        (provider_id, model),
                        request_fingerprint,
                        logical_request_id,
                        session_key=key,
                        empty_ttl=self.settings.empty_dedupe_ttl,
                        continuation=continuation,
                    )
                    if not check.allowed:
                        self.failure_guard.release_probe(claimed_half_open)
                        claimed_half_open = None
                        local_meter.origin_turn_id = check.origin_turn_id
                        local_meter.finish(status="short_circuited", error_code="EMPTY")
                        raise AgyError(
                            "EMPTY",
                            f"AGY 短期抑制重复 EMPTY 请求（origin_turn_id={check.origin_turn_id}，"
                            f"请约 {check.retry_after} 秒后再试）。",
                            retry_after=check.retry_after,
                            origin_turn_id=check.origin_turn_id,
                            no_retry=True,
                        )

                tombstone = None if temporary else self.telemetry.consume_tombstone(key)
                decision = decide_reuse(
                    session,
                    request,
                    model,
                    effort,
                    snapshot,
                    temporary=temporary,
                    tombstone=tombstone,
                )
                context = request.prompt_context
                if decision.reuse:
                    turn_index = session.turn_counter + 1
                    prompt, omitted = prompt_blocks(
                        request.messages[-1:], self.settings.history_bytes
                    )
                    replay_bytes = 0
                else:
                    turn_index = 1
                    prompt, omitted = prompt_blocks(
                        request.messages, self.settings.history_bytes
                    )
                    replay_bytes = sum(
                        len(block.get("text", "").encode())
                        for block in prompt
                        if block.get("type") == "text"
                    )
                    system_for_home = (
                        (
                            f"{context.stable_system}\n\n{TRANSIENT_SYSTEM_NOTICE}"
                            if context.stable_system
                            else TRANSIENT_SYSTEM_NOTICE
                        )
                        if context.structured
                        else request.system
                    )
                    result = await self.open(session, system_for_home, snapshot)
                    assert session.client
                    await select(session.client, result, model, effort)
                    session.system_hash = request.system_hash
                    session.system_structured = request.system_structured
                    session.model, session.effort = model, effort
                    session.had_transient = False

                prompt = attach_transient_data(
                    prompt,
                    context,
                    turn_index,
                    had_transient=session.had_transient,
                )
                prompt_bytes = sum(
                    len(block.get("text", "").encode())
                    for block in prompt
                    if block.get("type") == "text"
                )
                local_meter.set_decision(
                    reused=decision.reuse,
                    reasons=decision.reasons,
                    history_relation=decision.history_relation,
                    prompt_bytes=prompt_bytes,
                    replay_bytes=replay_bytes,
                    omitted_messages=omitted,
                    native_session_id=getattr(session.client, "session_id", None),
                )

                if omitted:
                    yield Event("history_trimmed", result={"omitted_messages": omitted})
                assert session.client
                if any(
                    p.get("type") == "image" for p in prompt
                ) and not session.client.capabilities.get("promptCapabilities", {}).get(
                    "image"
                ):
                    raise AgyError(
                        "IMAGE_CAPABILITY", "当前 CLI/model 没有声明图片输入能力。"
                    )
                if session.broker:
                    session.broker.count = 0
                    session.broker.result_bytes = 0
                    session.broker.notifications.clear()
                    session.broker.error = None
                text_parts = []
                local_meter.request_sent = True
                turn = tool_events(session.client, prompt, session.broker)
                try:
                    async for event in turn:
                        if event.kind == "usage":
                            payload = event.result or {}
                            local_meter.observe_usage(
                                payload, source=payload.get("source", "acp_update")
                            )
                            continue
                        if event.kind == "tools":
                            local_meter.record_tool_call()
                        if event.kind == "text":
                            text_parts.append(event.text)
                        if event.kind == "done":
                            result = event.result or {}
                            local_meter.observe_usage(result, source="acp_terminal")
                            reason = result.get("stopReason")
                            if reason == "cancelled":
                                raise AgyError("CANCELLED", "AGY 取消了本轮请求。")
                            if reason not in {"end_turn", "max_tokens"}:
                                raise AgyError(
                                    "STOP",
                                    "AGY 未正常结束本轮，请在 CLI 检查限制或拒绝原因。",
                                )
                            raw_answer = "".join(text_parts)[len(request.consumed_text) :]
                            answer, stripped_notices = strip_upstream_capacity_notices(
                                raw_answer
                            )
                            if not answer.strip():
                                if result.get("agy_retry_error"):
                                    raise classified_error(result["agy_retry_error"])
                                if stripped_notices:
                                    logger.warning(
                                        "AGY 丢弃了 %s 条上游容量告警，剩余正文为空。",
                                        stripped_notices,
                                    )
                                    raise AgyError(
                                        "QUOTA",
                                        "AGY 额度不足或容量不可用，请检查官方 CLI。",
                                    )
                                raise AgyError("EMPTY", "AGY 没有返回正文。")
                            if stripped_notices:
                                logger.warning(
                                    "AGY 已过滤 %s 条上游容量告警，仅保留正文。",
                                    stripped_notices,
                                )
                            assistant_hash = digest(
                                {"role": "assistant", "content": answer}
                            )
                            session.host_expected = request.hashes + [assistant_hash]
                            if decision.reuse and session.sent_window is not None:
                                session.sent_window.hashes.extend(
                                    [request.hashes[-1], assistant_hash]
                                )
                                session.sent_window.window_hash = digest(
                                    session.sent_window.hashes
                                )
                            else:
                                sent_hashes = request.hashes[omitted:] + [assistant_hash]
                                session.sent_window = SentWindow(
                                    hashes=sent_hashes,
                                    omitted_messages=omitted,
                                    window_hash=digest(sent_hashes),
                                )
                            session.turn_counter = turn_index
                            session.had_transient = bool(
                                context.structured
                                and context.transient_data not in (None, "")
                                and not context.clear_transient
                            )
                            if provider_id:
                                self.failure_guard.record_success((provider_id, model))
                            claimed_half_open = None
                            completed = True
                            local_meter.finish(status="ok")
                            event = Event("done", text=answer, result=result)
                        yield event
                finally:
                    await turn.aclose()
            except (asyncio.CancelledError, GeneratorExit) as exc:
                cancelled = True
                failure_exc = exc
                raise
            except BaseException as exc:
                failure_exc = exc
                raise
            finally:
                # Partial/cancelled turns never become a reusable history prefix.
                short_circuited = isinstance(failure_exc, AgyError) and failure_exc.no_retry
                if short_circuited:
                    self.failure_guard.release_probe(claimed_half_open)
                elif not completed:
                    latest_usage = getattr(session.client, "latest_usage", None)
                    if latest_usage is not None:
                        latest_source = (
                            getattr(session.client, "latest_usage_source", None)
                            or "acp_error"
                        )
                        local_meter.observe_usage(
                            {"usage": latest_usage},
                            source=latest_source,
                        )
                    error_code = (
                        failure_exc.code
                        if isinstance(failure_exc, AgyError)
                        else "CANCELLED"
                        if cancelled
                        else "UPSTREAM"
                    )
                    if not temporary and not self.stopped:
                        if cancelled or error_code == "CANCELLED":
                            tombstone_reason = "abort"
                        elif error_code in {"TRANSPORT", "STARTUP_EXIT"} or (
                            session.client is not None
                            and not getattr(session.client, "alive", True)
                        ):
                            tombstone_reason = "process_dead"
                        else:
                            tombstone_reason = "previous_error"
                        self.telemetry.record_eviction(key, tombstone_reason)
                    if provider_id and not cancelled:
                        self.failure_guard.record_failure(
                            (provider_id, model),
                            request_fingerprint,
                            error_code,
                            origin_turn_id=local_meter.turn_id,
                            retry_after=getattr(failure_exc, "retry_after", None),
                            logical_request_id=logical_request_id,
                            session_key=key,
                            empty_ttl=self.settings.empty_dedupe_ttl,
                            quota_ttl=self.settings.quota_cooldown,
                            continuation=continuation,
                        )
                    else:
                        self.failure_guard.release_probe(claimed_half_open)
                    local_meter.finish(
                        status="cancelled" if cancelled else "error",
                        error_code=error_code,
                    )
                    await session.close()

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(30, self.settings.idle_ttl))
                async with self.pool_lock:
                    now = time.monotonic()
                    for key, session in list(self.sessions.items()):
                        if (
                            not session.busy
                            and now - session.used_at >= self.settings.idle_ttl
                        ):
                            await session.close()
                            del self.sessions[key]
                            self.telemetry.record_eviction(key, "idle_expired")
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        self.stopped = True
        await asyncio.gather(*(turn.close() for turn in list(self.turns.values())))
        self.turns.clear()
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        async with self.pool_lock:
            await asyncio.gather(
                *(session.close() for session in self.sessions.values())
            )
            self.sessions.clear()
        # Busy callers own their final release; avoid deleting their files during
        # cleanup races. Empty runtime directories can always be removed safely.
        try:
            self.root.rmdir()
        except OSError:
            pass


async def tool_events(client, prompt, broker):
    """Multiplex MCP pending calls without blocking ACP's stdout reader."""
    source = client.generate(prompt)
    next_event = next_call = None
    try:
        next_event = asyncio.create_task(anext(source))
        if broker:
            next_call = asyncio.create_task(broker.ready.get())
        while True:
            tasks = [t for t in (next_event, next_call) if t]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if next_event.done():
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                yield event
                next_event = asyncio.create_task(anext(source))
            elif next_call and next_call.done():
                call = next_call.result()
                if call.id in broker.pending and not call.future.done():
                    yield Event(
                        "tools",
                        result={"call": call, "broker": broker, "client": client},
                    )
                next_call = asyncio.create_task(broker.ready.get())
    finally:
        tasks = [t for t in (next_event, next_call) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await source.aclose()
