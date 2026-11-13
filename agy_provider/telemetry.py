"""Structured turn cost/rebuild telemetry and bounded in-memory /agy cost stats."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import statistics
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from astrbot.api import logger

MAX_HISTORY_TURNS = 100
MAX_TOMBSTONES = 1024
TOMBSTONE_TTL_SECONDS = 3600

REBUILD_REASON_PRIORITY = (
    "no_identity",
    "abort",
    "previous_error",
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


def pick_primary_reason(reasons: list[str]) -> str | None:
    if not reasons:
        return None
    return min(reasons, key=lambda r: _PRIORITY_RANK.get(r, len(_PRIORITY_RANK)))


def _valid_nonneg_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def extract_acp_usage(
    payload: object,
) -> tuple[dict[str, int] | None, dict[str, bool]]:
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
            "input_cached": cached_read or 0,
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
    ):
        self._tracker = tracker
        self._clock = tracker.clock
        self._started_at = self._clock()
        self._finished = False
        self._usage: dict[str, int] | None = None
        self._usage_source: str | None = None
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
        self.tool_calls: int = 0
        self.request_sent: bool = False

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

        record = {
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
            "elapsed_ms": elapsed_ms,
            "status": status,
            "error_code": error_code if isinstance(error_code, str) and error_code else None,
        }
        self._tracker.record_turn(record)
        return record


class TelemetryTracker:
    """Per-runtime bounded history, HMAC anonymizer, and rebuild tombstones."""

    def __init__(
        self,
        *,
        cost_log_enabled: bool = True,
        clock: Callable[[], float] = time.monotonic,
        secret: bytes | None = None,
    ):
        self.cost_log_enabled = bool(cost_log_enabled)
        self.clock = clock
        self._secret = secret if secret is not None else secrets.token_bytes(32)
        self.history: deque[dict] = deque(maxlen=MAX_HISTORY_TURNS)
        self.tombstones: OrderedDict[str, _Tombstone] = OrderedDict()
        self.evictions: dict[str, int] = {}
        self.emitted_lines: deque[str] = deque(maxlen=MAX_HISTORY_TURNS)

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
        )

    def record_turn(self, record: dict) -> None:
        self.history.append(record)
        if self.cost_log_enabled:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            self.emitted_lines.append(line)
            _emit_json_line(line)

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

    def summarize(self) -> dict:
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
            if t["session_key"] is not None
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

        reason_counts: dict[str, int] = {}
        for t in turns:
            for reason in t["rebuild_reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        ranked_reasons = sorted(
            reason_counts.items(), key=lambda pair: (-pair[1], pair[0])
        )

        return {
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
                int(statistics.median(t["input_other"] for t in reported))
                if reported
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


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "样本不足"


def format_cost_report(instances: list[dict], settings_summary: dict) -> str:
    """Format a read-only /agy cost report across active provider instances."""
    lines = [
        "AGY 成本与会话复用统计（只读，未发送模型请求；每实例保留最近 100 轮）",
        "全局有效配置："
        f"reuse={settings_summary.get('session_reuse_mode', 'strict')} · "
        f"history_bytes={settings_summary.get('history_bytes', 160000)} · "
        f"enable_tools={settings_summary.get('enable_tools', False)} · "
        f"cost_log={settings_summary.get('cost_log_enabled', True)} · "
        f"empty_ttl={settings_summary.get('empty_dedupe_ttl', 30)}s · "
        f"quota_cooldown={settings_summary.get('quota_cooldown', 60)}s",
    ]
    if not instances:
        lines.append("当前没有已加载的 AGY provider 实例，尚无本轮统计样本。")
        return "\n".join(lines)

    for inst in instances:
        summary = inst["summary"]
        pool = inst["pool"]
        lines.append("")
        lines.append(
            f"[{inst['provider_id']}] model={inst.get('model') or '(未指定)'} · "
            f"tools_mode={inst.get('tools_mode', 'inherit')} (effective={inst.get('effective_mode', 'chat')})"
        )
        lines.append(
            f"  进程池：保留会话 {pool.get('active_sessions', 0)}/{pool.get('max_sessions', 0)} · "
            f"存活子进程 {pool.get('live_processes', 0)} · "
            f"活跃工具轮 {pool.get('active_tool_turns', 0)}"
        )
        if summary["total_turns"] == 0:
            lines.append("  请求样本：0 轮（启动以来样本不足）")
            continue
        lines.append(
            f"  请求数：总 {summary['total_turns']}（实际发送 {summary['request_sent_turns']}，"
            f"熔断/预检拦截 {summary['short_circuited_turns']}）"
        )
        lines.append(
            f"  复用率：全量 {_pct(summary['reuse_rate'])} ({summary['reused_turns']}/{summary['request_sent_turns']}) · "
            f"可复用候选 {_pct(summary['candidate_reuse_rate'])} ({summary['reused_turns']}/{summary['candidate_turns']})"
        )
        if summary["reported_usage_turns"] == 0:
            lines.append(
                f"  Token/缓存：尚无已上报 usage（未知 usage {summary['unavailable_usage_turns']} 轮，"
                f"占比 {_pct(summary['unavailable_ratio'])}；未知不按 0 计）"
            )
        else:
            lines.append(
                f"  Token 汇总（已上报 {summary['reported_usage_turns']} 轮）："
                f"input_other={summary['total_input_other']} · "
                f"input_cached={summary['total_input_cached']} · "
                f"output={summary['total_output']}"
            )
            lines.append(
                f"  缓存与中位数：缓存占比 {_pct(summary['cache_token_ratio'])} · "
                f"input_other 中位数={summary['median_input_other']} · "
                f"total_input 中位数={summary['median_total_input']} · "
                f"缺 cached 字段 {summary['missing_cache_field_turns']} 轮"
            )
            lines.append(
                f"  未知 usage：{summary['unavailable_usage_turns']}/{summary['request_sent_turns']} "
                f"({_pct(summary['unavailable_ratio'])})"
            )
        if summary["failed_reported_turns"] > 0:
            lines.append(
                f"  失败消耗（有 usage {summary['failed_reported_turns']} 轮）："
                f"input_other={summary['failed_input_other']} · "
                f"input_cached={summary['failed_input_cached']} · "
                f"output={summary['failed_output']}"
            )
        if summary["rebuild_reasons"]:
            ranked = ", ".join(
                f"{reason}={count}" for reason, count in summary["rebuild_reasons"][:6]
            )
            lines.append(f"  重建原因排行：{ranked}")
        if summary["evictions"]:
            ev = ", ".join(
                f"{reason}={count}"
                for reason, count in sorted(summary["evictions"].items())
            )
            lines.append(f"  会话回收次数：{ev}")
    return "\n".join(lines)
