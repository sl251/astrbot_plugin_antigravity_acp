import base64
import struct
import unittest

from agy_provider.config import Settings
from agy_provider.errors import AgyError
from agy_provider.media import _jpeg, validate_image

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"


def _seg(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def _sof(marker: int = 0xC0, *, bits: int = 8, width: int = 4, height: int = 4, components: int = 3) -> bytes:
    header = bytes([bits]) + struct.pack(">HH", height, width) + bytes([components])
    comps = b"".join(bytes([idx + 1, 0x11, 0]) for idx in range(components))
    return _seg(marker, header + comps)


def _sos(components: int = 3) -> bytes:
    body = bytes([components]) + b"".join(bytes([idx + 1, 0x00]) for idx in range(components)) + b"\x00\x3f\x00"
    return _seg(0xDA, body)


def _baseline_jpeg(*, extra_segment: bytes = b"", entropy: bytes = b"\x12\x34\xff\x00\x56\xff\xd0\x78") -> bytes:
    return (
        SOI
        + _seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        + extra_segment
        + _sof(0xC0)
        + _seg(0xC4, b"\x00\x01\x02\x03")
        + _sos()
        + entropy
        + EOI
    )


class JpegMediaTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(enable_images=True)

    def test_wechat_24b_trailer_after_eoi_accepted_and_trimmed(self):
        clean = _baseline_jpeg()
        trailer = b"WX_META_24_BYTES_TRAILER"
        self.assertEqual(len(trailer), 24)
        block = validate_image(clean + trailer, "image/jpeg", self.settings)
        self.assertEqual(block["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(block["data"]), clean)

    def test_trailer_with_embedded_eoi_trims_at_first_structural_eoi(self):
        # Segment payload containing FFD9 must be skipped; trailer FFD9 must be ignored.
        clean = _baseline_jpeg(extra_segment=_seg(0xE1, b"EXIF\x00\x00\xff\xd9thumb"))
        trailer = b"wx\xff\xd9extra_meta\xff\xd9"
        block = validate_image(clean + trailer, "image/jpeg", self.settings)
        self.assertEqual(base64.b64decode(block["data"]), clean)

    def test_truncated_jpeg_without_eoi_raises_image_eoi(self):
        clean_without_eoi = _baseline_jpeg()[:-2]
        samples = (
            clean_without_eoi,
            clean_without_eoi + b"\xff",  # trailing 0xFF inside entropy
            SOI + _seg(0xFE, b"comment only"),
            b"\xff\xd8\xff",
            b"\xff\xd8\xff\xff\xff",
        )
        for sample in samples:
            with self.subTest(sample=sample[-8:]):
                with self.assertRaises(AgyError) as caught:
                    validate_image(sample, "image/jpeg", self.settings)
                self.assertEqual(caught.exception.code, "IMAGE_EOI")

    def test_eoi_before_frame_or_scan_rejected(self):
        for sample in (
            SOI + EOI,
            SOI + _seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00") + EOI,
            SOI + _sof(0xC0) + EOI,
        ):
            with self.subTest(length=len(sample)):
                with self.assertRaises(AgyError) as caught:
                    validate_image(sample, "image/jpeg", self.settings)
                self.assertEqual(caught.exception.code, "IMAGE_FORMAT")
                self.assertIn("JPEG 缺少图像帧或扫描数据。", str(caught.exception))

        with self.assertRaises(AgyError) as caught:
            validate_image(SOI + _sos() + b"\x11\x22" + EOI, "image/jpeg", self.settings)
        self.assertEqual(caught.exception.code, "IMAGE_FORMAT")
        self.assertIn("JPEG 扫描头无效或缺少图像帧。", str(caught.exception))

    def test_progressive_multiscan_jpeg_accepted_and_trimmed(self):
        progressive = (
            SOI
            + _seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + _sof(0xC2)
            + _seg(0xC4, b"\x00\x01")
            + _sos(3)
            + b"\xaa\xff\x00\xbb\xff\xd1\xcc"
            + _seg(0xC4, b"\x10\x02")
            + _sos(1)
            + b"\xdd\xee\xff\x00\x11"
            + _sos(1)
            + b"\x22\x33"
            + EOI
        )
        trailer = b"wechat_progressive_meta\xff\xd9"
        block = validate_image(progressive + trailer, "image/jpeg", self.settings)
        self.assertEqual(base64.b64decode(block["data"]), progressive)

    def test_marker_structure_error_messages(self):
        cases = (
            (
                SOI + _seg(0xE0, b"test") + b"\x00\x01",
                "JPEG 标记结构无效。",
            ),
            (
                SOI + b"\xff\xe0\x00",
                "JPEG 标记长度被截断。",
            ),
            (
                SOI + b"\xff\xe0\x00\x01",
                "JPEG 数据段长度无效或被截断。",
            ),
            (
                SOI + b"\xff\xe0\x00\x20abc",
                "JPEG 数据段长度无效或被截断。",
            ),
            (
                SOI + _sof(0xC1),
                "JPEG 包含无效或不支持的标记。",
            ),
            (
                SOI + _seg(0xDC, b"\x00\x10"),
                "JPEG 包含无效或不支持的标记。",
            ),
            (
                SOI + _sof(0xC0, bits=12),
                "JPEG 帧头无效或不是 8 位图像。",
            ),
            (
                SOI + _sof(0xC0) + _sof(0xC0),
                "JPEG 帧头无效或不是 8 位图像。",
            ),
            (
                SOI + _sof(0xC0, components=4),
                "JPEG 仅支持 8 位灰度或 RGB。",
            ),
        )
        for sample, expected_text in cases:
            with self.subTest(expected=expected_text):
                with self.assertRaises(AgyError) as caught:
                    validate_image(sample, "image/jpeg", self.settings)
                self.assertEqual(caught.exception.code, "IMAGE_FORMAT")
                self.assertIn(expected_text, str(caught.exception))

        # Repeated 0xFF fill bytes and malformed entropy bytes must always terminate.
        with self.assertRaises(AgyError) as caught:
            _jpeg(SOI + b"\xff" * 2048, self.settings.image_max_pixels)
        self.assertEqual(caught.exception.code, "IMAGE_EOI")

    def test_byte_budget_checked_before_trimming(self):
        settings = Settings(enable_images=True, image_max_bytes=1024)
        padded_comment = _seg(0xFE, b"x" * 900)
        clean = _baseline_jpeg(extra_segment=padded_comment)
        self.assertLessEqual(len(clean), 1024)
        with_trailer = clean + b"t" * (1025 - len(clean))
        self.assertGreater(len(with_trailer), 1024)

        # Trimmed image fits within budget, but raw payload must be rejected first.
        validate_image(clean, "image/jpeg", settings)
        with self.assertRaises(AgyError) as caught:
            validate_image(with_trailer, "image/jpeg", settings)
        self.assertEqual(caught.exception.code, "IMAGE_BYTES")


if __name__ == "__main__":
    unittest.main()
