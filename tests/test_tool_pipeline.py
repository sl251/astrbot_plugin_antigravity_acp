import asyncio
import base64
import copy
import hashlib
import importlib
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import astrbot_stub  # noqa: F401
from tests.test_b07_b08_regressions import _png_data_url
from tests.test_host_replay import Response, runner_type
from tests.test_media import _baseline_jpeg
from tests.test_tools_mode import _load_provider_module
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.host_compat import install_host_compat, uninstall_host_compat
from agy_provider.messages import ConversationInput, normalize
from agy_provider.process import LaunchInfo, launch_args as real_launch_args
from agy_provider.sessions import Runtime, decide_reuse
from agy_provider.tools import ToolSnapshot
from agy_provider.tool_turn import ToolTurn
from agy_provider.transport import PermissionDenial


class ToolPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mcp-pipeline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "cli.jsonl"
        self.enterContext(patch.dict(os.environ, {"AGY_TEST_LOG": str(self.log)}))
        self.settings = Settings(auth_bridge=False, enable_tools=True, request_timeout=5,
                                 tool_approval_timeout=2, tool_execution_timeout=2)
        self.snapshot = ToolSnapshot(SimpleNamespace(tools=[SimpleNamespace(
            name="echo", active=True, description="echo", parameters={
                "type": "object", "properties": {"value": {"type": "string"}}})]))
        self.enterContext(patch("agy_provider.sessions.discover", AsyncMock(return_value=LaunchInfo("fake", "1.5.9", False))))
        self.enterContext(patch("agy_provider.sessions.launch_args", return_value=[sys.executable, str(Path(__file__).with_name("fake_tool_cli.py"))]))

    async def tool_runner(
        self, *, builtins=False, execution_timeout=2, initial_user=None, **settings,
    ):
        class RunnerResponse(Response):
            is_chunk = False

        stubs = _load_provider_module(self.root)
        stubs["astrbot.core.provider.entities"].LLMResponse = RunnerResponse
        self.enterContext(patch.dict(sys.modules, stubs))
        sys.modules.pop("agy_provider.provider", None)
        self.addCleanup(sys.modules.pop, "agy_provider.provider", None)
        module = importlib.import_module("agy_provider.provider")
        module.configure(replace(
            self.settings,
            builtin_search_web="allow" if builtins else "deny",
            tool_execution_timeout=execution_timeout,
            **settings,
        ))
        self.addCleanup(module.configure, Settings())
        provider = module.AgyProvider({"id": "p", "model": "gemini-test"}, {})
        self.addAsyncCleanup(provider.terminate)

        def launch(info, home, *, customizations_mode):
            fixtures = Path(__file__).parent
            real_args = real_launch_args(info, home, customizations_mode=customizations_mode)
            self.assertNotIn("--agent", real_args)
            agents_text = (home / "workspace" / "AGENTS.md").read_text(encoding="utf-8")
            tools_active = "No tools are available in this session." not in agents_text
            launch.tools_modes.append(tools_active)
            if tools_active:
                return [sys.executable, str(fixtures / "fake_tool_cli.py")]
            return [sys.executable, str(fixtures / "fake_cli.py"), "echo"]

        launch.tools_modes = []

        self.log.write_text("", encoding="utf-8")
        launches = self.enterContext(patch("agy_provider.sessions.launch_args", side_effect=launch))

        class Runner(runner_type(os.environ.get("AGY_HOST_RUNNER_SOURCE"))):
            MAX_STEPS_REACHED_PROMPT = "Step budget exhausted. Summarize the results."
            EMPTY_OUTPUT_RETRY_ATTEMPTS = 3
            EMPTY_OUTPUT_RETRY_WAIT_MIN_S, EMPTY_OUTPUT_RETRY_WAIT_MAX_S = 1, 4

            async def _iter_llm_responses(self, **kwargs):
                if self.provider is not provider:
                    self.provider.calls += 1
                    yield Response(role="assistant", completion_text="fallback", is_chunk=False)
                    return
                stream = provider.text_chat_stream(
                    contexts=self.run_context.messages,
                    session_id="max-steps",
                    func_tool=self.req.func_tool,
                    abort_signal=self._abort_signal,
                    extra_user_content_parts=getattr(self.req, "extra_user_content_parts", None),
                )
                try:
                    async for response in stream:
                        yield response
                except AgyError as exc:
                    self.last_provider_error = exc
                    raise
                finally:
                    await stream.aclose()

        install_host_compat(Runner, owner=module.OWNER, logger=Mock())
        self.addCleanup(uninstall_host_compat, Runner)
        runner = Runner()
        toolset = SimpleNamespace(tools=[SimpleNamespace(
            name="echo", active=True, description="echo",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        )])
        runner.req = SimpleNamespace(func_tool=toolset)
        runner.provider = provider
        fallback = SimpleNamespace(calls=0, provider_config={"id": "fallback"})
        runner.fallback_providers = [fallback]
        runner._abort_signal = asyncio.Event()
        runner._step_budget_max = runner._step_budget_used = 12
        runner.run_context = SimpleNamespace(messages=[
            {"role": "user", "content": "echo" if initial_user is None else initial_user},
        ])
        initial = await self.run_step(runner)
        call = initial[-1]
        runner.run_context.messages.extend(self.exchange(
            call.tools_call_ids[0], call.tools_call_args[0], "completed tool result",
        ))
        return runner, provider, fallback, launches, self.log

    async def max_steps_runner(self, *, builtins=False):
        runner, provider, fallback, launches, log = await self.tool_runner(builtins=builtins)
        runner.req.func_tool = None
        runner._step_budget_used += 1
        runner.run_context.messages.append({
            "role": "user", "content": runner.MAX_STEPS_REACHED_PROMPT,
        })
        return runner, provider, fallback, launches, log

    @staticmethod
    async def run_step(runner):
        responses = [response async for response in runner._iter_llm_responses_with_fallback()]
        runtime = getattr(runner.provider, "runtime", None)
        if runtime is not None:
            # The native fallback iterator returns at the final response. Let
            # its async generator finalizers release the per-request lock.
            async with asyncio.timeout(5):
                while runtime.entry_locks:
                    await asyncio.sleep(0.01)
        return responses

    @staticmethod
    def exchange(call_id, arguments, result):
        return [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": call_id, "function": {"name": "echo", "arguments": arguments},
            }]},
            {"role": "tool", "tool_call_id": call_id, "content": result},
        ]

    @staticmethod
    def image_result(messages, *urls):
        tool = messages[-1]
        parts = []
        for index, url in enumerate(urls):
            path = f"/tool_image_cache/result_{index}.img"
            tool["content"] += (
                f"\n\nImage returned and cached at path='{path}'. "
                "Review the image below. Use send_message_to_user to send it to the user if satisfied, "
                f"with type='image' and path='{path}'."
            )
            parts.extend([
                {"type": "text", "text": f"[Image from tool 'echo', path='{path}']"},
                {"type": "image_url", "image_url": {"url": url, "id": path}},
            ])
        messages.append({"role": "user", "content": parts})

    @staticmethod
    def cli_events(log, name):
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        return [event for event in events if event["event"] == name]

    @classmethod
    def prompts(cls, log):
        return cls.cli_events(log, "prompt")

    @staticmethod
    def jpeg_url():
        return "data:image/jpeg;base64," + base64.b64encode(_baseline_jpeg()).decode("ascii")

    def tool_cli(self, rounds=1, *, image=None):
        env = {"AGY_TEST_TOOL_ROUNDS": str(rounds)}
        if image is not None:
            env["AGY_TEST_TOOL_IMAGE"] = "1" if image else "0"
        return patch.dict(os.environ, env)

    async def image_runner(self, *urls, **settings):
        state = await self.tool_runner(
            enable_images=True, context_compaction_min_peak=200000, **settings,
        )
        runner = state[0]
        self.image_result(runner.run_context.messages, *urls)
        return state

    async def append_text_result(self, runner):
        call = (await self.run_step(runner))[-1]
        self.assertTrue(getattr(call, "tools_call_ids", None), call.completion_text)
        runner.run_context.messages.extend(
            self.exchange(call.tools_call_ids[0], call.tools_call_args[0], "second result")
        )

    async def assert_image_turn_reused(self, runner, provider, fallback, launches, count):
        messages = runner.run_context.messages
        session = next(iter(provider.runtime.sessions.values()))
        client = session.client
        final = (await self.run_step(runner))[-1]
        self.assertEqual(final.role, "assistant")
        self.assertEqual(len(session.host_expected), count)
        messages.extend([
            {"role": "assistant", "content": final.completion_text},
            {"role": "user", "content": "follow-up question"},
        ])
        runner._abort_signal = asyncio.Event()
        runner.req.extra_user_content_parts = []
        next_reply = (await self.run_step(runner))[-1]
        self.assertEqual(next_reply.completion_text, "follow-up")
        record = provider.runtime.telemetry.history[-1]
        self.assertTrue(record["reused"])
        self.assertEqual(record["history_relation"], "exact")
        self.assertIs(session.client, client)
        self.assertEqual(launches.call_count, 1)
        self.assertEqual(fallback.calls, 0)

    async def timed_out_runner(self, **settings):
        runner, provider, fallback, launches, log = await self.tool_runner(
            builtins=True, execution_timeout=1, **settings,
        )
        key, turn = next(iter(provider.runtime.turns.items()))
        call = turn.waiting["call"]
        async with asyncio.timeout(5):
            await turn.task
        self.assertTrue(call.execution_timed_out)
        self.assertTrue(call.future.cancelled())
        self.assertEqual(provider.runtime.tool_outcomes[call.id], "unknown")
        record = provider.runtime.telemetry.history[-1]
        self.assertEqual((record["status"], record["error_code"]), ("error", "TOOL_TIMEOUT"))
        self.assertTrue(record["tool_handed_off"])
        self.assertFalse(provider.runtime.turns)
        return runner, provider, fallback, launches, log, key

    async def assert_late_result_rejected(self, runner, fallback):
        with self.assertRaises(AgyError) as caught:
            _ = [response async for response in runner._iter_llm_responses()]
        self.assertEqual(caught.exception.code, "TOOL_TIMEOUT")
        self.assertTrue(caught.exception.no_retry and caught.exception.no_fallback)
        self.assertNotIn("进程重载", str(caught.exception))
        result = await self.run_step(runner)
        self.assertEqual(result[-1].role, "err")
        self.assertIn("[TOOL_TIMEOUT]", result[-1].completion_text)
        self.assertEqual(fallback.calls, 0)

    async def test_late_tool_result_rebuilds_without_tools_once(self):
        runner, provider, fallback, launches, log, key = await self.timed_out_runner()
        old_client = provider.runtime.sessions[key].client
        self.assertTrue(old_client.alive)
        result = await self.run_step(runner)
        self.assertEqual(result[-1].completion_text, "reply:1")
        self.assertFalse(getattr(result[-1], "tools_call_ids", None))
        self.assertEqual(fallback.calls, 0)
        self.assertFalse(old_client.alive)
        self.assertIsNone(provider.runtime.sessions[key].broker)
        await self.assert_late_result_rejected(runner, fallback)
        self.assertEqual(launches.side_effect.tools_modes, [True, False])
        prompts = self.prompts(log)
        self.assertEqual(len(prompts), 1)
        self.assertIn("completed tool result", "".join(prompts[0]["texts"]))

    async def test_late_tool_result_rejects_foreign_run_and_rewritten_history(self):
        runner, _, fallback, launches, _, _ = await self.timed_out_runner()
        signal = runner._abort_signal
        runner._abort_signal = asyncio.Event()
        await self.assert_late_result_rejected(runner, fallback)
        runner._abort_signal = signal

        runner.run_context.messages[0] = {"role": "user", "content": "changed question"}
        await self.assert_late_result_rejected(runner, fallback)
        runner.run_context.messages[0] = {"role": "user", "content": "echo"}
        runner.run_context.messages[-2]["tool_calls"][0]["function"]["arguments"] = {"value": "changed"}
        await self.assert_late_result_rejected(runner, fallback)
        self.assertEqual(launches.call_count, 1)

    async def test_late_tool_result_rejects_dead_session(self):
        runner, provider, fallback, launches, _, key = await self.timed_out_runner()
        await provider.runtime.sessions[key].client.close()
        # The pool can remove the dead session before the host returns.
        async with provider.runtime.lease(key, temporary=False):
            pass
        self.assertNotIn(key, provider.runtime.sessions)
        await self.assert_late_result_rejected(runner, fallback)
        self.assertEqual(launches.call_count, 1)

    async def test_late_tool_result_is_consumed_even_when_generation_fails(self):
        runner, _, fallback, launches, log, _ = await self.timed_out_runner()
        scenario = self.root / "scenario"
        scenario.write_text("empty", encoding="utf-8")
        with patch.dict(os.environ, {"AGY_SCENARIO_FILE": str(scenario)}):
            failed = await self.run_step(runner)
        self.assertEqual(failed[-1].role, "err")
        self.assertIn("[EMPTY]", failed[-1].completion_text)
        await self.assert_late_result_rejected(runner, fallback)
        self.assertEqual(launches.call_count, 2)
        self.assertEqual(len(self.prompts(log)), 1)

    async def test_tool_result_without_timeout_still_rejects_stale_history(self):
        runner, provider, fallback, launches, _ = await self.tool_runner()
        await next(iter(provider.runtime.turns.values())).close()
        stale = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", stale[-1].completion_text)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 1)

    async def test_stale_tool_image_after_reload_rejected_as_tool_stale(self):
        runner, provider, fallback, launches, _ = await self.tool_runner(enable_images=True)
        self.image_result(runner.run_context.messages, _png_data_url())
        await next(iter(provider.runtime.turns.values())).close()
        self.assertFalse(provider.runtime.turns)
        self.assertFalse(provider.runtime.sessions)
        await self.assert_direct_error(runner, "TOOL_STALE")
        stale = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", stale[-1].completion_text)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 1)

    async def test_malformed_stale_tool_image_rejected_before_media_io(self):
        runner, provider, fallback, launches, _ = await self.image_runner(_png_data_url())
        await next(iter(provider.runtime.turns.values())).close()
        parts = runner.run_context.messages[-1]["content"]
        parts[1]["image_url"]["url"] = "https://example.invalid/image.png"
        for label in (parts[0]["text"], "rewritten label"):
            parts[0]["text"] = label
            with self.subTest(rewritten=label == "rewritten label"), patch("agy_provider.media._download") as download:
                await self.assert_history_rejected(runner, fallback, launches)
                download.assert_not_called()

    async def assert_direct_error(self, runner, code):
        with self.assertRaises(AgyError) as caught:
            _ = [response async for response in runner._iter_llm_responses()]
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(caught.exception.no_retry and caught.exception.no_fallback)

    async def test_tool_image_continuation_single_and_multi_images_with_reuse(self):
        jpeg, png = self.jpeg_url(), _png_data_url()
        for urls in ((jpeg,), (png, jpeg), (jpeg, jpeg)):
            with self.subTest(mimes=[url.split(";")[0] for url in urls]), self.tool_cli(image=True):
                runner, provider, fallback, launches, log = await self.image_runner(*urls)
                # A repeated original text extra must not mark the tool's label temporary.
                label = runner.run_context.messages[-1]["content"][0]
                runner.req.extra_user_content_parts = [{**label, "_no_save": True}]
                await self.assert_image_turn_reused(runner, provider, fallback, launches, 5)
                self.assertEqual(self.cli_events(log, "tool_result"), [{
                    "event": "tool_result",
                    "types": ["text", *(["text", "image"] * len(urls))],
                    "images": [url.split(";")[0][5:] for url in urls],
                    "digests": [
                        hashlib.sha256(url.split(",")[1].encode("ascii")).hexdigest()
                        for url in urls
                    ],
                }])

    async def test_tool_image_continuation_preserves_repeated_user_image_without_merging(self):
        tool_image = _png_data_url()
        user_image = {"type": "image_url", "image_url": {"url": self.jpeg_url()}}
        for rounds in (1, 2):
            with self.subTest(rounds=rounds), self.tool_cli(rounds, image=True):
                runner, provider, fallback, launches, log = await self.image_runner(
                    tool_image, image_max_count=1,
                    initial_user=[{"type": "text", "text": "echo"}, user_image],
                )
                runner.req.extra_user_content_parts = [user_image]
                if rounds == 2:
                    await self.append_text_result(runner)
                await self.assert_image_turn_reused(
                    runner, provider, fallback, launches, 5 if rounds == 1 else 7,
                )
                results = self.cli_events(log, "tool_result")
                self.assertEqual(len(results), rounds)
                self.assertEqual(results[0]["types"], ["text", "text", "image"])
                self.assertEqual(results[0]["digests"], [
                    hashlib.sha256(tool_image.split(",")[1].encode("ascii")).hexdigest(),
                ])

    async def test_multistep_tool_image_then_text_tool_continuation(self):
        with self.tool_cli(2, image=True):
            runner, provider, fallback, launches, log = await self.image_runner(_png_data_url())
        first_turn = next(iter(provider.runtime.turns.values()))
        await self.append_text_result(runner)
        self.assertIs(next(iter(provider.runtime.turns.values())), first_turn)
        await self.assert_image_turn_reused(runner, provider, fallback, launches, 7)
        self.assertEqual(
            [result["types"] for result in self.cli_events(log, "tool_result")],
            [["text", "text", "image"], ["text"]],
        )

    async def test_tool_result_without_image_modality_keeps_two_message_path(self):
        with self.tool_cli():
            runner, _, fallback, launches, log = await self.tool_runner()
        self.image_result(runner.run_context.messages, _png_data_url())
        runner.run_context.messages.pop()  # host without image modality
        result = (await self.run_step(runner))[-1]
        self.assertEqual(result.role, "assistant")
        self.assertIn("Image returned and cached", result.completion_text)
        self.assertEqual(self.cli_events(log, "tool_result")[0]["types"], ["text"])
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 1)

    async def test_tool_image_without_cli_capability_submits_only_text_labels(self):
        for capability in (None, False):
            with self.subTest(capability=capability), self.tool_cli(image=capability):
                runner, provider, fallback, launches, log = await self.image_runner(_png_data_url())
                await self.assert_image_turn_reused(runner, provider, fallback, launches, 5)
                self.assertEqual(self.cli_events(log, "tool_result"), [{
                    "event": "tool_result", "types": ["text", "text"], "images": [], "digests": [],
                }])

    async def assert_history_rejected(self, runner, fallback, launches):
        final = (await self.run_step(runner))[-1]
        self.assertEqual(final.role, "err")
        self.assertIn("[TOOL_HISTORY]", final.completion_text)
        error = runner.last_provider_error
        self.assertTrue(error.no_retry and error.no_fallback)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 1)

    async def test_tool_image_rejects_rewritten_history_and_tampered_call(self):
        for mutation in ("old_args", "old_result", "args", "text", "foreign_id"):
            with self.subTest(mutation=mutation), self.tool_cli(2, image=True):
                runner, _, fallback, launches, _ = await self.tool_runner(enable_images=True)
                await self.append_text_result(runner)
                messages = runner.run_context.messages
                self.image_result(messages, _png_data_url())
                if mutation == "old_args":
                    messages[1]["tool_calls"][0]["function"]["arguments"] = {"value": "changed"}
                elif mutation == "old_result":
                    messages[2]["content"] = "changed result"
                elif mutation == "args":
                    messages[-3]["tool_calls"][0]["function"]["arguments"] = {"value": "changed"}
                elif mutation == "text":
                    messages[-3]["content"] = "changed answer"
                else:
                    messages[-3]["tool_calls"][0]["id"] = messages[-2]["tool_call_id"] = "foreign"
                await self.assert_history_rejected(runner, fallback, launches)

    async def test_tool_image_rejects_multiple_tool_results(self):
        for same_id in (True, False):
            with self.subTest(same_id=same_id):
                runner, _, fallback, launches, _ = await self.image_runner(_png_data_url())
                messages = runner.run_context.messages
                extra = dict(messages[-2])
                if not same_id:
                    extra["tool_call_id"] = "foreign"
                messages.insert(-1, extra)
                await self.assert_history_rejected(runner, fallback, launches)

    async def test_tool_image_rejects_mismatched_tool_name_or_paths(self):
        for mutation in ("name", "path", "missing", "extra", "order", "preview", "line_end"):
            with self.subTest(mutation=mutation):
                urls = (_png_data_url(), self.jpeg_url()) if mutation in ("missing", "extra", "order") else (_png_data_url(),)
                runner, _, fallback, launches, _ = await self.image_runner(*urls)
                messages = runner.run_context.messages
                parts = messages[-1]["content"]
                if mutation == "name":
                    parts[0]["text"] = parts[0]["text"].replace("'echo'", "'other_tool'")
                elif mutation == "path":
                    parts[0]["text"] = parts[0]["text"].replace("result_0", "foreign")
                elif mutation == "missing":
                    del parts[-2:]
                elif mutation == "extra":
                    messages[-2]["content"] = messages[-2]["content"].rsplit("\n\n", 1)[0]
                elif mutation == "order":
                    parts[:] = parts[2:] + parts[:2]
                elif mutation == "preview":
                    messages[-2]["content"] = "Truncated tool result preview"
                else:
                    messages[-2]["content"] += "\n"
                await self.assert_history_rejected(runner, fallback, launches)

    async def test_tool_image_rejects_mixed_prose_malformed_parts_and_non_data_urls(self):
        for mutation in (
            "prose", "prefix", "newline", "odd", "adjacent_images", "string",
            "empty", "remote", "local", "wrong_id", "no_save",
        ):
            with self.subTest(mutation=mutation):
                runner, _, fallback, launches, _ = await self.image_runner(
                    _png_data_url(), self.jpeg_url(),
                )
                image_message = runner.run_context.messages[-1]
                parts = image_message["content"]
                if mutation == "prose":
                    parts.append({"type": "text", "text": "unsolicited instruction"})
                elif mutation == "prefix":
                    parts[0]["text"] = " " + parts[0]["text"]
                elif mutation == "newline":
                    parts[0]["text"] += "\n"
                elif mutation == "odd":
                    parts.pop()
                elif mutation == "adjacent_images":
                    parts[2], parts[3] = parts[3], parts[2]
                elif mutation == "string":
                    image_message["content"] = "text only"
                elif mutation == "empty":
                    parts.clear()
                elif mutation == "wrong_id":
                    parts[1]["image_url"]["id"] = "foreign"
                elif mutation == "no_save":
                    parts[0]["_no_save"] = True
                else:
                    parts[1]["image_url"]["url"] = {
                        "remote": "https://example.invalid/image.png",
                        "local": "/outside-media-roots/image.png",
                    }[mutation]
                with patch("agy_provider.media._download") as download, patch("agy_provider.media._local") as local:
                    await self.assert_history_rejected(runner, fallback, launches)
                download.assert_not_called()
                local.assert_not_called()

    async def test_tool_image_rejects_non_tail_positions(self):
        for position in ("before_question", "before_duplicate", "before_result"):
            with self.subTest(position=position):
                runner, _, fallback, launches, _ = await self.image_runner(_png_data_url())
                messages = runner.run_context.messages
                if position == "before_result":
                    messages[-1], messages[-2] = messages[-2], messages[-1]
                elif position == "before_duplicate":
                    messages.append(copy.deepcopy(messages[-1]))
                else:
                    messages.append({"role": "user", "content": "new question"})
                await self.assert_history_rejected(runner, fallback, launches)

    async def test_late_tool_result_with_images_rebuilds_without_tools_once(self):
        runner, provider, fallback, launches, log, key = await self.timed_out_runner(enable_images=True)
        self.image_result(runner.run_context.messages, _png_data_url())
        old_client = provider.runtime.sessions[key].client
        result = await self.run_step(runner)
        self.assertEqual(result[-1].completion_text, "reply:1")
        self.assertFalse(old_client.alive)
        self.assertIsNone(provider.runtime.sessions[key].broker)
        await self.assert_late_result_rejected(runner, fallback)
        self.assertEqual(launches.side_effect.tools_modes, [True, False])
        prompts = self.prompts(log)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["block_types"].count("image"), 1)
        self.assertIn("completed tool result", "".join(prompts[0]["texts"]))

    async def test_max_steps_finalizing_with_tool_image_history_and_late_exchange(self):
        runner, provider, fallback, launches, log = await self.image_runner(_png_data_url())
        old_turn = next(iter(provider.runtime.turns.values()))
        runner.req.func_tool = None
        runner._step_budget_used += 1
        messages = runner.run_context.messages
        messages.append({"role": "user", "content": runner.MAX_STEPS_REACHED_PROMPT})
        self.assertEqual((await self.run_step(runner))[-1].completion_text, "reply:1")
        self.assertTrue(old_turn.finished)
        self.assertIsNone(next(iter(provider.runtime.sessions.values())).broker)
        messages.extend(self.exchange("late-image", {}, "late image result"))
        self.image_result(messages, self.jpeg_url())
        signal = runner._abort_signal
        runner._abort_signal = asyncio.Event()
        await self.assert_direct_error(runner, "TOOL_STALE")
        runner._abort_signal = signal
        self.assertEqual((await self.run_step(runner))[-1].completion_text, "reply:1")
        await self.assert_direct_error(runner, "TOOL_STALE")
        self.assertFalse(provider.runtime.turns)
        self.assertIsNone(next(iter(provider.runtime.sessions.values())).broker)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.side_effect.tools_modes, [True, False, False])
        self.assertEqual([event["block_types"].count("image") for event in self.prompts(log)], [0, 1])

    async def test_max_steps_retires_pending_tool_prompt_and_summarizes_without_tools(self):
        runner, provider, fallback, launches, log = await self.max_steps_runner(builtins=True)
        old_turn = next(iter(provider.runtime.turns.values()))
        call = old_turn.waiting["call"]
        summary = await self.run_step(runner)
        self.assertEqual(summary[-1].completion_text, "reply:1")
        self.assertFalse(getattr(summary[-1], "tools_call_ids", None))
        self.assertEqual(fallback.calls, 0)
        self.assertFalse(provider.runtime.turns)
        self.assertTrue(old_turn.finished)
        self.assertEqual(provider.runtime.tool_outcomes[call.id], "unknown")
        self.assertEqual(launches.side_effect.tools_modes, [True, False])
        self.assertIsNone(next(iter(provider.runtime.sessions.values())).broker)
        prompts = self.prompts(log)
        self.assertEqual(len(prompts), 1)
        self.assertIn("completed tool result", "".join(prompts[0]["texts"]))

    async def test_max_steps_late_tool_result_rebuilds_without_fallback_or_tools(self):
        runner, provider, fallback, launches, log = await self.max_steps_runner()
        # Exercise the no-retained-turn branch independently of the first fix.
        await next(iter(provider.runtime.turns.values())).close()
        first = await self.run_step(runner)
        self.assertEqual(first[-1].completion_text, "reply:1")
        runner.run_context.messages.extend(self.exchange("late-result", {}, "late tool result"))

        signal = runner._abort_signal
        runner._abort_signal = asyncio.Event()
        foreign = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", foreign[-1].completion_text)
        runner._abort_signal = signal

        summary = await self.run_step(runner)
        self.assertEqual(summary[-1].completion_text, "reply:1")
        self.assertFalse(getattr(summary[-1], "tools_call_ids", None))
        repeated = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", repeated[-1].completion_text)
        self.assertEqual(fallback.calls, 0)
        self.assertFalse(provider.runtime.turns)
        self.assertEqual(launches.side_effect.tools_modes, [True, False, False])
        self.assertIsNone(next(iter(provider.runtime.sessions.values())).broker)
        prompts = self.prompts(log)
        self.assertEqual(len(prompts), 2)
        self.assertIn("late tool result", "".join(prompts[-1]["texts"]))

    async def test_max_steps_rejects_results_without_a_matching_live_summary(self):
        runner, provider, fallback, launches, log = await self.max_steps_runner()
        old = next(iter(provider.runtime.turns.values()))
        forced_history = list(runner.run_context.messages)
        runner.run_context.messages.pop()
        pending = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", pending[-1].completion_text)
        self.assertFalse(old.finished)
        await old.close()
        stale = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", stale[-1].completion_text)

        runner.run_context.messages = forced_history
        first = await self.run_step(runner)
        self.assertEqual(first[-1].completion_text, "reply:1")
        runner.run_context.messages.extend(self.exchange("late-result", {}, "late tool result"))
        runner.run_context.messages[0] = {"role": "user", "content": "changed question"}
        rewritten = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", rewritten[-1].completion_text)
        runner.run_context.messages[0] = {"role": "user", "content": "echo"}
        await next(iter(provider.runtime.sessions.values())).close()
        expired = await self.run_step(runner)
        self.assertIn("[TOOL_STALE]", expired[-1].completion_text)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 2)
        self.assertEqual(len(self.prompts(log)), 1)

    async def test_max_steps_generation_failure_after_tool_results_does_not_replay(self):
        runner, provider, fallback, launches, log = await self.max_steps_runner()
        first = await self.run_step(runner)
        self.assertEqual(first[-1].completion_text, "reply:1")
        runner.run_context.messages.extend(self.exchange("late-result", {}, "late tool result"))
        scenario = self.root / "scenario"
        scenario.write_text("empty", encoding="utf-8")
        with patch.dict(os.environ, {"AGY_SCENARIO_FILE": str(scenario)}):
            failed = await self.run_step(runner)
        self.assertEqual(failed[-1].role, "err")
        self.assertIn("[EMPTY]", failed[-1].completion_text)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 3)
        self.assertEqual(len(self.prompts(log)), 2)
        self.assertFalse(provider.runtime.turns)

    async def test_max_steps_failure_after_trimmed_tool_history_does_not_replay(self):
        runner, provider, fallback, launches, log = await self.max_steps_runner()
        # The prior handoff still blocks replay when host compaction omits it.
        del runner.run_context.messages[1:-1]
        scenario = self.root / "scenario"
        scenario.write_text("empty", encoding="utf-8")
        with patch.dict(os.environ, {"AGY_SCENARIO_FILE": str(scenario)}):
            failed = await self.run_step(runner)
        self.assertEqual(failed[-1].role, "err")
        self.assertIn("[EMPTY]", failed[-1].completion_text)
        self.assertEqual(fallback.calls, 0)
        self.assertEqual(launches.call_count, 2)
        self.assertEqual(len(self.prompts(log)), 1)
        self.assertFalse(provider.runtime.turns)

    def turn(self, approver=None, *, request=None, settings=None):
        runtime = Runtime(settings or self.settings, self.root)
        self.addAsyncCleanup(runtime.close)
        if request is None:
            request = ConversationInput(system="", messages=[{"role": "user", "content": "echo"}])
        meter = runtime.telemetry.start_turn(provider="p", session_key_raw="id", mode="tools", model="gemini-test", effort="default")
        signal = asyncio.Event()
        turn = ToolTurn(runtime, request, "id", "gemini-test", "default", self.snapshot, signal,
                        approver=approver, meter=meter)
        self.addAsyncCleanup(turn.close)
        return runtime, turn, request, signal

    async def test_tool_exchange_preserves_the_persisted_user_hash(self):
        settings = replace(self.settings, context_compaction_min_peak=200000)
        request = normalize(
            [{"role": "user", "content": [
                {"type": "text", "text": "echo"},
                {"type": "text", "text": "temporary", "_no_save": True},
            ]}],
            None, None, None, settings.history_bytes, tools=True,
        )
        runtime, turn, _, signal = self.turn(request=request, settings=settings)
        async with asyncio.timeout(5):
            events = [event async for event in turn.events()]
            call = next(event.result["call"] for event in events if event.kind == "tools")
            exchange = [
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": call.id, "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }]},
                {"role": "tool", "tool_call_id": call.id, "content": "echo result"},
            ]
            continuation = normalize(
                [*request.messages, *exchange], None, None, None, settings.history_bytes,
                tools=True, continuation=True,
            )
            turn.accept(continuation, "gemini-test", "default", self.snapshot, signal, None)
            final = [event async for event in turn.events()]
        self.assertEqual(final[-1].text, "echo result")

        replay = normalize(
            [
                {"role": "user", "content": "echo"},
                *exchange,
                {"role": "assistant", "content": final[-1].text},
                {"role": "user", "content": "next"},
            ],
            None, None, None, settings.history_bytes, tools=True,
        )
        session = runtime.sessions["id"]
        decision = decide_reuse(session, replay, "gemini-test", "default", self.snapshot)
        self.assertTrue(decision.reuse)
        self.assertEqual(decision.history_relation, "exact")
        self.assertEqual(session.sent_window.hashes[0], request.hashes[0])
        self.assertNotEqual(session.host_expected[0], request.hashes[0])
        self.assertEqual(session.sent_window.hashes[1:], session.host_expected[1:])

    async def test_real_relay_handoff_approval_result_compaction_and_permission_denial(self):
        approver = AsyncMock(return_value=True)
        runtime, turn, request, signal = self.turn(approver)
        async with asyncio.timeout(5):
            events = [event async for event in turn.events()]
            tool = next(event for event in events if event.kind == "tools")
            call = tool.result["call"]
            client = tool.result["client"]
            self.assertTrue(client.context_usage.suspected_compaction)
            self.assertTrue(client.alive, "Must not retire before host side effect/result")
            self.assertEqual(client.permission_errors, [PermissionDenial(
                "session/request_permission", "run_command", "native-call-1"
            )])
            approver.assert_awaited_once_with("echo", {"value": "中文 🦊"}, call.id)
            continuation = ConversationInput(system="", messages=copy.deepcopy(request.messages) + [
                {"role": "assistant", "content": "", "tool_calls": [{"id": call.id, "type": "function", "function": {
                    "name": "echo", "arguments": '{"value":"中文 🦊"}'}}]},
                {"role": "tool", "tool_call_id": call.id, "content": "宿主执行一次 🦊"}])
            # Constructor does not normalize the wire argument representation.
            from agy_provider.messages import canonical_calls
            continuation.messages[-2]["tool_calls"] = canonical_calls(continuation.messages[-2]["tool_calls"])
            turn.accept(continuation, "gemini-test", "default", self.snapshot, signal, approver)
            final = [event async for event in turn.events()]
        self.assertEqual(final[-1].text, "宿主执行一次 🦊")
        self.assertEqual(len(runtime.telemetry.history), 1)
        self.assertEqual(runtime.telemetry.history[-1]["tool_calls"], 1)
        self.assertTrue(runtime.telemetry.history[-1]["upstream_compaction_detected"])
        self.assertNotIn("id", runtime.sessions)
        self.assertFalse(client.alive)

    async def test_rejected_approval_never_hands_tool_to_executor(self):
        approver = AsyncMock(return_value=False)
        runtime, turn, _, _ = self.turn(approver)
        async with asyncio.timeout(5):
            events = [event async for event in turn.events()]
        self.assertFalse(any(event.kind == "tools" for event in events))
        self.assertEqual(events[-1].text, "TOOL_APPROVAL_DENIED")
        self.assertEqual(approver.await_count, 1)

    async def test_cancel_after_handoff_records_unknown_and_cleans_process(self):
        runtime, turn, _, signal = self.turn()
        async with asyncio.timeout(5):
            events = [event async for event in turn.events()]
            client = events[-1].result["client"]
            broker = events[-1].result["broker"]
            call = events[-1].result["call"]
            signal.set()
            await turn.task
        self.assertFalse(client.alive)
        self.assertEqual(broker.audit[call.id], "unknown")
        self.assertFalse(broker.tasks)

    async def test_provider_marks_partial_output_failure_non_replayable(self):
        with patch.dict(sys.modules, _load_provider_module(self.root)):
            sys.modules.pop("agy_provider.provider", None)
            provider_module = importlib.import_module("agy_provider.provider")
            provider_module.configure(Settings(auth_bridge=False))
            provider = provider_module.AgyProvider({"id": "p", "model": "gemini-test"}, {"auth_bridge": False})
            try:
                with patch("agy_provider.sessions.launch_args", return_value=[sys.executable, str(Path(__file__).with_name("fake_cli.py")), "partial-fatal"]):
                    chunks = []
                    with self.assertRaises(AgyError) as caught:
                        async for response in provider.text_chat_stream(prompt="hi", session_id="p"):
                            chunks.append(response)
                    self.assertTrue(caught.exception.no_fallback and caught.exception.no_retry)
                    self.assertEqual(caught.exception.code, "QUOTA")
                    self.assertTrue(chunks)
            finally:
                await provider.terminate()
                sys.modules.pop("agy_provider.provider", None)

    async def test_broker_close_does_not_deadlock_with_long_execution_timeout(self):
        # 复现 Python 3.12+ Server.wait_closed() 在活跃连接未取消时的死锁风险
        long_settings = Settings(
            auth_bridge=False,
            enable_tools=True,
            request_timeout=30,
            tool_approval_timeout=30,
            tool_execution_timeout=60,
        )
        runtime = Runtime(long_settings, self.root / "long-close")
        self.addAsyncCleanup(runtime.close)
        request = ConversationInput(system="", messages=[{"role": "user", "content": "echo"}])
        signal = asyncio.Event()
        turn = ToolTurn(runtime, request, "id", "gemini-test", "default", self.snapshot, signal)
        self.addAsyncCleanup(turn.close)
        events = [event async for event in turn.events()]
        call = events[-1].result["call"]
        broker = events[-1].result["broker"]
        client = events[-1].result["client"]
        # 必须在 1.5s 内干净关闭，而不是卡满 60s tool_execution_timeout
        async with asyncio.timeout(1.5):
            await turn.close()
        self.assertFalse(client.alive)
        self.assertEqual(broker.audit[call.id], "unknown")

    async def test_concurrent_same_session_requests_do_not_kill_active_tool_turn(self):
        toolset = SimpleNamespace(tools=[SimpleNamespace(
            name="echo", active=True, description="echo",
            parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        )])

        class FakeToolResult:
            def __init__(self, call_id, args_json, content):
                self._msgs = [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "echo", "arguments": args_json},
                        }],
                    },
                    {"role": "tool", "tool_call_id": call_id, "content": content},
                ]

            def to_openai_messages(self):
                return list(self._msgs)

        with patch.dict(sys.modules, _load_provider_module(self.root)):
            sys.modules.pop("agy_provider.provider", None)
            pm = importlib.import_module("agy_provider.provider")
            pm.configure(Settings(
                auth_bridge=False,
                enable_tools=True,
                queue_timeout=1,
                request_timeout=10,
                tool_approval_timeout=5,
                tool_execution_timeout=5,
            ))
            provider = pm.AgyProvider({"id": "p", "model": "gemini-test"}, {"auth_bridge": False})
            try:
                sig1 = asyncio.Event()
                out1 = [
                    r async for r in provider.text_chat_stream(
                        prompt="echo", session_id="shared-group", func_tool=toolset, abort_signal=sig1
                    )
                ]
                call_id = out1[-1].tools_call_ids[0]
                self.assertEqual(len(provider.runtime.turns), 1)
                active_turn = next(iter(provider.runtime.turns.values()))

                # 1) 同会话外部请求预检失败（如不支持的音频），不得关闭或污染 Runner 1 的工具轮
                sig_bad = asyncio.Event()
                with self.assertRaises(AgyError) as bad_exc:
                    async for _ in provider.text_chat_stream(
                        prompt="hi", audio_urls=["a.mp3"], session_id="shared-group", abort_signal=sig_bad
                    ):
                        pass
                self.assertEqual(bad_exc.exception.code, "UNSUPPORTED")
                self.assertFalse(active_turn.finished)
                self.assertFalse(active_turn.meter.finished)
                self.assertEqual(len(provider.runtime.turns), 1)

                # 2) 同会话外部新消息等待超时（queue_timeout=1s），仅外部请求报 BUSY，原工具轮仍存活
                sig2 = asyncio.Event()
                with self.assertRaises(AgyError) as busy_exc:
                    async for _ in provider.text_chat_stream(
                        prompt="并发打断", session_id="shared-group", func_tool=toolset, abort_signal=sig2
                    ):
                        pass
                self.assertEqual(busy_exc.exception.code, "BUSY")
                self.assertFalse(active_turn.finished)
                self.assertEqual(len(provider.runtime.turns), 1)

                # 3) Runner 1 随后回填工具结果，并验证排队等待的 Runner 3 能接力完成
                pm.configure(Settings(
                    auth_bridge=False,
                    enable_tools=True,
                    queue_timeout=5,
                    request_timeout=10,
                    tool_approval_timeout=5,
                    tool_execution_timeout=5,
                ))
                provider.settings = pm._settings

                async def queued_chat():
                    # 纯聊天接力请求（tool_choice="none"）等待工具轮释放后完成
                    with patch(
                        "agy_provider.sessions.launch_args",
                        return_value=[sys.executable, str(Path(__file__).with_name("fake_cli.py")), "generate"],
                    ):
                        res = await provider.text_chat(
                            prompt="后续消息",
                            session_id="shared-group",
                            tool_choice="none",
                            abort_signal=asyncio.Event(),
                        )
                        return res.completion_text

                queued_task = asyncio.create_task(queued_chat())
                await asyncio.sleep(0.05)
                cont_res = await provider.text_chat(
                    contexts=[{"role": "user", "content": "echo"}],
                    tool_calls_result=FakeToolResult(call_id, '{"value":"中文 🦊"}', "宿主执行一次 🦊"),
                    session_id="shared-group",
                    func_tool=toolset,
                    abort_signal=sig1,
                )
                self.assertEqual(cont_res.completion_text, "宿主执行一次 🦊")
                self.assertEqual(await asyncio.wait_for(queued_task, 5), "fake answer")
            finally:
                await provider.terminate()
                sys.modules.pop("agy_provider.provider", None)
