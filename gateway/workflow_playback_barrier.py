"""复杂工作流终止步骤前的客户端播放完成屏障。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import threading
import time
from typing import Callable


PLAYBACK_COMPLETE = "playback_complete"
PLAYBACK_INTERRUPTED = "playback_interrupted"
PLAYBACK_CANCELLED = "cancelled"
PLAYBACK_TIMEOUT = "timeout"
_VALID_REPORT_TYPES = frozenset({PLAYBACK_COMPLETE, PLAYBACK_INTERRUPTED})


@dataclass(frozen=True)
class PlaybackBarrierKey:
    session_id: str
    round_id: str
    playback_id: str

    @classmethod
    def create(cls, session_id: str, round_id: str, playback_id: str) -> "PlaybackBarrierKey":
        values = tuple(str(value or "").strip() for value in (session_id, round_id, playback_id))
        if not all(values):
            raise ValueError("session_id、round_id、playback_id 均不能为空")
        return cls(*values)


@dataclass(frozen=True)
class PlaybackBarrierResult:
    status: str
    key: PlaybackBarrierKey
    reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == PLAYBACK_COMPLETE


class PlaybackBarrierWaiter:
    def __init__(
        self,
        registry: "PlaybackBarrierRegistry",
        key: PlaybackBarrierKey,
        future: asyncio.Future[PlaybackBarrierResult],
    ) -> None:
        self._registry = registry
        self.key = key
        self._future = future

    async def wait(self, timeout: float) -> PlaybackBarrierResult:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        try:
            return await asyncio.wait_for(asyncio.shield(self._future), timeout=timeout)
        except asyncio.TimeoutError:
            self._registry._remove_waiter(self.key, self._future)
            return PlaybackBarrierResult(status=PLAYBACK_TIMEOUT, key=self.key, reason="timeout")


class PlaybackBarrierRegistry:
    """线程安全地连接 TTS 播放等待项与客户端回执。"""

    def __init__(
        self,
        *,
        cache_ttl_sec: float = 30.0,
        cache_max_entries: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if cache_ttl_sec <= 0 or cache_max_entries < 1:
            raise ValueError("缓存 TTL 和容量必须大于 0")
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_max_entries = cache_max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._waiters: dict[
            PlaybackBarrierKey,
            tuple[asyncio.AbstractEventLoop, asyncio.Future[PlaybackBarrierResult]],
        ] = {}
        self._recent: OrderedDict[PlaybackBarrierKey, tuple[float, PlaybackBarrierResult]] = OrderedDict()

    def prepare(self, session_id: str, round_id: str, playback_id: str) -> PlaybackBarrierWaiter:
        """必须在发送对应 TTS 前调用，避免正常回执先于等待注册。"""
        key = PlaybackBarrierKey.create(session_id, round_id, playback_id)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._purge_expired_locked()
            cached = self._recent.get(key)
            existing = self._waiters.get(key)
            if existing is not None:
                existing_loop, existing_future = existing
                if existing_loop is not loop:
                    raise RuntimeError("同一播放屏障不能跨事件循环等待")
                return PlaybackBarrierWaiter(self, key, existing_future)
            future: asyncio.Future[PlaybackBarrierResult] = loop.create_future()
            if cached is None:
                self._waiters[key] = (loop, future)
        if cached is not None:
            _, result = cached
            self._complete_future(loop, future, result)
        return PlaybackBarrierWaiter(self, key, future)

    def record_report(
        self,
        *,
        session_id: str,
        round_id: str,
        playback_id: str,
        report_type: str,
        reason: str | None = None,
    ) -> bool:
        """记录播放终态；返回是否命中了一个正在等待的屏障。"""
        if report_type not in _VALID_REPORT_TYPES:
            return False
        key = PlaybackBarrierKey.create(session_id, round_id, playback_id)
        result = PlaybackBarrierResult(status=report_type, key=key, reason=reason)
        waiter = None
        with self._lock:
            self._purge_expired_locked()
            if key in self._recent:
                return False
            waiter = self._waiters.pop(key, None)
            self._recent[key] = (self._clock(), result)
            self._recent.move_to_end(key)
            while len(self._recent) > self._cache_max_entries:
                self._recent.popitem(last=False)
        if waiter is None:
            return False
        loop, future = waiter
        self._complete_future(loop, future, result)
        return True

    def cancel_session(self, session_id: str, *, reason: str) -> int:
        safe_session_id = str(session_id or "").strip()
        if not safe_session_id:
            return 0
        cancelled = []
        with self._lock:
            for key in list(self._waiters):
                if key.session_id == safe_session_id:
                    cancelled.append((key, self._waiters.pop(key)))
            for key in list(self._recent):
                if key.session_id == safe_session_id:
                    self._recent.pop(key, None)
        for key, (loop, future) in cancelled:
            self._complete_future(
                loop,
                future,
                PlaybackBarrierResult(status=PLAYBACK_CANCELLED, key=key, reason=reason),
            )
        return len(cancelled)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._purge_expired_locked()
            return {
                "active_waiters": len(self._waiters),
                "recent_reports": len(self._recent),
            }

    def _remove_waiter(
        self,
        key: PlaybackBarrierKey,
        future: asyncio.Future[PlaybackBarrierResult],
    ) -> None:
        with self._lock:
            existing = self._waiters.get(key)
            if existing is not None and existing[1] is future:
                self._waiters.pop(key, None)

    def _purge_expired_locked(self) -> None:
        cutoff = self._clock() - self._cache_ttl_sec
        while self._recent:
            _, (recorded_at, _) = next(iter(self._recent.items()))
            if recorded_at > cutoff:
                break
            self._recent.popitem(last=False)

    @staticmethod
    def _complete_future(
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[PlaybackBarrierResult],
        result: PlaybackBarrierResult,
    ) -> None:
        def complete() -> None:
            if not future.done():
                future.set_result(result)

        try:
            if loop.is_running():
                loop.call_soon_threadsafe(complete)
            else:
                complete()
        except RuntimeError:
            # 服务关闭时事件循环可能已先于清理逻辑退出。
            return
