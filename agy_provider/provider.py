"""AstrBot chat provider adapter. All model traffic stays on local stdio."""

import asyncio
import copy
import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from weakref import WeakSet

from astrbot.api import logger
from astrbot.api.provider import LLMResponse, Provider
from astrbot.core.provider.entities import TokenUsage
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .config import Settings
from .errors import AgyError
from .media import prepare_media
from .messages import digest, normalize
from .paths import plugin_data_root
from .sessions import Runtime
from .telemetry import TurnMeter
from .tool_turn import chat_events
from .tools import ToolSnapshot

PROVIDER_TYPE = "agy_acp"
OWNER = "sl251/astrbot_plugin_antigravity_acp"
DEFAULT_CONFIG = {
    "id": PROVIDER_TYPE,
    "type": PROVIDER_TYPE,
    "provider": PROVIDER_TYPE,
    "provider_type": "chat_completion",
    "enable": False,
    "key": [""],
    "model": "",
    "modalities": ["text"],
    "custom_extra_body": {
        "agy_thought_level": "default",
        "agy_tools_mode": "inherit",
    },
}
_settings = Settings()
_instances: WeakSet = WeakSet()


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings


async def terminate_all() -> None:
    # Also includes temporary model-list providers made by the dashboard.
    await asyncio.gather(*(instance.terminate() for instance in list(_instances)))


def resolve_tools_mode(
    plugin_enabled: bool, provider_mode: str, tool_choice: str
) -> bool:
    """Resolve effective tool usage; plugin enable_tools is the master switch."""
    if not isinstance(provider_mode, str) or provider_mode not in {
        "inherit",
        "chat",
        "tools",
    }:
        raise AgyError("CONFIG", "agy_tools_mode 必须为 inherit/chat/tools。")
    if not plugin_enabled or provider_mode == "chat" or tool_choice == "none":
        return False
    return True


def active_instances() -> list:
    return [inst for inst in list(_instances) if not inst.runtime.stopped]


def instance_cost_snapshots() -> list[dict]:
    rows = []
    for inst in active_instances():
        extra = inst.provider_config.get("custom_extra_body") or {}
        tools_mode = extra.get("agy_tools_mode", "inherit")
        if tools_mode not in {"inherit", "chat", "tools"}:
            tools_mode = "invalid"
        effective_mode = (
            "tools"
            if inst.settings.enable_tools and tools_mode in {"inherit", "tools"}
            else "chat"
        )
        rows.append(
            {
                "provider_id": inst.provider_id,
                "model": inst.get_model(),
                "tools_mode": tools_mode,
                "effective_mode": effective_mode,
                "summary": inst.runtime.telemetry.summarize(),
                "pool": inst.runtime.pool_stats(),
            }
        )
    return rows


def token_usage(result: dict) -> TokenUsage | None:
    raw = result.get("usage")
    if not isinstance(raw, dict):
        return None

    # AGY reports disjoint uncached input/cache read/cache write buckets.
    # AstrBot has no cache-write bucket; account for writes in input_other.
    def count(name):
        value = raw.get(name)
        return value if type(value) is int and value >= 0 else None

    uncached, output = count("inputTokens"), count("outputTokens")
    cached = count("cachedReadTokens") or 0
    written = count("cachedWriteTokens") or 0
    if uncached is None or output is None:
        return None
    return TokenUsage(
        input_other=uncached + written, input_cached=cached, output=output
    )


async def abortable(source: AsyncIterator, signal: asyncio.Event | None):
    stop = asyncio.create_task(signal.wait()) if signal is not None else None
    next_item = None
    try:
        while True:
            if signal is not None and signal.is_set():
                raise asyncio.CancelledError
            next_item = asyncio.create_task(anext(source))
            if stop is not None:
                done, _ = await asyncio.wait(
                    (next_item, stop), return_when=asyncio.FIRST_COMPLETED
                )
                if stop in done:
                    raise asyncio.CancelledError
            try:
                item = await next_item
            except StopAsyncIteration:
                return
            finally:
                if next_item.done():
                    next_item = None
            yield item
    finally:
        tasks = [task for task in (stop, next_item) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await source.aclose()


async def await_or_abort(operation, signal):
    task = asyncio.create_task(operation)
    stop = asyncio.create_task(signal.wait()) if signal is not None else None
    try:
        if signal is not None and signal.is_set():
            raise asyncio.CancelledError
        if stop is not None:
            done, _ = await asyncio.wait(
                (task, stop), return_when=asyncio.FIRST_COMPLETED
            )
            if stop in done:
                raise asyncio.CancelledError
        return await task
    finally:
        tasks = [t for t in (task, stop) if t]
        for child in tasks:
            child.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class AgyProvider(Provider):
    agy_plugin_owner = OWNER

    def __init__(self, provider_config: dict, provider_settings: dict):
        super().__init__(provider_config, provider_settings)
        self.settings = _settings
        self.provider_id = str(provider_config.get("id", PROVIDER_TYPE))
        self.set_model(
            str(
                provider_config.get("model")
                or (provider_config.get("model_config") or {}).get("model", "")
            )
        )
        namespace = digest(self.provider_id)[:16]
        root = plugin_data_root(Path(get_astrbot_plugin_data_path())) / namespace
        self.runtime = Runtime(self.settings, root)
        self.warned_tools = False
        _instances.add(self)

    def get_current_key(self) -> str:
        return ""

    def get_keys(self) -> list[str]:
        return [""]

    def set_key(self, key: str) -> None:
        if key:
            raise AgyError("CONFIG", "AGY 使用官方 CLI 登录态，不接受 API Key。")

    async def initialize(self) -> None:
        # Lazy launch: a missing CLI must not prevent AstrBot from starting.
        pass

    async def get_models(self) -> list[str]:
        return await self.runtime.get_models()

    async def test(self) -> None:
        # Explicit WebUI connectivity test includes a real, small generation.
        await self.text_chat(prompt="Reply PONG only.")

    async def terminate(self) -> None:
        await self.runtime.close()

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice="auto",
        request_max_retries=None,
        **kwargs,
    ) -> LLMResponse:
        final = None
        stream = self.text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            extra_user_content_parts=extra_user_content_parts,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            **kwargs,
        )
        try:
            async for response in stream:
                if not response.is_chunk:
                    final = response
        finally:
            await stream.aclose()
        if final is None:
            raise AgyError("EMPTY", "AGY 未返回最终响应。")
        return final

    async def text_chat_stream(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice="auto",
        request_max_retries=None,
        **kwargs,
    ) -> AsyncIterator[LLMResponse]:
        conversation_id = kwargs.get("conversation_id")
        key = (
            digest([session_id, conversation_id])
            if session_id or conversation_id
            else None
        )
        signal = kwargs.get("abort_signal")
        approver = kwargs.get("agy_tool_approver")
        logical_request_id = kwargs.get("agy_request_id") or kwargs.get("request_id")
        if not isinstance(logical_request_id, str) or not logical_request_id:
            logical_request_id = None

        extra = self.provider_config.get("custom_extra_body") or {}
        provider_mode = extra.get("agy_tools_mode", "inherit")
        selected = str(model or self.get_model() or "")
        effort_raw = kwargs.get(
            "agy_thought_level", extra.get("agy_thought_level", "default")
        )
        effort = (
            self.settings.thought_level if effort_raw == "default" else effort_raw
        )

        existing_turn = self.runtime.turns.get(key) if key is not None else None
        meter: TurnMeter | None = existing_turn.meter if existing_turn else None
        is_existing_turn = existing_turn is not None
        claimed_half_open: tuple[str, str] | None = None
        snapshot = ToolSnapshot(None)
        request = None
        continuation = False
        request_fingerprint: str | None = None

        try:
            if approver is not None and not inspect.iscoroutinefunction(approver):
                raise AgyError(
                    "TOOL_CONTEXT", "agy_tool_approver 必须为异步宿主审批回调。"
                )
            if audio_urls or kwargs.get("video_urls") or kwargs.get("pdf_urls"):
                raise AgyError("UNSUPPORTED", "音频、视频和 PDF 输入尚未支持。")
            if signal is not None and not isinstance(signal, asyncio.Event):
                raise AgyError("CONFIG", "abort_signal 必须是 asyncio.Event。")
            if tool_choice not in ("auto", "none"):
                raise AgyError(
                    "TOOL_CHOICE", "只支持 auto/none；required 和指定函数不可可靠实现。"
                )
            tools_for_prompt = resolve_tools_mode(
                self.settings.enable_tools, provider_mode, tool_choice
            )
            tools_allowed_in_history = resolve_tools_mode(
                self.settings.enable_tools, provider_mode, "auto"
            )
            if not tools_allowed_in_history and tool_calls_result:
                raise AgyError("TOOLS_DISABLED", "当前实例或插件未启用工具能力。")
            if func_tool and not self.settings.enable_tools and not self.warned_tools:
                logger.warning("AGY enable_tools=false，未向 CLI 提供宿主工具。")
                self.warned_tools = True
            snapshot = ToolSnapshot(func_tool if tools_for_prompt else None)
            if snapshot.skipped:
                logger.warning(
                    f"AGY 工具桥跳过 {len(snapshot.skipped)} 个不兼容工具 schema："
                    + "; ".join(snapshot.skipped)
                )
            if snapshot.tools and (key is None or signal is None):
                raise AgyError(
                    "TOOL_CONTEXT", "工具调用需要宿主会话身份及 abort_signal 生命周期。"
                )
            raw_contexts = self._ensure_message_to_dicts(contexts) or []
            raw_contexts, parts = await await_or_abort(
                prepare_media(
                    raw_contexts,
                    prompt,
                    extra_user_content_parts,
                    image_urls,
                    self.settings,
                ),
                signal,
            )
            if tool_calls_result:
                results = (
                    tool_calls_result
                    if isinstance(tool_calls_result, list)
                    else [tool_calls_result]
                )
                # Direct Provider API may pass the original prompt plus closed
                # ToolCallsResult units. Native runner already uses contexts-only.
                if prompt is not None:
                    base = normalize(
                        raw_contexts,
                        prompt,
                        system_prompt,
                        parts,
                        self.settings.history_bytes,
                        tools=True,
                        images=self.settings.enable_images,
                        trusted_parts=kwargs.get("agy_context_parts"),
                        reuse_mode=self.settings.session_reuse_mode,
                    )
                    raw_contexts = base.messages
                    system_prompt = base.system
                    prompt = None
                    parts = None
                for unit in results:
                    if not hasattr(unit, "to_openai_messages"):
                        raise AgyError(
                            "TOOL_HISTORY",
                            "tool_calls_result 必须为宿主 ToolCallsResult。",
                        )
                    exchange = unit.to_openai_messages()
                    ids = {
                        m.get("tool_call_id")
                        for m in exchange
                        if m.get("role") == "tool"
                    }
                    if any(
                        m.get("tool_call_id") in ids
                        for m in raw_contexts
                        if m.get("role") == "tool"
                    ):
                        raise AgyError(
                            "TOOL_HISTORY", "工具结果已在 contexts 中，不能重复提交。"
                        )
                    raw_contexts.extend(exchange)
            continuation = bool(
                raw_contexts
                and raw_contexts[-1].get("role") == "tool"
                and prompt is None
            )
            request = normalize(
                raw_contexts,
                prompt,
                system_prompt,
                parts,
                self.settings.history_bytes,
                tools=tools_allowed_in_history,
                images=self.settings.enable_images,
                continuation=continuation,
                trusted_parts=kwargs.get("agy_context_parts"),
                reuse_mode=self.settings.session_reuse_mode,
            )
            if not selected:
                raise AgyError(
                    "MODEL", "请先获取 Gemini 模型列表并在 AstrBot 中选择具体模型。"
                )
            if not isinstance(effort, str) or effort not in {
                "default",
                "low",
                "medium",
                "high",
            }:
                raise AgyError(
                    "CONFIG", "agy_thought_level 必须为 default/low/medium/high。"
                )

            request_fingerprint = digest(
                [
                    self.provider_id,
                    key or "",
                    request.hashes,
                    request.system_hash,
                    request.system_structured,
                    request.transient_hash,
                    selected,
                    effort,
                    snapshot.fingerprint,
                ]
            )
            if meter is None:
                meter = self.runtime.telemetry.start_turn(
                    provider=self.provider_id,
                    session_key_raw=key,
                    mode="tools" if snapshot.tools else "chat",
                    model=selected,
                    effort=effort,
                    system_bytes=request.system_bytes,
                    transient_bytes=len((request.transient_data or "").encode()),
                    tool_schema_bytes=snapshot.schema_bytes,
                )

            if not continuation and not is_existing_turn:
                guard_check = await self.runtime.failure_guard.check(
                    (self.provider_id, selected),
                    request_fingerprint,
                    logical_request_id,
                    session_key=key,
                    empty_ttl=self.settings.empty_dedupe_ttl,
                    quota_ttl=self.settings.quota_cooldown,
                    continuation=False,
                )
                if not guard_check.allowed:
                    meter.origin_turn_id = guard_check.origin_turn_id
                    meter.finish(
                        status="short_circuited", error_code=guard_check.code
                    )
                    raise AgyError(
                        guard_check.code or "QUOTA",
                        f"AGY 短期抑制/冷却（origin_turn_id={guard_check.origin_turn_id}，"
                        f"请约 {guard_check.retry_after} 秒后再试）。",
                        retry_after=guard_check.retry_after,
                        origin_turn_id=guard_check.origin_turn_id,
                        no_retry=True,
                    )
                claimed_half_open = guard_check.half_open_scope
        except BaseException as exc:
            self.runtime.failure_guard.release_probe(claimed_half_open)
            cancelled = isinstance(exc, asyncio.CancelledError)
            code = exc.code if isinstance(exc, AgyError) else ("CANCELLED" if cancelled else "CONFIG")
            if meter is None:
                meter = self.runtime.telemetry.start_turn(
                    provider=self.provider_id,
                    session_key_raw=key,
                    mode="tools" if snapshot.tools else "chat",
                    model=selected,
                    effort=effort if isinstance(effort, str) else "default",
                )
            if not (isinstance(exc, AgyError) and exc.no_retry):
                meter.finish(
                    status="cancelled" if cancelled else "error", error_code=code
                )
            pending = self.runtime.turns.pop(key, None)
            if pending:
                await pending.close()
            raise

        if snapshot.tools or key in self.runtime.turns or continuation:
            source = chat_events(
                self.runtime,
                request,
                key,
                selected,
                effort,
                snapshot,
                signal,
                approver,
                meter=meter,
                provider_id=self.provider_id,
                logical_request_id=logical_request_id,
                request_fingerprint=request_fingerprint,
                claimed_half_open=claimed_half_open,
            )
        else:
            source = self.runtime.stream(
                request,
                key,
                selected,
                effort,
                meter=meter,
                provider_id=self.provider_id,
                logical_request_id=logical_request_id,
                request_fingerprint=request_fingerprint,
                continuation=False,
                claimed_half_open=claimed_half_open,
            )
        events = abortable(source, signal)
        text_parts, thoughts = [], []
        handed_off = False
        done_seen = False
        exit_status = "cancelled"
        exit_code: str | None = "CANCELLED"
        try:
            async for event in events:
                if event.kind == "text":
                    text_parts.append(event.text)
                    yield LLMResponse(
                        role="assistant", completion_text=event.text, is_chunk=True
                    )
                elif event.kind == "thought":
                    thoughts.append(event.text)
                    yield LLMResponse(
                        role="assistant",
                        completion_text="",
                        reasoning_content=event.text,
                        is_chunk=True,
                    )
                elif event.kind == "history_trimmed":
                    logger.warning(
                        "AGY 历史重建省略了 %s 条较旧记录（字节预算）。",
                        (event.result or {}).get("omitted_messages", 0),
                    )
                elif event.kind == "tools":
                    handed_off = True
                    call = event.result["call"]
                    yield LLMResponse(
                        role="assistant",
                        completion_text="".join(text_parts),
                        reasoning_content="".join(thoughts) or None,
                        tools_call_name=[call.name],
                        tools_call_args=[copy.deepcopy(call.arguments)],
                        tools_call_ids=[call.id],
                        is_chunk=False,
                    )
                elif event.kind == "done":
                    done_seen = True
                    exit_status = "ok"
                    exit_code = None
                    result = event.result or {}
                    if result.get("stopReason") == "max_tokens":
                        logger.warning(
                            "AGY 达到输出上限，本轮返回已有内容，未自动续写。"
                        )
                    yield LLMResponse(
                        role="assistant",
                        completion_text=event.text or "".join(text_parts),
                        reasoning_content="".join(thoughts) or None,
                        usage=token_usage(result),
                        is_chunk=False,
                    )
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                exit_status = "cancelled"
                exit_code = "CANCELLED"
            elif isinstance(exc, AgyError):
                exit_status = "error"
                exit_code = exc.code
            else:
                exit_status = "error"
                exit_code = "UPSTREAM"
            raise
        finally:
            await events.aclose()
            if not handed_off and not done_seen and not meter.finished:
                self.runtime.failure_guard.release_probe(claimed_half_open)
                meter.finish(status=exit_status, error_code=exit_code)
