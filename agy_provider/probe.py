"""No-generation preflight; never sends a model prompt or prints raw RPCs."""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

from .config import Settings
from .errors import AgyError
from .process import discover, launch_args, make_home
from .transport import AcpClient


def capability_summary(capabilities: object) -> dict:
    """Allowlist boolean advertisements, never arbitrary upstream metadata."""
    source = capabilities if isinstance(capabilities, dict) else {}
    result = {}
    for group, keys in (
        ("mcpCapabilities", ("http", "sse")),
        ("promptCapabilities", ("image", "audio", "embeddedContext")),
    ):
        values = source.get(group)
        values = values if isinstance(values, dict) else {}
        result[group] = {key: values.get(key) is True for key in keys}
    return result


async def probe(settings: Settings) -> dict:
    report = {
        "report_version": 1,
        "status": "blocked",
        "stage": "discovery",
        "model_prompt_sent": False,
        "mcp_loading": "not_tested",
        "host_tool_loop": "not_tested",
    }
    try:
        info = await discover(settings)
        # Do not publish the raw version banner; wrappers may print private data.
        version = re.fullmatch(
            r"(?:agy(?: version)?\s+)?v?(\d{1,4}\.\d{1,4}\.\d{1,4})",
            info.version.strip(),
            re.IGNORECASE,
        )
        report["cli_version"] = version[1] if version else "unrecognized"
        report["stage"] = "isolation"
        with tempfile.TemporaryDirectory(prefix="agy-preflight-") as directory:
            home = make_home(Path(directory), "Preflight only; no generation.")
            client = AcpClient(settings)
            try:
                report["stage"] = "initialize"
                await client.start(launch_args(info, home), home / "workspace")
                report["protocol_version"] = 1
                report["advertised_capabilities"] = capability_summary(
                    client.capabilities
                )
                report["stage"] = "session/new"
                await client.new_session(home / "workspace")
            finally:
                await client.close()
        report["stage"] = "complete"
        report["status"] = "passed"
    except AgyError as exc:
        report["error_code"] = exc.code
    except Exception:
        # Even local exceptions may contain paths or an upstream payload.
        report["error_code"] = "LOCAL"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="", help="Absolute official AGY path")
    parser.add_argument("--timeout", type=int, default=30, choices=range(5, 301))
    args = parser.parse_args()
    try:
        settings = Settings.from_dict(
            {"command": args.command, "startup_timeout": args.timeout}
        )
        report = asyncio.run(probe(settings))
    except AgyError:
        report = {"status": "blocked", "stage": "configuration", "error_code": "CONFIG"}
    except KeyboardInterrupt:
        report = {
            "status": "blocked",
            "stage": "interrupted",
            "error_code": "CANCELLED",
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
