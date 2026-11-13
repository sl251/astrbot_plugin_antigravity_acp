"""Bounded image inputs, with no credential access, proxies or temporary files.

PNG and JPEG are accepted. PNG's compressed raster is checked incrementally;
JPEG's frame dimensions and marker envelope are checked before handing the bytes
to the model. Animated PNG, other image formats and other media are rejected.
"""

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import os
import socket
import ssl
import stat
import struct
import zlib
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from .errors import AgyError

MIMES = {"image/png", "image/jpeg"}
PNG = b"\x89PNG\r\n\x1a\n"


def _error(code="IMAGE", message="图片无效、格式不支持或已失效。"):
    return AgyError(code, message)


def _pixels(width, height, limit):
    if not width or not height or width * height > limit:
        raise _error("IMAGE_PIXELS", "图片尺寸超过解码像素预算。")


def _png(data, pixel_limit):
    pos, header, ended, compressed = 8, None, False, []
    saw_data = False
    chunks = 0
    while pos < len(data):
        chunks += 1
        if chunks > 4096:
            raise _error("IMAGE_FORMAT", "图片分块数量超过上限。")
        if pos + 12 > len(data):
            raise _error()
        size = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4 : pos + 8]
        end = pos + 12 + size
        if end > len(data):
            raise _error()
        body = data[pos + 8 : end - 4]
        crc = struct.unpack_from(">I", data, end - 4)[0]
        if binascii.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise _error()
        if header is None and kind != b"IHDR":
            raise _error()
        if kind == b"IHDR":
            if header is not None or size != 13:
                raise _error()
            width, height, depth, color, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", body
            )
            _pixels(width, height, pixel_limit)
            if depth != 8 or color not in {0, 2, 4, 6} or comp or filt or interlace:
                raise _error("IMAGE_FORMAT", "PNG 仅支持非交错 8 位灰度/RGB/RGBA。")
            header = (height, width * {0: 1, 2: 3, 4: 2, 6: 4}[color] + 1)
        elif kind in {b"zTXt", b"iCCP", b"iTXt"}:
            raise _error("IMAGE_FORMAT", "暂不支持含压缩或国际文本元数据的 PNG。")
        elif kind == b"acTL":
            raise _error("IMAGE_FORMAT", "不支持动画图片。")
        elif kind == b"IDAT":
            saw_data = True
            compressed.append(body)
        elif kind == b"IEND":
            if size or not saw_data or end != len(data):
                raise _error()
            ended = True
        elif kind[0] & 0x20 == 0 and kind != b"PLTE":
            raise _error()
        pos = end
    if not ended or not header:
        raise _error()
    height, row = header
    expected, emitted = height * row, 0
    decoder = zlib.decompressobj()
    try:
        for chunk in compressed:
            while chunk:
                output = decoder.decompress(chunk, 65536)
                chunk = decoder.unconsumed_tail
                if emitted + len(output) > expected:
                    raise _error()
                start = (-emitted) % row
                if any(output[i] > 4 for i in range(start, len(output), row)):
                    raise _error()
                emitted += len(output)
        if not decoder.eof or decoder.unused_data or emitted != expected:
            raise _error()
    except zlib.error:
        raise _error() from None


def _jpeg(data, pixel_limit):
    pos, frame, saw_scan = 2, False, False
    # JPEG entropy data is not decoded here; every SOF must agree with the
    # accepted frame and dimensions. DNL/unknown frames are rejected.
    while pos < len(data):
        if data[pos] != 0xFF:
            raise _error("IMAGE_FORMAT", "JPEG 标记结构无效。")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            if not frame or not saw_scan:
                raise _error("IMAGE_FORMAT", "JPEG 缺少图像帧或扫描数据。")
            # Only an EOI reached through the marker structure ends the image.
            # Ignore client trailers, including trailers containing another EOI.
            return pos
        if marker not in {0xC0, 0xC2, 0xC4, 0xDB, 0xDD, 0xDA, 0xFE, *range(0xE0, 0xF0)}:
            raise _error("IMAGE_FORMAT", "JPEG 包含无效或不支持的标记。")
        if pos + 2 > len(data):
            raise _error("IMAGE_FORMAT", "JPEG 标记长度被截断。")
        size = struct.unpack_from(">H", data, pos)[0]
        if size < 2 or pos + size > len(data):
            raise _error("IMAGE_FORMAT", "JPEG 数据段长度无效或被截断。")
        body = data[pos + 2 : pos + size]
        pos += size
        if marker in {0xC0, 0xC2}:
            if frame or len(body) < 6 or body[0] != 8:
                raise _error("IMAGE_FORMAT", "JPEG 帧头无效或不是 8 位图像。")
            height, width = struct.unpack_from(">HH", body, 1)
            _pixels(width, height, pixel_limit)
            components = body[5]
            if components not in {1, 3} or len(body) != 6 + 3 * components:
                raise _error("IMAGE_FORMAT", "JPEG 仅支持 8 位灰度或 RGB。")
            frame = True
        elif marker == 0xDA:
            if not frame or not body or len(body) != 1 + 2 * body[0] + 3:
                raise _error("IMAGE_FORMAT", "JPEG 扫描头无效或缺少图像帧。")
            saw_scan = True
            # Walk entropy bytes, honoring stuffed FF bytes and restart markers.
            while pos < len(data):
                if data[pos] != 0xFF:
                    pos += 1
                    continue
                if pos + 1 >= len(data):
                    break
                if data[pos + 1] == 0 or 0xD0 <= data[pos + 1] <= 0xD7:
                    pos += 2
                    continue
                break
    raise _error("IMAGE_EOI", "JPEG 缺少有效 EOI 结束标记，图片可能被截断。")


def validate_image(data: bytes, declared: str | None, settings) -> dict:
    if not data or len(data) > settings.image_max_bytes:
        raise _error("IMAGE_BYTES", "图片超过字节预算。")
    if data.startswith(PNG):
        mime = "image/png"
        _png(data, settings.image_max_pixels)
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
        data = data[:_jpeg(data, settings.image_max_pixels)]
    else:
        raise _error("IMAGE_FORMAT", "仅支持实际内容为 PNG 或 JPEG 的图片。")
    if declared is not None and declared.lower().split(";", 1)[0].strip() != mime:
        raise _error("IMAGE_MIME", "图片 MIME 与实际文件签名不一致。")
    return {"type": "image", "mimeType": mime, "data": base64.b64encode(data).decode()}


def _local(source: str, settings) -> bytes:
    path = Path(source)
    if not path.is_absolute() or ".." in path.parts:
        raise _error("IMAGE_PATH", "图片须位于已授权媒体目录，不允许路径穿越。")
    roots = [Path(root) for root in settings.media_roots]
    if not any(path.is_relative_to(root) and root.is_absolute() for root in roots):
        raise _error("IMAGE_PATH", "图片不在已授权媒体目录内。")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise _error("IMAGE_PATH", "此平台没有安全的无符号链接媒体读取接口。")
    descriptor = None
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        for index, component in enumerate(path.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(path.parts) - 2:
                flags |= os.O_DIRECTORY
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _error("IMAGE_PATH", "图片必须为普通文件。")
        if info.st_size > settings.image_max_bytes:
            raise _error("IMAGE_BYTES", "图片超过字节预算。")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(settings.image_max_bytes + 1)
        if len(data) > settings.image_max_bytes:
            raise _error("IMAGE_BYTES", "图片超过字节预算。")
        return data
    except (OSError, ValueError):
        raise _error("IMAGE_PATH", "图片不可读或路径包含符号链接。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _url(source):
    if (
        not isinstance(source, str)
        or any(ord(c) < 33 for c in source)
        or "\\" in source
    ):
        raise _error("IMAGE_URL", "无效的图片 URL。")
    try:
        parsed = urlsplit(source)
        hostname = (parsed.hostname or "").encode("idna").decode("ascii")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, UnicodeError):
        raise _error("IMAGE_URL", "无效的图片 URL。") from None
    if (
        parsed.scheme not in {"https", "http"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {80, 443}
        or "%" in hostname
    ):
        raise _error("IMAGE_URL", "图片 URL 仅允许无凭据的公开 HTTP(S) 标准端口。")
    return parsed, hostname, port


async def _resolve(hostname, port):
    try:
        records = await asyncio.get_running_loop().getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        )
    except OSError:
        raise _error("IMAGE_DOWNLOAD", "图片主机解析失败。") from None
    addresses = []
    for _, _, _, _, addr in records:
        ip = ipaddress.ip_address(addr[0])
        # Reject all non-public records, not just the address chosen below.
        if (
            not ip.is_global
            or ip.is_multicast
            or getattr(ip, "ipv4_mapped", None)
            or getattr(ip, "sixtofour", None)
            or getattr(ip, "teredo", None)
            or (ip.version == 6 and ip in ipaddress.ip_network("64:ff9b::/96"))
        ):
            raise _error("IMAGE_SSRF", "图片 URL 指向非公开网络，已拒绝。")
        addresses.append(str(ip))
    if not addresses:
        raise _error("IMAGE_DOWNLOAD", "图片主机没有可用地址。")
    return addresses[0]


async def _read_body(reader, headers, limit):
    encoding = headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise _error("IMAGE_DOWNLOAD", "图片响应不支持压缩传输编码。")
    transfer = headers.get("transfer-encoding", "").lower()
    if transfer:
        if transfer != "chunked" or "content-length" in headers:
            raise _error("IMAGE_DOWNLOAD", "图片响应传输编码无效。")
        parts, count = [], 0
        while True:
            line = await reader.readline()
            if len(line) > 128 or not line.endswith(b"\r\n"):
                raise _error("IMAGE_DOWNLOAD", "图片分块响应无效。")
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise _error("IMAGE_DOWNLOAD", "图片分块响应无效。") from None
            if size < 0 or count + size > limit:
                raise _error("IMAGE_BYTES", "图片超过字节预算。")
            if not size:
                return b"".join(parts)
            parts.append(await reader.readexactly(size))
            if await reader.readexactly(2) != b"\r\n":
                raise _error("IMAGE_DOWNLOAD", "图片分块响应无效。")
            count += size
    length = headers.get("content-length")
    if length is not None:
        if not length.isdigit():
            raise _error("IMAGE_DOWNLOAD", "图片响应长度无效。")
        size = int(length)
        if size > limit:
            raise _error("IMAGE_BYTES", "图片超过字节预算。")
        return await reader.readexactly(size)
    parts, count = [], 0
    while chunk := await reader.read(min(65536, limit + 1 - count)):
        count += len(chunk)
        if count > limit:
            raise _error("IMAGE_BYTES", "图片超过字节预算。")
        parts.append(chunk)
    return b"".join(parts)


@lru_cache(maxsize=1)
def _tls_context():
    return ssl.create_default_context()


async def _download(source, settings):
    try:
        async with asyncio.timeout(settings.image_timeout):
            for _ in range(4):
                parsed, hostname, port = _url(source)
                address = await _resolve(hostname, port)
                kwargs = {"limit": 32768}
                if parsed.scheme == "https":
                    kwargs.update(ssl=_tls_context(), server_hostname=hostname)
                # Connect directly to the validated IP, keeping TLS SNI on the
                # original hostname. No second DNS lookup and no inherited proxy.
                reader, writer = await asyncio.open_connection(address, port, **kwargs)
                try:
                    host = f"[{hostname}]" if ":" in hostname else hostname
                    target = (parsed.path or "/") + (
                        "?" + parsed.query if parsed.query else ""
                    )
                    request = (
                        f"GET {target} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                        "Accept: image/png,image/jpeg\r\nAccept-Encoding: identity\r\n"
                        "Connection: close\r\n\r\n"
                    )
                    writer.write(request.encode("ascii"))
                    await writer.drain()
                    raw = await reader.readuntil(b"\r\n\r\n")
                    if len(raw) > 32768:
                        raise _error("IMAGE_DOWNLOAD", "图片响应头过大。")
                    lines = raw.decode("iso-8859-1").split("\r\n")
                    status = lines[0].split(" ", 2)
                    if len(status) < 2 or status[0] not in {"HTTP/1.0", "HTTP/1.1"}:
                        raise _error("IMAGE_DOWNLOAD", "图片响应无效。")
                    headers = {}
                    for line in lines[1:-2]:
                        name, sep, value = line.partition(":")
                        key = name.lower()
                        if not sep or key in headers or name != name.strip():
                            raise _error("IMAGE_DOWNLOAD", "图片响应头无效。")
                        headers[key] = value.strip()
                    if status[1] in {"301", "302", "303", "307", "308"}:
                        location = headers.get("location")
                        if not location:
                            raise _error("IMAGE_DOWNLOAD", "图片跳转目标缺失。")
                        source = urljoin(source, location)
                        continue
                    if status[1] != "200":
                        raise _error("IMAGE_DOWNLOAD", "图片下载返回非成功状态。")
                    mime = headers.get("content-type", "").split(";", 1)[0].lower()
                    if mime not in MIMES:
                        raise _error("IMAGE_MIME", "远程响应未声明支持的图片 MIME。")
                    return await _read_body(
                        reader, headers, settings.image_max_bytes
                    ), mime
                finally:
                    writer.close()
                    # close() immediately stops I/O; do not let TLS shutdown
                    # delay cancellation or consume the entire image deadline.
                    try:
                        await asyncio.wait_for(writer.wait_closed(), 0.2)
                    except (OSError, TimeoutError):
                        pass
            raise _error("IMAGE_DOWNLOAD", "图片跳转次数超过上限。")
    except TimeoutError:
        raise _error("IMAGE_TIMEOUT", "图片下载超时。") from None
    except (
        OSError,
        ValueError,
        UnicodeError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        raise _error("IMAGE_DOWNLOAD", "图片下载失败或响应不完整。") from None


class _Images:
    def __init__(self, settings):
        self.settings = settings
        self.sources = {}
        self.unique = {}
        self.byte_count = 0

    async def block(self, source, declared=None):
        if not self.settings.enable_images:
            raise _error("IMAGES_DISABLED", "图片输入尚未启用。")
        if not isinstance(source, str):
            raise _error()
        key = (source, declared)
        if key in self.sources:
            return self.sources[key]
        if len(self.sources) >= self.settings.image_max_count * 4:
            raise _error("IMAGE_COUNT", "图片来源数量超过预算。")
        # Bound source strings before parsing/decoding allocation.
        if len(source) > self.settings.image_max_bytes * 4 // 3 + 256:
            raise _error("IMAGE_BYTES", "图片超过字节预算。")
        if declared is not None:
            try:
                data = base64.b64decode(source, validate=True)
            except (ValueError, binascii.Error):
                raise _error() from None
        elif source.startswith("data:"):
            meta, sep, encoded = source.partition(",")
            if not sep or not meta.endswith(";base64"):
                raise _error("IMAGE_FORMAT", "图片 data URL 必须使用 base64。")
            declared = meta[5:-7]
            if declared not in MIMES:
                raise _error("IMAGE_FORMAT", "仅支持 PNG/JPEG data URL。")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise _error() from None
        elif source.startswith(("https://", "http://")):
            data, declared = await _download(source, self.settings)
        else:
            if source.startswith("file:"):
                parsed = urlsplit(source)
                if parsed.netloc or parsed.query or parsed.fragment:
                    raise _error("IMAGE_PATH", "文件 URL 不允许主机、查询或片段。")
                source = unquote(parsed.path)
            elif "://" in source:
                raise _error("IMAGE_URL", "不支持此图片 URL 协议。")
            data = _local(source, self.settings)
        block = validate_image(data, declared, self.settings)
        identity = hashlib.sha256(data).digest()
        if identity not in self.unique:
            if len(self.unique) >= self.settings.image_max_count:
                raise _error("IMAGE_COUNT", "图片数量超过预算。")
            self.byte_count += len(data)
            if self.byte_count > self.settings.image_max_bytes:
                raise _error("IMAGE_BYTES", "图片总量超过字节预算。")
            self.unique[identity] = block
        block = self.unique[identity]
        self.sources[key] = block
        return block

    async def parts(self, value, *, allow_images=True):
        if not isinstance(value, list):
            return value
        result, seen = [], set()
        for raw in value:
            part = raw.model_dump() if hasattr(raw, "model_dump") else raw
            if not isinstance(part, dict):
                raise _error("INPUT", "内容块必须为对象。")
            kind = part.get("type")
            if kind in {"image", "image_url"} and not allow_images:
                raise _error(
                    "UNSUPPORTED", "仅支持用户图片输入，不支持工具或其他角色的图片。"
                )
            if kind == "image_url":
                item = part.get("image_url")
                source = item.get("url") if isinstance(item, dict) else None
                part = await self.block(source)
            elif kind == "image":
                if not isinstance(part.get("mimeType"), str):
                    raise _error("IMAGE_MIME", "图片块缺少 MIME。")
                part = await self.block(part.get("data"), part["mimeType"])
            elif kind not in {"text", "think"}:
                raise _error("UNSUPPORTED", "音频、视频、PDF 及未知内容块尚未支持。")
            if part.get("type") == "image":
                identity = part["data"]
                if identity in seen:
                    continue
                seen.add(identity)
            result.append(dict(part))
        return result


def _merge_parts(left, right):
    if isinstance(left, str):
        left = [{"type": "text", "text": left}] if left else []
    left = list(left or [])
    images = {part["data"] for part in left if part.get("type") == "image"}
    for part in right:
        if part.get("type") == "image":
            if part["data"] in images:
                continue
            images.add(part["data"])
        left.append(part)
    return left


async def prepare_media(contexts, prompt, extra_parts, image_urls, settings):
    """Return copied contexts/extra parts with canonical ACP image blocks.

    The caller still owns text/history normalization. Content hashes include the
    canonical base64 bytes, so replacing a file at the same path changes history.
    No attachment cache survives this invocation and no original file is removed.
    In contexts-only calls only extra images are merged, since AstrBot has already
    assembled text parts. Repeated images within a turn are sent once.
    """
    images = _Images(settings)
    normalized = []
    for raw in contexts or []:
        message = raw.model_dump() if hasattr(raw, "model_dump") else raw
        if not isinstance(message, dict):
            raise _error("INPUT", "消息必须为对象。")
        message = dict(message)
        message["content"] = await images.parts(
            message.get("content"), allow_images=message.get("role") == "user"
        )
        normalized.append(message)
    extras = await images.parts(extra_parts or [])
    if not isinstance(extras, list):
        raise _error("INPUT", "extra_user_content_parts 必须为列表。")
    if image_urls is not None and not isinstance(image_urls, (list, tuple)):
        raise _error("INPUT", "image_urls 必须为列表。")
    for source in image_urls or []:
        extras = _merge_parts(extras, [await images.block(source)])
    if prompt is None and normalized:
        additions = [part for part in extras if part.get("type") == "image"]
        if additions:
            if normalized[-1].get("role") == "user":
                normalized[-1]["content"] = _merge_parts(
                    normalized[-1].get("content"), additions
                )
            else:
                # The native runner repeats extra_user_content_parts on the
                # contexts-only tool continuation. Only attachments already in
                # its latest user turn may be ignored; a genuinely new image
                # cannot be smuggled into an outstanding tool request.
                latest_user = next(
                    (m for m in reversed(normalized) if m.get("role") == "user"),
                    None,
                )
                content = latest_user.get("content") if latest_user else None
                known = (
                    {part["data"] for part in content if part.get("type") == "image"}
                    if isinstance(content, list)
                    else set()
                )
                if any(part["data"] not in known for part in additions):
                    raise _error("INPUT", "工具续轮不能附加新的图片。")
        extras = []
    return normalized, extras
