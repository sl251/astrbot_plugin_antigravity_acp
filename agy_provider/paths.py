"""Shared authorized session data root and Windows filesystem paths."""

import os
from pathlib import Path


def windows_long_path(path: Path) -> Path:
    """Use extended-length absolute paths on Windows, without resolving links."""
    if os.name != "nt":
        return path
    value = os.fspath(path)
    if value.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    # Normalize relative paths, separators and dot components before adding the
    # prefix: Win32 skips that normalization for extended-length paths. Prefix
    # even short roots, since their session files may grow beyond MAX_PATH.
    value = os.path.abspath(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def plugin_data_root(base: Path | None = None) -> Path:
    if base is None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        except ImportError:
            # Standalone CLI: run from AstrBot's working directory, or pass root.
            base = Path.cwd() / "data" / "plugin_data"
        else:
            base = Path(get_astrbot_plugin_data_path())
    return base / "astrbot_plugin_antigravity_acp"
