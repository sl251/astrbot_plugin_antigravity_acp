"""Provider API contracts against a fake host API with pinned v4.29.4 fields.

Real AstrBot runner validation is deliberately separate: real_loop_check.py.
The ACP and MCP subprocesses here are real local processes, without network.
"""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agy_provider import sessions
from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.messages import normalize, prompt_blocks
from agy_provider.process import LaunchInfo

FAKE = Path(__file__).with_name("fake_tool_acp.py")


class FakeProvider:
    def __init__(self, config, settings):
        self.provider_config = config

    def set_model(self, model):
        self.model = model

    def get_model(self):
        return self.model

    def _ensure_message_to_dicts(self, contexts):
        return contexts


class FakeResponse:
    def __init__(
        self,
        role,
        completion_text="",
        reasoning_content=None,
        usage=None,
        is_chunk=False,
        tools_call_name=None,
        tools_call_args=None,
        tools_call_ids=None,
    ):
        self.role, self.completion_text = role, completion_text
        self.reasoning_content, self.usage, self.is_chunk = (
            reasoning_content,
            usage,
            is_chunk,
        )
        self.tools_call_name, self.tools_call_args, self.tools_call_ids = (
            tools_call_name or [],
            tools_call_args or [],
            tools_call_ids or [],
        )


def load_provider(root):
    modules = {
        name: types.ModuleType(name)
        for name in (
            "astrbot",
            "astrbot.api",
            "astrbot.api.provider",
            "astrbot.core",
            "astrbot.core.provider",
            "astrbot.core.provider.entities",
            "astrbot.core.utils",
            "astrbot.core.utils.astrbot_path",
        )
    }
    modules["astrbot.api"].logger = logging.getLogger("fake-astrbot")
    modules["astrbot.api.provider"].Provider = FakeProvider
    modules["astrbot.api.provider"].LLMResponse = FakeResponse
    modules["astrbot.core.provider.entities"].TokenUsage = SimpleNamespace
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_plugin_data_path = lambda: (
        str(root)
    )
    assert (
        sessions.Runtime is not None
    )  # Import before patch.dict restores sys.modules.
    path = Path(__file__).parents[1] / "agy_provider" / "provider.py"
    spec = importlib.util.spec_from_file_location(
        "agy_provider._contract_provider", path
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def tools():
    return SimpleNamespace(
        tools=[
            SimpleNamespace(
                name="echo_probe",
                description="Echo",
                handler=lambda **kw: kw,
                active=True,
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
            )
        ]
    )


def result_messages(response, content):
    return [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": response.completion_text}],
            "tool_calls": [
                {
                    "id": ident,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
                for ident, name, args in zip(
                    response.tools_call_ids,
                    response.tools_call_name,
                    response.tools_call_args,
                )
            ],
        },
        {
            "role": "tool",
            "tool_call_id": response.tools_call_ids[0],
            "content": [{"type": "text", "text": content}],
        },
    ]


class ProviderContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-provider-contract-")
        self.root = Path(self.temp.name)
        self.module = load_provider(self.root)
        self.module.configure(
            Settings(
                enable_tools=True,
                enable_images=True,
                media_roots=(str(self.root),),
                startup_timeout=5,
            )
        )
        self.provider = self.module.AgyProvider(
            {"id": "test", "model": "gemini-test"}, {}
        )
        self.provider.runtime.info = LaunchInfo(sys.executable, "1.5.2", False)
        self.patch = patch(
            "agy_provider.sessions.launch_args",
            lambda _info, _home, use_default_agent=False: [
                sys.executable,
                str(FAKE),
                "--log",
                str(self.root / "trace.jsonl"),
            ],
        )
        self.patch.start()
        self.signal = asyncio.Event()
        self.func_tool = tools()
        # stdlib PNG generator shared with the operator's real acceptance script.
        spec = importlib.util.spec_from_file_location(
            "real_check_helpers",
            Path(__file__).parents[1] / "scripts" / "real_loop_check.py",
        )
        self.helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helpers)
        self.picture = self.root / "7391.png"
        self.helpers.png_digits(self.picture)

    async def asyncTearDown(self):
        await self.provider.terminate()
        self.patch.stop()
        self.temp.cleanup()

    async def chat(self, **kwargs):
        return await self.provider.text_chat(
            session_id="group",
            abort_signal=self.signal,
            func_tool=self.func_tool,
            **kwargs,
        )

    async def test_native_fields_then_contexts_only_result(self):
        first = await self.chat(prompt="prefix:echo")
        self.assertEqual(first.tools_call_name, ["echo_probe"])
        self.assertEqual(first.tools_call_args, [{"message": "prefix:echo"}])
        self.assertEqual(first.completion_text, "Before tool. ")
        contexts = [{"role": "user", "content": "prefix:echo"}] + result_messages(
            first, "ACTUAL-HOST-RESULT"
        )
        final = await self.chat(contexts=contexts)
        self.assertFalse(final.tools_call_name)
        self.assertEqual(final.completion_text, "Answer: ACTUAL-HOST-RESULT")
        contexts += [
            {"role": "assistant", "content": final.completion_text},
            {"role": "user", "content": "next"},
        ]
        await self.chat(contexts=contexts)
        rows = [
            json.loads(line)
            for line in (self.root / "trace.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(r["kind"] == "session_new" for r in rows), 1)

    async def test_direct_tool_calls_result_api(self):
        first = await self.chat(prompt="direct")
        unit = SimpleNamespace(
            to_openai_messages=lambda: result_messages(first, "DIRECT")
        )
        final = await self.chat(prompt="direct", tool_calls_result=unit)
        self.assertEqual(final.completion_text, "Answer: DIRECT")

    async def test_choices_missing_context_and_disabled_media(self):
        for choice in (
            "required",
            {"type": "function", "function": {"name": "echo_probe"}},
        ):
            with self.assertRaisesRegex(AgyError, "TOOL_CHOICE"):
                await self.chat(prompt="x", tool_choice=choice)
        with self.assertRaisesRegex(AgyError, "TOOL_CONTEXT"):
            await self.provider.text_chat(prompt="x", func_tool=self.func_tool)
        response = await self.chat(prompt="plain", tool_choice="none")
        self.assertFalse(response.tools_call_name)
        self.assertEqual(response.completion_text, "Text: plain")
        for field in ("audio_urls", "video_urls", "pdf_urls"):
            with self.assertRaisesRegex(AgyError, "UNSUPPORTED"):
                await self.chat(prompt="x", **{field: ["forbidden"]})
        self.provider.settings = Settings()
        with self.assertRaisesRegex(AgyError, "IMAGES_DISABLED"):
            await self.provider.text_chat(prompt="", image_urls=[str(self.picture)])

    async def test_image_only_dedup_and_tool_mixed_continuation(self):
        part = {"type": "image_url", "image_url": {"url": str(self.picture)}}
        # Inspect actual ACP prompt blocks, rather than fake model visual claims.
        observed = []
        from agy_provider.transport import AcpClient

        original = AcpClient._write

        async def write(client, obj):
            if obj.get("method") == "session/prompt":
                observed.append(obj["params"]["prompt"])
            await original(client, obj)

        with patch.object(AcpClient, "_write", write):
            plain = await self.provider.text_chat(
                prompt="",
                image_urls=[str(self.picture)],
                extra_user_content_parts=[part],
            )
            self.assertTrue(plain.completion_text)
            self.assertEqual([p["type"] for p in observed[-1]], ["image"])
            first = await self.chat(
                prompt="image-tool",
                extra_user_content_parts=[part],
                image_urls=[str(self.picture)],
            )
            self.assertEqual(sum(p["type"] == "image" for p in observed[-1]), 1)
            contexts = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "image-tool"}, part],
                }
            ] + result_messages(first, "IMAGE-HOST")
            final = await self.chat(contexts=contexts, extra_user_content_parts=[part])
            self.assertEqual(final.completion_text, "Answer: IMAGE-HOST")
            self.assertEqual(len(observed), 2)

    async def test_bad_continuation_configuration_cancels_outstanding(self):
        first = await self.chat(prompt="x")
        contexts = [{"role": "user", "content": "x"}] + result_messages(first, "late")
        with self.assertRaisesRegex(AgyError, "CONFIG"):
            await self.chat(contexts=contexts, agy_thought_level="invalid")
        self.assertFalse(self.provider.runtime.turns)
        self.assertFalse(self.provider.runtime.sessions)
        with self.assertRaisesRegex(AgyError, "TOOL_STALE"):
            await self.chat(contexts=contexts)

    async def test_cancelled_media_preparation_does_not_start_download(self):
        self.signal.set()
        with patch("agy_provider.media._download") as download:
            with self.assertRaises(asyncio.CancelledError):
                await self.chat(
                    prompt="x", image_urls=["https://example.com/image.png"]
                )
            download.assert_not_called()

    async def test_history_images_dedup_and_attachment_edit_rebuild(self):
        from agy_provider.media import prepare_media

        image_part = {"type": "image_url", "image_url": {"url": str(self.picture)}}
        history = [
            {"role": "user", "content": [image_part]},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": [image_part]},
        ]
        contexts, _ = await prepare_media(history, None, [], [], self.provider.settings)
        request = normalize(contexts, None, "", None, 160000, images=True)
        blocks, omitted = prompt_blocks(request.messages, 160000)
        self.assertEqual(omitted, 0)
        self.assertEqual(sum(b["type"] == "image" for b in blocks), 1)
        self.assertEqual(blocks[0]["text"].count('"attachment": 1'), 2)

    async def test_unknown_tool_history_and_partial_exchange_refused(self):
        with self.assertRaisesRegex(AgyError, "TOOL_HISTORY"):
            await self.chat(
                contexts=[
                    {"role": "tool", "tool_call_id": "unknown", "content": "result"}
                ]
            )
        first = await self.chat(prompt="x")
        incomplete = [{"role": "user", "content": "x"}] + result_messages(
            first, "result"
        )[:1]
        with self.assertRaisesRegex(AgyError, "TOOL_HISTORY"):
            await self.chat(contexts=incomplete)
        self.assertFalse(self.provider.runtime.turns)

    async def test_editing_old_attachment_rebuilds_session(self):
        from test_media import png

        part = {"type": "image_url", "image_url": {"url": str(self.picture)}}
        first = await self.provider.text_chat(
            prompt="first", image_urls=[str(self.picture)], session_id="image-edit"
        )
        contexts = [
            {"role": "user", "content": [{"type": "text", "text": "first"}, part]},
            {"role": "assistant", "content": first.completion_text},
            {"role": "user", "content": "next"},
        ]
        self.picture.write_bytes(png(pixel=b"\x00\xff\x00"))
        await self.provider.text_chat(contexts=contexts, session_id="image-edit")
        rows = [
            json.loads(line)
            for line in (self.root / "trace.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(r["kind"] == "session_new" for r in rows), 2)

    async def test_host_exception_result_is_returned_once_as_data(self):
        first = await self.chat(prompt="error-tool")
        contexts = [{"role": "user", "content": "error-tool"}] + result_messages(
            first, "error: simulated host tool failure"
        )
        final = await self.chat(contexts=contexts)
        self.assertEqual(
            final.completion_text, "Answer: error: simulated host tool failure"
        )
        self.assertEqual(
            self.provider.runtime.tool_outcomes[first.tools_call_ids[0]], "completed"
        )
