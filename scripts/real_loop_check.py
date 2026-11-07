#!/usr/bin/env python3
"""Real Provider -> AstrBot runner -> MCP -> AGY acceptance (never run in CI).

Prerequisites on the operator's machine:
* Python 3.11+ with AstrBot v4.29.4 and its existing dependencies importable
  (run with AstrBot's Python, or provide --astrbot-source /path/to/AstrBot).
* Official AGY CLI 1.5.2, normally ~/.local/bin/agy; an existing official login
  token at ~/.gemini/antigravity-cli/antigravity-oauth-token.
* Working SOCKS egress at 127.0.0.1:10808, as in the verified probe harness.

    python3 scripts/real_loop_check.py
    python3 scripts/real_loop_check.py --with-image

This script, and ONLY this script, links that existing token into its temporary
isolated AGY home. It never reads, copies, refreshes, or prints token contents.
The original ToolLoopAgentRunner and FunctionToolExecutor execute one harmless
local echo. No stubs replace AstrBot, MCP, or AGY. No live chat platform is used.
The supplied echo ToolSet is explicitly pre-authorized by this harness. Audit
hooks are observations, not approval gates: AstrBot catches on_tool_start errors.
Applications needing extra approval must pass the optional async
agy_tool_approver callback to Provider calls, or reject inside their host executor
before executing. The original runner need not pass that optional callback.
All AstrBot state lives in a temporary ASTRBOT_ROOT. Only whitelisted evidence is
persisted; stdout has PASS/FAIL lines and EVIDENCE_JSON. Exit 0 means all requested
checks passed; exit 1 includes missing prerequisites. No package is installed.

Host API source pinned to AstrBot v4.29.4 commit
61335414433db721b3f7ecc93f90deafe0a59d78:
  astrbot/core/provider/entities.py
  astrbot/core/agent/tool.py
  astrbot/core/agent/runners/tool_loop_agent_runner.py
  astrbot/core/astr_agent_tool_exec.py
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import uuid
import zlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class CheckFailure(RuntimeError):
    """Only a fixed, non-sensitive machine-readable error code is exposed."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise CheckFailure(code)


def safe_path(path: Path) -> str:
    return str(path).replace(str(Path.home()), "<HOME>")


def png_digits(path: Path) -> None:
    """Write 7391 as an ordinary RGB PNG using only stdlib."""
    glyphs = (
        ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
        ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
        ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
        ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    )
    scale, width, height = 8, 208, 88
    pixels = bytearray(b"\xff" * width * height * 3)
    for digit, rows in enumerate(glyphs):
        for y, row in enumerate(rows):
            for x, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        pos = (
                            (16 + y * scale + dy) * width
                            + 12
                            + digit * 48
                            + x * scale
                            + dx
                        ) * 3
                        pixels[pos : pos + 3] = b"\0\0\0"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    scanlines = b"".join(
        b"\0" + pixels[y * width * 3 : (y + 1) * width * 3] for y in range(height)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


class HarnessEvent:
    """An isolated event for the real host executor; it cannot send messages."""

    unified_msg_origin = "agy-check:FriendMessage:acceptance"

    def get_extra(self, _key, default=None):
        return default

    def get_result(self):
        return None

    async def send(self, _result):
        raise CheckFailure("UNEXPECTED_MESSAGE_SEND")


async def run_checks(args, state: Path, evidence: dict) -> None:
    if args.astrbot_source:
        sys.path.insert(0, str(args.astrbot_source.resolve()))
    try:
        astrbot = importlib.import_module("astrbot")
    except ImportError:
        raise CheckFailure("ASTRBOT_UNAVAILABLE") from None
    require(getattr(astrbot, "__version__", None) == "4.29.4", "ASTRBOT_VERSION")
    evidence["astrbot_version"] = "4.29.4"
    try:
        from astrbot.core.agent.hooks import BaseAgentRunHooks
        from astrbot.core.agent.run_context import ContextWrapper
        from astrbot.core.agent.runners.tool_loop_agent_runner import (
            ToolLoopAgentRunner,
        )
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
        from astrbot.core.provider.entities import ProviderRequest
    except ImportError:
        raise CheckFailure("ASTRBOT_DEPENDENCIES") from None

    from agy_provider import sessions
    from agy_provider.config import Settings
    from agy_provider.process import discover
    from agy_provider.provider import AgyProvider, configure
    from agy_provider.transport import AcpClient

    command = args.command or shutil.which("agy") or str(Path.home() / ".local/bin/agy")
    require(Path(command).is_file(), "CLI_MISSING")
    require(args.token_file.is_file(), "OFFICIAL_LOGIN_MISSING")
    settings = Settings.from_dict(
        {
            "command": str(Path(command).resolve()),
            "enable_tools": True,
            "enable_images": args.with_image,
            "media_roots": [str(state)],
            "request_timeout": args.timeout,
            "max_concurrent": 1,
            "max_sessions": 1,
        }
    )
    info = await discover(settings)
    require(
        re.search(r"(?<![\d.])1\.5\.2(?![\d.])", info.version) is not None,
        "CLI_VERSION",
    )
    evidence["cli_version"] = "1.5.2"
    configure(settings)
    original_home = sessions.make_home
    original_write = AcpClient._write
    original_update = AcpClient._on_update
    host_requests: list[dict] = []
    host_results: list[dict] = []
    protocol: list[dict] = []
    executed: list[dict] = []
    hook_events: list[str] = []
    feedback_ids: list[str] = []
    expected_message = "BRIDGE-7731"

    def bridged_home(root, system, tools_mode=True):
        # Same isolated layout and symlink bridge as probes/acp_probe.py.
        # tools_mode defaults to True: bridge-backed sessions must run AGY's
        # default agent — session MCP tools never reach the LLM while a
        # custom --agent is active (real-CLI experiments, AGY 1.5.2).
        home = original_home(root, system, tools_mode=tools_mode)
        (home / "agy" / "antigravity-oauth-token").symlink_to(args.token_file.resolve())
        return home

    async def observed_write(client, obj):
        method = obj.get("method")
        params = obj.get("params", {})
        if method == "session/new":
            protocol.append(
                {
                    "method": method,
                    "stdio_servers": [
                        s.get("name") for s in params.get("mcpServers", [])
                    ],
                }
            )
        elif method == "session/prompt":
            protocol.append(
                {
                    "method": method,
                    "image_blocks": sum(
                        p.get("type") == "image" for p in params.get("prompt", [])
                    ),
                }
            )
        await original_write(client, obj)

    def observed_update(client, params):
        update = (params or {}).get("update", {})
        if update.get("sessionUpdate") in {"tool_call", "tool_call_update"}:
            protocol.append(
                {
                    "method": "session/update",
                    "kind": update.get("sessionUpdate"),
                    "status": update.get("status"),
                    "name": update.get("name"),
                    "id": update.get("toolCallId"),
                }
            )
        original_update(client, params)

    class ObservedProvider(AgyProvider):
        async def text_chat(self, *positional, **kwargs):
            # Observe the original native API and results. Never call a handler here.
            for raw in kwargs.get("contexts") or []:
                message = raw.model_dump() if hasattr(raw, "model_dump") else raw
                if message.get("role") == "tool":
                    call_id = message.get("tool_call_id")
                    if call_id not in feedback_ids:
                        feedback_ids.append(call_id)
                        content = message.get("content")
                        host_results.append({"id": call_id, "content": content})
            response = await super().text_chat(*positional, **kwargs)
            for name, arguments, call_id in zip(
                response.tools_call_name,
                response.tools_call_args,
                response.tools_call_ids,
                strict=True,
            ):
                host_requests.append(
                    {"id": call_id, "name": name, "arguments": arguments}
                )
            return response

    class AuditHooks(BaseAgentRunHooks):
        async def on_tool_start(self, _context, tool, _args):
            hook_events.append("start:" + tool.name)

        async def on_tool_end(self, _context, tool, _args, _result):
            hook_events.append("end:" + tool.name)

    async def echo(event, message: str):
        require(isinstance(event, HarnessEvent), "EXECUTION_CONTEXT")
        require(message == expected_message, "MODEL_ARGUMENTS")
        require(not executed, "DUPLICATE_EXECUTION")
        # Generated only at execution time. The prompt never contains this nonce.
        result = {"echo": message, "marker": "BRIDGE-ECHO-" + uuid.uuid4().hex}
        executed.append(result)
        return json.dumps(result)

    toolset = ToolSet(
        tools=[
            FunctionTool(
                name="echo_probe",
                description="Echo a message and return the actual runtime-generated marker.",
                parameters={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                handler=echo,
            )
        ]
    )
    sessions.make_home = bridged_home
    AcpClient._write = observed_write
    AcpClient._on_update = observed_update
    provider = None
    try:
        provider = ObservedProvider(
            {
                "id": "agy-real-loop-check",
                "type": "agy_acp",
                "model": args.model,
                "modalities": ["text", "tool_use", "image"]
                if args.with_image
                else ["text", "tool_use"],
                "max_context_tokens": 0,
            },
            {},
        )
        runner = ToolLoopAgentRunner()
        request = ProviderRequest(
            prompt=(
                'Call echo_probe exactly once with message="BRIDGE-7731". '
                "After receiving its result, repeat the entire actual JSON result including "
                "the runtime-generated marker. Do not invent a result."
            ),
            session_id="agy-real-loop-acceptance",
            func_tool=toolset,
            contexts=[],
            model=args.model,
        )
        await runner.reset(
            provider=provider,
            request=request,
            run_context=ContextWrapper(context=SimpleNamespace(event=HarnessEvent())),
            tool_executor=FunctionToolExecutor(),
            agent_hooks=AuditHooks(),
            streaming=False,
            tool_schema_mode="full",
            request_max_retries=1,
        )
        # v4.29.4 drops func_tool when nonempty modalities lacks "tool_use".
        # Check the real runner before spending a model request.
        require(runner._func_tool_for_provider() is toolset, "RUNNER_TOOLS_REMOVED")
        require(isinstance(runner._abort_signal, asyncio.Event), "RUNNER_ABORT_SIGNAL")
        require(
            isinstance(runner.run_context.context.event, HarnessEvent),
            "RUNNER_EXECUTION_CONTEXT",
        )
        evidence["runner_preflight"] = {
            "native_toolset_retained": True,
            "abort_signal_present": True,
            "schema_mode": "full",
            "approval_mode": "explicitly_authorized_echo_toolset",
            "extra_approval_callback": "agy_tool_approver (optional; unused here)",
        }
        async with asyncio.timeout(args.timeout):
            async for _ in runner.step_until_done(4):
                pass
        final = runner.get_final_llm_resp()
        answer = final.completion_text if final else ""
        broker_evidence = []
        for session in provider.runtime.sessions.values():
            broker = getattr(session, "broker", None)
            if broker is not None:
                broker_evidence.append(
                    {
                        "call_count": broker.count,
                        "execution_outcomes": dict(broker.audit),
                        "notifications": dict(broker.notifications),
                    }
                )
        evidence["tool_loop"] = {
            "brokers": broker_evidence,
            "native_requests": host_requests,
            "host_results": host_results,
            "executions": executed,
            "execution_count": len(executed),
            "hooks": hook_events,
            "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
            "actual_result_readback": bool(
                executed
                and executed[0]["marker"] in answer
                and expected_message in answer
            ),
            "protocol": protocol,
            "runner": "ToolLoopAgentRunner",
            "executor": "FunctionToolExecutor",
        }
        require(len(host_requests) == len(executed) == 1, "TOOL_EXECUTION_COUNT")
        require(
            any(
                b["execution_outcomes"].get(host_requests[0]["id"]) == "completed"
                for b in broker_evidence
            ),
            "MCP_COMPLETION_AUDIT",
        )
        require(
            host_requests[0]["arguments"] == {"message": expected_message},
            "TOOL_ARGUMENTS",
        )
        require(feedback_ids == [host_requests[0]["id"]], "TOOL_RESULT_ASSOCIATION")
        require(hook_events == ["start:echo_probe", "end:echo_probe"], "HOST_HOOKS")
        require(any(p.get("stdio_servers") for p in protocol), "MCP_NOT_INJECTED")
        require(
            sum(p.get("method") == "session/prompt" for p in protocol) == 1,
            "PROMPT_NOT_OUTSTANDING",
        )
        require(evidence["tool_loop"]["actual_result_readback"], "TOOL_RESULT_READBACK")
        evidence["checks"]["M2_REAL_LOOP"] = "PASS"
        if args.with_image:
            image_path = state / "probe_7391.png"
            png_digits(image_path)
            evidence["image_sha256"] = hashlib.sha256(
                image_path.read_bytes()
            ).hexdigest()
            for name, prompt in (
                (
                    "M3_IMAGE_TEXT",
                    "Read the four digits in the image. Reply with those digits only.",
                ),
                ("M3_IMAGE_ONLY", ""),
            ):
                response = await provider.text_chat(
                    prompt=prompt,
                    image_urls=[str(image_path)],
                    session_id=name,
                    system_prompt="Read the digits in the supplied image; reply with the digits only.",
                )
                require("7391" in response.completion_text, name + "_READBACK")
                require(protocol[-1].get("image_blocks") == 1, name + "_BLOCK_COUNT")
                evidence["checks"][name] = "PASS"
    finally:
        # Persist only fixed harness inputs/outputs; never credentials or raw stderr.
        evidence.setdefault("tool_loop", {})
        evidence["tool_loop"].update(
            native_requests=host_requests,
            host_results=host_results,
            executions=executed,
            execution_count=len(executed),
            hooks=hook_events,
            protocol=protocol,
        )
        try:
            if provider is not None:
                await provider.terminate()
        finally:
            sessions.make_home = original_home
            AcpClient._write = original_write
            AcpClient._on_update = original_update


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--with-image", action="store_true")
    parser.add_argument(
        "--command",
        help="Official AGY 1.5.2 executable; defaults to PATH or ~/.local/bin/agy",
    )
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--astrbot-source", type=Path, help="Optional installed AstrBot source root"
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".gemini/antigravity-cli/antigravity-oauth-token",
    )
    parser.add_argument("--output-dir", type=Path, default=REPO / "results")
    args = parser.parse_args()
    args.token_file = args.token_file.expanduser()
    evidence = {
        "schema_version": 1,
        "date_utc": datetime.now(UTC).isoformat(),
        "checks": {"M2_REAL_LOOP": "FAIL"},
        "fixture_origin": "2026-11-07 losvps real probe",
    }
    if args.with_image:
        evidence["checks"].update(M3_IMAGE_TEXT="FAIL", M3_IMAGE_ONLY="FAIL")
    started = time.monotonic()
    try:
        require(10 <= args.timeout <= 1800, "TIMEOUT_CONFIG")
        with tempfile.TemporaryDirectory(prefix="agy-real-loop-") as temp:
            state = Path(temp)
            os.environ["ASTRBOT_ROOT"] = str(state)
            os.environ.update(
                HTTP_PROXY="socks5h://127.0.0.1:10808",
                HTTPS_PROXY="socks5h://127.0.0.1:10808",
                ALL_PROXY="socks5h://127.0.0.1:10808",
                NO_PROXY="localhost,127.0.0.1",
            )
            logging.disable(logging.CRITICAL)
            # Imports and host logger setup must not emit account/path diagnostics.
            with (
                open(os.devnull, "w") as sink,
                contextlib.redirect_stdout(sink),
                contextlib.redirect_stderr(sink),
            ):
                asyncio.run(run_checks(args, state, evidence))
    except (Exception, KeyboardInterrupt) as exc:
        if isinstance(exc, CheckFailure):
            code = str(exc)
        else:
            code = getattr(exc, "code", None) or type(exc).__name__
        evidence["error_code"] = re.sub(r"[^A-Za-z0-9_]", "_", str(code))[:80]
    evidence["elapsed_seconds"] = round(time.monotonic() - started, 3)
    passed = "error_code" not in evidence and all(
        v == "PASS" for v in evidence["checks"].values()
    )
    evidence["status"] = "PASS" if passed else "FAIL"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / (
        "real-loop-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2).replace(
        str(Path.home()), "<HOME>"
    )
    output.write_text(serialized + "\n", encoding="utf-8")
    output.chmod(0o600)
    for check, status in evidence["checks"].items():
        print(f"{status} {check}")
    print(
        f"{evidence['status']} REAL_LOOP_CHECK"
        + (" code=" + evidence["error_code"] if "error_code" in evidence else "")
    )
    print("EVIDENCE_JSON " + safe_path(output.resolve()))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
