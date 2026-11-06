"""Validated plugin defaults. No shell fragments or authentication fields."""

from dataclasses import dataclass
from pathlib import Path

from .errors import AgyError


@dataclass(frozen=True)
class Settings:
    command: str = ""
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

    @classmethod
    def from_dict(cls, values: dict) -> "Settings":
        limits = {
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
