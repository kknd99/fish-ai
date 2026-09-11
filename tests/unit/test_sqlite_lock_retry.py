"""SQLite 写锁重试测试（P3：`database is locked` 静默丢结果）。

多个爬虫子进程与 Web 进程会同时写同一个库；WAL 下写是串行的，超过 busy_timeout
仍拿不到锁就抛 OperationalError。历史实现把这条异常在入库路径上吞成 False——
表现为"结果莫名少了几条"，而且没有任何告警。现在：有限重试 + 失败时明确报错。
"""
import sqlite3

import pytest

from src.infrastructure.persistence.sqlite_connection import (
    WRITE_RETRY_ATTEMPTS,
    is_database_locked_error,
    run_with_lock_retry,
)

LOCKED = sqlite3.OperationalError("database is locked")
BUSY = sqlite3.OperationalError("database table is busy")
OTHER = sqlite3.OperationalError("no such table: tasks")
NOT_SQLITE = ValueError("boom")


@pytest.mark.parametrize("exc", [LOCKED, BUSY])
def test_locked_errors_are_retryable(exc):
    assert is_database_locked_error(exc) is True


@pytest.mark.parametrize("exc", [OTHER, NOT_SQLITE])
def test_other_errors_are_not_retryable(exc):
    assert is_database_locked_error(exc) is False


def test_retries_until_success():
    calls = []

    def operation():
        calls.append(1)
        if len(calls) < 3:
            raise LOCKED
        return "ok"

    assert run_with_lock_retry(operation, base_delay=0) == "ok"
    assert len(calls) == 3


def test_non_locked_error_is_raised_immediately():
    calls = []

    def operation():
        calls.append(1)
        raise OTHER

    with pytest.raises(sqlite3.OperationalError):
        run_with_lock_retry(operation, base_delay=0)
    assert len(calls) == 1, "非锁相关错误不应重试"


def test_gives_up_after_max_attempts_and_raises():
    calls = []

    def operation():
        calls.append(1)
        raise LOCKED

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        run_with_lock_retry(operation, attempts=3, base_delay=0)
    assert len(calls) == 3


def test_default_attempts_is_bounded():
    assert 2 <= WRITE_RETRY_ATTEMPTS <= 10, "重试次数要有意义但不能无限拖住入库"


# --------------------------------------------------- 端到端：入库路径确实会重试


def test_save_result_record_retries_on_locked_db(monkeypatch, tmp_path):
    """入库遇到锁时应重试并最终成功，而不是丢结果。"""
    import src.services.result_storage_service as storage
    from src.infrastructure.persistence import sqlite_connection as conn_module

    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    monkeypatch.setattr(conn_module, "WRITE_RETRY_BASE_DELAY", 0.0, raising=False)

    attempts = {"count": 0}
    real_connection = storage.sqlite_connection

    def flaky_connection(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connection(*args, **kwargs)

    monkeypatch.setattr(storage, "sqlite_connection", flaky_connection)

    record = {
        "搜索关键字": "sony a7m4",
        "任务名称": "Sony A7M4",
        "爬取时间": "2026-01-01T10:00:00",
        "商品信息": {
            "商品ID": "1",
            "商品标题": "Sony A7M4",
            "商品链接": "https://www.goofish.com/item?id=1",
            "当前售价": "¥10000",
        },
    }

    assert storage._save_result_record_sync(record, "sony a7m4") is True
    assert attempts["count"] >= 2, "第一次遇到锁后必须重试"

    with real_connection() as conn:
        rows = conn.execute("SELECT item_id FROM result_items").fetchall()
    assert [row["item_id"] for row in rows] == ["1"], "重试后结果应当已入库"


def test_save_result_record_raises_when_lock_persists(monkeypatch, tmp_path, capsys):
    """锁持续存在时不能返回 True 假装成功，也不能静默——要抛出去让上层记录。"""
    import sqlite3 as _sqlite3

    import src.services.result_storage_service as storage
    from src.utils import save_to_jsonl

    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))

    def always_locked(*args, **kwargs):
        raise _sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "sqlite_connection", always_locked)
    # 走默认重试次数（4 次、0.2s 线性退避）；这里只断言最终行为，不改变时序

    import asyncio

    record = {"商品信息": {"商品链接": "https://www.goofish.com/item?id=2"}, "搜索关键字": "k"}
    result = asyncio.run(save_to_jsonl(record, "k"))

    assert result is False
    output = capsys.readouterr().out
    assert "未入库" in output, "失败必须明确提示，不能静默丢结果"
