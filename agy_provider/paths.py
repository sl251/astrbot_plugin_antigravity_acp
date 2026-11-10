"""Shared authorized session data root for Runtime and no-generation doctor."""

from pathlib import Path


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
