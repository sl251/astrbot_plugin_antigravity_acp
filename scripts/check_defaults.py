"""Check plugin schema/default parity without AstrBot or third-party packages.

Run from the repository root: python3 -B -m scripts.check_defaults
"""

import json
from dataclasses import asdict, fields
from pathlib import Path

from agy_provider.config import Settings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    names = {field.name for field in fields(Settings)}
    if names != set(schema):
        raise SystemExit("Settings and configuration schema fields differ")
    defaults = {key: value["default"] for key, value in schema.items()}
    # JSON normalizes tuple-backed list settings without changing their values.
    settings_defaults = json.loads(json.dumps(asdict(Settings())))
    if defaults != settings_defaults:
        raise SystemExit("Settings and configuration schema defaults differ")
    if Settings.from_dict(defaults) != Settings():
        raise SystemExit("Configuration schema defaults fail Settings validation")
    print(f"Configuration defaults agree ({len(names)} fields)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
