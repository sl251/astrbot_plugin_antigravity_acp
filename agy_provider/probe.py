"""No-generation preflight; never sends a model prompt or prints raw RPCs."""

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

from .config import Settings
from .errors import AgyError
from .models import catalog_names, choices, find_option, options, select
from .paths import plugin_data_root
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


async def probe(
    settings: Settings,
    root: Path | None = None,
    *,
    details: bool = False,
    model: str = "",
    effort: str = "default",
    progress=None,
) -> dict:
    report = {
        "report_version": 1,
        "status": "blocked",
        "stage": "discovery",
        "proxy_mode": settings.proxy_mode,
        "auth_bridge": settings.auth_bridge,
        "model_catalog": "not_checked",
        "model_prompt_sent": False,
        "mcp_loading": "not_tested",
        "host_tool_loop": "not_tested",
    }
    try:
        async with asyncio.timeout(settings.startup_timeout):
            await _run_probe(settings, root, report, details, model, effort, progress)
    except TimeoutError:
        report.setdefault("error_code", "TIMEOUT")
    except AgyError as exc:
        report["error_code"] = exc.code
    except Exception:
        # Even local exceptions may contain paths or an upstream payload.
        report["error_code"] = "LOCAL"
    return report


async def _run_probe(
    settings, root, report, details=False, model="", effort="default", progress=None
):
    def stage(value):
        report["stage"] = value
        if progress:
            progress(dict(report))

    stage("discovery")
    info = await discover(settings)
    # Do not publish the raw version banner; wrappers may print private data.
    version = re.fullmatch(
        r"(?:agy(?: version)?\s+)?v?(\d{1,4}\.\d{1,4}\.\d{1,4})",
        info.version.strip(),
        re.IGNORECASE,
    )
    report["cli_version"] = version[1] if version else "unrecognized"
    stage("isolation")
    root = root if root is not None else plugin_data_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="preflight-", dir=root) as directory:
        stage("authentication")
        home = make_home(
            Path(directory), "Preflight only; no generation.", settings=settings
        )
        client = AcpClient(settings)
        try:
            stage("initialize")
            await client.start(launch_args(info, home), home / "workspace")
            report["protocol_version"] = 1
            report["advertised_capabilities"] = capability_summary(client.capabilities)
            stage("session/new")
            result = await client.new_session(home / "workspace")
            stage("model_catalog")
            names = catalog_names(result)
            report["model_count"] = len(names)
            report["model_catalog"] = "available"
            if details:
                report["models"] = [
                    {"id": key, "name": name} for key, name in names.items()
                ]
                config = options(result)
                current = (find_option(config, "model") or {}).get("currentValue") or (
                    result.get("models") or {}
                ).get("currentModelId")
                if model:
                    stage("model_selection")
                    config = await select(client, result, model, effort)
                    current = model
                report["selected_model"] = current if current in names else ""
                report["efforts"] = [
                    value for value in choices(find_option(config, "thought_level"))
                    if value in {"low", "medium", "high"}
                ]
        except asyncio.CancelledError:
            # The total budget cancels a pending RPC before its own timeout
            # handler runs. Keep its allowlisted evidence before closing it;
            # an external cancellation still propagates without a report.
            report["error_code"] = client._diagnose(
                AgyError("TIMEOUT", "无生成自检超时。")
            ).code
            raise
        finally:
            await client.close()
    report["stage"] = "complete"
    report["status"] = "passed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", default="", help="Absolute official AGY path")
    parser.add_argument("--timeout", type=int, default=30, choices=range(5, 301))
    parser.add_argument(
        "--data-root", type=Path, help="Same plugin data root as Runtime"
    )
    args = parser.parse_args()
    try:
        settings = Settings.from_dict(
            {"command": args.command, "startup_timeout": args.timeout}
        )
        report = asyncio.run(probe(settings, root=args.data_root))
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
