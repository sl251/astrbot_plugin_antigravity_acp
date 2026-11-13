"""Failure deduplication (EMPTY) and short-term circuit breaker (QUOTA)."""

from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

MAX_GUARD_ENTRIES = 1024
MAX_QUOTA_COOLDOWN = 300


@dataclass
class FailureCheckResult:
    allowed: bool
    code: str | None = None
    origin_turn_id: str | None = None
    retry_after: int | None = None
    half_open_scope: tuple[str, str] | None = None


@dataclass
class _QuotaEntry:
    expires_at: float
    origin_turn_id: str
    probe_in_flight: bool = False


class FailureGuard:
    """Cache only failure markers (never success answers) with bounded TTL maps."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_GUARD_ENTRIES,
    ):
        self.clock = clock
        self.max_entries = max(1, int(max_entries))
        self._lock = asyncio.Lock()
        self._empty: OrderedDict[tuple, tuple[float, str]] = OrderedDict()
        self._quota: OrderedDict[tuple[str, str], _QuotaEntry] = OrderedDict()

    def _prune(self, now: float) -> None:
        expired_empty = [k for k, (exp, _) in self._empty.items() if now >= exp]
        for k in expired_empty:
            del self._empty[k]
        # Keep expired quota entries only while a half-open probe is in flight,
        # otherwise stale entries can be pruned once past cooldown and idle.
        while len(self._empty) > self.max_entries:
            self._empty.popitem(last=False)
        while len(self._quota) > self.max_entries:
            victim = next(
                (k for k, entry in self._quota.items() if not entry.probe_in_flight),
                None,
            )
            if victim is None:
                self._quota.popitem(last=False)
            else:
                del self._quota[victim]

    @staticmethod
    def _empty_key(
        provider_id: str,
        request_fingerprint: str | None,
        logical_request_id: str | None,
        session_key: str | None,
    ) -> tuple | None:
        if isinstance(logical_request_id, str) and logical_request_id:
            return (provider_id, "req", logical_request_id)
        if session_key and request_fingerprint:
            return (provider_id, "sess", session_key, request_fingerprint)
        # Never deduplicate across anonymous users lacking both session and request ID.
        return None

    async def check(
        self,
        scope: tuple[str, str],
        request_fingerprint: str | None,
        logical_request_id: str | None = None,
        *,
        session_key: str | None = None,
        empty_ttl: int = 30,
        quota_ttl: int = 60,
        continuation: bool = False,
    ) -> FailureCheckResult:
        if continuation:
            return FailureCheckResult(allowed=True)
        provider_id = scope[0] if scope else ""
        async with self._lock:
            now = self.clock()
            self._prune(now)

            claimed_scope: tuple[str, str] | None = None
            if quota_ttl > 0:
                entry = self._quota.get(scope)
                if entry is not None:
                    if now < entry.expires_at:
                        remaining = max(1, math.ceil(entry.expires_at - now))
                        return FailureCheckResult(
                            allowed=False,
                            code="QUOTA",
                            origin_turn_id=entry.origin_turn_id,
                            retry_after=remaining,
                        )
                    if entry.probe_in_flight:
                        return FailureCheckResult(
                            allowed=False,
                            code="QUOTA",
                            origin_turn_id=entry.origin_turn_id,
                            retry_after=1,
                        )
                    entry.probe_in_flight = True
                    claimed_scope = scope

            if empty_ttl > 0:
                ekey = self._empty_key(
                    provider_id, request_fingerprint, logical_request_id, session_key
                )
                if ekey is not None and ekey in self._empty:
                    expires_at, origin_turn_id = self._empty[ekey]
                    if now < expires_at:
                        if claimed_scope is not None:
                            self._quota[claimed_scope].probe_in_flight = False
                        return FailureCheckResult(
                            allowed=False,
                            code="EMPTY",
                            origin_turn_id=origin_turn_id,
                            retry_after=max(1, math.ceil(expires_at - now)),
                        )
                    del self._empty[ekey]

            return FailureCheckResult(allowed=True, half_open_scope=claimed_scope)

    async def check_empty_only(
        self,
        scope: tuple[str, str],
        request_fingerprint: str | None,
        logical_request_id: str | None = None,
        *,
        session_key: str | None = None,
        empty_ttl: int = 30,
        continuation: bool = False,
    ) -> FailureCheckResult:
        if continuation or empty_ttl <= 0:
            return FailureCheckResult(allowed=True)
        provider_id = scope[0] if scope else ""
        async with self._lock:
            now = self.clock()
            self._prune(now)
            ekey = self._empty_key(
                provider_id, request_fingerprint, logical_request_id, session_key
            )
            if ekey is not None and ekey in self._empty:
                expires_at, origin_turn_id = self._empty[ekey]
                if now < expires_at:
                    return FailureCheckResult(
                        allowed=False,
                        code="EMPTY",
                        origin_turn_id=origin_turn_id,
                        retry_after=max(1, math.ceil(expires_at - now)),
                    )
                del self._empty[ekey]
            return FailureCheckResult(allowed=True)

    def record_success(self, scope: tuple[str, str]) -> None:
        self._quota.pop(scope, None)

    def release_probe(self, scope: tuple[str, str] | None) -> None:
        if scope is None:
            return
        entry = self._quota.get(scope)
        if entry is not None:
            entry.probe_in_flight = False

    def record_failure(
        self,
        scope: tuple[str, str],
        fingerprint: str | None,
        code: str,
        *,
        origin_turn_id: str,
        retry_after: int | None = None,
        logical_request_id: str | None = None,
        session_key: str | None = None,
        empty_ttl: int = 30,
        quota_ttl: int = 60,
        continuation: bool = False,
    ) -> None:
        now = self.clock()
        self._prune(now)
        if code == "QUOTA" and quota_ttl > 0:
            cooldown = quota_ttl
            if type(retry_after) is int and retry_after > 0:
                cooldown = min(MAX_QUOTA_COOLDOWN, max(quota_ttl, retry_after))
            self._quota[scope] = _QuotaEntry(
                expires_at=now + cooldown,
                origin_turn_id=origin_turn_id,
                probe_in_flight=False,
            )
            self._quota.move_to_end(scope)
            self._prune(now)
            return

        # Any non-QUOTA failure must release an active half-open probe.
        self.release_probe(scope)

        if code == "EMPTY" and empty_ttl > 0 and not continuation:
            provider_id = scope[0] if scope else ""
            ekey = self._empty_key(
                provider_id, fingerprint, logical_request_id, session_key
            )
            if ekey is not None:
                self._empty[ekey] = (now + empty_ttl, origin_turn_id)
                self._empty.move_to_end(ekey)
                self._prune(now)


_default_guard = FailureGuard()


async def check_failure_guard(
    scope: tuple[str, str],
    request_fingerprint: str | None,
    logical_request_id: str | None = None,
    *,
    guard: FailureGuard | None = None,
    session_key: str | None = None,
    empty_ttl: int = 30,
    quota_ttl: int = 60,
    continuation: bool = False,
) -> FailureCheckResult:
    target = guard or _default_guard
    return await target.check(
        scope,
        request_fingerprint,
        logical_request_id,
        session_key=session_key,
        empty_ttl=empty_ttl,
        quota_ttl=quota_ttl,
        continuation=continuation,
    )


def record_failure(
    scope: tuple[str, str],
    fingerprint: str | None,
    code: str,
    *,
    origin_turn_id: str,
    retry_after: int | None = None,
    guard: FailureGuard | None = None,
    logical_request_id: str | None = None,
    session_key: str | None = None,
    empty_ttl: int = 30,
    quota_ttl: int = 60,
    continuation: bool = False,
) -> None:
    target = guard or _default_guard
    target.record_failure(
        scope,
        fingerprint,
        code,
        origin_turn_id=origin_turn_id,
        retry_after=retry_after,
        logical_request_id=logical_request_id,
        session_key=session_key,
        empty_ttl=empty_ttl,
        quota_ttl=quota_ttl,
        continuation=continuation,
    )
