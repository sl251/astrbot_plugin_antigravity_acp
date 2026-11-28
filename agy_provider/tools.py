"""Host-owned pending calls. No tool handlers execute in this module or MCP child.

AstrBot v4.29.4 ToolSet / LLMResponse contract, pinned to 6133541.
The host runner remains responsible for approval, hooks, execution and audit.
"""

import asyncio
import copy
import json
import math
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .builtin_tools import (
    SEARCH_ABORTED_NOTICE,
    BuiltinDenied,
    is_known_builtin,
    read_args_digest,
    read_target,
)
from .config import Settings
from .bridge_transport import BridgeTransport, TOKEN_ENV
from .errors import AgyError
from .messages import canonical_calls, digest
from .paths import remove_tree_robust

SERVER_NAME = "astrbot"
FRAME_LIMIT = 1048576
MAX_SESSION_CALLS = 4096


def _sanitize_for_gemini(schema, depth=0):
    """为 Gemini 模型副本补齐缺失的 array.items 提示，不改变宿主原始契约校验。

    Gemini 的 GenerateContentRequest 要求所有 array 属性（含 Optional/anyOf 联合分支）
    必须携带 items 字段；执行端仍按独立的宿主原始 schema 校验，不收窄合法参数范围。
    """
    if depth > 32 or not isinstance(schema, dict):
        return schema
    schema_type = schema.get("type")
    is_array = schema_type == "array" or (
        isinstance(schema_type, list) and "array" in schema_type
    )
    if is_array and not isinstance(schema.get("items"), dict):
        schema["items"] = {"type": "string"}
    props = schema.get("properties")
    if isinstance(props, dict):
        for value in props.values():
            _sanitize_for_gemini(value, depth + 1)
    for key in ("items", "additionalProperties"):
        child = schema.get(key)
        if isinstance(child, dict):
            _sanitize_for_gemini(child, depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        children = schema.get(key)
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _sanitize_for_gemini(child, depth + 1)
    return schema


def validate_json(value, code, depth=0):
    """Validate the entire JSON tree, including unconstrained extra properties."""
    if depth > 32:
        raise AgyError(code, "JSON 嵌套超过预算。")
    kind = type(value)
    if kind is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgyError(code, "JSON 对象键必须为字符串。")
            validate_json(key, code, depth + 1)
            validate_json(item, code, depth + 1)
    elif kind is list:
        for item in value:
            validate_json(item, code, depth + 1)
    elif kind is float:
        if not math.isfinite(value):
            raise AgyError(code, "JSON 不接受非有限数值。")
    elif kind is int:
        # Bound conversions even for direct in-process callers. This is below
        # Python's default decimal serialization limit of 4300 digits.
        if value.bit_length() > 14000:
            raise AgyError(code, "JSON 整数超过预算。")
    elif kind is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise AgyError(code, "JSON 字符串包含无效 Unicode。") from None
    elif value is not None and kind is not bool:
        raise AgyError(code, "工具数据必须为 JSON 类型。")


def reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def validate_schema(schema, depth=0):
    """Fail closed on schema constructs this stdlib validator cannot enforce."""
    if depth > 32 or not isinstance(schema, dict):
        raise AgyError("TOOL_SCHEMA", "工具 schema 无效或嵌套过深。")
    if depth == 0:
        validate_json(schema, "TOOL_SCHEMA")
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "anyOf",
        "oneOf",
        "allOf",
        "description",
        "title",
        "default",
        "examples",
        "$schema",
    }
    if set(schema) - allowed:
        raise AgyError("TOOL_SCHEMA", "工具 schema 含未支持的验证约束。")
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    if any(
        not isinstance(t, str)
        or t
        not in {"object", "array", "string", "integer", "number", "boolean", "null"}
        for t in types
    ):
        raise AgyError("TOOL_SCHEMA", "工具 schema 类型无效。")
    props = schema.get("properties", {})
    if not isinstance(props, dict) or any(not isinstance(k, str) for k in props):
        raise AgyError("TOOL_SCHEMA", "工具属性无效。")
    for child in props.values():
        validate_schema(child, depth + 1)
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(k, str) for k in required):
        raise AgyError("TOOL_SCHEMA", "工具必填属性无效。")
    for key in ("items", "additionalProperties"):
        if key in schema and not (
            key == "additionalProperties" and type(schema[key]) is bool
        ):
            validate_schema(schema[key], depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise AgyError("TOOL_SCHEMA", "工具组合 schema 无效。")
            for child in schema[key]:
                validate_schema(child, depth + 1)
    for key in (
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    ):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise AgyError("TOOL_SCHEMA", "工具 schema 长度无效。")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and (
            type(schema[key]) not in (int, float)
            or (type(schema[key]) is float and not math.isfinite(schema[key]))
        ):
            raise AgyError("TOOL_SCHEMA", "工具 schema 数值无效。")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not schema["enum"]
    ):
        raise AgyError("TOOL_SCHEMA", "工具 schema 枚举无效。")
    if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
        raise AgyError("TOOL_SCHEMA", "工具 schema uniqueItems 无效。")


def validate(value, schema, depth=0):
    def invalid():
        raise AgyError("TOOL_ARGUMENTS", "工具参数不满足完整 schema。")

    if depth > 32:
        invalid()
    if depth == 0:
        validate_json(value, "TOOL_ARGUMENTS")
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) is int or (type(value) is float and math.isfinite(value)),
        "boolean": type(value) is bool,
        "null": value is None,
    }
    types = schema.get("type", [])
    types = types if isinstance(types, list) else [types]
    if types and not any(matches.get(t, False) for t in types):
        invalid()
    if "enum" in schema and digest(value) not in [digest(v) for v in schema["enum"]]:
        invalid()
    if "const" in schema and digest(value) != digest(schema["const"]):
        invalid()
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword not in schema:
            continue
        successes = 0
        for child in schema[keyword]:
            try:
                validate(value, child, depth + 1)
                successes += 1
            except AgyError:
                pass
        if (
            (keyword == "anyOf" and not successes)
            or (keyword == "oneOf" and successes != 1)
            or (keyword == "allOf" and successes != len(schema[keyword]))
        ):
            invalid()
    if isinstance(value, dict):
        if not set(schema.get("required", [])) <= value.keys():
            invalid()
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                validate(item, props[key], depth + 1)
            elif schema.get("additionalProperties") is False:
                invalid()
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(item, schema["additionalProperties"], depth + 1)
        low, high = "minProperties", "maxProperties"
    elif isinstance(value, list):
        for item in value:
            validate(item, schema.get("items", {}), depth + 1)
        if schema.get("uniqueItems") and len({digest(v) for v in value}) != len(value):
            invalid()
        low, high = "minItems", "maxItems"
    elif isinstance(value, str):
        low, high = "minLength", "maxLength"
    else:
        low = high = None
    if low and (
        len(value) < schema.get(low, 0) or len(value) > schema.get(high, float("inf"))
    ):
        invalid()
    if type(value) in (int, float):
        if (
            (type(value) is float and not math.isfinite(value))
            or value < schema.get("minimum", -float("inf"))
            or value > schema.get("maximum", float("inf"))
            or value <= schema.get("exclusiveMinimum", -float("inf"))
            or value >= schema.get("exclusiveMaximum", float("inf"))
        ):
            invalid()


class ToolSnapshot:
    def __init__(self, func_tool=None):
        if func_tool is not None and not isinstance(
            getattr(func_tool, "tools", None), list
        ):
            raise AgyError("TOOL_CONTEXT", "func_tool 必须为完整的宿主 ToolSet。")
        self.tools = []
        self.to_host = {}
        self.to_host_schema = {}
        self.to_wire = {}
        self.skipped = []
        for tool in getattr(func_tool, "tools", []):
            if not getattr(tool, "active", True):
                continue
            name = getattr(tool, "name", None)
            try:
                # Providers serialize the host-supplied active ToolSet. A
                # FunctionTool may implement run()/call(), or be a schema-only
                # skills_like view; neither needs a handler here. Execution and
                # raw-tool lookup remain exclusively in AstrBot's executor.
                if not isinstance(name, str) or not name or name in self.to_wire:
                    raise AgyError("TOOL_SCHEMA", "工具名称为空或重复。")
                wire = (
                    name
                    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                    and not name.startswith("agy_")
                    else "agy_" + digest(name)[:48]
                )
                if wire in self.to_host:
                    raise AgyError("TOOL_SCHEMA", "工具名称映射冲突。")
                schema = copy.deepcopy(tool.parameters)
                if schema is None:
                    schema = {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    }
                validate_schema(schema)
                if schema.get("type") != "object":
                    raise AgyError("TOOL_SCHEMA", "工具根参数必须为 object。")
                wire_schema = _sanitize_for_gemini(copy.deepcopy(schema))
                validate_schema(wire_schema)
            except AgyError as exc:
                label = name if isinstance(name, str) and name else "<unnamed>"
                self.skipped.append(f"{label}: {exc}")
                continue
            self.to_host[wire] = name
            self.to_host_schema[wire] = schema
            self.to_wire[name] = wire
            self.tools.append(
                {
                    "name": wire,
                    "description": str(tool.description or ""),
                    "inputSchema": wire_schema,
                }
            )
        self.tools.sort(key=lambda item: item["name"])
        encoded_tools = json.dumps(self.tools, ensure_ascii=False).encode()
        if len(self.tools) > 128 or len(encoded_tools) > FRAME_LIMIT // 2:
            raise AgyError("TOOL_SCHEMA", "工具集超过预算。")
        self.schema_bytes = len(encoded_tools) if self.tools else 0
        # Distinct host contracts can have identical Gemini schemas (e.g. a
        # missing items vs explicit string items). Both must invalidate reuse
        # and host tool continuation when the contract changes.
        self.fingerprint = (
            digest({"tools": self.tools, "host_schemas": self.to_host_schema})
            if self.tools else ""
        )

    def notification_name(self, name):
        prefix = f"mcp_{SERVER_NAME}_"
        return (
            self.to_host.get(name[len(prefix) :])
            if isinstance(name, str) and name.startswith(prefix)
            else None
        )


@dataclass
class PendingCall:
    id: str
    name: str
    arguments: dict
    future: asyncio.Future
    state: str = "not_executed"
    delivered: bool = False
    created_at: float = 0
    handed_off: asyncio.Event = field(default_factory=asyncio.Event)
    execution_timed_out: bool = False

    def wire(self):
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    def assistant_message(self, text):
        calls = canonical_calls([
            {"id": self.id, "function": {"name": self.name, "arguments": self.arguments}},
        ])
        return {"role": "assistant", "content": text, "tool_calls": calls}


class ToolBroker:
    def __init__(self, snapshot: ToolSnapshot, settings: Settings):
        self.snapshot, self.settings = snapshot, settings
        self.session_id = None
        self.turn_active = False
        self.pending: dict[str, PendingCall] = {}
        self.audit: dict[str, str] = {}
        self.seen = set()
        self.count = 0
        self.result_bytes = 0
        self.ready = asyncio.Queue(maxsize=settings.tool_max_concurrent)
        self.server = None
        self.directory = None
        self.path = None
        self.closed = False
        self.tasks = set()
        self.notifications = {}
        self.read_workspace = None
        self.read_calls = {}
        self.read_args = {}
        self.read_decisions = {}
        self.search_calls = {}
        self.search_seen = set()
        self.search_args = {}
        self.on_builtin_call = None
        self.close_task = None
        self.audit_sink = None
        self.transport = BridgeTransport(tcp=os.name == "nt")

    def record_outcome(self, call):
        self.audit[call.id] = call.state
        if self.audit_sink is not None:
            self.audit_sink(call.id, call.state)

    async def start(self):
        if os.name not in {"posix", "nt"}:
            raise AgyError("UNSUPPORTED", "当前平台没有可用的本地工具桥。")
        try:
            if self.transport.tcp:
                self.server = await asyncio.start_server(
                    self._connection, host="127.0.0.1", port=0, limit=FRAME_LIMIT,
                )
                endpoint = f"tcp:{self.server.sockets[0].getsockname()[1]}"
            else:
                self.directory = Path(tempfile.mkdtemp(prefix="agy-mcp-"))
                self.path = self.directory / "bridge.sock"
                self.server = await asyncio.start_unix_server(
                    self._connection, path=self.path, limit=FRAME_LIMIT
                )
                self.path.chmod(0o600)
                endpoint = str(self.path)
        except BaseException:
            await self.close()
            raise
        return {
            "name": SERVER_NAME,
            "type": "stdio",
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("mcp_bridge.py")), endpoint],
            "env": {TOKEN_ENV: self.transport.token} if self.transport.tcp else {},
        }

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        if self.closed or len(self.tasks) >= self.settings.tool_max_concurrent + 4:
            writer.close()
            return
        self.tasks.add(task)
        work = eof = None
        try:
            async with asyncio.timeout(self.settings.tool_approval_timeout):
                request = await self.transport.receive(reader, writer)
            work = asyncio.create_task(self.dispatch(request))
            eof = asyncio.create_task(reader.read(1))
            await asyncio.wait((work, eof), return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                try:
                    reply = {"result": await work}
                except AgyError as exc:
                    reply = {"error": {"code": -32001, "message": exc.code}}
                writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                await asyncio.wait_for(writer.drain(), 2)
        except (ValueError, OSError, TimeoutError, RecursionError, asyncio.LimitOverrunError):
            pass
        finally:
            try:
                for child in (work, eof):
                    if child:
                        child.cancel()
                # Broker shutdown can cancel this task while it joins its children.
                writer.close()
                await asyncio.gather(
                    *(t for t in (work, eof) if t), return_exceptions=True
                )
                try:
                    await asyncio.wait_for(writer.wait_closed(), 2)
                except (OSError, TimeoutError):
                    pass
            finally:
                self.tasks.discard(task)

    async def dispatch(self, request):
        if self.closed:
            raise AgyError("CLOSED", "工具会话已结束。")
        if not isinstance(request, dict):
            raise AgyError("TOOL_PROTOCOL", "无效工具请求。")
        if request.get("method") == "tools/list":
            return {"tools": self.snapshot.tools}
        if request.get("method") != "tools/call":
            raise AgyError("TOOL_PROTOCOL", "未知工具请求。")
        if not self.turn_active:
            raise AgyError("TOOL_STALE", "当前没有活跃工具轮，拒绝过期调用。")
        params = request.get("params")
        if not isinstance(params, dict):
            raise AgyError("TOOL_PROTOCOL", "工具参数无效。")
        meta = params.get("_meta", {})
        if (
            not isinstance(meta, dict)
            or not self.session_id
            or any(
                meta.get(key) != self.session_id
                for key in (
                    "antigravity.google/conversation_id",
                    "antigravity.google/root_conversation_id",
                )
            )
        ):
            raise AgyError("TOOL_SESSION", "工具调用不属于当前会话。")
        request_id = request.get("id")
        if type(request_id) not in (str, int) or not str(request_id):
            raise AgyError("TOOL_ID", "工具调用 ID 无效。")
        identity = (type(request_id).__name__, request_id)
        if identity in self.seen:
            raise AgyError("TOOL_DUPLICATE", "工具调用 ID 已使用。")
        # Count invalid attempts too: a bad model must not grow memory forever.
        if (
            self.count >= self.settings.tool_max_calls
            or len(self.seen) >= MAX_SESSION_CALLS
        ):
            raise AgyError("TOOL_BUDGET", "工具次数超过单轮或会话预算。")
        self.seen.add(identity)
        self.count += 1
        name = params.get("name")
        if not isinstance(name, str) or name not in self.snapshot.to_host:
            raise AgyError("TOOL_UNKNOWN", "工具不在当前宿主许可集合。")
        arguments = params.get("arguments", {})
        validate(arguments, self.snapshot.to_host_schema[name])
        if (
            not isinstance(arguments, dict)
            or len(json.dumps(arguments).encode()) > FRAME_LIMIT // 2
        ):
            raise AgyError("TOOL_ARGUMENTS", "工具参数超过预算。")
        if len(self.pending) >= self.settings.tool_max_concurrent:
            raise AgyError("TOOL_CONCURRENCY", "待处理工具数超过并发预算。")
        call = PendingCall(
            "agy_" + uuid.uuid4().hex,
            self.snapshot.to_host[name],
            copy.deepcopy(arguments),
            asyncio.get_running_loop().create_future(),
        )
        self.pending[call.id] = call
        call.created_at = asyncio.get_running_loop().time()
        delivered = asyncio.create_task(call.handed_off.wait())
        try:
            try:
                self.ready.put_nowait(call)
            except asyncio.QueueFull:
                raise AgyError("TOOL_CONCURRENCY", "工具交接队列已满。") from None
            async with asyncio.timeout(self.settings.tool_approval_timeout):
                await asyncio.wait(
                    (delivered, call.future), return_when=asyncio.FIRST_COMPLETED
                )
                if call.future.done():
                    return await call.future
            async with asyncio.timeout(self.settings.tool_execution_timeout):
                return await call.future
        except TimeoutError:
            if not call.delivered:
                self.reject(call, "TOOL_APPROVAL_TIMEOUT")
                return call.future.result()
            call.execution_timed_out = True
            raise AgyError(
                "TOOL_TIMEOUT", "宿主审批或工具执行超时；不会自动重放。"
            ) from None
        finally:
            delivered.cancel()
            await asyncio.gather(delivered, return_exceptions=True)
            self.record_outcome(call)
            self.pending.pop(call.id, None)
            if not call.future.done():
                call.future.cancel()
            # Atomic on this event loop: remove only this call, preserving
            # other pending calls and their order after cancellation/timeout.
            retained = []
            while not self.ready.empty():
                queued = self.ready.get_nowait()
                if queued is not call:
                    retained.append(queued)
            for queued in retained:
                self.ready.put_nowait(queued)

    def handoff(self, call):
        if (
            self.closed
            or call.id not in self.pending
            or call.future.done()
            or call.delivered
        ):
            raise AgyError("TOOL_STALE", "工具交接已失效。")
        call.delivered = True
        call.handed_off.set()
        # Native runner has no execute-start callback. After handoff, cancellation
        # MUST conservatively record unknown, never claim not-executed.
        call.state = "unknown"

    def reject(self, call, code):
        if not call.future.done():
            call.future.set_result(
                {"content": [{"type": "text", "text": code}], "isError": True}
            )

    def resolve(self, call_id, content, *, attachments: list[dict] | None = None):
        call = self.pending.get(call_id)
        if self.closed or call is None or not call.delivered or call.future.done():
            raise AgyError("TOOL_STALE", "工具结果迟到或重复。")
        blocks, text_size = self.build_result_blocks(content, attachments=attachments)
        self.result_bytes += text_size
        call.state = "completed"
        call.future.set_result({"content": blocks, "isError": False})

    def build_result_blocks(self, content, *, attachments: list[dict] | None = None):
        text_size = self.check_result(content)
        blocks = [{"type": "text", "text": content}]
        if attachments is not None and not isinstance(attachments, list):
            raise AgyError("TOOL_RESULT", "工具附件必须为内容块列表。")
        for part in attachments or []:
            if not isinstance(part, dict):
                raise AgyError("TOOL_RESULT", "工具附件块无效。")
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                text = part["text"]
                validate_json(text, "TOOL_RESULT")
                text_size += len(text.encode())
                blocks.append({"type": "text", "text": text})
            elif (
                part.get("type") == "image"
                and set(part) == {"type", "mimeType", "data"}
                and part["mimeType"] in ("image/png", "image/jpeg")
                and isinstance(part["data"], str)
                and part["data"]
            ):
                validate_json(part["data"], "TOOL_RESULT")
                blocks.append(dict(part))
            else:
                raise AgyError("TOOL_RESULT", "工具附件块格式无效。")
        if self.result_bytes + text_size > self.settings.tool_result_bytes:
            raise AgyError("TOOL_RESULT_LIMIT", "工具文本结果超过本轮总字节预算；不会截断。")
        # Match the socket reply encoding and reserve space for the MCP envelope.
        wire = json.dumps({"result": {"content": blocks, "isError": False}}, ensure_ascii=False)
        if len(wire.encode()) + 1 > 15 * FRAME_LIMIT:
            raise AgyError("TOOL_RESULT_LIMIT", "工具结果超过本地桥单帧上限；不会截断。")
        return blocks, text_size

    def check_result(self, content):
        if not isinstance(content, str):
            raise AgyError("TOOL_RESULT", "仅支持宿主文本/JSON文本工具结果。")
        validate_json(content, "TOOL_RESULT")
        size = len(content.encode())
        if (
            self.result_bytes + size > self.settings.tool_result_bytes
            or len(json.dumps(content, ensure_ascii=False).encode()) > 15 * FRAME_LIMIT
        ):
            raise AgyError(
                "TOOL_RESULT_LIMIT", "工具结果超过本轮总字节预算；不会截断。"
            )
        return size

    @property
    def read_ledger_full(self):
        return len(self.read_decisions) >= MAX_SESSION_CALLS

    @property
    def observed_count(self):
        return len(self.notifications) + len(self.read_calls) + len(self.search_calls)

    def observe_read(self, update, ident, raw_name):
        if not self.read_workspace:
            raise BuiltinDenied()
        previous = self.read_calls.get(ident)
        if not ident or len(ident) > 256 or ident in self.notifications or raw_name not in ("read_file", None):
            raise BuiltinDenied()
        if previous is None and (
            update.get("sessionUpdate") != "tool_call" or raw_name != "read_file"
            or ident in self.read_decisions or self.read_ledger_full
            or self.observed_count >= self.settings.tool_max_calls
        ):
            raise BuiltinDenied()
        status = update.get("status")
        if status not in ("in_progress", "pending", "completed", "failed"):
            raise BuiltinDenied()
        if previous and previous[1] in ("completed", "failed"):
            raise BuiltinDenied()
        if status == "completed" and not self.read_decisions.get(ident):
            raise BuiltinDenied()
        target = read_target(update, self.read_workspace)
        args_key = read_args_digest(update.get("rawInput"))
        if previous and not any(k in update for k in ("rawInput", "args", "locations")):
            target = previous[0]
            args_key = self.read_args.get(ident)
        if target is None or (previous and previous[0] != target):
            raise BuiltinDenied()
        if previous and ident in self.read_args and args_key != self.read_args[ident]:
            raise BuiltinDenied()
        self.read_calls[ident] = (target, status)
        if args_key is not None:
            self.read_args[ident] = args_key

    def observe_search(self, update, ident):
        if (not self.turn_active or self.closed or not self.settings.enable_tools
                or self.settings.builtin_search_web != "allow"
                or not ident or len(ident) > 256 or ident in self.notifications
                or ident in self.read_calls or ident in self.read_decisions):
            raise BuiltinDenied(message=SEARCH_ABORTED_NOTICE, user_notice=SEARCH_ABORTED_NOTICE)
        status = update.get("status")
        if ident in self.search_calls:
            if (update.get("sessionUpdate") != "tool_call_update"
                    or update.get("name", "search_web") != "search_web"
                    or update.get("kind", "search") != "search"
                    or self.search_calls[ident] != "in_progress"
                    or status not in ("in_progress", "completed", "failed")
                    or ("rawInput" in update and update["rawInput"] != self.search_args[ident])
                    or ("args" in update and update["args"] != self.search_args[ident])):
                raise BuiltinDenied(message=SEARCH_ABORTED_NOTICE)
            self.search_calls[ident] = status
            return

        args = update.get("rawInput")
        if (update.get("sessionUpdate") != "tool_call" or update.get("kind") != "search"
                or status != "in_progress" or ident in self.search_seen
                or not isinstance(args, dict) or set(args) != {"query"}
                or not isinstance(args["query"], str) or update.get("args", args) != args):
            raise BuiltinDenied()
        over_budget = (max(self.count, self.observed_count) >= self.settings.tool_max_calls
                       or len(self.search_seen) >= MAX_SESSION_CALLS)
        # The CLI can send the request before this notification, even over budget.
        self.count += 1
        if self.on_builtin_call:
            self.on_builtin_call(observed=True)
        self.search_args[ident] = args
        if over_budget:
            raise BuiltinDenied("TOOL_BUDGET", "检测到超预算的 CLI 联网搜索，已中止本轮；搜索请求可能已经发出。",
                                user_notice=SEARCH_ABORTED_NOTICE)
        self.search_seen.add(ident)
        self.search_calls[ident] = status

    def observe(self, update):
        ident = update.get("toolCallId")
        if not isinstance(ident, str):
            raise AgyError("TOOL_PROTOCOL", "工具通知缺少 ID。")
        raw_name = update.get("name")
        if raw_name == "search_web" or ident in self.search_seen:
            self.observe_search(update, ident)
            return
        if ident in self.read_calls or raw_name == "read_file":
            self.observe_read(update, ident, raw_name)
            return
        if is_known_builtin(raw_name):
            raise BuiltinDenied()
        name = self.snapshot.notification_name(raw_name)
        previous = self.notifications.get(ident)
        if name is None and ("name" in update or previous is None):
            raise AgyError("TOOL_PERMISSION", "AGY 尝试调用未授权的工具。",
                           no_retry=True, no_fallback=True)
        if previous and name and previous[0] != name:
            raise AgyError("TOOL_PROTOCOL", "工具通知 ID 名称发生变化。")
        if not previous and self.observed_count >= self.settings.tool_max_calls:
            raise AgyError("TOOL_BUDGET", "工具通知超过本轮预算。")
        status = update.get("status", "in_progress")
        if previous and previous[1] in {"completed", "failed"}:
            return
        self.notifications[ident] = (name or previous[0], status)

    async def close(self):
        if self.close_task is None:
            self.closed = True
            self.close_task = asyncio.create_task(
                self._close(), name="agy:broker-close"
            )
        cancelled = None
        while True:
            try:
                await asyncio.shield(self.close_task)
                break
            except asyncio.CancelledError as exc:
                cancelled = cancelled or exc
                if self.close_task.done():
                    break
        self.close_task.result()
        if cancelled:
            raise cancelled

    async def _close(self):
        if self.server:
            self.server.close()
        # Python 3.12+ 的 wait_closed() 会阻塞等待已建立的客户端连接退出；
        # 必须先取消待处理调用与连接协程，再等待 server 完全关闭以避免死锁。
        for call in list(self.pending.values()):
            self.record_outcome(call)
            if not call.future.done():
                call.future.cancel()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        self.transport.token = ""
        if self.directory:
            directory, self.directory = self.directory, None
            await asyncio.to_thread(remove_tree_robust, directory)
