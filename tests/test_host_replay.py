"""Exercise the native host's catch-and-fallback contract without AstrBot I/O.

Optionally run the exact official 4.29.4 method via AGY_HOST_RUNNER_SOURCE.
The optional source is read/AST-selected locally; tests never fetch code.
"""

import ast
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agy_provider.errors import AgyError
from agy_provider.host_compat import install_host_compat, uninstall_host_compat


class Response:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Attempt:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _OneAttempt:
    """AgyError is not EmptyModelOutputError, so native retry makes one attempt."""
    def __init__(self, **kwargs):
        pass

    async def __aiter__(self):
        yield _Attempt()


def runner_type(official_source=None):
    class Runner:
        async def reset(self, provider, request, tool_schema_mode="full"):
            self.provider = provider

        async def step_until_done(self):
            if False:
                yield

        async def _iter_llm_responses(self, **kwargs):
            provider = self.provider
            provider.calls += 1
            await asyncio.sleep(0)
            if provider.partial:
                yield Response(is_chunk=True, completion_text=provider.partial)
            if provider.error:
                raise provider.error
            yield Response(is_chunk=False, role="assistant", completion_text="success")

        async def _iter_llm_responses_with_fallback(self):
            # Same boundary as 4.29.4: broad Exception fallback, no attribute check.
            for candidate in [self.provider, *self.fallback_providers]:
                self.provider = candidate
                try:
                    async for response in self._iter_llm_responses():
                        yield response
                    return
                except Exception:
                    continue
            yield Response(role="err", completion_text="all failed")

        def _is_stop_requested(self):
            return False

        def _sanitize_malformed_tool_calls(self, response):
            pass

    if official_source:
        module = ast.parse(Path(official_source).read_text(encoding="utf-8"))
        cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "ToolLoopAgentRunner")
        method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_iter_llm_responses_with_fallback")
        namespace = {"T": types, "LLMResponse": Response, "AsyncRetrying": _OneAttempt,
                     "EmptyModelOutputError": type("EmptyModelOutputError", (Exception,), {}),
                     "retry_if_exception_type": lambda *a, **k: None,
                     "stop_after_attempt": lambda *a, **k: None, "wait_exponential": lambda *a, **k: None,
                     "logger": Mock()}
        # Deferred annotations avoid importing AstrBot/typing/tenacity modules.
        code = "from __future__ import annotations\n" + ast.unparse(method)
        exec(compile(code, str(official_source), "exec"), namespace)
        Runner._iter_llm_responses_with_fallback = namespace[method.name]
    return Runner


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, runner, *, partial="", error=None, owned=True):
        one = SimpleNamespace(agy_plugin_owner="test-owner" if owned else "someone-else",
                              provider_config={"id": "primary"}, calls=0, partial=partial, error=error)
        two = SimpleNamespace(provider_config={"id": "fallback"}, calls=0, partial="", error=None)
        instance = runner()
        instance.provider, instance.fallback_providers = one, [two]
        instance.run_context = SimpleNamespace(messages=["hello"])
        instance.EMPTY_OUTPUT_RETRY_ATTEMPTS = 3
        instance.EMPTY_OUTPUT_RETRY_WAIT_MIN_S, instance.EMPTY_OUTPUT_RETRY_WAIT_MAX_S = 1, 4
        entities = SimpleNamespace(LLMResponse=Response)
        with patch.dict(sys.modules, {"astrbot.core.provider.entities": entities}):
            events = [event async for event in instance._iter_llm_responses_with_fallback()]
        return events, one, two, instance

    async def exercise(self, runner):
        install_host_compat(runner, owner="test-owner", logger=Mock())
        try:
            for partial, error in (("partial", AgyError("QUOTA", "safe")),
                                   ("", AgyError("UPSTREAM", "after tool", no_retry=True, no_fallback=True))):
                events, one, two, instance = await self.run_case(runner, partial=partial, error=error)
                self.assertEqual((one.calls, two.calls), (1, 0))
                self.assertEqual(events[-1].role, "err")
                self.assertIs(instance.provider, one)
            # Cooldown is no_retry but permits a different fallback before output.
            events, one, two, _ = await self.run_case(runner, error=AgyError("RATE_LIMIT", "cooldown", no_retry=True))
            self.assertEqual(two.calls, 1)
            events, one, two, _ = await self.run_case(runner, partial="foreign", error=AgyError("QUOTA", "safe"), owned=False)
            self.assertEqual(two.calls, 1)
            results = await asyncio.gather(
                self.run_case(runner, partial="partial", error=AgyError("QUOTA", "safe")),
                self.run_case(runner))
            self.assertEqual([result[2].calls for result in results], [0, 0])
            self.assertEqual(results[1][0][-1].completion_text, "success")
        finally:
            uninstall_host_compat(runner)

    async def test_partial_tool_and_preoutput_fallback_scopes(self):
        await self.exercise(runner_type())

    @unittest.skipUnless(os.environ.get("AGY_HOST_RUNNER_SOURCE"), "Optional official source not supplied")
    async def test_official_4294_fallback_contract(self):
        await self.exercise(runner_type(os.environ["AGY_HOST_RUNNER_SOURCE"]))

    async def test_uninstall_preserves_third_party_wrappers(self):
        runner = runner_type()
        original = runner._iter_llm_responses
        install_host_compat(runner, owner="test-owner", logger=Mock())
        ours = runner._iter_llm_responses
        def third_party(self, *args, **kwargs):
            return ours(self, *args, **kwargs)
        runner._iter_llm_responses = third_party
        uninstall_host_compat(runner)
        self.assertIs(runner._iter_llm_responses, third_party)
        self.assertIsNot(runner._iter_llm_responses, original)
        _, _, two, _ = await self.run_case(runner, partial="partial", error=AgyError("QUOTA", "safe"))
        self.assertEqual(two.calls, 1)
