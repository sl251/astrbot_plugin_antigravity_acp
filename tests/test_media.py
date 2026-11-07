"""Offline media security contracts; sockets and DNS are explicitly faked."""

import asyncio
import base64
import binascii
import socket
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agy_provider.errors import AgyError
from agy_provider.media import PNG, _download, _read_body, _resolve, prepare_media
from agy_provider.messages import digest


def png(width=2, height=1, pixel=b"\xff\x00\x00"):
    def chunk(kind, body):
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", binascii.crc32(kind + body) & 0xFFFFFFFF)
        )

    raw = (b"\0" + pixel * width) * height
    return (
        PNG
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def uri(data):
    return "data:image/png;base64," + base64.b64encode(data).decode()


def part(data):
    return {"type": "image_url", "image_url": {"url": uri(data), "id": None}}


def settings(root, **kwargs):
    values = dict(
        enable_images=True,
        media_roots=(str(root),),
        image_max_count=8,
        image_max_bytes=1024 * 1024,
        image_max_pixels=20000,
        image_timeout=2,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


class FakeWriter:
    def __init__(self):
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def response(data):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader, FakeWriter()


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-media-")
        self.root = Path(self.temp.name)
        self.config = settings(self.root)
        self.file = self.root / "image.png"
        self.file.write_bytes(png())

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_default_disabled_rejects_all_entrypoints(self):
        config = settings(self.root, enable_images=False)
        cases = [
            ([], "q", [], [str(self.file)]),
            ([], "", [part(png())], []),
            ([{"role": "user", "content": [part(png())]}], None, [], []),
        ]
        for args in cases:
            with (
                self.subTest(args=args),
                self.assertRaisesRegex(AgyError, "IMAGES_DISABLED"),
            ):
                await prepare_media(*args, config)

    async def test_pure_image_and_double_image(self):
        ctx, extras = await prepare_media([], "", [], [str(self.file)], self.config)
        self.assertEqual(ctx, [])
        self.assertEqual(extras[0]["type"], "image")
        self.assertEqual(base64.b64decode(extras[0]["data"]), png())
        ctx, extras = await prepare_media(
            [{"role": "user", "content": [part(png())]}],
            None,
            [part(png(pixel=b"\x00\xff\x00"))],
            [],
            self.config,
        )
        self.assertEqual(len(ctx[0]["content"]), 2)
        self.assertEqual(extras, [])

    async def test_all_entrypoints_deduplicated_by_content(self):
        raw = [
            {"role": "user", "content": [{"type": "text", "text": "look"}, part(png())]}
        ]
        ctx, extras = await prepare_media(
            raw, None, [part(png())], [str(self.file)], self.config
        )
        self.assertEqual(len(ctx[0]["content"]), 2)
        self.assertEqual(extras, [])
        self.assertEqual(raw[0]["content"][1]["type"], "image_url")
        _, extras = await prepare_media(
            [], "q", [part(png())], [str(self.file)], self.config
        )
        self.assertEqual(len(extras), 1)

    async def test_contexts_only_preserves_text_without_duplicate_extra(self):
        ctx, extra = await prepare_media(
            [{"role": "user", "content": "look\nextra"}],
            None,
            [{"type": "text", "text": "extra"}],
            [],
            self.config,
        )
        self.assertEqual(ctx[0]["content"], "look\nextra")
        self.assertEqual(extra, [])

    async def test_file_edit_changes_history_identity(self):
        raw = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": str(self.file)}}
                ],
            }
        ]
        before, _ = await prepare_media(raw, None, [], [], self.config)
        self.file.write_bytes(png(pixel=b"\x00\xff\x00"))
        after, _ = await prepare_media(raw, None, [], [], self.config)
        self.assertNotEqual(digest(before), digest(after))
        self.assertTrue(self.file.exists())
        self.assertEqual(list(self.root.iterdir()), [self.file])

    async def test_path_traversal_and_unauthorized_path(self):
        for source in [
            str(self.root / ".." / self.root.name / "image.png"),
            "/etc/passwd",
            "image.png",
            "file:///etc/passwd",
            "file://localhost/etc/passwd",
            "file:///tmp/%2e%2e/etc/passwd",
        ]:
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(AgyError, "IMAGE_PATH"),
            ):
                await prepare_media([], "q", [], [source], self.config)

    async def test_symlink_file_and_parent_directory_rejected(self):
        link = self.root / "link.png"
        link.symlink_to(self.file)
        parent = self.root / "linkdir"
        parent.symlink_to(self.root, target_is_directory=True)
        for source in (link, parent / "image.png"):
            with self.assertRaisesRegex(AgyError, "IMAGE_PATH"):
                await prepare_media([], "q", [], [str(source)], self.config)

    async def test_non_regular_file_rejected(self):
        with self.assertRaisesRegex(AgyError, "IMAGE_PATH"):
            await prepare_media([], "q", [], [str(self.root)], self.config)

    async def test_non_image_wrong_mime_truncated_crc_and_compression_bomb(self):
        valid = png()
        changed = bytearray(valid)
        changed[-1] ^= 1
        for data in (b"%PDF-1.7", valid[:-2], bytes(changed), valid + b"trailing"):
            with self.subTest(data=data[:8]), self.assertRaises(AgyError):
                await prepare_media([], "q", [part(data)], [], self.config)
        wrong = {
            "type": "image",
            "mimeType": "image/jpeg",
            "data": base64.b64encode(valid).decode(),
        }
        with self.assertRaisesRegex(AgyError, "IMAGE_MIME"):
            await prepare_media([], "q", [wrong], [], self.config)
        # Valid CRC/zlib, but decoded data much larger than IHDR dimensions.
        huge = png(width=300, height=200)
        small_header = png(width=1, height=1)[8:33]
        bomb = PNG + small_header + huge[33:]
        with self.assertRaises(AgyError):
            await prepare_media([], "q", [part(bomb)], [], self.config)

    async def test_byte_pixel_and_total_count_limits(self):
        for config in (
            settings(self.root, image_max_bytes=10),
            settings(self.root, image_max_pixels=1),
        ):
            with self.assertRaises(AgyError):
                await prepare_media([], "", [], [str(self.file)], config)
        with self.assertRaisesRegex(AgyError, "IMAGE_COUNT"):
            await prepare_media(
                [],
                "",
                [part(png()), part(png(pixel=b"\0\0\0"))],
                [],
                settings(self.root, image_max_count=1),
            )
        with self.assertRaisesRegex(AgyError, "IMAGE_BYTES"):
            await prepare_media(
                [],
                "",
                [part(png()), part(png(pixel=b"\0\0\0"))],
                [],
                settings(self.root, image_max_bytes=len(png()) + 10),
            )

    async def test_audio_video_pdf_blocks_rejected(self):
        for kind in ("audio", "audio_url", "video_url", "file"):
            with self.assertRaisesRegex(AgyError, "UNSUPPORTED"):
                await prepare_media([], "q", [{"type": kind}], [], self.config)

    async def test_malicious_url_schemes_userinfo_controls_ports(self):
        for url in (
            "http://user:pw@example.com/x",
            "http://example.com:22/x",
            "http://example.com/x\r\nHeader: y",
            "http://example.com\\@127.0.0.1/x",
            "gopher://127.0.0.1/x",
            "http://[::1%25lo]/x",
        ):
            with self.subTest(url=url), self.assertRaises(AgyError):
                await prepare_media([], "q", [], [url], self.config)

    async def test_private_loopback_metadata_and_ipv6_addresses(self):
        loop = asyncio.get_running_loop()
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.2",
            "192.168.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "::ffff:127.0.0.1",
            "2002:7f00:1::",
            "64:ff9b::7f00:1",
        ):
            records = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))]
            with patch.object(loop, "getaddrinfo", return_value=records):
                with (
                    self.subTest(address=address),
                    self.assertRaisesRegex(AgyError, "IMAGE_SSRF"),
                ):
                    await _resolve("example.com", 80)

    async def test_dns_mixed_public_private_records_rejected(self):
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))
            for address in ("93.184.216.34", "127.0.0.1")
        ]
        with patch.object(
            asyncio.get_running_loop(), "getaddrinfo", return_value=records
        ):
            with self.assertRaisesRegex(AgyError, "IMAGE_SSRF"):
                await _resolve("example.com", 80)

    async def test_dns_pin_tls_name_and_redirect_revalidation(self):
        first = response(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/x\r\n\r\n")
        with patch(
            "agy_provider.media._resolve",
            side_effect=["93.184.216.34", AgyError("IMAGE_SSRF", "blocked")],
        ) as resolve:
            with patch(
                "agy_provider.media.asyncio.open_connection", return_value=first
            ) as connect:
                with self.assertRaisesRegex(AgyError, "IMAGE_SSRF"):
                    await _download("https://example.com/img", self.config)
                self.assertEqual(resolve.await_args_list[1].args, ("127.0.0.1", 80))
                self.assertEqual(connect.await_args.args, ("93.184.216.34", 443))
                self.assertEqual(
                    connect.await_args.kwargs["server_hostname"], "example.com"
                )
        self.assertTrue(first[1].closed)

    async def test_download_mime_length_failures_and_success(self):
        data = png()
        samples = [
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nhtml", "IMAGE_MIME"),
            (
                b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 999999999\r\n\r\n",
                "IMAGE_BYTES",
            ),
            (b"HTTP/1.1 404 Nope\r\n\r\n", "IMAGE_DOWNLOAD"),
            (
                b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 2\r\n\r\nx",
                "IMAGE_DOWNLOAD",
            ),
        ]
        for raw, code in samples:
            pair = response(raw)
            with (
                patch("agy_provider.media._resolve", return_value="93.184.216.34"),
                patch("agy_provider.media.asyncio.open_connection", return_value=pair),
            ):
                with self.subTest(code=code), self.assertRaisesRegex(AgyError, code):
                    await prepare_media(
                        [], "q", [], ["https://example.com/x"], self.config
                    )
            self.assertTrue(pair[1].closed)
        raw = (
            b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: "
            + str(len(data)).encode()
            + b"\r\n\r\n"
            + data
        )
        with (
            patch("agy_provider.media._resolve", return_value="93.184.216.34"),
            patch(
                "agy_provider.media.asyncio.open_connection", return_value=response(raw)
            ),
        ):
            _, extras = await prepare_media(
                [], "q", [], ["https://example.com/x"], self.config
            )
            self.assertEqual(extras[0]["mimeType"], "image/png")

    async def test_download_timeout_cancel_close_and_chunk_budget(self):
        reader, writer = asyncio.StreamReader(), FakeWriter()
        with (
            patch("agy_provider.media._resolve", return_value="93.184.216.34"),
            patch(
                "agy_provider.media.asyncio.open_connection",
                return_value=(reader, writer),
            ),
        ):
            with self.assertRaisesRegex(AgyError, "IMAGE_TIMEOUT"):
                await _download(
                    "https://example.com/x", settings(self.root, image_timeout=0.01)
                )
        self.assertTrue(writer.closed)
        reader, _ = response(b"10\r\n" + b"x" * 16 + b"\r\n0\r\n\r\n")
        with self.assertRaisesRegex(AgyError, "IMAGE_BYTES"):
            await _read_body(reader, {"transfer-encoding": "chunked"}, 10)

    async def test_jpeg_dimensions_and_unsupported_media(self):
        # Minimal marker envelope is enough to exercise header validation. JPEG
        # entropy decoding belongs to AGY; pixel limits are checked before send.
        sof = (
            b"\xff\xc0"
            + struct.pack(">H", 11)
            + b"\x08"
            + struct.pack(">HH", 2, 3)
            + b"\x01\x01\x11\x00"
        )
        sos = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"
        jpeg = b"\xff\xd8" + sof + sos + b"\x01\x02\x03\xff\xd9"
        image = {
            "type": "image",
            "mimeType": "image/jpeg",
            "data": base64.b64encode(jpeg).decode(),
        }
        _, result = await prepare_media([], "q", [image], [], self.config)
        self.assertEqual(result[0]["mimeType"], "image/jpeg")
        with self.assertRaisesRegex(AgyError, "IMAGE_PIXELS"):
            await prepare_media(
                [], "q", [image], [], settings(self.root, image_max_pixels=5)
            )
        for broken in (
            jpeg[:-1],
            b"\xff\xd8" + sof + b"\xff\xd9",
            b"GIF89a",
            b"RIFF0000WEBP",
        ):
            bad = dict(image, data=base64.b64encode(broken).decode())
            with self.assertRaises(AgyError):
                await prepare_media([], "q", [bad], [], self.config)

    async def test_download_cancel_closes_socket(self):
        reader, writer = asyncio.StreamReader(), FakeWriter()
        with (
            patch("agy_provider.media._resolve", return_value="93.184.216.34"),
            patch(
                "agy_provider.media.asyncio.open_connection",
                return_value=(reader, writer),
            ),
        ):
            task = asyncio.create_task(_download("https://example.com/x", self.config))
            while not writer.data:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(writer.closed)

    async def test_source_cache_deduplicates_download(self):
        with patch(
            "agy_provider.media._download", return_value=(png(), "image/png")
        ) as download:
            _, result = await prepare_media(
                [], "q", [], ["https://example.com/x"] * 20, self.config
            )
        self.assertEqual(len(result), 1)
        download.assert_awaited_once()

    async def test_tool_continuation_repeated_images_not_new_input(self):
        contexts = [
            {"role": "user", "content": [part(png())]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        ]
        normalized, extras = await prepare_media(
            contexts, None, [part(png())], [str(self.file)], self.config
        )
        self.assertEqual(extras, [])
        self.assertEqual(len(normalized[0]["content"]), 1)
        self.assertEqual(normalized[-1], contexts[-1])
        with self.assertRaisesRegex(AgyError, "INPUT"):
            await prepare_media(
                contexts, None, [part(png(pixel=b"\0\0\0"))], [], self.config
            )

    async def test_tool_continuation_cannot_attach_older_turn_image(self):
        contexts = [
            {"role": "user", "content": [part(png())]},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call-2"}]},
            {"role": "tool", "tool_call_id": "call-2", "content": "done"},
        ]
        with self.assertRaisesRegex(AgyError, "INPUT"):
            await prepare_media(contexts, None, [part(png())], [], self.config)

    async def test_non_user_images_rejected_before_file_or_network_read(self):
        inputs = [
            {
                "type": "image_url",
                "image_url": {"url": "https://example.com/image.png"},
            },
            {"type": "image_url", "image_url": {"url": str(self.file)}},
            {
                "type": "image",
                "mimeType": "image/png",
                "data": base64.b64encode(png()).decode(),
            },
        ]
        for role in ("assistant", "tool", "system", "developer"):
            for image in inputs:
                with self.subTest(role=role, kind=image["type"]):
                    with (
                        patch("agy_provider.media._download") as download,
                        patch("agy_provider.media._local") as local,
                        patch("agy_provider.media.validate_image") as validate,
                    ):
                        with self.assertRaisesRegex(AgyError, "UNSUPPORTED"):
                            await prepare_media(
                                [{"role": role, "content": [image]}],
                                "q",
                                [],
                                [],
                                self.config,
                            )
                        download.assert_not_awaited()
                        local.assert_not_called()
                        validate.assert_not_called()

    async def test_missing_file_error_redacts_source(self):
        with self.assertRaises(AgyError) as caught:
            await prepare_media(
                [], "q", [], [str(self.root / "private-name.png")], self.config
            )
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotIn("private-name", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
