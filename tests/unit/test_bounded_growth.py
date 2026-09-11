"""无界增长测试（P3：卖家资料缓存与卖家主页滚动）。

两处都会随运行时间单调增长：
- ``SellerProfileCache`` 没有容量上限，也没有定期淘汰——只有"同一个卖家在过期后被
  再次查询"才会替换该条，于是每个见过的卖家都会留一份深拷贝（含商品列表与评价）；
- 卖家主页/评价的滚动循环只靠 8 秒空闲超时兜底，页面若持续分页就会一直滚动，
  同时把抓到的条目全堆在内存里。
"""
import asyncio

import pytest

from src.scraper import (
    MAX_SELLER_PROFILE_CACHE_ENTRIES,
    MAX_SELLER_PROFILE_SCROLLS,
    _get_seller_profile_cache_max_entries,
)
from src.services.seller_profile_cache import DEFAULT_MAX_ENTRIES, SellerProfileCache


def make_cache(max_entries=3, clock=None):
    ticks = clock or {"now": 0.0}
    return SellerProfileCache(
        ttl_seconds=3600,
        time_source=lambda: ticks["now"],
        max_entries=max_entries,
    )


def run(coro):
    return asyncio.run(coro)


def test_cache_evicts_when_over_capacity():
    cache = make_cache(max_entries=3)

    async def loader(user_id):
        return {"卖家昵称": user_id, "商品": list(range(5))}

    for index in range(10):
        run(cache.get_or_load(f"seller-{index}", loader))

    stats = cache.stats()
    assert stats["entries"] == 3, f"缓存未受限：{stats}"
    assert stats["max_entries"] == 3


def test_cache_keeps_recently_used_entry():
    """淘汰应优先清掉过期项，而不是刚写入的那条。"""
    clock = {"now": 0.0}
    cache = SellerProfileCache(ttl_seconds=100, time_source=lambda: clock["now"], max_entries=2)

    async def loader(user_id):
        return {"id": user_id}

    run(cache.get_or_load("old", loader))
    clock["now"] = 1000.0  # old 过期
    run(cache.get_or_load("new-1", loader))
    run(cache.get_or_load("new-2", loader))

    stats = cache.stats()
    assert stats["entries"] <= 2
    assert "old" not in cache._entries


def test_expired_entry_is_dropped_on_read():
    clock = {"now": 0.0}
    cache = SellerProfileCache(ttl_seconds=10, time_source=lambda: clock["now"], max_entries=10)

    async def loader(user_id):
        return {"id": user_id}

    run(cache.get_or_load("a", loader))
    assert cache.stats()["entries"] == 1

    clock["now"] = 100.0
    run(cache.get_or_load("b", loader))
    assert "a" not in cache._entries


def test_cache_still_serves_hits_and_dedupes_inflight():
    """加容量限制不能破坏缓存命中与并发合并这两个既有语义。"""
    calls = []
    cache = SellerProfileCache(ttl_seconds=3600, max_entries=10)

    async def loader(user_id):
        calls.append(user_id)
        await asyncio.sleep(0.01)
        return {"id": user_id}

    async def scenario():
        first, second = await asyncio.gather(
            cache.get_or_load("same", loader),
            cache.get_or_load("same", loader),
        )
        third = await cache.get_or_load("same", loader)
        return first, second, third

    first, second, third = run(scenario())
    assert first == second == third == {"id": "same"}
    assert calls == ["same"], f"并发请求应当合并为一次加载，实际 {calls}"


def test_default_and_max_entries_are_bounded():
    assert 1 <= DEFAULT_MAX_ENTRIES <= MAX_SELLER_PROFILE_CACHE_ENTRIES
    assert _get_seller_profile_cache_max_entries({"seller_profile_cache_max_entries": 999999}) == (
        MAX_SELLER_PROFILE_CACHE_ENTRIES
    )
    assert _get_seller_profile_cache_max_entries({}) == DEFAULT_MAX_ENTRIES
    assert _get_seller_profile_cache_max_entries({"seller_profile_cache_max_entries": 0}) == 1


def test_seller_scroll_cap_is_configured():
    assert MAX_SELLER_PROFILE_SCROLLS >= 5, "滚动上限过小会影响正常采集"
    assert MAX_SELLER_PROFILE_SCROLLS <= 200, "滚动上限过大则失去约束意义"
