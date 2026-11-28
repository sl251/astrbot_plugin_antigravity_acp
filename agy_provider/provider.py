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

from .builtin_tools import BuiltinDenied, builtin_tools_configured
from .config import Settings
from .errors import AgyError
from .host_compat import max_steps_notice
from .media import ImageFingerprints, prepare_media
from .messages import digest, has_tool_image_markers, normalize, tool_continuation_tail
from .paths import plugin_data_root
from .sessions import Runtime
from .system_prompt import filter_host_capabilities
from .telemetry import TurnMeter, provider_tag, safe_context, safe_label
from .turn_history import HISTORY_ID, instance_tag
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


def _is_temporary(part):
    if isinstance(part, dict):
        return part.get("_no_save") is True
    return getattr(part, "_no_save", False) is True


def _temporary_extra_indices(content, extras):
    """Match AstrBot's assembled [prompt?, *text_extras, *images] layout."""
    if not isinstance(extras, list) or not any(_is_temporary(p) for p in extras):
        return []
    blocks = [p.model_dump() if hasattr(p, "model_dump") else p for p in extras]
    if not all(isinstance(p, dict) and p.get("type") == "text" for p in blocks):
        return []

    texts = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            break
        texts.append(part.get("text"))
    if any(
        not isinstance(p, dict) or p.get("type") not in {"image", "image_url"}
        for p in content[len(texts) :]
    ):
        return []
    offset = len(texts) - len(blocks)
    if offset not in (0, 1) or texts[offset:] != [p.get("text") for p in blocks]:
        return []
    return [offset + i for i, p in enumerate(extras) if _is_temporary(p)]


def _restore_user_text_markers(message, original, extras):
    """Recover persistence flags lost when AstrBot dumps a Message to dict."""
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return message

    if isinstance(original, dict):
        # The modality filter serializes Message objects before calling us,
        # but the runner also passes the original extra_user_content_parts.
        if original.get("role") != "user" or any(_is_temporary(p) for p in content):
            return message
        marked = _temporary_extra_indices(content, extras)
    else:
        source = getattr(original, "content", None)
        if (
            getattr(original, "role", None) != "user"
            or not isinstance(source, list)
            or len(source) != len(content)
        ):
            return message
        marked = [i for i, p in enumerate(source) if _is_temporary(p)]

    if not marked:
        return message
    restored = list(content)
    for index in marked:
        part = content[index]
        if isinstance(part, dict) and part.get("type") == "text":
            restored[index] = {**part, "_no_save": True}
    return {**message, "content": restored}


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


def instance_monitor_snapshots(manager) -> list[dict]:
    """Synchronous snapshot on AstrBot's event loop: no await, I/O or AGY calls.

    Only the manager's current instance counts. WeakSet also contains temporary
    model-list providers and replaced instances, so it is unsuitable here.
    """
    rows = []
    for config in list(manager.providers_config):
        merged = manager.get_merged_provider_config(config)
        if merged.get("type") != PROVIDER_TYPE:
            continue
        provider_id = config.get("id")
        inst = manager.inst_map.get(provider_id)
        active = (isinstance(inst, AgyProvider) and not inst.runtime.stopped
                  and inst.provider_id == provider_id)
        settings = inst.settings if active else _settings
        running_config = inst.provider_config if active else merged
        tools_mode = (running_config.get("custom_extra_body") or {}).get("agy_tools_mode", "inherit")
        if tools_mode not in {"inherit", "chat", "tools"}:
            tools_mode = "invalid"
        effective_tools = settings.enable_tools and tools_mode in {"inherit", "tools"}
        model = safe_label(inst.get_model() if active else
                           merged.get("model") or (merged.get("model_config") or {}).get("model", ""))
        latest_context = {
            **safe_context({}), "source": "not_reported",
            "scope": "latest_live_session", "model": model,
        }
        if active:
            # Historical turn records are not a live context. Closed/dead,
            # replaced and different-model sessions cannot supply this sample.
            samples = []
            for session in inst.runtime.sessions.values():
                if session.model != model or session.client is None or not session.client.alive:
                    continue
                sample = safe_context(session.client.context_usage.summary())
                if sample["observed_at"] is not None:
                    samples.append(sample)
            if samples:
                latest_context.update(max(samples, key=lambda s: s["observed_at"]))
                latest_context["source"] = "acp_usage_update"
        history_now = inst.runtime.telemetry.clock() if active else None
        rows.append({
            "provider_id": safe_label(provider_id), "provider_tag": provider_tag(provider_id), "model": model,
            "enabled": bool(merged.get("enable")),
            "tools_mode": tools_mode, "effective_mode": "tools" if effective_tools else "chat",
            "tools_enabled": bool(effective_tools),
            "runtime_state": "active" if active else "configured",
            "pool": inst.runtime.pool_stats() if active else None,
            "summary": inst.runtime.telemetry.monitor_summary(now=history_now) if active else None,
            "history_storage": inst.runtime.telemetry.history_storage if active else None,
            "latest_context": latest_context,
            "recent_turns": inst.runtime.telemetry.safe_recent_turns(20, now=history_now) if active else [],
        })
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
        self.runtime.telemetry.restore_history(
            instance_tag(provider_config.get("id"), provider_config.get(HISTORY_ID))
        )
        self.image_fingerprints = ImageFingerprints()
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
        stream = self._chat_responses(
            False,
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
        responses = self._chat_responses(
            True, prompt, session_id, image_urls, audio_urls, func_tool, contexts,
            system_prompt, tool_calls_result, model, extra_user_content_parts,
            tool_choice, request_max_retries, **kwargs,
        )
        try:
            async for response in responses:
                yield response
        finally:
            await responses.aclose()

    async def _chat_responses(
        self,
        streaming,
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
        diagnostic_model = selected if selected == self.get_model() or selected in self.runtime.model_cache else ""
        effort_raw = kwargs.get(
            "agy_thought_level", extra.get("agy_thought_level", "default")
        )
        effort = (
            self.settings.thought_level if effort_raw == "default" else effort_raw
        )

        existing_turn = self.runtime.turns.get(key) if key is not None else None
        owns_existing_turn = (
            existing_turn is not None
            and signal is not None
            and signal is existing_turn.signal
        )
        meter: TurnMeter | None = existing_turn.meter if owns_existing_turn else None
        is_existing_turn = owns_existing_turn
        claimed_half_open: tuple[str, str] | None = None
        snapshot = ToolSnapshot(None)
        request = None
        continuation = bool(tool_calls_result)
        final_notice = max_steps_notice(self, signal) if func_tool is None else None
        finalizing = finalization_has_results = False
        prior_delivery = False
        request_fingerprint: str | None = None

        try:
            raw_contexts = self._ensure_message_to_dicts(contexts) or []
            raw_tail = tool_continuation_tail(raw_contexts, stage="raw")
            continuation = continuation or bool(
                prompt is None and raw_contexts
                and (raw_tail is not None or raw_contexts[-1].get("role") == "tool")
            )
            if (
                prompt is None and raw_tail is None and len(raw_contexts) >= 2
                and has_tool_image_markers(raw_contexts[-1], raw_contexts[-2])
            ):
                # A malformed image return is still a tool-result failure,
                # including after the original turn has already been retired.
                continuation = True
                raise AgyError("TOOL_HISTORY", "工具图片消息未通过原始格式校验。")
            if raw_contexts and raw_tail is None and prompt is None and not tool_calls_result:
                latest = _restore_user_text_markers(
                    raw_contexts[-1], contexts[-1], extra_user_content_parts
                )
                if latest is not raw_contexts[-1]:
                    raw_contexts = [*raw_contexts[:-1], latest]
            finalization_has_results = final_notice is not None and any(
                message.get("role") == "tool" for message in raw_contexts
            )
            if prompt is None and raw_tail is not None and raw_tail.has_images:
                original_parts = getattr(contexts[-1], "content", None)
                if isinstance(original_parts, list) and any(_is_temporary(p) for p in original_parts):
                    raise AgyError("TOOL_HISTORY", "工具图片标注不能包含临时文本。")
            if (
                owns_existing_turn and final_notice is None and not tool_calls_result
                and prompt is None and raw_tail is None
            ):
                # A failed raw exchange must not reach URL or local file loading.
                raise AgyError("TOOL_HISTORY", "工具续轮缺少唯一、完整且匹配的已审计调用/结果。")
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
            allow_builtins = bool(tools_for_prompt and builtin_tools_configured(self.settings))
            if snapshot.skipped:
                logger.warning(
                    f"AGY 工具桥跳过 {len(snapshot.skipped)} 个不兼容工具 schema："
                    + "; ".join(snapshot.skipped)
                )
            if snapshot.tools and (key is None or signal is None):
                raise AgyError(
                    "TOOL_CONTEXT", "工具调用需要宿主会话身份及 abort_signal 生命周期。"
                )
            raw_contexts, parts = await await_or_abort(
                prepare_media(
                    raw_contexts,
                    prompt,
                    extra_user_content_parts,
                    image_urls,
                    self.settings, cache=self.image_fingerprints, scope=key,
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
            tail = tool_continuation_tail(raw_contexts) if prompt is None else None
            if tail is not None and tail.has_images and (
                raw_tail is None or not raw_tail.has_images
            ):
                continuation = True
                raise AgyError("TOOL_HISTORY", "工具图片消息未通过原始格式校验。")
            continuation = tail is not None or continuation
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
            finalizing = final_notice is not None and (
                continuation or request.messages[-1]["content"] == final_notice
            )
            if finalizing:
                allow_builtins = False
                # The retained prompt owns its original meter until it closes.
                prior_delivery = meter is not None and meter.replay_blocked
                meter = None
            # Normalize validates any trusted split first; filtering its stable
            # system cannot promote a previously invalid split into trusted data.
            request.system = filter_host_capabilities(request.system)
            request.system_bytes = len(request.system.encode())
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
                    mode="tools" if (snapshot.tools or allow_builtins) else "chat",
                    model=diagnostic_model,
                    effort=effort,
                    system_bytes=request.system_bytes,
                    system_hash8=(request.system_hash or "")[:8],
                    transient_bytes=len((request.transient_data or "").encode()),
                    tool_schema_bytes=snapshot.schema_bytes,
                )
                meter.no_fallback = prior_delivery

            if logical_request_id and meter.logical_request_tag is None:
                meter.logical_request_tag = self.runtime.telemetry.opaque_tag(logical_request_id)

            if continuation or (finalizing and finalization_has_results):
                meter.tool_result_supplied = True
            if not meter.tool_result_supplied and not is_existing_turn:
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
                    model=diagnostic_model,
                    effort=effort if isinstance(effort, str) and effort in {"low", "medium", "high", "default"} else "",
                )
            if continuation or finalization_has_results:
                meter.tool_result_supplied = True
            meter.no_fallback |= prior_delivery
            replay_blocked = meter.replay_blocked or prior_delivery
            if replay_blocked and isinstance(exc, AgyError):
                exc.no_retry = exc.no_fallback = True
            if not meter.finished:
                meter.finish(
                    status="cancelled" if cancelled else "error", error_code=code
                )
            if owns_existing_turn and self.runtime.turns.get(key) is existing_turn:
                self.runtime.turns.pop(key, None)
                await existing_turn.close()
            if replay_blocked and isinstance(exc, Exception) and not isinstance(exc, AgyError):
                raise AgyError("UPSTREAM", "工具交接后失败，已停止本轮。", no_retry=True, no_fallback=True) from None
            raise

        if key is not None and not owns_existing_turn and not meter.tool_result_supplied:
            # 同一会话存在其他调用方持有的进行中工具轮时，排队等待其结束而不误杀原会话。
            if (
                self.runtime.callers
                >= self.settings.max_pending + self.settings.max_concurrent
            ):
                self.runtime.failure_guard.release_probe(claimed_half_open)
                meter.finish(status="error", error_code="BUSY")
                raise AgyError("BUSY", "AGY 等待队列已满，请稍后再试。")
            try:
                async with asyncio.timeout(self.settings.queue_timeout):
                    while True:
                        foreign = self.runtime.turns.get(key)
                        if foreign is None or (
                            signal is not None and signal is foreign.signal
                        ):
                            if key not in self.runtime.entry_locks:
                                break
                            await await_or_abort(asyncio.sleep(0.01), signal)
                        else:
                            await await_or_abort(foreign.done_event.wait(), signal)
            except TimeoutError:
                self.runtime.failure_guard.release_probe(claimed_half_open)
                meter.finish(status="error", error_code="BUSY")
                raise AgyError(
                    "BUSY", "等待当前会话工具轮完成超时，请稍后再试。"
                ) from None
            except BaseException as exc:
                self.runtime.failure_guard.release_probe(claimed_half_open)
                cancelled = isinstance(exc, asyncio.CancelledError)
                meter.finish(
                    status="cancelled" if cancelled else "error",
                    error_code="CANCELLED" if cancelled else "BUSY",
                )
                raise

        if snapshot.tools or owns_existing_turn or continuation or finalizing:
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
                allow_builtins=allow_builtins,
                finalizing=finalizing,
            )
        else:
            source = self.runtime.stream(
                request,
                key,
                selected,
                effort,
                snapshot,
                meter=meter,
                provider_id=self.provider_id,
                logical_request_id=logical_request_id,
                request_fingerprint=request_fingerprint,
                continuation=False,
                claimed_half_open=claimed_half_open,
                allow_builtins=allow_builtins,
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
                    meter.observe_chunk(event.kind, event.text, streaming=streaming)
                    text_parts.append(event.text)
                    yield LLMResponse(
                        role="assistant", completion_text=event.text, is_chunk=True
                    )
                elif event.kind == "thought":
                    meter.observe_chunk(event.kind, event.text, streaming=streaming)
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
                    handed_off = meter.tool_handed_off = True
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
            if not done_seen and not handed_off:
                raise AgyError("EMPTY", "AGY 未返回最终响应。")
        except BuiltinDenied as exc:
            exit_status, exit_code = "error", exc.code
            if meter.replay_blocked or prior_delivery:
                exc.no_retry = exc.no_fallback = True
                raise
            # Runtime retired the failed native session and recorded its cost.
            # Deliver a local terminal message; a new user turn can proceed.
            notice = getattr(exc, "user_notice", None)
            if notice:
                # With tools disabled no AstrBot tool is offered through this plugin.
                notice += ("你可以继续提问。" if exc.code == "TOOLS_DISABLED"
                           else "你可以继续提问，或使用 AstrBot 已启用的工具。")
            else:
                notice = "本轮请求了受限的 CLI 内置工具，无法继续取得结果。你可以继续提问，或使用 AstrBot 已启用的工具。"
            done_seen = True
            if streaming:
                yield LLMResponse(role="assistant", completion_text=notice, is_chunk=True)
            yield LLMResponse(role="assistant", completion_text=notice, is_chunk=False)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                exit_status = "cancelled"
                exit_code = "CANCELLED"
            elif isinstance(exc, AgyError):
                exit_status = "error"
                exit_code = exc.code
                # Surface an explicit failure after partial delivery. Neither
                # this provider nor callers honoring no_retry may replay it.
                if meter.replay_blocked or prior_delivery:
                    exc.no_retry = True
                    exc.no_fallback = True
            else:
                exit_status = "error"
                exit_code = "UPSTREAM"
                if isinstance(exc, Exception) and (meter.replay_blocked or prior_delivery):
                    raise AgyError(
                        "UPSTREAM", "本轮已输出内容或交接工具后失败；已停止，禁止自动重放。",
                        no_retry=True, no_fallback=True,
                    ) from None
            raise
        finally:
            try:
                await events.aclose()
            except Exception as exc:
                if meter.replay_blocked or prior_delivery or done_seen:
                    if isinstance(exc, AgyError):
                        exc.no_retry = exc.no_fallback = True
                        raise
                    raise AgyError("UPSTREAM", "结束已交付回合时失败。", no_retry=True, no_fallback=True) from None
                raise
            if not handed_off and not done_seen and not meter.finished:
                self.runtime.failure_guard.release_probe(claimed_half_open)
                meter.finish(status=exit_status, error_code=exit_code)
