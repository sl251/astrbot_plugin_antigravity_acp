"""Bounded native process pool; AstrBot history is the source of truth."""

import asyncio
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .errors import AgyError
from .messages import ConversationInput, digest, replay
from .models import catalog, select
from .process import LaunchInfo, discover, launch_args, make_home
from .transport import AcpClient, Event


@dataclass
class Session:
    client: AcpClient | None = None
    home: Path | None = None
    system_hash: str = ""
    model: str = ""
    effort: str = ""
    expected: list[str] = field(default_factory=list)
    busy: bool = False
    used_at: float = field(default_factory=time.monotonic)
    lifecycle: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def close(self) -> None:
        async with self.lifecycle:
            await self.close_unlocked()

    async def close_unlocked(self) -> None:
        try:
            if self.client:
                await self.client.close()
        finally:
            self.client = None
            if self.home:
                await asyncio.to_thread(shutil.rmtree, self.home, True)
                self.home = None
            self.expected.clear()


class Runtime:
    def __init__(self, settings: Settings, root: Path):
        self.settings = settings
        self.root = root / ("runtime-" + uuid.uuid4().hex)
        self.sessions: dict[str, Session] = {}
        self.key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.pool_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(settings.max_concurrent)
        self.callers = 0
        self.stopped = False
        self.info: LaunchInfo | None = None
        self.info_lock = asyncio.Lock()
        self.models_lock = asyncio.Lock()
        self.model_cache: list[str] = []
        self.model_cache_time = 0.0
        self.sweeper: asyncio.Task | None = None

    async def launch_info(self) -> LaunchInfo:
        async with self.info_lock:
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用或重新加载。")
            if self.info is None:
                self.info = await discover(self.settings)
            return self.info

    @asynccontextmanager
    async def lease(self, key: str, temporary: bool):
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用或重新加载。")
        if self.callers >= self.settings.max_pending + self.settings.max_concurrent:
            raise AgyError("BUSY", "AGY 等待队列已满，请稍后再试。")
        self.callers += 1
        lock, refs = self.key_locks.get(key, (asyncio.Lock(), 0))
        self.key_locks[key] = (lock, refs + 1)
        locked = acquired = False
        session = None
        try:
            try:
                async with asyncio.timeout(self.settings.queue_timeout):
                    await lock.acquire()
                    locked = True
                    await self.slots.acquire()
                    acquired = True
            except TimeoutError:
                raise AgyError("BUSY", "等待 AGY 会话超时，请稍后再试。") from None
            async with self.pool_lock:
                if self.stopped:
                    raise AgyError("CLOSED", "插件已停用或重新加载。")
                if self.sweeper is None:
                    self.sweeper = asyncio.create_task(
                        self._sweep(), name="agy:idle-reaper"
                    )
                session = self.sessions.get(key)
                if session is None:
                    if len(self.sessions) >= self.settings.max_sessions:
                        idle = [(k, s) for k, s in self.sessions.items() if not s.busy]
                        if not idle:
                            raise AgyError("BUSY", "AGY 会话池已满。")
                        old_key, old = min(idle, key=lambda pair: pair[1].used_at)
                        await old.close()
                        del self.sessions[old_key]
                    session = self.sessions[key] = Session()
                session.busy = True
            yield session
        finally:
            try:
                if session:
                    async with self.pool_lock:
                        session.used_at = time.monotonic()
                        session.busy = False
                        if (
                            temporary
                            or self.stopped
                            or session.client is None
                            or not session.client.alive
                        ):
                            await session.close()
                            if self.sessions.get(key) is session:
                                del self.sessions[key]
            finally:
                if acquired:
                    self.slots.release()
                if locked:
                    lock.release()
                _, refs = self.key_locks[key]
                if refs == 1:
                    del self.key_locks[key]
                else:
                    self.key_locks[key] = (lock, refs - 1)
                self.callers -= 1

    async def open(self, session: Session, system: str) -> dict:
        async with session.lifecycle:
            return await self._open_unlocked(session, system)

    async def _open_unlocked(self, session: Session, system: str) -> dict:
        await session.close_unlocked()
        info = await self.launch_info()
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用。")
        session.home = make_home(self.root, system)
        session.client = AcpClient(self.settings)
        try:
            await session.client.start(
                launch_args(info, session.home), session.home / "workspace"
            )
            result = await session.client.new_session(session.home / "workspace")
            if self.stopped:
                raise AgyError("CLOSED", "插件已停用。")
            return result
        except BaseException:
            await session.close_unlocked()
            raise

    async def get_models(self) -> list[str]:
        if self.stopped:
            raise AgyError("CLOSED", "插件已停用。")
        async with self.models_lock:
            if self.model_cache and time.monotonic() - self.model_cache_time < 300:
                return list(self.model_cache)
            async with self.lease(uuid.uuid4().hex, temporary=True) as session:
                result = await self.open(session, "")
                self.model_cache = catalog(result)
                self.model_cache_time = time.monotonic()
                return list(self.model_cache)

    async def stream(
        self, request: ConversationInput, key: str | None, model: str, effort: str
    ) -> AsyncIterator[Event]:
        temporary = key is None
        async with self.lease(key or uuid.uuid4().hex, temporary) as session:
            completed = False
            try:
                hashes = request.hashes
                reusable = (
                    session.client is not None
                    and session.client.alive
                    and session.system_hash == request.system_hash
                    and session.model == model
                    and session.effort == effort
                    and bool(session.expected)
                    and session.expected == hashes[:-1]
                )
                if reusable:
                    prompt, omitted = replay(
                        request.messages[-1:], self.settings.history_bytes
                    )
                else:
                    prompt, omitted = replay(
                        request.messages, self.settings.history_bytes
                    )
                    result = await self.open(session, request.system)
                    assert session.client
                    await select(session.client, result, model, effort)
                    session.system_hash = request.system_hash
                    session.model, session.effort = model, effort
                if omitted:
                    yield Event("history_trimmed", result={"omitted_messages": omitted})
                assert session.client
                text_parts = []
                turn = session.client.generate(prompt)
                try:
                    async for event in turn:
                        if event.kind == "text":
                            text_parts.append(event.text)
                        if event.kind == "done":
                            result = event.result or {}
                            reason = result.get("stopReason")
                            if reason == "cancelled":
                                raise AgyError("CANCELLED", "AGY 取消了本轮请求。")
                            if reason not in {"end_turn", "max_tokens"}:
                                raise AgyError(
                                    "STOP",
                                    "AGY 未正常结束本轮，请在 CLI 检查限制或拒绝原因。",
                                )
                            answer = "".join(text_parts)
                            if not answer.strip():
                                raise AgyError("EMPTY", "AGY 没有返回正文。")
                            session.expected = hashes + [
                                digest({"role": "assistant", "content": answer})
                            ]
                            completed = True
                        yield event
                finally:
                    await turn.aclose()
            finally:
                # Partial/cancelled turns never become a reusable history prefix.
                if not completed:
                    await session.close()

    async def _sweep(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(30, self.settings.idle_ttl))
                async with self.pool_lock:
                    now = time.monotonic()
                    for key, session in list(self.sessions.items()):
                        if (
                            not session.busy
                            and now - session.used_at >= self.settings.idle_ttl
                        ):
                            await session.close()
                            del self.sessions[key]
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        self.stopped = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        async with self.pool_lock:
            await asyncio.gather(
                *(session.close() for session in self.sessions.values())
            )
            self.sessions.clear()
        # Busy callers own their final release; avoid deleting their files during
        # cleanup races. Empty runtime directories can always be removed safely.
        try:
            self.root.rmdir()
        except OSError:
            pass
