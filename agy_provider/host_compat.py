"""Reversible AstrBot runner compatibility, scoped to our ACP provider only.

The host's skills_like re-query cannot resume an already retained ACP prompt.
No tool schema/history protocol is changed here, and no native tool is replayed.
"""

import asyncio
import copy
import inspect
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any

from .errors import AgyError


@dataclass
class _Patch:
    reset: Any
    step: Any
    owner: str
    logger: Any
    enabled: bool = True
    reset_wrapper: Any = None
    step_wrapper: Any = None
    responses: Any = None
    fallback: Any = None
    responses_wrapper: Any = None
    fallback_wrapper: Any = None


_patches: dict[type, _Patch] = {}
_replay_scope: ContextVar[Any] = ContextVar("agy_replay_scope", default=None)


def _install_replay_guard(runner_cls, state):
    """4.29.4 catches all Exception instances even after stream output.

    Keep its native fallback policy before output. After an ACP no-replay
    failure, intercept subsequent candidates at the request boundary, so none
    of their providers are called. Do not mutate the configured candidate list.
    """
    state.responses = getattr(runner_cls, "_iter_llm_responses", None)
    state.fallback = getattr(runner_cls, "_iter_llm_responses_with_fallback", None)
    if not callable(state.responses) or not callable(state.fallback):
        state.logger.warning("AGY 宿主缺少 fallback 兼容接口，不能保证外部 fallback 不重放；请禁用宿主 fallback。")
        return

    @wraps(state.responses)
    def responses_wrapper(self, *args, **kwargs):
        scope = _replay_scope.get()
        if not state.enabled or scope is None or scope["runner"] is not self:
            return state.responses(self, *args, **kwargs)

        async def guarded():
            if scope["blocked"] is not None:
                from astrbot.core.provider.entities import LLMResponse

                yield LLMResponse(role="err", completion_text=str(scope["blocked"]))
                return
            provider = getattr(self, "provider", None)
            source = state.responses(self, *args, **kwargs)
            emitted = False
            try:
                async for response in source:
                    emitted = emitted or bool(
                        getattr(response, "completion_text", "")
                        or getattr(response, "reasoning_content", "")
                        or getattr(response, "tools_call_ids", None)
                    )
                    yield response
            except AgyError as exc:
                if _owned(provider, state) and (exc.no_fallback or emitted):
                    exc.no_retry = True
                    exc.no_fallback = True
                    scope["blocked"] = exc
                    scope["provider"] = provider
                raise
            finally:
                await source.aclose()

        return guarded()

    @wraps(state.fallback)
    def fallback_wrapper(self, *args, **kwargs):
        providers = [getattr(self, "provider", None),
                     *(getattr(self, "fallback_providers", None) or [])]
        source = state.fallback(self, *args, **kwargs)
        if not any(_owned(provider, state) for provider in providers):
            return source

        async def guarded():
            scope = {"runner": self, "blocked": None, "provider": None}
            try:
                while True:
                    # Set/reset around each advance, never across a yield:
                    # hosts may advance a generator from different tasks.
                    token = _replay_scope.set(scope)
                    try:
                        response = await anext(source)
                    except StopAsyncIteration:
                        return
                    finally:
                        _replay_scope.reset(token)
                    if scope["blocked"] is not None:
                        self.provider = scope["provider"]
                    yield response
            finally:
                try:
                    await source.aclose()
                finally:
                    if scope["blocked"] is not None:
                        self.provider = scope["provider"]

        return guarded()

    state.responses_wrapper = responses_wrapper
    state.fallback_wrapper = fallback_wrapper
    runner_cls._iter_llm_responses = responses_wrapper
    runner_cls._iter_llm_responses_with_fallback = fallback_wrapper


def _owned(provider, state):
    return state.enabled and getattr(provider, "agy_plugin_owner", None) == state.owner


async def _close_matching_turns(provider, signal, logger):
    """Await full ToolTurn.close, including stream/session lease release.

    Snapshot by signal identity, never by shared chat/session ID. ToolTurn.close
    cancels the retained prompt, joins its stream finalizers, and records handed
    off calls conservatively as unknown through ToolBroker.close.
    """
    if signal is None:
        return
    runtime = getattr(provider, "runtime", None)
    turns = getattr(runtime, "turns", {})
    selected = [turn for turn in list(turns.values()) if turn.signal is signal]
    if not selected:
        return

    async def close_all():
        results = await asyncio.gather(
            *(turn.close() for turn in selected), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.error("AGY 原生 runner pending turn 清理异常；不会重放工具。")
        logger.info(
            "AGY 原生 runner 结束：清理 %s 个本轮 pending turn；"
            "未回填的已交接调用按 unknown 处理，不重放。",
            len(selected),
        )

    cleanup = asyncio.create_task(close_all(), name="agy:host-runner-cleanup")
    cancelled = None
    while True:
        try:
            await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            if cleanup.done():
                break
    cleanup.result()
    if cancelled:
        raise cancelled


def install_host_compat(runner_cls=None, *, owner=None, logger):
    """Install once, retaining whatever third-party chain is currently present.

    The plugin entry point supplies the logger from astrbot.api.
    Lazy imports let tests exercise the exact seam without an AstrBot install.
    OWNER must come from the provider source: historical owner strings must not
    broaden this scope. Production callers use the actual constant by default.
    """
    if logger is None:
        raise TypeError("logger must be supplied by the caller")
    if runner_cls is None:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )

        runner_cls = ToolLoopAgentRunner
    if owner is None:
        from .provider import OWNER

        owner = OWNER
    current = _patches.get(runner_cls)
    if current is not None and current.enabled:
        if current.owner != owner:
            raise RuntimeError("AGY runner compatibility owner 冲突。")
        return
    state = _Patch(runner_cls.reset, runner_cls.step_until_done, owner, logger)
    signature = inspect.signature(state.reset)
    if not {"provider", "request", "tool_schema_mode"} <= set(signature.parameters):
        raise RuntimeError("AstrBot runner.reset 签名不兼容，不能安全安装 ACP 适配。")

    @wraps(state.reset)
    async def reset_wrapper(self, *args, **kwargs):
        if not state.enabled:
            return await state.reset(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        provider = bound.arguments.get("provider")
        if not _owned(provider, state):
            return await state.reset(self, *args, **kwargs)
        requested_mode = bound.arguments.get("tool_schema_mode", "full")
        bound.arguments["tool_schema_mode"] = "full"
        if requested_mode != "full":
            state.logger.info(
                "AGY ACP 原生 runner：tool_schema_mode=%s -> full；"
                "避免 skills_like 二次参数请求破坏 retained prompt 续接。",
                requested_mode,
            )
        request = bound.arguments["request"]
        if not getattr(request, "session_id", None) and not getattr(
            request, "conversation_id", None
        ):
            # A request may be shared by nested runners; never write onto the
            # caller's object, model or global config. Each reset owns one ID.
            request = copy.copy(request)
            request.session_id = "agy-native-run:" + uuid.uuid4().hex
            bound.arguments["request"] = request
            state.logger.info(
                "AGY ACP 原生 runner：缺省会话使用独立 run 身份，不复用 QQ/父任务身份。"
            )
        return await state.reset(*bound.args, **bound.kwargs)

    @wraps(state.step)
    def step_wrapper(self, *args, **kwargs):
        # Capture before yielding: a later reset must not redirect cleanup to
        # the new runner's signal/runtime. None can never authorize cleanup.
        provider = getattr(self, "provider", None)
        signal = getattr(self, "_abort_signal", None)
        source = state.step(self, *args, **kwargs)
        if not _owned(provider, state):
            # Even generator identity/close semantics remain exactly native
            # for other providers and disabled third-party wrapper chains.
            return source

        async def scoped_step():
            try:
                async for response in source:
                    yield response
            finally:
                try:
                    await source.aclose()
                finally:
                    if state.enabled:
                        await _close_matching_turns(provider, signal, state.logger)

        return scoped_step()

    state.reset_wrapper, state.step_wrapper = reset_wrapper, step_wrapper
    _patches[runner_cls] = state
    runner_cls.reset, runner_cls.step_until_done = reset_wrapper, step_wrapper
    _install_replay_guard(runner_cls, state)
    state.logger.info(
        "AGY ACP 原生 runner 兼容已安装：仅本插件 provider 使用 full、独立 run 身份和结束清理。"
    )


def uninstall_host_compat(runner_cls=None):
    """Disable captured wrappers, restore only attributes still exactly ours."""
    if runner_cls is None:
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )

        runner_cls = ToolLoopAgentRunner
    state = _patches.pop(runner_cls, None)
    if state is None:
        return
    state.enabled = False
    # functools.wraps can copy attributes onto somebody else's wrapper; exact
    # object identity (not a marker attribute) is the only safe restore test.
    if runner_cls.reset is state.reset_wrapper:
        runner_cls.reset = state.reset
    if runner_cls.step_until_done is state.step_wrapper:
        runner_cls.step_until_done = state.step
    if state.responses_wrapper is not None and runner_cls._iter_llm_responses is state.responses_wrapper:
        runner_cls._iter_llm_responses = state.responses
    if state.fallback_wrapper is not None and runner_cls._iter_llm_responses_with_fallback is state.fallback_wrapper:
        runner_cls._iter_llm_responses_with_fallback = state.fallback
    state.logger.info("AGY ACP 原生 runner 兼容已停用；保留第三方后装 patch。")


# Short public entry points for isolated container integration probes.
install = install_host_compat
uninstall = uninstall_host_compat
