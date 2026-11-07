#!/usr/bin/env python3
"""隔离的真实 ACP / AstrBot 原生 executor / 小米 MiMo 语音验收。

仅由操作者在已有 AstrBot 容器运行；默认 full 必验（一次 call 探针、一次
真实 MimoTTSSpeakTool.run / Main.run_mimo_tts / HTTP 合成）。需要额外模型
请求的 skills_like run-echo 探针用 --with-skills-like 显式开启，不重复语音。

    python3 scripts/native_tool_compat_check.py --execute \
        --config-dir /data/config --output-dir /data/native-tool-check
    python3 scripts/native_tool_compat_check.py --execute --with-skills-like \
        --config-dir /data/config --output-dir /data/native-tool-check

配置只读；不初始化平台、不连接 QQ、不注册插件。CaptureEvent 继承真实
AstrMessageEvent，send 仅保留 Record/WAV 和白名单 JSON，不调用父类 send。
当前配置模型必须为 gemini-3.8-flash；没有换模型或 fallback 的入口。
官方登录由现有部署 auth wrapper 桥接；脚本不读取、复制或创建凭据 symlink。
使用已有容器代理；MiMo 原实现的空 ProxyHandler 在本进程被绑定到同一
代理（只改 HTTP transport，不复制 payload/API/音频解码实现）。

输出目录中 evidence.json 为机器可读证据，capture-*.json 为消息捕获，
plugin/audio_cache/*.wav 为真实合成产物。所有计数均来自观察真实调用。
原插件返回 None；full 验收专用 run 包装器在真实工具执行/捕获完成后返回
基于真实 WAV 的 JSON 回执，使宿主续轮并验收 ACP 回填。证据明确记录
original_return_is_none 和 receipt_adapter，不宣称已覆盖原生 None/direct-result
的生命周期（这是独立审计项）。--native-terminal 安装本插件 host_compat，
保留真实 None/direct-result，验收宿主 DONE 后 pending turn/caller 清理和 unknown。
两种模式互斥地运行一次 full；不改生产配置或部署 wrapper。
不带 --execute 时仅显示帮助，不发模型/TTS 请求；--help 不导入 AstrBot。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import wave
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIMO_PLUGIN = "astrbot_plugin_xiaomi_mimo_voice_dispatcher"
EXPECTED_MODEL = "gemini-3.8-flash"


class CheckFailure(RuntimeError):
    """仅携带脚本定义的非敏感错误码。"""


def require(value, code):
    if not value:
        raise CheckFailure(code)


def read_object(path):
    require(path.is_file(), "CONFIG_FILE_MISSING")
    obj = json.loads(path.read_text(encoding="utf-8-sig"))
    require(isinstance(obj, dict), "CONFIG_NOT_OBJECT")
    return obj


def select_provider(config, provider_id=None):
    """合并当前 provider_sources/provider，不读取或输出 key 字段。"""
    sources = {
        p.get("id"): p for p in config.get("provider_sources", [])
        if isinstance(p, dict)
    }
    candidates = []
    for raw in config.get("provider", []) + config.get("provider_sources", []):
        if not isinstance(raw, dict):
            continue
        merged = dict(sources.get(raw.get("provider_source_id"), {}))
        merged.update(raw)
        if not any(merged.get(k) == "agy_acp" for k in ("type", "provider")):
            continue
        model = merged.get("model") or (merged.get("model_config") or {}).get("model")
        if not model or merged.get("enable") is False:
            continue
        merged["model"] = model
        if provider_id is None or merged.get("id") == provider_id:
            candidates.append(merged)
    unique = {p.get("id"): p for p in candidates}
    main_id = (
        config.get("agent_runner", {}).get("config", {}).get("model", {})
        .get("provider_id")
    )
    if provider_id is None and main_id in unique:
        return unique[main_id]
    candidates = list(unique.values())
    if provider_id is None:
        candidates = [p for p in candidates if p["model"] == EXPECTED_MODEL]
    require(len(candidates) == 1, "AGY_PROVIDER_MISSING_OR_AMBIGUOUS")
    return candidates[0]


def sha(value):
    return hashlib.sha256(value).hexdigest()


def source_evidence(obj):
    path = Path(inspect.getsourcefile(obj) or "")
    require(path.is_file(), "NATIVE_SOURCE_UNAVAILABLE")
    return {"module": obj.__module__, "source": str(path),
            "sha256": sha(path.read_bytes())}


def text_evidence(value, expected):
    if not isinstance(value, str):
        return {"matches_test_text": False, "type": type(value).__name__}
    result = {"matches_test_text": value == expected,
              "characters": len(value), "sha256": sha(value.encode())}
    if value == expected:
        result["text"] = value
    return result


def wav_evidence(path):
    size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(12)
    require(header[:4] == b"RIFF" and header[8:12] == b"WAVE", "WAV_HEADER_INVALID")
    with wave.open(str(path), "rb") as audio:
        result = {
            "path": str(path), "bytes": size, "container": "RIFF/WAVE",
            "channels": audio.getnchannels(), "sample_width": audio.getsampwidth(),
            "sample_rate": audio.getframerate(), "frames": audio.getnframes(),
            "compression": audio.getcomptype(),
            "pcm_bytes": len(audio.readframes(audio.getnframes())),
        }
    require(result["frames"] > 0 and result["pcm_bytes"] > 0, "WAV_EMPTY")
    require(result["compression"] == "NONE", "WAV_NOT_PCM")
    result["sha256"] = sha(path.read_bytes())
    return result


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


# Only known non-sensitive phrases are emitted, never raw stderr/exception text.
SAFE_DIAGNOSTIC_MARKERS = {
    "Refusing auth bridge outside AGY plugin session directory": "AUTH_BRIDGE_OUTSIDE_PLUGIN_ROOT",
    "Invalid isolated ACP directories": "AUTH_BRIDGE_INVALID_DIRECTORIES",
    "Unexpected existing auth symlink": "AUTH_BRIDGE_UNEXPECTED_SYMLINK",
    "Unexpected existing isolated auth file; refusing overwrite": "AUTH_BRIDGE_EXISTING_AUTH_FILE",
    "No such file or directory": "FILE_NOT_FOUND",
    "Permission denied": "PERMISSION_DENIED",
    "Connection refused": "CONNECTION_REFUSED",
    "certificate verify failed": "TLS_CERTIFICATE_VERIFY_FAILED",
    "ACP 子进程已退出": "ACP_PROCESS_EXITED",
}


def safe_exception(exc):
    result: dict[str, object] = {"type": type(exc).__name__ if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", type(exc).__name__) else "Exception"}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,47}", code):
        result["code"] = code
    if isinstance(exc, CheckFailure) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", str(exc)):
        result["check_code"] = str(exc)
    result["markers"] = sorted({value for key, value in SAFE_DIAGNOSTIC_MARKERS.items() if key in str(exc)})
    if isinstance(exc, OSError) and isinstance(exc.errno, int):
        result["errno"] = exc.errno
    # Exception strings, args, traceback, response bodies and headers are omitted.
    return result


def make_probe_runtime(config_dir):
    """Honor deployed wrapper ROOT; make_home keeps its required session-* leaf."""
    plugin_root = (config_dir.resolve().parent / "plugin_data" / "astrbot_plugin_agy_provider").resolve()
    require(plugin_root.is_dir(), "AGY_PLUGIN_RUNTIME_ROOT_MISSING")
    runtime = Path(tempfile.mkdtemp(prefix="native-tool-check-", dir=plugin_root)).resolve()
    require(runtime.is_relative_to(plugin_root), "PROBE_RUNTIME_OUTSIDE_PLUGIN_ROOT")
    runtime.chmod(0o700)
    return runtime


async def run(args, directory, evidence):
    config_dir = args.config_dir.resolve()
    cmd_path = args.astrbot_config or config_dir.parent / "cmd_config.json"
    config = read_object(cmd_path)
    plugin_config_path = config_dir / "astrbot_plugin_agy_provider_config.json"
    mimo_config_path = config_dir / (MIMO_PLUGIN + "_config.json")
    plugin_config = read_object(plugin_config_path)
    mimo_config = read_object(mimo_config_path)
    configured_provider = select_provider(config, args.provider_id)
    require(configured_provider["model"] == EXPECTED_MODEL, "CONFIG_MODEL_DRIFT")
    require(plugin_config.get("enable_tools") is True, "CONFIG_TOOLS_DISABLED")
    require(bool(mimo_config.get("api_key")), "MIMO_KEY_NOT_CONFIGURED")
    require(args.token_file.is_file(), "OFFICIAL_LOGIN_MISSING")
    proxy = next((os.environ.get(k) for k in (
        "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"
    ) if os.environ.get(k)), None) or config.get("http_proxy")
    require(isinstance(proxy, str) and bool(proxy.strip()), "CONTAINER_PROXY_MISSING")
    require(proxy.startswith(("http://", "https://", "socks5://", "socks5h://")),
            "CONTAINER_PROXY_SCHEME")
    # 保留现有代理值，不硬编码出口、不打印代理（其 URL 可能带认证信息）。
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[key] = proxy
    # 不清除 NO_PROXY，原生 broker 的 loopback 请求仍按原实现直连。
    evidence["configuration"] = {
        "astrbot_config": str(cmd_path), "agy_config": str(plugin_config_path),
        "mimo_config": str(mimo_config_path), "provider_id": configured_provider.get("id"),
        "model": configured_provider["model"], "model_override": False,
        "fallback": False, "proxy_present": True, "config_written": False,
    }
    if args.astrbot_source:
        sys.path.insert(0, str(args.astrbot_source.resolve()))
    sys.path.insert(0, str(REPO))
    astrbot = importlib.import_module("astrbot")
    from astrbot.core.agent.hooks import BaseAgentRunHooks
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.message.components import Plain, Record
    from astrbot.core.platform.astr_message_event import (
        AstrMessageEvent,
        current_message_event,
        event_context,
    )
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.star.context import Context

    from agy_provider.config import Settings
    from agy_provider.provider import AgyProvider, configure
    from agy_provider.tools import ToolSnapshot
    from agy_provider.transport import AcpClient

    plugin_dir = (args.mimo_plugin_dir or config_dir.parent / "plugins" / MIMO_PLUGIN).resolve()
    require((plugin_dir / "main.py").is_file(), "MIMO_PLUGIN_SOURCE_MISSING")
    # 使用安装插件的真实 namespace 包；不把 main.py 当孤立文件 exec/load。
    sys.path.insert(0, str(plugin_dir.parent))
    mimo = importlib.import_module(plugin_dir.name + ".main")
    require(Path(inspect.getsourcefile(mimo.Main)).resolve() == plugin_dir / "main.py",
            "MIMO_NAMESPACE_SOURCE_MISMATCH")
    require(issubclass(mimo.MimoTTSSpeakTool, FunctionTool), "MIMO_NOT_NATIVE_TOOL")
    require(mimo.MimoTTSSpeakTool.call is FunctionTool.call, "MIMO_CALL_METHOD_CHANGED")
    evidence["sources"] = {
        "astrbot_version": str(getattr(astrbot, "__version__", "unknown")),
        "provider": source_evidence(AgyProvider), "runner": source_evidence(ToolLoopAgentRunner),
        "executor": source_evidence(FunctionToolExecutor),
        "mimo_tool": source_evidence(mimo.MimoTTSSpeakTool),
        "run_mimo_tts": source_evidence(mimo.Main.run_mimo_tts),
    }
    settings = Settings.from_dict(plugin_config)
    configure(settings)
    # 不改 provider/settings/model；捕获文件与运行 session 目录分别隔离。
    native_context = Context.__new__(Context)
    plugin = mimo.Main.__new__(mimo.Main)
    plugin.context = native_context
    plugin.config = mimo_config
    plugin._plugin_data_dir = directory / "plugin"
    plugin._audio_dir = plugin._plugin_data_dir / "audio_cache"
    plugin._clone_dir = plugin._plugin_data_dir / "clone_cache"
    plugin._audio_dir.mkdir(parents=True, mode=0o700)
    plugin._clone_dir.mkdir(mode=0o700)
    plugin._session_clone_samples = {}
    nonce = uuid.uuid4().hex[:10]
    test_text = "原生语音验收，编号" + nonce + "。"
    probe_text = "native-context-" + nonce
    evidence["test_text"] = test_text
    evidence["nonce"] = nonce
    evidence["tts"] = {"run_count": 0, "run_mimo_tts_count": 0, "request_count": 0,
                       "http_count": 0, "records": [], "send_to_qq_count": 0}
    loops = evidence["loops"]
    current = {}

    class CaptureEvent(AstrMessageEvent):
        def __init__(self, label):
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.self_id = "native-check-no-adapter"
            message.session_id = "native-check-" + nonce + "-" + label
            message.message_id = uuid.uuid4().hex
            message.sender = MessageMember(user_id="native-check", nickname="native-check")
            message.message = [Plain(text=test_text)]
            message.message_str = test_text
            message.raw_message = {}
            super().__init__(test_text, message,
                             PlatformMetadata(name="native_capture", description="No platform adapter",
                                              id="native-check-" + nonce), message.session_id)
            self.captures = []

        async def send(self, message):
            # 绝不调用 super().send / platform API；连统计上报也不触发。
            captured = {"delivery": "file_capture_only", "sent_to_qq": False, "components": []}
            for component in message.chain or []:
                if isinstance(component, Record):
                    path = Path(component.file).resolve()
                    require(path.is_relative_to(plugin._audio_dir.resolve()), "RECORD_OUTSIDE_ISOLATION")
                    require(path.is_file(), "RECORD_FILE_MISSING")
                    item = {"type": "Record", "capture_path": str(path), "wav": wav_evidence(path)}
                    evidence["tts"]["records"].append(item)
                    self.untrack_temporary_local_file(str(path))
                elif isinstance(component, Plain):
                    item = {"type": "Plain", **text_evidence(component.text, test_text)}
                else:
                    raise CheckFailure("UNEXPECTED_CAPTURE_COMPONENT")
                captured["components"].append(item)
            path = directory / ("capture-" + str(len(self.captures) + 1) + ".json")
            dump(path, captured)
            self.captures.append(str(path))
            evidence["tts"]["capture_files"] = list(self.captures)
            self._has_send_oper = True

    def context_ok(wrapper):
        return (isinstance(wrapper, ContextWrapper)
                and isinstance(wrapper.context, AstrAgentContext)
                and wrapper.context.context is native_context
                and isinstance(wrapper.context.event, CaptureEvent)
                and current_message_event.get() is wrapper.context.event)

    def checked_probe(kind, context, text):
        row = {"method": kind, "context_ok": context_ok(context) if kind == "call" else
               isinstance(context, CaptureEvent) and current_message_event.get() is context,
               "arguments": text_evidence(text, probe_text)}
        current["executions"].append(row)
        require(row["context_ok"], "TOOL_CONTEXT_INVALID")
        require(text == probe_text, "ECHO_ARGUMENT_MISMATCH")
        require(len(current["executions"]) == 1, "DUPLICATE_PROBE_EXECUTION")
        marker = "native-result-" + uuid.uuid4().hex
        current["marker"] = marker
        return json.dumps({"text": text, "marker": marker})

    class CallProbe(FunctionTool):
        async def call(self, context, text):
            return checked_probe("call", context, text)

    class RunProbe(FunctionTool):
        async def run(self, event, text):
            return checked_probe("run", event, text)

    class Hooks(BaseAgentRunHooks):
        async def on_tool_start(self, context, tool, tool_args):
            current["hooks"].append({"phase": "start", "name": tool.name,
                                     "context_ok": context_ok(context)})

        async def on_tool_end(self, context, tool, tool_args, result):
            current["hooks"].append({"phase": "end", "name": tool.name,
                                     "context_ok": context_ok(context),
                                     "result_is_none": result is None})

    class ObservedProvider(AgyProvider):
        async def text_chat(self, *positional, **kwargs):
            row = {"model": kwargs.get("model") or self.get_model(),
                   "abort_signal_present": isinstance(kwargs.get("abort_signal"), asyncio.Event),
                   "schema": []}
            tools = kwargs.get("func_tool")
            for tool in getattr(tools, "tools", []):
                row["schema"].append({"name": tool.name,
                                      "properties": sorted((tool.parameters or {}).get("properties", {})),
                                      "has_handler": bool(tool.handler)})
            current["model_calls"].append(row)
            try:
                response = await super().text_chat(*positional, **kwargs)
            except Exception as exc:
                row["error_type"] = type(exc).__name__
                row["diagnostic"] = safe_exception(exc)
                if "code" in row["diagnostic"]:
                    row["error_code"] = row["diagnostic"]["code"]
                raise
            row["response_tool_names"] = list(response.tools_call_name)
            for name, arguments, call_id in zip(response.tools_call_name, response.tools_call_args,
                                                response.tools_call_ids, strict=True):
                safe_args = {"keys": sorted(arguments)}
                if "text" in arguments:
                    safe_args["text"] = text_evidence(arguments["text"],
                                                      test_text if name == "mimo_tts_speak" else probe_text)
                current["tool_calls"].append({"name": name, "id": call_id, "arguments": safe_args})
            return response

    original_execute = FunctionToolExecutor.execute
    original_write = AcpClient._write
    original_drain_stderr = AcpClient._drain_stderr
    original_mimo_run = mimo.MimoTTSSpeakTool.run
    original_run_tts = mimo.Main.run_mimo_tts
    original_request = mimo.Main._request_tts
    original_open = mimo.Main._open_direct
    original_build_opener = urllib.request.build_opener
    provider = None

    @functools.wraps(original_execute)
    async def observed_execute(cls, tool, run_context, **tool_args):
        row = {"name": tool.name, "context_ok": context_ok(run_context), "native": True}
        current["native_executor"].append(row)
        require(row["context_ok"], "EXECUTOR_CONTEXT_INVALID")
        require(tool.name in ("native_context_call", "native_context_run", "mimo_tts_speak"),
                "UNAUTHORIZED_HOST_TOOL")
        try:
            async with contextlib.aclosing(original_execute(tool, run_context, **tool_args)) as source:
                async for result in source:
                    yield result
            row["returned"] = True
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            raise

    async def diagnostic_stderr(self):
        # Continuously drain the pipe as the native implementation does. Retain
        # only a bounded transient window and emit literal allowlisted markers.
        row = {"markers": [], "bytes_drained": 0, "exit_code": None}
        current["startup_diagnostics"].append(row)
        pending = ""
        try:
            while data := await self.proc.stderr.read(8192):
                row["bytes_drained"] += len(data)
                window = pending + data.decode("utf-8", "replace")
                row["markers"] = sorted(set(row["markers"]) | {
                    value for key, value in SAFE_DIAGNOSTIC_MARKERS.items() if key in window
                })
                pending = window[-16384:]
                window = ""
        finally:
            pending = ""
            row["exit_code"] = self.proc.returncode
            row["last_acp_method"] = current.get("last_acp_method")

    async def observed_write(client, obj):
        method = obj.get("method")
        params = obj.get("params", {})
        if method in {"initialize", "session/new", "session/prompt", "session/set_model", "session/set_config_option", "session/cancel"}:
            current["last_acp_method"] = method
        if method == "session/prompt":
            current["acp_prompts"] += 1
        elif method == "session/new":
            current["mcp_servers"].append([s.get("name") for s in params.get("mcpServers", [])])
        elif method == "session/set_model":
            current["acp_model_selections"].append(params.get("modelId"))
        elif method == "session/set_config_option" and params.get("configId") == "model":
            current["acp_model_selections"].append(params.get("value"))
        await original_write(client, obj)

    @functools.wraps(original_mimo_run)
    async def observed_mimo_run(tool, event, *positional, **kwargs):
        stats = evidence["tts"]
        stats["run_count"] += 1
        stats["run_event_ok"] = isinstance(event, CaptureEvent) and current_message_event.get() is event
        stats["run_text"] = text_evidence(kwargs.get("text"), test_text)
        require(stats["run_count"] == 1, "DUPLICATE_TTS_BLOCKED")
        require(stats["run_event_ok"], "MIMO_RUN_EVENT_INVALID")
        require(stats["run_text"]["matches_test_text"], "MIMO_RUN_TEXT_MISMATCH")
        native_result = await original_mimo_run(tool, event, *positional, **kwargs)
        stats["original_return_is_none"] = native_result is None
        require(native_result is None, "MIMO_RETURN_CONTRACT_CHANGED")
        require(len(stats["records"]) == 1, "TTS_RECEIPT_WITHOUT_REAL_CAPTURE")
        if args.native_terminal:
            stats["receipt_adapter"] = None
            stats["return_contract"] = "native_None_direct_result"
            return native_result
        wav = stats["records"][0]["wav"]
        stats["return_contract"] = "isolated_JSON_receipt"
        stats["receipt_adapter"] = "isolated_run_receipt_after_native_tool_and_real_HTTP"
        stats["receipt_marker"] = "native-tts-result-" + uuid.uuid4().hex
        return json.dumps({"marker": stats["receipt_marker"], "sent_to_qq": False,
                           "capture_path": wav["path"], "bytes": wav["bytes"],
                           "format": wav["container"], "sha256": wav["sha256"]})

    @functools.wraps(original_run_tts)
    async def observed_run_tts(instance, event, *positional, **kwargs):
        stats = evidence["tts"]
        stats["run_mimo_tts_count"] += 1
        require(instance is plugin and isinstance(event, CaptureEvent), "MIMO_MAIN_CONTEXT_INVALID")
        require(kwargs.get("text") == test_text, "MIMO_MAIN_TEXT_MISMATCH")
        require(stats["run_mimo_tts_count"] == 1, "DUPLICATE_TTS_MAIN_BLOCKED")
        return await original_run_tts(instance, event, *positional, **kwargs)

    @functools.wraps(original_request)
    async def observed_request(instance, payload):
        stats = evidence["tts"]
        stats["request_count"] += 1
        require(stats["request_count"] == 1, "DUPLICATE_TTS_HTTP_BLOCKED")
        messages = payload.get("messages", [])
        actual_text = messages[-1].get("content") if messages else None
        stats["http_payload_text"] = text_evidence(actual_text, test_text)
        stats["tts_model"] = payload.get("model")
        stats["requested_format"] = payload.get("audio", {}).get("format")
        require(actual_text == test_text, "TTS_PAYLOAD_TEXT_MISMATCH")
        audio = await original_request(instance, payload)
        stats["decoded_bytes"] = len(audio)
        stats["decoded_sha256"] = sha(audio)
        return audio

    @functools.wraps(original_open)
    def observed_open(instance, request):
        stats = evidence["tts"]
        stats["http_count"] += 1
        require(stats["http_count"] == 1, "DUPLICATE_TTS_OPEN_BLOCKED")
        require(request.get_method() == "POST", "TTS_HTTP_METHOD_INVALID")
        # body/headers/响应从不序列化到证据或日志。
        try:
            response = original_open(instance, request)
            stats["http_status"] = response.getcode()
            return response
        except urllib.error.HTTPError as exc:
            stats["http_status"] = exc.code
            raise

    def proxy_bound_opener(*handlers):
        # 执行的仍是 Main._open_direct、_request_tts、原 API/解码实现。
        # 仅将原本强制直连的空 ProxyHandler 绑定至已有容器 proxy。
        bound = []
        for handler in handlers:
            if isinstance(handler, urllib.request.ProxyHandler) and not handler.proxies:
                handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
                evidence["tts"]["existing_proxy_bound"] = True
            bound.append(handler)
        return original_build_opener(*bound)

    async def one_loop(label, probe_class, schema_mode, with_tts):
        nonlocal current, provider
        current = {"schema_mode": schema_mode, "status": "FAIL", "model_calls": [],
                   "tool_calls": [], "native_executor": [], "hooks": [], "executions": [],
                   "mcp_servers": [], "acp_prompts": 0, "acp_model_selections": [],
                   "startup_diagnostics": [], "native_terminal": bool(args.native_terminal and with_tts)}
        loops[label] = current
        event = CaptureEvent(label)
        wrapper = ContextWrapper(context=AstrAgentContext(context=native_context, event=event),
                                 tool_call_timeout=settings.tool_execution_timeout)
        probe_name = "native_context_call" if probe_class is CallProbe else "native_context_run"
        probe = probe_class(name=probe_name, description="Echo the exact text and return a runtime marker.",
                            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                                        "required": ["text"], "additionalProperties": False})
        tools = [probe]
        if with_tts:
            tools.append(mimo.MimoTTSSpeakTool(plugin=plugin))
        toolset = ToolSet(tools=tools)
        snapshot = ToolSnapshot(toolset)
        current["snapshot"] = {"names": sorted(snapshot.to_wire),
                               "skipped_count": len(snapshot.skipped)}
        require(current["snapshot"]["names"] == sorted(t.name for t in tools), "TOOL_SNAPSHOT_DROPPED_NATIVE")
        prompt = (f"Call {probe_name} exactly once with text={json.dumps(probe_text)}. "
                  "Wait for its real result; do not invent the runtime marker. ")
        if with_tts:
            prompt += ("After receiving that result, call mimo_tts_speak exactly once with "
                       f"text={json.dumps(test_text, ensure_ascii=False)}, audio_format=\"wav\", "
                       "send_text=false. Leave voice/mode/instruction/clone_sample at plugin defaults. "
                       "This is an explicitly authorized short voice test; its output is captured to a local "
                       "WAV by an isolated event, NOT delivered to QQ. Do not call any other tool or repeat speech. ")
            if args.native_terminal:
                prompt += "Speech is a native terminal/direct-result tool; after speech do not request another model turn or replay it."
            else:
                prompt += "Finally repeat both runtime markers: the echo marker and the TTS capture receipt marker."
        else:
            prompt += "Then repeat the complete real result JSON including its runtime marker. Do not call other tools."
        request = ProviderRequest(prompt=prompt, session_id=event.session_id, func_tool=toolset,
                                  contexts=[], model=configured_provider["model"])
        runner = ToolLoopAgentRunner()
        provider = ObservedProvider(dict(configured_provider), config.get("provider_settings", {}))
        # 此实例唯一写入目录；不触碰当前运行中 AstrBot 的 runtime。
        runtime_root = make_probe_runtime(config_dir)
        provider.runtime.root = runtime_root
        current["runtime_isolation"] = {"root": str(runtime_root), "auth_bridge": "deployment_wrapper",
                                        "script_auth_symlink_created": False}
        try:
            with event_context(event):
                await runner.reset(provider=provider, request=request, run_context=wrapper,
                                   tool_executor=FunctionToolExecutor(), agent_hooks=Hooks(),
                                   streaming=False, tool_schema_mode=schema_mode,
                                   fallback_providers=[], request_max_retries=1)
                require(runner._func_tool_for_provider() is not None, "RUNNER_TOOLS_REMOVED")
                require(context_ok(runner.run_context), "RUNNER_CONTEXT_INVALID")
                require(isinstance(runner._abort_signal, asyncio.Event), "RUNNER_ABORT_SIGNAL_MISSING")
                async with asyncio.timeout(args.timeout):
                    async for _ in runner.step_until_done(6):
                        pass
            state = getattr(runner, "_state", None)
            current["host_state"] = getattr(state, "name", None)
            current["effective_schema_mode"] = getattr(runner, "tool_schema_mode", schema_mode)
            final = runner.get_final_llm_resp()
            answer = final.completion_text if final else ""
            current["answer_sha256"] = sha(answer.encode())
            current["actual_echo_result_readback"] = bool(current.get("marker") and current["marker"] in answer)
            require(len(current["executions"]) == 1, "NATIVE_PROBE_NOT_EXECUTED_ONCE")
            expected_names = [probe_name] + (["mimo_tts_speak"] if with_tts else [])
            require([r["name"] for r in current["native_executor"]] == expected_names,
                    "NATIVE_EXECUTOR_COUNT_OR_ORDER")
            require([r["name"] for r in current["tool_calls"]] == expected_names,
                    "MODEL_TOOL_CALL_COUNT_OR_ORDER")
            require(all(r.get("returned") for r in current["native_executor"]), "NATIVE_EXECUTOR_FAILED")
            require([(r["phase"], r["name"]) for r in current["hooks"]] ==
                    [(phase, name) for name in expected_names for phase in ("start", "end")], "NATIVE_HOOK_ORDER")
            require(all(r["context_ok"] for r in current["hooks"]), "NATIVE_HOOK_CONTEXT_INVALID")
            require(current["acp_prompts"] >= 1 and any(current["mcp_servers"]), "REAL_ACP_MCP_MISSING")
            require(all(c["model"] == EXPECTED_MODEL for c in current["model_calls"]), "MODEL_CHANGED")
            if with_tts:
                stats = evidence["tts"]
                require(stats["run_count"] == stats["run_mimo_tts_count"] == stats["request_count"] ==
                        stats["http_count"] == len(stats["records"]) == 1, "TTS_COUNTS")
                require(stats.get("http_status") == 200 and stats.get("existing_proxy_bound"), "TTS_HTTP_PROXY_FAILED")
                wav = stats["records"][0]["wav"]
                require(wav["bytes"] == stats["decoded_bytes"] and wav["sha256"] == stats["decoded_sha256"],
                        "TTS_WAV_BYTES_MISMATCH")
                if args.native_terminal:
                    current["terminal_semantics"] = "native_None_direct_result; host_DONE_cleanup; TTS_unknown_no_replay"
                    require(current["host_state"] == "DONE", "NATIVE_TERMINAL_HOST_NOT_DONE")
                    require(current["hooks"][-1].get("result_is_none") is True, "NATIVE_NONE_HOOK_CONTRACT")
                    require(stats["original_return_is_none"] and stats["receipt_adapter"] is None,
                            "NATIVE_TERMINAL_RECEIPT_ADAPTER_USED")
                    require(current["acp_prompts"] == 1, "NATIVE_TERMINAL_PROMPT_REPLAY")
                    current["actual_tts_receipt_readback"] = None
                else:
                    current["actual_tts_receipt_readback"] = stats["receipt_marker"] in answer
                    require(current["actual_tts_receipt_readback"], "TTS_RECEIPT_NOT_READ_BACK")
                    current["terminal_semantics"] = "isolated_JSON_receipt_after_native_None; original_direct_result_not_tested"
            if not (with_tts and args.native_terminal):
                require(current["actual_echo_result_readback"], "ECHO_RUNTIME_RESULT_NOT_READ_BACK")
            outcomes = provider.runtime.tool_outcomes
            for row in current["tool_calls"]:
                expected = "unknown" if with_tts and args.native_terminal and row["name"] == "mimo_tts_speak" else "completed"
                require(outcomes.get(row["id"]) == expected, "ACP_TOOL_OUTCOME_CONTRACT")
            current["retained_turns_at_host_done"] = len(provider.runtime.turns)
            current["callers_at_host_done"] = provider.runtime.callers
            require(current["retained_turns_at_host_done"] == 0, "ACP_RETAINED_TURN_NOT_CLOSED")
            require(current["callers_at_host_done"] == 0, "ACP_CALLERS_NOT_RELEASED")
            current["status"] = "PASS"
        finally:
            current["tool_outcomes_before_close"] = dict(provider.runtime.tool_outcomes)
            current["retained_turns_before_close"] = len(provider.runtime.turns)
            current["callers_before_close"] = provider.runtime.callers
            runner.request_stop()
            await provider.terminate()
            shutil.rmtree(runtime_root, ignore_errors=True)
            current["runtime_isolation"]["removed_after_close"] = not runtime_root.exists()
            current["tool_outcomes_after_close"] = dict(provider.runtime.tool_outcomes)
            current["retained_turns_after_close"] = len(provider.runtime.turns)
            current["callers_after_close"] = provider.runtime.callers
            current["model_call_count"] = len(current["model_calls"])
            current["tool_call_count"] = len(current["tool_calls"])
            current["native_executor_count"] = len(current["native_executor"])
            current["hook_start_count"] = sum(r["phase"] == "start" for r in current["hooks"])
            current["hook_end_count"] = sum(r["phase"] == "end" for r in current["hooks"])
            provider = None

    FunctionToolExecutor.execute = classmethod(observed_execute)
    AcpClient._write = observed_write
    AcpClient._drain_stderr = diagnostic_stderr
    mimo.MimoTTSSpeakTool.run = observed_mimo_run
    mimo.Main.run_mimo_tts = observed_run_tts
    mimo.Main._request_tts = observed_request
    mimo.Main._open_direct = observed_open
    urllib.request.build_opener = proxy_bound_opener
    host_compat = None
    try:
        if args.native_terminal:
            host_compat = importlib.import_module("agy_provider.host_compat")
            host_compat.install_host_compat(ToolLoopAgentRunner)
            evidence["host_compat"] = {"installed": True, "source": source_evidence(host_compat.install_host_compat)}
        await one_loop("full", CallProbe, "full", True)
        if args.with_skills_like:
            # 单独 echo run；native-terminal 安装的 host_compat 会强制 full，证据记录实际模式。
            await one_loop("skills_like", RunProbe, "skills_like", False)
    finally:
        if provider is not None:
            await provider.terminate()
        FunctionToolExecutor.execute = classmethod(original_execute.__func__)
        AcpClient._write = original_write
        AcpClient._drain_stderr = original_drain_stderr
        if host_compat is not None:
            host_compat.uninstall_host_compat(ToolLoopAgentRunner)
        mimo.MimoTTSSpeakTool.run = original_mimo_run
        mimo.Main.run_mimo_tts = original_run_tts
        mimo.Main._request_tts = original_request
        mimo.Main._open_direct = original_open
        urllib.request.build_opener = original_build_opener


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="授权本次真实模型请求及最多一次语音 HTTP 合成")
    parser.add_argument("--with-skills-like", action="store_true", help="额外验收 skills_like run-echo；不重复 TTS")
    parser.add_argument("--native-terminal", action="store_true", help="安装 host_compat 并保留真实 None/direct-result；与默认 receipt 模式二选一，TTS仍最多一次")
    parser.add_argument("--config-dir", type=Path, default=Path("/data/config"))
    parser.add_argument("--astrbot-config", type=Path, help="默认 config-dir 的上级/cmd_config.json")
    parser.add_argument("--provider-id", help="存在多个 AGY 同模型配置时显式选择现有条目")
    parser.add_argument("--mimo-plugin-dir", type=Path, help="默认 config-dir 的上级/plugins/真实小米插件目录")
    parser.add_argument("--astrbot-source", type=Path, help="AstrBot 源码根目录（若未在容器 sys.path）")
    parser.add_argument("--token-file", type=Path,
                        default=Path.home() / ".gemini/antigravity-cli/antigravity-oauth-token")
    parser.add_argument("--output-dir", type=Path, default=Path("/data/native-tool-check"))
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if not args.execute:
        parser.print_help()
        return 0
    args.token_file = args.token_file.expanduser()
    require(10 <= args.timeout <= 1800, "TIMEOUT_CONFIG")
    os.umask(0o077)
    directory = args.output_dir.resolve() / ("native-" + uuid.uuid4().hex[:16])
    directory.mkdir(parents=True, mode=0o700)
    evidence = {"schema_version": 1, "started_utc": datetime.now(UTC).isoformat(), "status": "FAIL",
                "isolation": {"event": "CaptureEvent(AstrMessageEvent)", "platform_adapter_created": False,
                              "sent_to_qq": False, "login_contents_read": False,
                              "tts_max_real_requests": 1},
                "requested": {"full": True, "skills_like": args.with_skills_like,
                              "native_terminal": args.native_terminal}, "loops": {}}
    # 必须在框架 import 前设置；框架临时数据隔离，ACP session另在wrapper授权根隔离。
    os.environ["ASTRBOT_ROOT"] = str(directory / "astrbot-state")
    os.environ["TMPDIR"] = str(directory / "scratch")
    (directory / "scratch").mkdir(mode=0o700)
    tempfile.tempdir = None  # 丢弃 import 前缓存，临时文件必须尊重隔离 TMPDIR。
    started = time.monotonic()
    old_logging = logging.root.manager.disable
    try:
        # 不保存原始日志、响应、traceback：其中可能出现 key / 内联音频。
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as null, contextlib.redirect_stdout(null), contextlib.redirect_stderr(null):
            asyncio.run(run(args, directory, evidence))
        evidence["status"] = "PASS"
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        evidence["diagnostic"] = safe_exception(exc)
        evidence["error_code"] = evidence["diagnostic"].get("check_code") or evidence["diagnostic"].get("code", "NATIVE_CHECK_FAILED")
    finally:
        logging.disable(old_logging)
        evidence["duration_seconds"] = round(time.monotonic() - started, 3)
        # 保留 WAV/捕获/白名单证据；隔离 AGY token symlink 和运行目录均已关闭清理。
        shutil.rmtree(directory / "astrbot-state", ignore_errors=True)
        shutil.rmtree(directory / "scratch", ignore_errors=True)
        dump(directory / "evidence.json", evidence)
    print(json.dumps({"status": evidence["status"], "evidence_file": str(directory / "evidence.json"),
                      "sent_to_qq": False}, ensure_ascii=False))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
