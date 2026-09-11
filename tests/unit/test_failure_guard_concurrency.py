"""FailureGuard 并发正确性测试（P3：文件锁失效导致熔断计数丢更新）。

对应缺陷：状态文件用 ``os.replace`` 原子替换写入，而旧的锁加在**状态文件本身**的
``"a+"`` 句柄上 —— 替换之后锁留在已被解除链接的旧 inode 上，第二个进程打开的是新
inode、能立刻拿到锁，于是"读取-修改-写入"根本不互斥，并发时会丢更新。
后果不是性能问题：熔断计数少算就意味着**继续去撞已经触发风控的账号**，
恰恰是这个守卫存在的意义。

修复：锁一个从不被替换的旁路文件（``<path>.lock``），并让读-改-写整体持锁。
"""
import concurrent.futures
import json
import threading
from datetime import datetime, timezone

import pytest

from src.failure_guard import FailureGuard


@pytest.fixture()
def guard(tmp_path):
    # 阈值设高，避免并发计数过程中进入暂停分支，让我们专注验证"计数不丢"
    return FailureGuard(
        path=str(tmp_path / "guard.json"),
        threshold=10_000,
        pause_seconds=3600,
    )


def read_state(guard) -> dict:
    with open(guard.path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_state_file_and_sidecar_lock_are_created(guard):
    guard.record_failure("task-a", "boom")
    import os

    assert os.path.exists(guard.path)
    assert os.path.exists(f"{guard.path}.lock"), "必须在旁路文件上加锁"


def test_sequential_failures_accumulate(guard):
    for _ in range(5):
        guard.record_failure("task-a", "boom")
    assert read_state(guard)["tasks"]["task-a"]["consecutive_failures"] == 5


def test_record_success_resets(guard):
    guard.record_failure("task-a", "boom")
    guard.record_success("task-a")
    assert read_state(guard)["tasks"]["task-a"]["consecutive_failures"] == 0


def test_concurrent_failures_do_not_lose_updates(guard):
    """核心回归：并发记录失败时，计数必须等于调用次数（不能丢更新）。"""
    total = 40
    barrier = threading.Barrier(total)
    errors = []

    def worker(index: int):
        barrier.wait()
        try:
            guard.record_failure("task-a", f"boom-{index}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=total) as pool:
        list(pool.map(worker, range(total)))

    assert not errors, f"并发记录失败时出现异常: {errors}"
    stored = read_state(guard)["tasks"]["task-a"]["consecutive_failures"]
    assert stored == total, f"熔断计数丢更新：期望 {total}，实际 {stored}"


def test_concurrent_failures_across_guard_instances(guard):
    """多实例（模拟"API 进程 + 爬虫子进程"同时写同一文件）也不能丢计数。"""
    total = 24
    barrier = threading.Barrier(total)

    def worker(index: int):
        local_guard = FailureGuard(
            path=guard.path, threshold=10_000, pause_seconds=3600
        )
        barrier.wait()
        local_guard.record_failure("task-shared", f"boom-{index}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=total) as pool:
        list(pool.map(worker, range(total)))

    stored = read_state(guard)["tasks"]["task-shared"]["consecutive_failures"]
    assert stored == total, f"跨实例计数丢更新：期望 {total}，实际 {stored}"


def test_threshold_still_pauses(guard):
    """并发修复不能破坏熔断语义本身。"""
    small = FailureGuard(path=guard.path, threshold=3, pause_seconds=3600)
    for _ in range(3):
        small.record_failure("task-b", "boom")

    decision = small.should_skip_start("task-b")
    assert decision.skip is True
    assert decision.consecutive_failures >= 3


def test_cookie_update_recovers_from_pause(guard, tmp_path):
    """既有的"更新登录态即自动恢复"行为必须保留。"""
    small = FailureGuard(path=guard.path, threshold=2, pause_seconds=3600)
    cookie = tmp_path / "state.json"
    cookie.write_text("{}", encoding="utf-8")

    for _ in range(2):
        small.record_failure("task-c", "login required", cookie_path=str(cookie))
    assert small.should_skip_start("task-c", cookie_path=str(cookie)).skip is True

    # 更新 cookie（mtime 变化）后应自动恢复
    import os
    import time

    time.sleep(0.01)
    cookie.write_text('{"cookies": []}', encoding="utf-8")
    os.utime(cookie, (time.time() + 5, time.time() + 5))

    decision = small.should_skip_start("task-c", cookie_path=str(cookie))
    assert decision.skip is False
    assert decision.reason == "cookie_updated"
