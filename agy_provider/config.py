"""Validated plugin defaults. No shell fragments or credential contents."""

import ipaddress
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import AgyError


@dataclass(frozen=True)
class Settings:
    setup_auto_check: bool = True
    command: str = ""
    auth_bridge: bool = True
    proxy_mode: str = "inherit"
    proxy_url: str = field(default="", repr=False)
    thought_level: str = "default"
    startup_timeout: int = 60
    request_timeout: int = 180
    queue_timeout: int = 60
    max_concurrent: int = 2
    max_sessions: int = 4
    max_pending: int = 16
    idle_ttl: int = 600
    history_bytes: int = 160000
    max_output_bytes: int = 4194304
    enable_tools: bool = False
    enable_images: bool = False
    tool_max_calls: int = 16
    tool_max_concurrent: int = 1
    tool_approval_timeout: int = 60
    tool_execution_timeout: int = 120
    tool_result_bytes: int = 262144
    media_roots: tuple[str, ...] = ()
    image_max_count: int = 8
    image_max_bytes: int = 8388608
    image_max_pixels: int = 20000000
    image_timeout: int = 15

    @classmethod
    def from_dict(cls, values: dict) -> "Settings":
        limits = {
            "tool_max_calls": (1, 128),
            "tool_max_concurrent": (1, 8),
            "tool_approval_timeout": (1, 300),
            "tool_execution_timeout": (1, 1800),
            "tool_result_bytes": (1024, 8388608),
            "image_max_count": (1, 16),
            "image_max_bytes": (1024, 33554432),
            "image_max_pixels": (1, 40000000),
            "image_timeout": (1, 60),
            "startup_timeout": (5, 300),
            "request_timeout": (10, 1800),
            "queue_timeout": (1, 300),
            "max_concurrent": (1, 8),
            "max_sessions": (1, 16),
            "max_pending": (1, 128),
            "idle_ttl": (30, 3600),
            "history_bytes": (4096, 160000),
            "max_output_bytes": (4096, 8388608),
        }
        parsed = {}
        defaults = cls()
        for key in ("enable_tools", "enable_images", "auth_bridge", "setup_auto_check"):
            value = values.get(key, getattr(defaults, key))
            if type(value) is not bool:
                raise AgyError("CONFIG", f"{key} 必须是布尔值。")
            parsed[key] = value
        mode = values.get("proxy_mode", defaults.proxy_mode)
        proxy_url = values.get("proxy_url", defaults.proxy_url)
        if not isinstance(mode, str) or mode not in {"inherit", "custom", "direct"}:
            raise AgyError("CONFIG", "proxy_mode 应为 inherit/custom/direct。")
        if not isinstance(proxy_url, str):
            raise AgyError("CONFIG", "proxy_url 必须是字符串。")
        if mode == "custom":
            validate_proxy_url(proxy_url)
        parsed["proxy_mode"] = mode
        parsed["proxy_url"] = proxy_url
        roots = values.get("media_roots", [])
        if not isinstance(roots, (list, tuple)) or any(
            not isinstance(p, str) or not Path(p).is_absolute() or ".." in Path(p).parts
            for p in roots
        ):
            raise AgyError("CONFIG", "media_roots 必须为授权媒体目录的绝对路径列表。")
        parsed["media_roots"] = tuple(roots)
        for key, (low, high) in limits.items():
            raw = values.get(key, getattr(defaults, key))
            try:
                if isinstance(raw, bool) or str(int(raw)) != str(raw):
                    raise ValueError
                value = int(raw)
            except (TypeError, ValueError, OverflowError):
                raise AgyError("CONFIG", f"{key} 必须是整数。") from None
            if not low <= value <= high:
                raise AgyError("CONFIG", f"{key} 范围为 {low}–{high}。")
            parsed[key] = value
        command = str(values.get("command", "")).strip()
        if command and not Path(command).expanduser().is_absolute():
            raise AgyError(
                "CONFIG", "command 请填写 agy 可执行文件的绝对路径，不要填写命令参数。"
            )
        effort = str(values.get("thought_level", "default")).lower()
        if effort not in {"default", "low", "medium", "high"}:
            raise AgyError("CONFIG", "thought_level 应为 default/low/medium/high。")
        if parsed["max_concurrent"] > parsed["max_sessions"]:
            raise AgyError("CONFIG", "max_sessions 不能小于 max_concurrent。")
        return cls(command=command, thought_level=effort, **parsed)


def validate_proxy_url(value: str) -> None:
    """Validate without including the potentially credential-bearing URL in errors."""
    try:
        if (
            not value
            or any(c.isspace() or unicodedata.category(c) == "Cc" for c in value)
            or any(c in value for c in "\\?#")
            or any(unicodedata.category(c) == "Cc" for c in unquote(value))
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "socks5h"} or parsed.path not in {"", "/"}:
            raise ValueError
        if not parsed.hostname or parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if parsed.netloc.count("@") > 1:
            raise ValueError
        host, separator, port = authority.rpartition(":")
        if not separator or not re.fullmatch(r"[0-9]+", port):
            raise ValueError
        if host.startswith("[") and host.endswith("]"):
            ipaddress.IPv6Address(host[1:-1])
        elif any(c in host for c in ":[]%"):
            raise ValueError
    except (ValueError, UnicodeError):
        raise AgyError(
            "CONFIG",
            "custom 代理须为 http:// 或 socks5h:// URL，包含主机和 1–65535 端口；"
            "IPv6 使用方括号，不允许空白、控制字符、反斜线、查询或片段。",
        ) from None
