"""
卖家资料缓存服务
"""
import asyncio
import copy
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


SellerProfileLoader = Callable[[str], Awaitable[dict]]

#: 缓存条目上限。每个条目是一份卖家资料深拷贝（含商品列表与评价），
#: 旧实现没有上限也没有淘汰，只有"同一个卖家在过期后被再次查询"才会替换该条，
#: 于是一次长时间运行会为每个见过的卖家永久保留一份深拷贝。
DEFAULT_MAX_ENTRIES = 500


@dataclass(frozen=True)
class _CacheEntry:
    value: dict
    expires_at: float


class SellerProfileCache:
    """带 TTL 和并发合并的卖家资料缓存。"""

    def __init__(
        self,
        ttl_seconds: int = 1800,
        time_source: Optional[Callable[[], float]] = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = max(0, ttl_seconds)
        self._time_source = time_source or time.monotonic
        self._max_entries = max(1, int(max_entries))
        self._entries: dict[str, _CacheEntry] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return float(self._time_source())

    def _clone(self, value: dict) -> dict:
        return copy.deepcopy(value)

    def _get_entry_value(self, user_id: str) -> Optional[dict]:
        entry = self._entries.get(user_id)
        if entry is None:
            return None
        if entry.expires_at < self._now():
            self._entries.pop(user_id, None)
            return None
        return self._clone(entry.value)

    def _evict_if_needed(self) -> None:
        """收紧缓存占用：先清过期项；仍超上限时按过期时间淘汰最旧的。

        在持锁状态下调用（``_lock`` 已由调用方持有）。清理过期项是**无条件**的——
        否则一个"写入后再也没被读过"的过期条目会一直占着内存，直到容量淘汰才释放。
        单次扫描 O(n) 且 n 有上限（默认几百），相对一次网络抓取可以忽略。
        """
        now = self._now()
        for key in [k for k, e in self._entries.items() if e.expires_at < now]:
            self._entries.pop(key, None)

        overflow = len(self._entries) - self._max_entries
        if overflow <= 0:
            return
        # 过期时间最早的最先淘汰（近似 LRU/LFU 的简单替代：TTL 相同即按写入顺序）
        for key, _ in sorted(self._entries.items(), key=lambda item: item[1].expires_at)[:overflow]:
            self._entries.pop(key, None)

    def stats(self) -> dict:
        """缓存现状（供排查/观测使用）。"""
        return {
            "entries": len(self._entries),
            "inflight": len(self._inflight),
            "max_entries": self._max_entries,
            "ttl_seconds": self._ttl_seconds,
        }

    async def get_or_load(self, user_id: str, loader: SellerProfileLoader) -> dict:
        async with self._lock:
            cached_value = self._get_entry_value(user_id)
            if cached_value is not None:
                return cached_value
            task = self._inflight.get(user_id)
            if task is None:
                task = asyncio.create_task(self._load_and_store(user_id, loader))
                self._inflight[user_id] = task
        return self._clone(await task)

    async def _load_and_store(self, user_id: str, loader: SellerProfileLoader) -> dict:
        try:
            value = self._clone(await loader(user_id))
            expires_at = self._now() + self._ttl_seconds
            async with self._lock:
                self._entries[user_id] = _CacheEntry(value=value, expires_at=expires_at)
                self._evict_if_needed()
            return value
        finally:
            async with self._lock:
                self._inflight.pop(user_id, None)
