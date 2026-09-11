"""任务仓储并发正确性测试（P3：任务 ID 竞态与静默覆盖）。

对应缺陷：``_next_task_id`` = ``MAX(id)+1`` 在**事务之外**计算，且写入用
``INSERT OR REPLACE``。两个并发创建会算出同一个 id，后写入者直接**覆盖**先写入的
任务、且不报任何错——表现为"任务无声消失"。
"""
import concurrent.futures
import sqlite3
import threading

import pytest

from src.domain.models.task import Task, TaskCreate
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import (
    MAX_TASK_ID_ATTEMPTS,
    SqliteTaskRepository,
)


@pytest.fixture()
def repository(tmp_path):
    return SqliteTaskRepository(
        db_path=str(tmp_path / "app.sqlite3"),
        legacy_config_file=None,
    )


def make_task(name: str) -> Task:
    """按 TaskService.create_task 的方式构造实体（TaskCreate → Task）。"""
    payload = TaskCreate(
        task_name=name,
        keyword="sony a7m4",
        description="d",
        enabled=True,
        decision_mode="keyword",
        keyword_rules=["a7m4"],
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/macbook_criteria.txt",
    )
    return Task(**payload.model_dump(), is_running=False)


def test_sequential_creates_get_increasing_ids(repository):
    first = repository._save_sync(make_task("任务A"))
    second = repository._save_sync(make_task("任务B"))
    assert (first.id, second.id) == (0, 1)
    assert [t.task_name for t in repository._find_all_sync()] == ["任务A", "任务B"]


def test_update_does_not_create_a_duplicate_row(repository):
    created = repository._save_sync(make_task("任务A"))
    updated = created.model_copy(update={"keyword": "新的关键词"})
    repository._save_sync(updated)

    tasks = repository._find_all_sync()
    assert len(tasks) == 1, "更新不应该产生第二行"
    assert tasks[0].keyword == "新的关键词"


def test_update_reinserts_when_row_was_deleted(repository):
    """行被删除后保存同一 id，应回退为插入（保持原有 upsert 语义）。"""
    created = repository._save_sync(make_task("任务A"))
    repository._delete_sync(created.id)

    repository._save_sync(created)
    assert len(repository._find_all_sync()) == 1


def test_concurrent_creates_do_not_overwrite_each_other(repository):
    """核心回归：并发创建必须各自成行，不能互相覆盖。"""
    total = 12
    barrier = threading.Barrier(total)
    results = []
    errors = []

    def worker(index: int):
        # 让所有线程尽量同时进入保存逻辑，逼近"同时算 id"的窗口
        barrier.wait()
        try:
            task = repository._save_sync(make_task(f"并发任务{index}"))
            results.append(task.id)
        except Exception as exc:  # pragma: no cover - 失败时用于定位
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=total) as pool:
        list(pool.map(worker, range(total)))

    assert not errors, f"并发创建出现异常: {errors}"
    assert len(results) == total
    assert len(set(results)) == total, f"出现了重复 id（意味着互相覆盖）: {sorted(results)}"

    stored = repository._find_all_sync()
    assert len(stored) == total, f"期望 {total} 个任务，实际 {len(stored)} —— 有任务被覆盖"
    assert len({task.task_name for task in stored}) == total


def test_concurrent_creates_with_external_writer_are_ordered(repository, tmp_path):
    """外部进程也在写同一个库时，创建仍应成功（靠 BEGIN IMMEDIATE + busy_timeout）。"""
    db_path = repository.db_path
    repository._find_all_sync()  # 建库建表

    stop = threading.Event()

    def external_writer():
        while not stop.is_set():
            with sqlite_connection(db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO app_metadata(key, value) VALUES (?, ?)",
                    ("noise", "1"),
                )
                conn.commit()

    thread = threading.Thread(target=external_writer, daemon=True)
    thread.start()
    try:
        created = repository._save_sync(make_task("竞争任务"))
        assert created.id is not None
        assert any(t.task_name == "竞争任务" for t in repository._find_all_sync())
    finally:
        stop.set()
        thread.join(timeout=5)


def test_id_retry_limit_is_sane():
    assert MAX_TASK_ID_ATTEMPTS >= 2
