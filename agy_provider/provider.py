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
from .sessions import Runtime
from .tool_turn import chat_events
from .tools import ToolSnapshot

PROVIDER_TYPE = "agy_acp"
OWNER = "sl251/astrbot_plugin_agy_provider"
DEFAULT_CONFIG = {
    "id": PROVIDER_TYPE,
    "type": PROVIDER_TYPE,
    "provider": PROVIDER_TYPE,
    "provider_type": "chat_completion",
    "enable": False,
    "key": [""],
    "model": "",
    "modalities": ["text"],
    "custom_extra_body": {"agy_thought_level": "default"},
}
_settings = Settings()
_instances: WeakSet = WeakSet()


def configure(settings: Settings) -> None:
    global _settings
    _settings = settings


async def terminate_all() -> None:
    # Also includes temporary model-list providers made by the dashboard.
    await asyncio.gather(*(instance.terminate() for instance in list(_instances)))


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
        self.set_model(
            str(
                provider_config.get("model")
                or (provider_config.get("model_config") or {}).get("model", "")
            )
        )
        namespace = digest(str(provider_config.get("id", PROVIDER_TYPE)))[:16]
        root = (
            Path(get_astrbot_plugin_data_path())
            / "astrbot_plugin_agy_provider"
            / namespace
        )
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
            if not self.settings.enable_tools and tool_calls_result:
                raise AgyError("TOOLS_DISABLED", "工具能力尚未启用。")
            if func_tool and not self.settings.enable_tools and not self.warned_tools:
                logger.warning("AGY enable_tools=false，未向 CLI 提供宿主工具。")
                self.warned_tools = True
            snapshot = ToolSnapshot(
                func_tool
                if self.settings.enable_tools and tool_choice == "auto"
                else None
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
                tools=self.settings.enable_tools,
                images=self.settings.enable_images,
                continuation=continuation,
            )
            selected = model or self.get_model()
            if not selected:
                raise AgyError(
                    "MODEL", "请先获取 Gemini 模型列表并在 AstrBot 中选择具体模型。"
                )
            extra = self.provider_config.get("custom_extra_body") or {}
            effort = kwargs.get(
                "agy_thought_level", extra.get("agy_thought_level", "default")
            )
            if effort == "default":
                effort = self.settings.thought_level
            if not isinstance(effort, str) or effort not in {
                "default",
                "low",
                "medium",
                "high",
            }:
                raise AgyError(
                    "CONFIG", "agy_thought_level 必须为 default/low/medium/high。"
                )
        except BaseException:
            pending = self.runtime.turns.pop(key, None)
            if pending:
                await pending.close()
            raise
        if snapshot.tools or key in self.runtime.turns or continuation:
            source = chat_events(
                self.runtime, request, key, selected, effort, snapshot, signal, approver
            )
        else:
            source = self.runtime.stream(request, key, selected, effort)
        events = abortable(source, signal)
        text_parts, thoughts = [], []
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
                    result = event.result or {}
                    if result.get("stopReason") == "max_tokens":
                        logger.warning(
                            "AGY 达到输出上限，本轮返回已有内容，未自动续写。"
                        )
                    yield LLMResponse(
                        role="assistant",
                        completion_text="".join(text_parts),
                        reasoning_content="".join(thoughts) or None,
                        usage=token_usage(result),
                        is_chunk=False,
                    )
        finally:
            await events.aclose()
