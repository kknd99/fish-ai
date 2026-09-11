"""Schema 迁移测试（已有部署升级时必须补列）。

背景：``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表不会补列，所以新增字段必须
配套迁移。加交易字段时正是如此 —— 老部署升级上来要有这两列，且默认值必须是
"交易关闭"（升级不应悄悄打开一个能动钱的功能）。

这里用一个"旧结构的数据库"来真实验证升级路径，而不是只看新建库是否正确。
"""
import sqlite3

from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository

#: 加交易字段之前的 tasks 建表语句（老部署的实际结构）
LEGACY_TASKS_DDL = """
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
"""

LEGACY_ROW = (
    0, "老任务", 1, "sony a7m4", "d", 1, 3, 1, "8000", "16000", "*/15 * * * *",
    "prompts/base_prompt.txt", "prompts/macbook_criteria.txt", None, "auto", 1,
    None, None, "ai", "[]", 0,
)


def make_legacy_db(path) -> None:
    """造一个"加字段之前"的库：旧 DDL + 一条老数据。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(LEGACY_TASKS_DDL)
    placeholders = ", ".join("?" for _ in LEGACY_ROW)
    conn.execute(f"INSERT INTO tasks VALUES ({placeholders})", LEGACY_ROW)
    conn.commit()
    conn.close()


def columns_of(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}


def test_migration_adds_trade_columns_to_existing_db(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.sqlite3"
    make_legacy_db(db_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(db_path))

    with sqlite_connection(str(db_path)) as conn:
        assert "trade_enabled" not in columns_of(conn)
        init_schema(conn)
        columns = columns_of(conn)

    assert {"trade_enabled", "trade_action"} <= columns


def test_migration_preserves_legacy_row_and_defaults_to_trade_off(tmp_path, monkeypatch):
    """老数据必须还读得出来，且交易字段默认为关闭。"""
    db_path = tmp_path / "legacy.sqlite3"
    make_legacy_db(db_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(db_path))

    repository = SqliteTaskRepository(db_path=str(db_path), legacy_config_file=None)
    tasks = repository._find_all_sync()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.task_name == "老任务"
    assert task.keyword == "sony a7m4"
    assert task.decision_mode == "ai"
    # 升级不应悄悄打开一个能动钱的功能
    assert task.trade_enabled is False
    assert task.trade_action == "notify_link"


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    make_legacy_db(db_path)

    with sqlite_connection(str(db_path)) as conn:
        init_schema(conn)
        init_schema(conn)  # 再跑一次不应报错
        init_schema(conn)
        assert {"trade_enabled", "trade_action"} <= columns_of(conn)
        marker = conn.execute(
            "SELECT value FROM app_metadata WHERE key = 'migration:tasks_trade_columns'"
        ).fetchone()
    assert marker is not None and marker[0] == "done"


def test_fresh_db_has_trade_columns_without_migration(tmp_path):
    """新库靠建表语句就有这两列，迁移函数对它是无害空操作。"""
    db_path = tmp_path / "fresh.sqlite3"
    with sqlite_connection(str(db_path)) as conn:
        init_schema(conn)
        columns = columns_of(conn)
    assert {"trade_enabled", "trade_action"} <= columns


def test_migration_survives_pre_existing_trade_column(tmp_path):
    """万一某部署已经手工加过其中一列，迁移也要能跑完（幂等到列级别）。"""
    db_path = tmp_path / "half.sqlite3"
    make_legacy_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE tasks ADD COLUMN trade_enabled INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()

    with sqlite_connection(str(db_path)) as conn:
        init_schema(conn)
        columns = columns_of(conn)
    assert {"trade_enabled", "trade_action"} <= columns
