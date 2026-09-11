"""
SQLite 连接与 schema 初始化。
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.infrastructure.persistence.storage_names import DEFAULT_DATABASE_PATH


BUSY_TIMEOUT_MS = 5000

#: 写操作遇到 "database is locked" 时的重试次数与退避基数。
#: 为什么需要：多个爬虫子进程 + Web 进程会同时写同一个库，WAL 下写是串行的，
#: 超过 busy_timeout 仍拿不到锁就抛 OperationalError。历史实现把这条异常在入库
#: 路径上吞成了 False（"静默丢结果"）——现在有限重试，仍失败则明确报错。
WRITE_RETRY_ATTEMPTS = 4
WRITE_RETRY_BASE_DELAY = 0.2


def is_database_locked_error(exc: BaseException) -> bool:
    """判断异常是否是"库被占用"（可重试），而不是别的数据库错误。"""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def run_with_lock_retry(
    operation,
    *,
    attempts: int = WRITE_RETRY_ATTEMPTS,
    base_delay: float = WRITE_RETRY_BASE_DELAY,
):
    """执行写操作，遇到"库被占用"时按线性退避重试。

    非锁相关异常立即抛出（重试它们没有意义）。重试次数用尽后抛出最后一次异常，
    由调用方决定如何提示——关键是不能静默吞掉。
    """
    total = max(1, int(attempts))
    last_error: Optional[BaseException] = None
    for attempt in range(total):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - 需要分类后决定是否重试
            if not is_database_locked_error(exc):
                raise
            last_error = exc
            if attempt < total - 1:
                time.sleep(base_delay * (attempt + 1))
    assert last_error is not None
    raise last_error

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        task_name TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        keyword TEXT NOT NULL,
        description TEXT,
        analyze_images INTEGER NOT NULL,
        max_pages INTEGER NOT NULL,
        personal_only INTEGER NOT NULL,
        min_price TEXT,
        max_price TEXT,
        cron TEXT,
        ai_prompt_base_file TEXT NOT NULL,
        ai_prompt_criteria_file TEXT NOT NULL,
        account_state_file TEXT,
        account_strategy TEXT NOT NULL,
        free_shipping INTEGER NOT NULL,
        new_publish_option TEXT,
        region TEXT,
        decision_mode TEXT NOT NULL,
        keyword_rules_json TEXT NOT NULL,
        is_running INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result_filename TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        crawl_time TEXT NOT NULL,
        publish_time TEXT,
        price REAL,
        price_display TEXT,
        item_id TEXT,
        title TEXT,
        link TEXT,
        link_unique_key TEXT NOT NULL,
        seller_nickname TEXT,
        is_recommended INTEGER NOT NULL,
        analysis_source TEXT,
        keyword_hit_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        raw_json TEXT NOT NULL,
        UNIQUE(result_filename, link_unique_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword_slug TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        snapshot_time TEXT NOT NULL,
        snapshot_day TEXT NOT NULL,
        run_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        title TEXT,
        price REAL NOT NULL,
        price_display TEXT,
        tags_json TEXT NOT NULL,
        region TEXT,
        seller TEXT,
        publish_time TEXT,
        link TEXT,
        UNIQUE(keyword_slug, run_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_blacklist_rules (
        result_filename TEXT PRIMARY KEY,
        blacklist_keywords_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        task_name TEXT NOT NULL,
        item_id TEXT,
        title TEXT,
        link TEXT,
        seller TEXT,
        price REAL,
        adapter TEXT NOT NULL,
        outcome TEXT NOT NULL,
        allowed INTEGER NOT NULL,
        reasons_json TEXT NOT NULL,
        checks_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        external_ref TEXT,
        detail TEXT,
        duration_ms INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_attempts_created
    ON trade_attempts(created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_attempts_idempotency
    ON trade_attempts(idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_trade_attempts_outcome
    ON trade_attempts(outcome, created_at DESC)
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_name ON tasks(task_name)",
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_crawl
    ON result_items(result_filename, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_publish
    ON result_items(result_filename, publish_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_price
    ON result_items(result_filename, price DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_recommended
    ON result_items(result_filename, is_recommended, analysis_source, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_time
    ON price_snapshots(keyword_slug, snapshot_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_item_time
    ON price_snapshots(keyword_slug, item_id, snapshot_time DESC)
    """,
)


def get_database_path() -> str:
    return os.getenv("APP_DATABASE_FILE", DEFAULT_DATABASE_PATH)


def _prepare_database_file(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    # 说明：当前 schema 没有任何 FOREIGN KEY 声明，这条 PRAGMA 实际是空转。
    # 保留它是为了将来加外键时默认生效；删除任务时的级联目前由
    # src/api/routes/tasks.py 里的手写 DELETE 完成。
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    _migrate_result_items_status(conn)
    conn.commit()


def _migrate_result_items_status(conn: sqlite3.Connection) -> None:
    """为 result_items 表添加 status 列（仅执行一次）。"""
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = 'migration:result_items_status'"
    ).fetchone()
    if row is not None:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(result_items)").fetchall()]
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE result_items ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('migration:result_items_status', 'done')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_filename_status_crawl"
        " ON result_items(result_filename, status, crawl_time DESC)"
    )


@contextmanager
def sqlite_connection(
    db_path: str | None = None,
) -> Iterator[sqlite3.Connection]:
    path = db_path or get_database_path()
    _prepare_database_file(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()
