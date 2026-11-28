"""Offline regression tests for B07 (image budgets/cache/security) and B08 (streaming notice segmentation)."""

import asyncio
import base64
import hashlib
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from tests import astrbot_stub  # noqa: F401 -- install offline AstrBot stubs

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.media import ImageFingerprints, prepare_media, validate_image
from agy_provider.messages import digest, normalize, prompt_blocks
from agy_provider.notices import RetryNoticeGuard, UpstreamState
from agy_provider.transport import AcpClient, _AgentTextGuard


def _png_bytes(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (12, 34, 56)) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _png_data_url(width: int = 2, height: int = 2, rgb: tuple[int, int, int] = (12, 34, 56)) -> str:
    return "data:image/png;base64," + base64.b64encode(_png_bytes(width, height, rgb)).decode("ascii")


def _truncated_jpeg_bytes() -> bytes:
    # Valid JPEG header + 2x2 RGB SOF0 + SOS without EOI (\xff\xd9)
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000"
        "ffc0000b080002000201011100"
        "ffda0008010100003f00"
        "1234"
    )


class MediaB07Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-b07-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media_dir = self.root / "media"
        self.media_dir.mkdir(mode=0o700)

    def _settings(self, **overrides):
        base = dict(enable_images=True, image_max_count=8, media_roots=(str(self.media_dir),))
        base.update(overrides)
        return Settings(**base)

    async def test_b07_9_historical_with_0_current_passes_but_9_current_rejects(self):
        settings = self._settings(image_max_count=8)
        contexts = []
        for idx in range(9):
            url = _png_data_url(rgb=(idx + 1, 20, 30))
            contexts.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]})
            contexts.append({"role": "assistant", "content": f"seen {idx}"})

        normalized, extras = await prepare_media(contexts, "only text now", None, None, settings)
        req = normalize(normalized, "only text now", "", extras, 160000, images=True)
        blocks, omitted = prompt_blocks(req.messages, 160000)
        self.assertEqual(omitted, 0)
        self.assertEqual([b["type"] for b in blocks], ["text"])
        self.assertEqual(sum(1 for m in req.messages if isinstance(m["content"], list) and m["content"][0]["type"] == "historical_image"), 9)

        # 9 unique images in current turn still enforced against image_max_count=8
        cur_urls = [_png_data_url(rgb=(idx + 50, 40, 20)) for idx in range(9)]
        with self.assertRaises(AgyError) as caught:
            await prepare_media(contexts, "too many current", None, cur_urls, settings)
        self.assertEqual(caught.exception.code, "IMAGE_COUNT")

    async def test_b07_historical_expired_urls_and_unreadable_paths_never_network_or_block(self):
        outside = self.root / "secret.png"
        outside.write_bytes(_png_bytes(rgb=(9, 9, 9)))
        sym_inside = self.media_dir / "sym.png"
        sym_inside.symlink_to(outside)
        missing_inside = self.media_dir / "deleted.png"

        # Current invalid URL / symlink / pixel bomb still rejected!
        settings = self._settings(image_max_pixels=16)

        with (
            patch("agy_provider.media._download", side_effect=AssertionError("historical URL must never download")),
            patch("agy_provider.media._resolve", side_effect=AssertionError("historical URL must never DNS resolve")),
            patch("agy_provider.media.asyncio.open_connection", side_effect=AssertionError("historical URL must never connect")),
        ):
            contexts = [
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://expired.example.invalid/gone.png"}}]},
                {"role": "assistant", "content": "ok1"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://127.0.0.1/private.png"}}]},
                {"role": "assistant", "content": "ok2"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": str(sym_inside)}}]},
                {"role": "assistant", "content": "ok3"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": str(missing_inside)}}]},
                {"role": "assistant", "content": "ok4"},
                {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"file://{outside}"}}]},
                {"role": "assistant", "content": "ok5"},
            ]
            normalized, extras = await prepare_media(contexts, "current plain question", None, None, settings)
            self.assertEqual(extras, [])
            hist_parts = [m["content"][0] for m in normalized if m["role"] == "user"]
            self.assertTrue(all(p["type"] == "historical_image" and isinstance(p["image_sha256"], str) for p in hist_parts))

        # Current turn preserves all security guards:
        with self.assertRaises(AgyError) as caught_sym:
            await prepare_media([], "cur", None, [str(sym_inside)], settings)
        self.assertEqual(caught_sym.exception.code, "IMAGE_PATH")

        with self.assertRaises(AgyError) as caught_out:
            await prepare_media([], "cur", None, [str(outside)], settings)
        self.assertEqual(caught_out.exception.code, "IMAGE_PATH")

        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            # Test _resolve direct check or via mocked loop.getaddrinfo
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
                with self.assertRaises(AgyError) as caught_ssrf:
                    await prepare_media([], "cur", None, ["https://ssrf.example.test/a.png"], settings)
                self.assertEqual(caught_ssrf.exception.code, "IMAGE_SSRF")

        # Pixel budget & byte limit & truncated JPEG EOI checks preserved
        with self.assertRaises(AgyError) as caught_pix:
            validate_image(_png_bytes(width=8, height=8), "image/png", self._settings(image_max_pixels=16))
        self.assertEqual(caught_pix.exception.code, "IMAGE_PIXELS")

        with self.assertRaises(AgyError) as caught_bytes:
            validate_image(_png_bytes(), "image/png", self._settings(image_max_bytes=16))
        self.assertEqual(caught_bytes.exception.code, "IMAGE_BYTES")

        with self.assertRaises(AgyError) as caught_eoi:
            validate_image(_truncated_jpeg_bytes(), "image/jpeg", settings)
        self.assertEqual(caught_eoi.exception.code, "IMAGE_EOI")

    async def test_b07_image_fingerprints_cache_hit_rewrite_invalidation_and_restart(self):
        if sys.platform == "win32" or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
            self.skipTest("本地媒体读取需要 O_NOFOLLOW/dir_fd 安全接口，Windows 上产品按设计 fail-closed")
        settings = self._settings()
        img_file = self.media_dir / "photo.png"
        bytes_v1 = _png_bytes(width=2, height=2, rgb=(1, 2, 3))
        img_file.write_bytes(bytes_v1)

        cache = ImageFingerprints()
        # Turn 1: current local image cached into ImageFingerprints
        _, extras1 = await prepare_media([], "turn 1", None, [str(img_file)], settings, cache=cache, scope="scope-a")
        expected_v1_sha = digest(extras1[0])

        # Turn 2: historical reference to same untouched path gets exact cached image_sha256 without reading bytes
        contexts2 = [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": str(img_file)}}]},
            {"role": "assistant", "content": "noticed"},
        ]
        norm2, _ = await prepare_media(contexts2, "turn 2", None, None, settings, cache=cache, scope="scope-a")
        self.assertEqual(norm2[0]["content"][0]["image_sha256"], expected_v1_sha)

        # Mutate same path with different bytes & changed size/mtime: stamp miss invalidates cache!
        bytes_v2 = _png_bytes(width=3, height=3, rgb=(200, 100, 50))
        img_file.write_bytes(bytes_v2)
        os.utime(img_file, (img_file.stat().st_atime, img_file.stat().st_mtime + 5))
        norm_mut, _ = await prepare_media(contexts2, "turn 3", None, None, settings, cache=cache, scope="scope-a")
        self.assertNotEqual(norm_mut[0]["content"][0]["image_sha256"], expected_v1_sha)

        # Simulated process restart (fresh cache): same path in history falls back cleanly to opaque ref hash, no error
        fresh_cache = ImageFingerprints()
        norm_restart, _ = await prepare_media(contexts2, "after restart", None, None, settings, cache=fresh_cache, scope="scope-a")
        self.assertEqual(norm_restart[0]["content"][0]["type"], "historical_image")
        self.assertNotEqual(norm_restart[0]["content"][0]["image_sha256"], expected_v1_sha)

        # Resending exact same image in current turn is NOT suppressed by historical turn
        _, extras_resent = await prepare_media(contexts2, "resend", None, [str(img_file)], settings, cache=fresh_cache, scope="scope-a")
        self.assertEqual(len(extras_resent), 1)
        self.assertEqual(extras_resent[0]["type"], "image")

    async def test_b07_continuation_marker_smuggling_rejected_but_known_resend_allowed(self):
        settings = self._settings()
        url_cur = _png_data_url(rgb=(11, 22, 33))
        url_smuggled = _png_data_url(rgb=(44, 55, 66))
        call = {"id": "c-1", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
        contexts = [
            {"role": "user", "content": [{"type": "text", "text": "check"}, {"type": "image_url", "image_url": {"url": url_cur}}]},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "c-1", "content": "ok"},
        ]
        # Repeated extra_user_content_parts with the SAME current image succeeds (4.29.4 native runner repeats it)
        norm_ok, extras_ok = await prepare_media(
            contexts, None, [{"type": "image_url", "image_url": {"url": url_cur}}], None, settings
        )
        self.assertEqual(extras_ok, [])
        self.assertEqual(norm_ok[0]["content"][1]["type"], "historical_image")

        # Smuggling a NEW unauthenticated image during continuation is rejected
        with self.assertRaises(AgyError) as caught:
            await prepare_media(
                contexts, None, [{"type": "image_url", "image_url": {"url": url_smuggled}}], None, settings
            )
        self.assertEqual(caught.exception.code, "INPUT")


    async def test_tool_image_continuation_rejects_smuggled_extra_image_and_disabled_images(self):
        path = "/tool_image_cache/result.png"
        user_image = {"type": "image_url", "image_url": {"url": _png_data_url()}}
        tool_image = {"type": "image_url", "image_url": {
            "url": _png_data_url(rgb=(11, 22, 33)), "id": path,
        }}
        contexts = [
            {"role": "user", "content": [user_image]},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "c-1", "function": {"name": "echo", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "c-1", "content": (
                f"Image returned and cached at path='{path}'. "
                "Review the image below. Use send_message_to_user to send it to the user if satisfied, "
                f"with type='image' and path='{path}'."
            )},
            {"role": "user", "content": [
                {"type": "text", "text": f"[Image from tool 'echo', path='{path}']"},
                tool_image,
            ]},
        ]
        smuggled_url = _png_data_url(rgb=(44, 55, 66))
        for extras, urls, settings, code in (
            ([{"type": "image_url", "image_url": {"url": smuggled_url}}], None, self._settings(), "INPUT"),
            (None, [smuggled_url], self._settings(), "INPUT"),
            # Empty host modalities still append the tool image.
            (None, None, self._settings(enable_images=False), "IMAGES_DISABLED"),
        ):
            with self.subTest(code=code, via_image_urls=urls is not None):
                with self.assertRaises(AgyError) as caught:
                    await prepare_media(contexts, None, extras, urls, settings)
                self.assertEqual(caught.exception.code, code)
                if code == "INPUT":
                    self.assertIn("工具续轮不能附加新的图片。", str(caught.exception))


class NoticeSegmentationB08Tests(unittest.TestCase):
    def test_b08_acp_client_on_update_no_newline_prefix_flushed_and_retries_segmented_across_tools(self):
        client = AcpClient(Settings())
        client.session_id = "sess-1"
        client.active = True

        # Observe via real _on_update: pre-tool retry notice (503) followed by no-newline text prefix, then tool_call
        client._on_update({
            "sessionId": "sess-1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "⚠ API error (attempt 1): UNAVAILABLE (code 503): no capacity\n先看"}},
        })
        client._on_update({
            "sessionId": "sess-1",
            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Agent"}},
        })
        # Pre-tool segment saw 1 filtered notice, TRANSIENT_RETRY classification, and RATE_LIMIT public code;
        # held "Agent" (possible terminal marker prefix) without newline waits in pending until flushed by tool_call.
        self.assertEqual(client.text_guard.retries.filtered_count, 1)
        self.assertEqual(client.text_guard.retries.state, UpstreamState.TRANSIENT_RETRY)
        self.assertEqual(client.text_guard.retries.error_code, "RATE_LIMIT")

        # tool_call with no broker raises TOOLS_DISABLED AFTER flushing pending "Agent" and resetting retry segment!
        with self.assertRaises(AgyError) as caught:
            client._on_update({"sessionId": "sess-1", "update": {"sessionUpdate": "tool_call", "toolCallId": "t1", "name": "ls"}})
        self.assertEqual(caught.exception.code, "TOOLS_DISABLED")

        emitted = []
        while not client.events.empty():
            evt = client.events.get_nowait()
            emitted.append((evt.kind, evt.text))
        self.assertEqual(emitted, [("text", "先看"), ("text", "Agent")])
        # begin_segment cleared transient pre-tool error_code/state, while keeping prompt-level filtered_count!
        self.assertEqual(client.text_guard.retries.error_code, "")
        self.assertEqual(client.text_guard.retries.state, UpstreamState.UNKNOWN)
        self.assertEqual(client.text_guard.filtered_count, 1)

    def test_b08_split_notices_and_post_tool_terminal_vs_quoted_prose(self):
        guard = _AgentTextGuard()
        # Pre-tool retry notice split across small chunks + prefix without trailing newline
        out = []
        for chunk in ("⚠ API ", "error (attempt 1): 503 Unavailable\n", "步骤一"):
            out.append(guard.feed(chunk))
        tail = guard.end_segment(before_tool=True)
        out.append(tail)
        self.assertEqual("".join(out), "步骤一")
        self.assertEqual(guard.retries.error_code, "")
        self.assertEqual(guard.filtered_count, 1)

        # Normal markdown prose quoting markers inline/quoted is NEVER falsely stripped
        quoted_1 = "文档引用：⚠ API error (attempt 1): 429\n"
        quoted_2 = "说明：Agent execution terminated due to error.\n"
        preserved = guard.feed(quoted_1) + guard.feed(quoted_2) + guard.end_segment()
        self.assertEqual(preserved, quoted_1 + quoted_2)
        self.assertEqual(guard.filtered_count, 1)

        # Split stream-interrupted notice + detail-free 429 + split terminal marker across chunks
        guard2 = _AgentTextGuard()
        seg1 = guard2.feed("⚠ API error (attempt 2): 429 RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota)\n")
        self.assertEqual(seg1, "")
        self.assertTrue(guard2.retries.detail_free_seen)
        self.assertEqual(guard2.retries.state, UpstreamState.RATE_LIMIT_OR_CAPACITY)
        self.assertEqual(guard2.retries.error_code, "RATE_LIMIT")

        seg_irq = guard2.feed("⚠ The stream was ") + guard2.feed("interrupted. Please continue\n")
        self.assertEqual(seg_irq, "")
        self.assertEqual(guard2.retries.error_code, "NETWORK")

        with self.assertRaises(AgyError) as caught_term:
            for piece in ("Agent execution ", "terminated due to error."):
                self.assertEqual(guard2.feed(piece), "")
            guard2.end_segment()
        self.assertEqual(caught_term.exception.code, "NETWORK")
        self.assertEqual(guard2.filtered_count, 3)


if __name__ == "__main__":
    unittest.main()
