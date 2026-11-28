"""Structured turn cost, rebuild telemetry, and bounded /agy cost stats."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import re
import statistics
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from astrbot.api import logger

from .daily_usage import DailyUsage
from .failure_events import FailureJournal, STOP_REASONS, event_fields, MAX_BYTES, MAX_EVENTS, TTL
from .turn_history import HISTORY_TTL_SECONDS, MAX_HISTORY_TURNS, TurnHistory

MAX_TOMBSTONES = 1024
TOMBSTONE_TTL_SECONDS = 3600

REBUILD_REASON_PRIORITY = (
    "no_identity",
    "abort",
    "previous_error",
    "upstream_compaction_detected",
    "process_dead",
    "idle_expired",
    "pool_evicted",
    "runtime_restart",
    "cold_start",
    "system_changed",
    "model_changed",
    "effort_changed",
    "tool_hash_changed",
    "tool_ledger_limit",
    "history_rewrite",
    "history_host_trim",
    "history_append_gap",
    "unknown",
)

_PRIORITY_RANK = {reason: idx for idx, reason in enumerate(REBUILD_REASON_PRIORITY)}

_VALID_HISTORY_RELATIONS = {
    "exact",
    "host_trim",
    "rewrite",
    "append_gap",
    "unknown",
}

# Closed vocabulary: unknown errors/reasons must never become free-form text.
MONITOR_ERROR_CODES = frozenset("""
AUTH AUTH_BRIDGE BACKPRESSURE BUSY CANCELLED CLOSED CONFIG EMPTY IMAGE_CAPABILITY
INPUT INPUT_LIMIT LAUNCH LOGIN_EXPIRED LOGIN_URL MODEL NOT_INSTALLED NO_MODELS
OUTPUT_LIMIT PERMISSION PROTOCOL RATE_LIMIT QUOTA STARTUP_EXIT STOP TIMEOUT
TOOLS_DISABLED TOOL_ARGUMENTS TOOL_BUDGET TOOL_CANCELLED TOOL_CHOICE
TOOL_CONCURRENCY TOOL_CONTEXT TOOL_DUPLICATE TOOL_HISTORY TOOL_ID TOOL_PERMISSION
TOOL_PROTOCOL TOOL_RESULT TOOL_RESULT_LIMIT TOOL_SCHEMA TOOL_SESSION TOOL_STALE
TOOL_TERMINATED TOOL_TIMEOUT TOOL_UNKNOWN TRANSPORT UNSUPPORTED UPSTREAM
CLI_WRAPPER COMMAND_NOT_FOUND PROXY_AUTH PROXY NETWORK TLS IMAGE IMAGE_BYTES IMAGE_COUNT
IMAGE_DOWNLOAD IMAGE_EOI IMAGE_FORMAT IMAGE_MIME IMAGE_PATH IMAGE_PIXELS IMAGE_SSRF
IMAGE_TIMEOUT IMAGE_URL IMAGES_DISABLED HOST ROUTE_MISMATCH REFERENCE_BLOCKED
""".split())


_journal = None
_daily = None
_turn_history = None
_last_service = (None, None)


def start_diagnostics(root):
    global _journal, _daily, _turn_history, _last_service
    stop_diagnostics()
    _last_service = (None, None)
    _journal = FailureJournal(root, MONITOR_ERROR_CODES)
    _daily = DailyUsage(root)
    _turn_history = TurnHistory(root)


def stop_diagnostics():
    global _journal, _daily, _turn_history
    if _turn_history is not None:
        _turn_history.close()
        _turn_history = None
    if _daily is not None:
        _daily.close()
        _daily = None
    if _journal is not None:
        _journal.close()
        _journal = None


def daily_snapshot(days=90):
    if _daily is None:
        raise OSError("每日统计尚未就绪。")
    return _daily.snapshot(days)


def provider_tag(provider_id):
    return _journal.provider_tag(provider_id) if _journal is not None else None


def failure_snapshot():
    if _journal is not None:
        return _journal.snapshot()
    return {"failure_events": [], "failure_retention": {
        "ttl_seconds": TTL, "max_events": MAX_EVENTS, "max_bytes": MAX_BYTES,
        "storage_state": "unavailable",
    }}


def last_service_observation():
    provider, stamp = _last_service
    return {"last_observed_provider_id": provider, "last_observed_at": stamp,
            "last_observed_scope": "plugin_only" if stamp else "unknown"}


def safe_label(value: object) -> str:
    return value[:256] if isinstance(value, str) else ""


def safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except ValueError:
        return None


def safe_context(source: dict) -> dict:
    used = _valid_nonneg_int(source.get("context_used"))
    size = _valid_nonneg_int(source.get("context_window_size"))
    observed_at = safe_timestamp(source.get("observed_at"))
    if (source.get("context_model_known") is False or used is None or size is None
            or not 0 < used <= size <= 2**53 - 1 or observed_at is None):
        used = size = observed_at = None
    return {"context_used": used, "context_window_size": size, "observed_at": observed_at}


def pick_primary_reason(reasons: list[str]) -> str | None:
    if not reasons:
        return None
    return min(reasons, key=lambda r: _PRIORITY_RANK.get(r, len(_PRIORITY_RANK)))


def _valid_nonneg_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _history_stats(record: dict) -> dict:
    """Keep only the fields needed for statistics. Apply the same projection on read."""
    def choice(key, values, default=None):
        value = record.get(key)
        return value if isinstance(value, str) and value in values else default

    def model_id(key):
        value = record.get(key)
        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.+/-]{0,191}", value):
            return value
        return None

    reasons = record.get("rebuild_reasons")
    cache = record.get("cache_fields_present")
    counts = {}
    for key in ("elapsed_ms", "input_other", "input_cached", "output", "prompt_bytes",
                "replay_bytes", "omitted_messages"):
        value = record.get(key)
        counts[key] = value if type(value) is int and 0 <= value < 2**53 else None
    return {
        "model": model_id("model") or "",
        "negotiated_model": model_id("negotiated_model"),
        "mode": choice("mode", {"chat", "tools"}, "unknown"),
        "status": choice("status", {"ok", "error", "cancelled", "short_circuited"}, "unknown"),
        "error_code": choice("error_code", MONITOR_ERROR_CODES, "UPSTREAM" if record.get("error_code") else None),
        "usage_status": choice("usage_status", {"reported", "unavailable", "not_sent"}, "unavailable"),
        "request_sent": record.get("request_sent") is True,
        "reused": record.get("reused") is True,
        "stable_identity": record.get("stable_identity") is True,
        "upstream_compaction_detected": record.get("upstream_compaction_detected") is True,
        "fast_abort_reason": record.get("fast_abort_reason") is True,
        "rebuild_reasons": [r for r in REBUILD_REASON_PRIORITY if isinstance(reasons, list) and r in reasons],
        "cache_fields_present": {
            key: isinstance(cache, dict) and cache.get(key) is True
            for key in ("cachedReadTokens", "cachedWriteTokens")
        },
        **counts,
    }


def extract_acp_usage(
    payload: object,
) -> tuple[dict[str, int | None] | None, dict[str, bool]]:
    """Extract allowlisted camelCase ACP usage buckets without summing updates."""
    raw = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None, {"cachedReadTokens": False, "cachedWriteTokens": False}

    uncached = _valid_nonneg_int(raw.get("inputTokens"))
    output = _valid_nonneg_int(raw.get("outputTokens"))
    cached_read = _valid_nonneg_int(raw.get("cachedReadTokens"))
    cached_write = _valid_nonneg_int(raw.get("cachedWriteTokens"))
    cache_fields_present = {
        "cachedReadTokens": cached_read is not None,
        "cachedWriteTokens": cached_write is not None,
    }
    if uncached is None or output is None:
        return None, cache_fields_present
    return (
        {
            "input_other": uncached + (cached_write or 0),
            "input_cached": cached_read,
            "output": output,
        },
        cache_fields_present,
    )


def _emit_json_line(line: str) -> None:
    logger.info(line)


@dataclass
class _Tombstone:
    reason: str
    created_at: float


class TurnMeter:
    """Idempotent single-turn telemetry meter shared across tool continuations."""

    def __init__(
        self,
        tracker: TelemetryTracker,
        *,
        provider: str,
        session_key: str | None,
        mode: str,
        model: str,
        effort: str,
        system_bytes: int = 0,
        transient_bytes: int = 0,
        tool_schema_bytes: int = 0,
        turn_id: str | None = None,
        origin_turn_id: str | None = None,
        estimate_method: str = "none",
        system_hash8: str = "",
    ):
        self._tracker = tracker
        self._clock = tracker.clock
        self._started_at = self._clock()
        self._finished = False
        self._usage: dict[str, int | None] | None = None
        self._usage_source: str | None = None
        # Content-429 candidate observability (docs/429 doc P1): heuristic
        # only, never a confirmed attribution; no system text is stored.
        self._system_hash8 = system_hash8[:8]
        self._detail_free_exhausted = False
        self._retry_attempt: str | None = None
        self._has_output = False
        self.negotiated_model = None
        self.logical_request_tag = None
        self.chunk_seen = False
        self.body_seen = False
        self.body_streamed = False
        self.tool_handed_off = False
        self.tool_result_supplied = False
        self.body_bytes = 0
        self.thought_bytes = 0
        self.permission_denial_count = None
        self.filtered_notice_count = None
        self.stop_reason = None
        self._cache_fields_present: dict[str, bool] = {
            "cachedReadTokens": False,
            "cachedWriteTokens": False,
        }

        self.turn_id: str = turn_id or uuid.uuid4().hex[:16]
        self.origin_turn_id: str | None = origin_turn_id
        self.provider: str = str(provider or "")
        self.session_key: str | None = session_key
        self.mode: str = "tools" if mode == "tools" else "chat"
        self.model: str = str(model or "")
        self.effort: str = str(effort or "")
        self.native_session_tag: str | None = None
        self.reused: bool = False
        self.rebuild_reasons: list[str] = []
        self.primary_reason: str | None = None
        self.history_relation: str = "unknown"
        self.prompt_bytes: int = 0
        self.replay_bytes: int = 0
        self.system_bytes: int = max(0, int(system_bytes))
        self.transient_bytes: int = max(0, int(transient_bytes))
        self.tool_schema_bytes: int = max(0, int(tool_schema_bytes))
        self.omitted_messages: int = 0
        self.estimate_method: str = estimate_method
        self.no_fallback = False
        self.builtin_used = False
        self.builtin_observed = False
        self.tool_calls: int = 0
        self.request_sent: bool = False
        self.rpc_stage = "idle"
        self.fast_abort_reason: str | None = None
        self.context = {
            "context_used": None, "context_window_size": None,
            "peak_context_used": None, "upstream_compaction_detected": False,
        }

    def observe_client(self, client) -> None:
        if client is None:
            return
        self.request_sent = self.request_sent or getattr(client, "prompt_sent", False)
        stage = getattr(client, "stage", "idle")
        self.rpc_stage = stage if stage in {
            "idle", "initialize", "session/new", "session/set_config_option", "session/prompt", "rpc"
        } else "rpc"
        context = getattr(client, "context_usage", None)
        if context is not None:
            self.context = context.summary()
        guard = getattr(client, "text_guard", None)
        retries = getattr(guard, "retries", None)
        code = getattr(retries, "fast_fail_code", "")
        self.fast_abort_reason = code if code in {"QUOTA", "RATE_LIMIT", "AUTH"} else None
        if getattr(client, "prompt_sent", False):
            self.permission_denial_count = _valid_nonneg_int(getattr(client, "permission_denial_count", None))
            self.filtered_notice_count = _valid_nonneg_int(getattr(guard, "filtered_count", None))
            self.stop_reason = getattr(client, "terminal_reason", None)
            self.negotiated_model = (getattr(client, "confirmed_model", None)
                                     if getattr(context, "model_known", False) else None)
        if retries is not None:
            self._detail_free_exhausted = self._detail_free_exhausted or bool(
                getattr(retries, "detail_free_seen", False)
            )
            attempt = getattr(retries, "attempt_number", None)
            if attempt:
                self._retry_attempt = str(attempt)

    def observe_chunk(self, kind, text, *, streaming):
        self.chunk_seen = True
        size = len(text.encode())
        if kind == "thought":
            self.thought_bytes += size
        else:
            self.body_bytes += size
            if text.strip():
                self.body_seen = True
                self.body_streamed |= streaming

    @property
    def replay_blocked(self):
        return self.body_seen or self.tool_handed_off or self.tool_result_supplied or self.builtin_used

    def observe_output(self, *, has_output: bool) -> None:
        """Record whether this turn produced any body text, thought, or tool call."""
        self._has_output = self._has_output or bool(has_output)

    def set_decision(
        self,
        *,
        reused: bool,
        reasons: list[str],
        history_relation: str,
        prompt_bytes: int = 0,
        replay_bytes: int = 0,
        omitted_messages: int = 0,
        native_session_id: str | None = None,
    ) -> None:
        self.reused = bool(reused)
        self.rebuild_reasons = list(reasons)
        self.primary_reason = None if self.reused else pick_primary_reason(self.rebuild_reasons)
        self.history_relation = (
            history_relation
            if history_relation in _VALID_HISTORY_RELATIONS
            else "unknown"
        )
        self.prompt_bytes = max(0, int(prompt_bytes))
        self.replay_bytes = max(0, int(replay_bytes))
        self.omitted_messages = max(0, int(omitted_messages))
        if native_session_id:
            self.native_session_tag = self._tracker.opaque_tag(native_session_id)

    def set_native_session(self, session_id: str | None) -> None:
        self.native_session_tag = (
            self._tracker.opaque_tag(session_id) if session_id else None
        )

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_builtin_call(self, *, observed=False):
        self.builtin_used = True
        self.builtin_observed |= observed
        self.record_tool_call()

    def observe_usage(self, result: dict, *, source: str) -> None:
        buckets, cache_present = extract_acp_usage(result)
        if buckets is None:
            return
        # Terminal/error payloads supersede intermediate updates; never sum.
        if self._usage is not None and self._usage_source == "acp_terminal" and source != "acp_terminal":
            return
        self._usage = buckets
        self._usage_source = source
        self._cache_fields_present = cache_present

    @property
    def finished(self) -> bool:
        return self._finished

    def finish(self, *, status: str, error_code: str | None = None) -> dict | None:
        if self._finished:
            return None
        self._finished = True
        elapsed_ms = max(0, int((self._clock() - self._started_at) * 1000))
        if self._usage is not None:
            input_other: int | None = self._usage["input_other"]
            input_cached: int | None = self._usage["input_cached"]
            output: int | None = self._usage["output"]
            usage_status = "reported"
            usage_source = self._usage_source
        elif not self.request_sent:
            input_other = 0
            input_cached = 0
            output = 0
            usage_status = "not_sent"
            usage_source = None
        else:
            input_other = None
            input_cached = None
            output = None
            usage_status = "unavailable"
            usage_source = None

        fallback_reason = None
        if status in {"error", "short_circuited"}:
            if self.builtin_observed:
                fallback_reason = "builtin_observed"
            elif self.builtin_used:
                fallback_reason = "builtin_grant"
            elif self.tool_handed_off:
                fallback_reason = "tool_handoff"
            elif self.tool_result_supplied:
                fallback_reason = "tool_continuation"
            elif self.body_seen:
                fallback_reason = "visible_prefix"
            elif self.no_fallback:
                fallback_reason = "policy_denial"
            else:
                fallback_reason = "no_visible_prefix"
        record = {
            "logical_request_tag": self.logical_request_tag,
            "negotiated_model": self.negotiated_model,
            "cli_retry_attempt": int(self._retry_attempt) if self._retry_attempt
            and re.fullmatch("[0-9]{1,9}", self._retry_attempt) else None,
            "chunk_seen": self.chunk_seen, "body_seen": self.body_seen,
            "body_streamed": self.body_streamed, "tool_handed_off": self.tool_handed_off,
            "tool_result_supplied": self.tool_result_supplied,
            "body_bytes": self.body_bytes, "thought_bytes": self.thought_bytes,
            "stop_reason": self.stop_reason if self.stop_reason in STOP_REASONS else None,
            "permission_denial_count": self.permission_denial_count,
            "filtered_notice_count": self.filtered_notice_count,
            "fallback_allowed": fallback_reason == "no_visible_prefix" if fallback_reason else None,
            "fallback_reason": fallback_reason,
            "event": "agy.turn",
            "v": 1,
            "provider": self.provider,
            "turn_id": self.turn_id,
            "origin_turn_id": self.origin_turn_id,
            "session_key": self.session_key,
            "mode": self.mode,
            "model": self.model,
            "effort": self.effort,
            "native_session_tag": self.native_session_tag,
            "reused": self.reused,
            "rebuild_reasons": list(self.rebuild_reasons),
            "primary_reason": self.primary_reason,
            "history_relation": self.history_relation,
            "prompt_bytes": self.prompt_bytes,
            "replay_bytes": self.replay_bytes,
            "system_bytes": self.system_bytes,
            "transient_bytes": self.transient_bytes,
            "tool_schema_bytes": self.tool_schema_bytes,
            "omitted_messages": self.omitted_messages,
            "input_other": input_other,
            "input_cached": input_cached,
            "output": output,
            "usage_status": usage_status,
            "usage_source": usage_source,
            "cache_fields_present": dict(self._cache_fields_present),
            "estimate_method": self.estimate_method,
            "tool_calls": self.tool_calls,
            "request_sent": self.request_sent,
            "rpc_stage": self.rpc_stage,
            "fast_abort_reason": self.fast_abort_reason,
            **self.context,
            "elapsed_ms": elapsed_ms,
            "status": status,
            "error_code": error_code if isinstance(error_code, str) and error_code else None,
        }
        if (
            self._detail_free_exhausted
            and isinstance(error_code, str)
            and error_code in {"QUOTA", "RATE_LIMIT"}
        ):
            # Heuristic candidate only: the same detail-free shape also matches
            # genuine quota exhaustion, and real sustained throttling can hit
            # the same signature with no output. CLI summary, not raw HTTP body.
            record["content_429_candidate"] = {
                "system_hash8": self._system_hash8 or None,
                "system_bytes": self.system_bytes,
                "has_output": self._has_output,
                "attempt": self._retry_attempt,
            }
        self._tracker.record_turn(record)
        return record


class TelemetryTracker:
    """Per-runtime bounded history, HMAC anonymizer, and rebuild tombstones."""

    def __init__(
        self,
        *,
        cost_log_enabled: bool = False,
        clock: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
    ):
        self.cost_log_enabled = bool(cost_log_enabled)
        self.clock = clock
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self.history: deque[dict] = deque(maxlen=MAX_HISTORY_TURNS)
        self._history_times: deque[float] = deque(maxlen=MAX_HISTORY_TURNS)
        self.tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self.evictions: dict[str, int] = {}
        self.emitted_lines: deque[str] = deque(maxlen=MAX_HISTORY_TURNS)
        self._emitted_times: deque[float] = deque(maxlen=MAX_HISTORY_TURNS)
        self._history_store = None
        self._history_tag = None

    def restore_history(self, tag: str | None) -> None:
        if tag is None or _turn_history is None:
            return
        self._history_store = _turn_history
        self._history_tag = tag
        now = time.time()
        monotonic_now = self.clock()
        for stamp, stats in self._history_store.load(tag, now):
            if isinstance(stats, dict):
                self.history.append({
                    **_history_stats(stats), "historical": True,
                    "recorded_at": datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                })
                self._history_times.append(monotonic_now - (now - stamp))

    @property
    def history_storage(self) -> str:
        return "available" if self._history_store is not None and self._history_store.db is not None else "memory_only"

    def opaque_tag(self, value: str | None) -> str | None:
        if not value:
            return None
        return hmac.new(
            self._secret, str(value).encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

    def start_turn(
        self,
        *,
        provider: str,
        session_key_raw: str | None,
        mode: str,
        model: str,
        effort: str,
        system_bytes: int = 0,
        transient_bytes: int = 0,
        tool_schema_bytes: int = 0,
        origin_turn_id: str | None = None,
        system_hash8: str = "",
    ) -> TurnMeter:
        return TurnMeter(
            self,
            provider=provider,
            session_key=self.opaque_tag(session_key_raw) if session_key_raw else None,
            mode=mode,
            model=model,
            effort=effort,
            system_bytes=system_bytes,
            transient_bytes=transient_bytes,
            tool_schema_bytes=tool_schema_bytes,
            origin_turn_id=origin_turn_id,
            system_hash8=system_hash8,
        )

    def record_turn(self, record: dict) -> None:
        self._prune_history()
        stamp = time.time()
        record["recorded_at"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        if _daily is not None:
            _daily.record(record)
        global _last_service
        if record.get("status") == "ok" and record.get("request_sent") and record.get("provider"):
            _last_service = (safe_label(record["provider"]), record["recorded_at"])
        elif _journal is not None and record.get("status") != "ok":
            _journal.append(record)
        self.history.append(record)
        self._history_times.append(self.clock())
        sample = record.get("turn_id")
        if (self.history_storage == "available" and isinstance(sample, str)
                and re.fullmatch(r"[0-9a-f]{16}", sample)):
            stats = _history_stats({
                **record, "stable_identity": record.get("session_key") is not None,
                "fast_abort_reason": bool(record.get("fast_abort_reason")),
            })
            anonymous_sample = hmac.digest(bytes.fromhex(self._history_tag), sample.encode(), "sha256").hex()[:32]
            self._history_store.record(self._history_tag, anonymous_sample, stamp, stats)
        if self.cost_log_enabled:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            self.emitted_lines.append(line)
            self._emitted_times.append(self.clock())
            _emit_json_line(line)

    def _safe_turns(self, limit: int, *, max_limit: int, now: float | None = None) -> list[dict]:
        """最新记录在前；严格白名单裁剪并按时间窗口过滤，绝不暴露原始历史。"""
        self._prune_history(now)
        bounded = min(max_limit, max(0, limit)) if type(limit) is int else max_limit
        rows = []
        for record in list(reversed(self.history))[:bounded]:
            row = {
                "recorded_at": safe_timestamp(record.get("recorded_at")),
                "model": safe_label(record.get("model")),
                "mode": record.get("mode") if record.get("mode") in {"chat", "tools"} else "unknown",
                "status": record.get("status") if record.get("status") in {"ok", "error", "cancelled", "short_circuited"} else "unknown",
                "error_code": (record.get("error_code") if record.get("error_code") in MONITOR_ERROR_CODES
                               else "UPSTREAM" if record.get("error_code") else None),
                "usage_status": record.get("usage_status") if record.get("usage_status") in {"reported", "unavailable", "not_sent"} else "unavailable",
                "reused": record.get("reused") is True,
                "rebuild_reasons": [r for r in REBUILD_REASON_PRIORITY if r in record.get("rebuild_reasons", [])],
                "cache_fields_present": {
                    key: record.get("cache_fields_present", {}).get(key) is True
                    for key in ("cachedReadTokens", "cachedWriteTokens")
                },
                **safe_context(record), **event_fields(record),
                "negotiated_model": safe_label(record.get("negotiated_model")) or None,
            }
            if record.get("historical"):
                row["historical"] = True
            for key in ("elapsed_ms", "input_other", "input_cached", "output", "prompt_bytes", "replay_bytes", "omitted_messages"):
                row[key] = _valid_nonneg_int(record.get(key))
            candidate = record.get("content_429_candidate")
            row["content_429_candidate"] = None
            if isinstance(candidate, dict):
                hash8 = candidate.get("system_hash8")
                attempt = candidate.get("attempt")
                row["content_429_candidate"] = {
                    "system_hash8": hash8 if isinstance(hash8, str) and re.fullmatch(r"[0-9a-f]{8}", hash8) else None,
                    "system_bytes": _valid_nonneg_int(candidate.get("system_bytes")),
                    "has_output": candidate.get("has_output") is True,
                    "attempt": int(attempt) if isinstance(attempt, (str, int)) and re.fullmatch(r"[0-9]{1,9}", str(attempt)) else None,
                }
            rows.append(row)
        return rows

    def safe_recent_turns(self, limit: int = 20, *, now: float | None = None) -> list[dict]:
        """Newest first; fresh, bounded, recursive allowlist, never raw history."""
        return self._safe_turns(limit, max_limit=20, now=now)

    def safe_turn_history(self, limit: int = MAX_HISTORY_TURNS, *, now: float | None = None) -> list[dict]:
        """最新记录在前；返回近 24 小时窗口内最多 100 条脱敏历史记录，供按需详情视图读取。"""
        return self._safe_turns(limit, max_limit=MAX_HISTORY_TURNS, now=now)

    def monitor_summary(self, *, now: float | None = None) -> dict:
        """Use /agy cost denominators, preserving unknown token buckets as null."""
        stats = self.summarize(now=now)
        keys = ("total_turns", "request_sent_turns", "reuse_rate", "cache_token_ratio",
                "unavailable_usage_turns", "reported_usage_turns", "missing_cache_field_turns",
                "total_input_other", "total_input_cached", "total_output")
        result = {key: stats[key] for key in keys}
        result["success_turns"] = sum(t["status"] == "ok" for t in self.history)
        result["failed_turns"] = stats["total_turns"] - result["success_turns"]
        result["rebuild_reasons"] = [[r, n] for r, n in stats["rebuild_reasons"] if r in REBUILD_REASON_PRIORITY]
        if not stats["reported_usage_turns"]:
            result["total_input_other"] = result["total_output"] = None
        if stats["reported_usage_turns"] == stats["missing_cache_field_turns"]:
            result["total_input_cached"] = None
        return result

    def _prune_history(self, now: float | None = None) -> None:
        cutoff = (self.clock() if now is None else now) - HISTORY_TTL_SECONDS
        for times, records in ((self._history_times, self.history),
                               (self._emitted_times, self.emitted_lines)):
            while times and times[0] <= cutoff:
                times.popleft()
                records.popleft()

    def _prune_tombstones(self, now: float) -> None:
        expired = [
            key
            for key, item in self.tombstones.items()
            if now - item.created_at >= TOMBSTONE_TTL_SECONDS
        ]
        for key in expired:
            del self.tombstones[key]
        while len(self.tombstones) >= MAX_TOMBSTONES:
            self.tombstones.popitem(last=False)

    def record_eviction(self, key: str | None, reason: str) -> None:
        self.evictions[reason] = self.evictions.get(reason, 0) + 1
        if not key:
            return
        now = self.clock()
        self._prune_tombstones(now)
        self.tombstones[key] = _Tombstone(reason=reason, created_at=now)
        self.tombstones.move_to_end(key)

    def consume_tombstone(self, key: str | None) -> str | None:
        if not key:
            return None
        now = self.clock()
        item = self.tombstones.pop(key, None)
        if item is None:
            return None
        if now - item.created_at >= TOMBSTONE_TTL_SECONDS:
            return None
        return item.reason

    def peek_tombstone(self, key: str | None) -> str | None:
        if not key:
            return None
        now = self.clock()
        item = self.tombstones.get(key)
        if item is None:
            return None
        if now - item.created_at >= TOMBSTONE_TTL_SECONDS:
            del self.tombstones[key]
            return None
        return item.reason

    def summarize(self, *, now: float | None = None) -> dict:
        self._prune_history(now)
        turns = list(self.history)
        total = len(turns)
        sent_turns = [t for t in turns if t["request_sent"]]
        short_circuited = total - len(sent_turns)
        reused = [t for t in sent_turns if t["reused"]]

        # Eligible reuse candidates: subsequent user turns with stable identity
        # whose previous rebuild wasn't cold_start or no_identity.
        candidates = [
            t
            for t in sent_turns
            if t.get("stable_identity", t.get("session_key") is not None)
            and "cold_start" not in t["rebuild_reasons"]
            and "no_identity" not in t["rebuild_reasons"]
        ]

        reported = [t for t in sent_turns if t["usage_status"] == "reported"]
        unavailable = [t for t in sent_turns if t["usage_status"] == "unavailable"]
        cache_checked = [
            t
            for t in reported
            if t["cache_fields_present"].get("cachedReadTokens", False)
        ]
        missing_cache_fields = len(reported) - len(cache_checked)

        total_input_other = sum(t["input_other"] or 0 for t in reported)
        total_input_cached = sum(t["input_cached"] or 0 for t in reported)
        total_output = sum(t["output"] or 0 for t in reported)
        total_input = total_input_other + total_input_cached

        checked_other = sum(t["input_other"] or 0 for t in cache_checked)
        checked_cached = sum(t["input_cached"] or 0 for t in cache_checked)
        checked_total = checked_other + checked_cached

        failed_reported = [t for t in reported if t["status"] != "ok"]
        failed_other = sum(t["input_other"] or 0 for t in failed_reported)
        failed_cached = sum(t["input_cached"] or 0 for t in failed_reported)
        failed_output = sum(t["output"] or 0 for t in failed_reported)
        known_input_other = [t["input_other"] for t in reported if t["input_other"] is not None]

        reason_counts: dict[str, int] = {}
        for t in turns:
            for reason in t["rebuild_reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        ranked_reasons = sorted(
            reason_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )

        groups = {}
        for turn in turns:
            key = (turn["mode"], turn["model"], turn["status"],
                   "not_sent" if not turn["request_sent"] else "warm" if turn["reused"] else "cold")
            group = groups.setdefault(key, {
                "mode": key[0], "model": key[1], "status": key[2], "session": key[3],
                "turns": 0, "reported": 0, "unknown": 0, "cached_read_unknown": 0,
                "input_other": 0, "input_cached": 0, "output": 0,
            })
            group["turns"] += 1
            if turn["usage_status"] == "reported":
                group["reported"] += 1
                group["cached_read_unknown"] += not turn["cache_fields_present"]["cachedReadTokens"]
                for bucket in ("input_other", "input_cached", "output"):
                    group[bucket] += turn[bucket] or 0
            elif turn["request_sent"]:
                group["unknown"] += 1

        return {
            "groups": list(groups.values()),
            "compaction_turns": sum(bool(t.get("upstream_compaction_detected")) for t in turns),
            "fast_abort_turns": sum(bool(t.get("fast_abort_reason")) for t in turns),
            "total_turns": total,
            "request_sent_turns": len(sent_turns),
            "short_circuited_turns": short_circuited,
            "reused_turns": len(reused),
            "reuse_rate": (len(reused) / len(sent_turns)) if sent_turns else None,
            "candidate_turns": len(candidates),
            "candidate_reuse_rate": (
                (len(reused) / len(candidates)) if candidates else None
            ),
            "reported_usage_turns": len(reported),
            "unavailable_usage_turns": len(unavailable),
            "unavailable_ratio": (
                (len(unavailable) / len(sent_turns)) if sent_turns else None
            ),
            "missing_cache_field_turns": missing_cache_fields,
            "total_input_other": total_input_other,
            "total_input_cached": total_input_cached,
            "total_input": total_input,
            "total_output": total_output,
            "cache_token_ratio": (
                (checked_cached / checked_total) if checked_total > 0 else None
            ),
            "median_input_other": (
                int(statistics.median(known_input_other))
                if known_input_other
                else None
            ),
            "median_total_input": (
                int(
                    statistics.median(
                        (t["input_other"] or 0) + (t["input_cached"] or 0)
                        for t in reported
                    )
                )
                if reported
                else None
            ),
            "failed_reported_turns": len(failed_reported),
            "failed_input_other": failed_other,
            "failed_input_cached": failed_cached,
            "failed_output": failed_output,
            "rebuild_reasons": ranked_reasons,
            "evictions": dict(self.evictions),
        }


_MODE_LABELS = {
    "chat": "纯聊天",
    "tools": "工具模式",
    "inherit": "跟随总闸",
}

_REASON_LABELS = {
    "no_identity": "无会话标识",
    "abort": "请求中断",
    "previous_error": "上轮异常",
    "upstream_compaction_detected": "疑似上下文压缩",
    "process_dead": "子进程已退出",
    "idle_expired": "空闲超时回收",
    "pool_evicted": "会话池淘汰",
    "runtime_restart": "实例重启",
    "cold_start": "冷启动",
    "system_changed": "系统提示变更",
    "model_changed": "底层模型变更",
    "effort_changed": "思考档位变更",
    "tool_hash_changed": "工具列表变更",
    "tool_ledger_limit": "工具账本上限",
    "history_rewrite": "历史消息重写",
    "history_host_trim": "宿主裁剪历史",
    "history_append_gap": "历史断档",
    "unknown": "其他原因",
}


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "样本不足"


def _fmt_num(value: int | None) -> str:
    return f"{value:,}" if isinstance(value, int) else "未知"


def format_cost_report(instances: list[dict], settings_summary: dict) -> str:
    """生成只读 /agy cost 摘要文本，保持精简且人类可读。"""
    reuse_mode = settings_summary.get("session_reuse_mode", "strict")
    history_kb = max(1, round(int(settings_summary.get("history_bytes", 160000)) / 1024))
    tools_on = "开启" if settings_summary.get("enable_tools", False) else "关闭"
    lines = [
        "AGY 成本与复用（近 24h · 只读，未发送模型请求）",
        f"策略：复用 {reuse_mode} · 历史上限 {history_kb}KB · 工具总闸 {tools_on}",
    ]
    if not instances:
        lines.append("当前没有已加载的 AGY 实例，暂无统计样本。")
        return "\n".join(lines)

    for inst in instances:
        summary = inst["summary"]
        pool = inst.get("pool") or {}
        mode_cn = _MODE_LABELS.get(inst.get("effective_mode") or inst.get("tools_mode", "inherit"), "未知")
        model = inst.get("model") or "(未指定)"
        lines.append("")
        lines.append(f"【{model}】({inst['provider_id']}) · {mode_cn}")
        active_sess = pool.get("active_sessions", pool.get("active", 0))
        max_sess = pool.get("max_sessions", pool.get("capacity", 0))
        live_proc = pool.get("live_processes", 0)
        tool_turns = pool.get("active_tool_turns", 0)
        pool_line = f"进程池：{active_sess}/{max_sess} 会话 · {live_proc} 进程"
        if tool_turns:
            pool_line += f" · {tool_turns} 工具续轮"
        lines.append(pool_line)

        if summary["total_turns"] == 0:
            lines.append("请求：暂无样本")
            continue

        req_line = f"请求：{summary['total_turns']} 轮（实发 {summary['request_sent_turns']}"
        if summary["short_circuited_turns"]:
            req_line += f"，拦截 {summary['short_circuited_turns']}"
        req_line += "）"
        lines.append(req_line)

        reuse_line = (
            f"复用：全量 {_pct(summary['reuse_rate'])} ({summary['reused_turns']}/{summary['request_sent_turns']})"
        )
        if summary["candidate_turns"] != summary["request_sent_turns"]:
            reuse_line += (
                f" · 候选 {_pct(summary['candidate_reuse_rate'])} "
                f"({summary['reused_turns']}/{summary['candidate_turns']})"
            )
        lines.append(reuse_line)

        if summary["reported_usage_turns"] == 0:
            lines.append(
                f"Token：暂无上报（未知 {summary['unavailable_usage_turns']} 轮，"
                f"{_pct(summary['unavailable_ratio'])}）"
            )
        else:
            lines.append(
                f"Token：输入 {_fmt_num(summary['total_input_other'])} · "
                f"缓存 {_fmt_num(summary['total_input_cached'])} · "
                f"输出 {_fmt_num(summary['total_output'])}"
            )
            cache_line = (
                f"缓存占比：{_pct(summary['cache_token_ratio'])} · "
                f"中位输入 {_fmt_num(summary['median_total_input'])}"
            )
            if summary["missing_cache_field_turns"]:
                cache_line += f" · 缺缓存字段 {summary['missing_cache_field_turns']} 轮"
            if summary["unavailable_usage_turns"]:
                cache_line += (
                    f" · 未知 {summary['unavailable_usage_turns']}/{summary['request_sent_turns']} "
                    f"({_pct(summary['unavailable_ratio'])})"
                )
            lines.append(cache_line)

        if summary["failed_reported_turns"] > 0:
            lines.append(
                f"失败消耗（{summary['failed_reported_turns']} 轮）："
                f"输入 {_fmt_num(summary['failed_input_other'])} · "
                f"缓存 {_fmt_num(summary['failed_input_cached'])} · "
                f"输出 {_fmt_num(summary['failed_output'])}"
            )
        if summary["rebuild_reasons"]:
            ranked = " · ".join(
                f"{_REASON_LABELS.get(reason, reason)} {count}"
                for reason, count in summary["rebuild_reasons"][:4]
            )
            lines.append(f"重建原因：{ranked}")
        if summary["evictions"]:
            ev = " · ".join(
                f"{_REASON_LABELS.get(reason, reason)} {count}"
                for reason, count in sorted(summary["evictions"].items())
            )
            lines.append(f"会话回收：{ev}")
        if summary.get("compaction_turns") or summary.get("fast_abort_turns"):
            lines.append(
                f"异常保护：疑似压缩 {summary.get('compaction_turns', 0)} · "
                f"快速中断 {summary.get('fast_abort_turns', 0)}"
            )
    return "\n".join(lines)
