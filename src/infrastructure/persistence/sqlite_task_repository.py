"""
基于 SQLite 的任务仓储实现。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import List, Optional

from src.domain.models.task import Task
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection

#: 任务 ID 冲突时的重试次数（并发创建时的兜底）。
MAX_TASK_ID_ATTEMPTS = 5


def _row_to_task(row) -> Task:
    payload = dict(row)
    payload["enabled"] = bool(payload["enabled"])
    payload["analyze_images"] = bool(payload["analyze_images"])
    payload["personal_only"] = bool(payload["personal_only"])
    payload["free_shipping"] = bool(payload["free_shipping"])
    payload["is_running"] = bool(payload["is_running"])
    payload["keyword_rules"] = json.loads(payload.pop("keyword_rules_json") or "[]")
    payload["trade_enabled"] = bool(payload.get("trade_enabled", 0))
    payload["trade_action"] = payload.get("trade_action") or "notify_link"
    return Task(**payload)


def find_task_by_name_sync(task_name: str) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC LIMIT 1",
            (task_name,),
        ).fetchone()
    return _row_to_task(row) if row else None


class SqliteTaskRepository(TaskRepository):
    """基于 SQLite 的任务仓储"""

    def __init__(
        self,
        db_path: str | None = None,
        legacy_config_file: str | None = "config.json",
    ):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file

    async def find_all(self) -> List[Task]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def save(self, task: Task) -> Task:
        return await asyncio.to_thread(self._save_sync, task)

    async def delete(self, task_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _find_all_sync(self) -> List[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return [_row_to_task(row) for row in rows]

    def _find_by_id_sync(self, task_id: int) -> Optional[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    #: 建表列清单（INSERT 与 UPDATE 共用，避免两处各写一份导致漏列）。
    _COLUMNS = (
        "id", "task_name", "enabled", "keyword", "description", "analyze_images",
        "max_pages", "personal_only", "min_price", "max_price", "cron",
        "ai_prompt_base_file", "ai_prompt_criteria_file", "account_state_file",
        "account_strategy", "free_shipping", "new_publish_option", "region",
        "decision_mode", "keyword_rules_json", "is_running",
        "trade_enabled", "trade_action",
    )

    def _insert_sql(self) -> str:
        columns = ", ".join(self._COLUMNS)
        placeholders = ", ".join(f":{name}" for name in self._COLUMNS)
        return f"INSERT INTO tasks ({columns}) VALUES ({placeholders})"

    def _update_sql(self) -> str:
        assignments = ", ".join(
            f"{name} = :{name}" for name in self._COLUMNS if name != "id"
        )
        return f"UPDATE tasks SET {assignments} WHERE id = :id"

    def _save_sync(self, task: Task) -> Task:
        """保存任务。

        并发正确性（修复"任务无声消失"）：
        旧实现把 ``MAX(id)+1`` 放在事务之外，并用 ``INSERT OR REPLACE`` 写入 ——
        两个并发创建会算出同一个 id，后写入者**直接覆盖**先写入的任务，且不报错。
        现在：
        - ``BEGIN IMMEDIATE`` 让"算 id + 插入"成为一个串行化的临界区；
        - 新建走 ``INSERT``（不再 OR REPLACE），id 冲突时重算重试；
        - 更新走 ``UPDATE``；若目标行已被删除则回退为插入（保持原有的 upsert 语义）。
        """
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            return self._save_locked(conn, task)

    def _save_locked(self, conn, task: Task) -> Task:
        last_error: Optional[Exception] = None
        for _ in range(MAX_TASK_ID_ATTEMPTS):
            task_id = task.id
            try:
                # 显式开启写事务：既锁住 id 分配，也让并发写在此排队而不是各算各的
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                pass  # 已在事务中（例如调用方自行开启）

            try:
                if task_id is None:
                    task_id = self._next_task_id(conn)
                    payload = self._task_values(task.model_copy(update={"id": task_id}))
                    conn.execute(self._insert_sql(), payload)
                else:
                    payload = self._task_values(task)
                    cursor = conn.execute(self._update_sql(), payload)
                    if cursor.rowcount == 0:
                        # 行已不存在（例如刚被删除）：回退为插入，保持 upsert 语义
                        conn.execute(self._insert_sql(), payload)
                conn.commit()
                return task.model_copy(update={"id": task_id})
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if task.id is not None:
                    raise
                # 极少数情况下仍撞上并发的 id（例如外部进程也在写同一库）：重算重试
                last_error = exc
                continue
            except Exception:
                conn.rollback()
                raise

        raise RuntimeError(
            f"无法分配任务 ID：连续 {MAX_TASK_ID_ATTEMPTS} 次遇到并发冲突"
        ) from last_error

    def _delete_sync(self, task_id: int) -> bool:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
        return cursor.rowcount > 0

    def _next_task_id(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(id), -1) AS max_id FROM tasks").fetchone()
        return int(row["max_id"]) + 1

    def _task_values(self, task: Task) -> dict:
        values = task.model_dump()
        values["enabled"] = int(task.enabled)
        values["analyze_images"] = int(task.analyze_images)
        values["personal_only"] = int(task.personal_only)
        values["free_shipping"] = int(task.free_shipping)
        values["is_running"] = int(task.is_running)
        values["trade_enabled"] = int(getattr(task, "trade_enabled", False))
        values["keyword_rules_json"] = json.dumps(task.keyword_rules or [], ensure_ascii=False)
        values["trade_action"] = str(getattr(task, "trade_action", "notify_link") or "notify_link")
        values.pop("keyword_rules", None)
        return values
