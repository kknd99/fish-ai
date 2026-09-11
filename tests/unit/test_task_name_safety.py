"""任务名安全回归测试。

对应安全审计 C1：任务名曾经无校验地拼进 ``images/task_images_<name>``，
使 ``task_name="x/../../victim"`` 能在任务结束时递归删除任意目录。
这些用例把当时的利用方式固定下来，防止将来被改回去。
"""
import pytest
from pydantic import ValidationError

from src.core.safe_paths import (
    UnsafePathError,
    is_safe_task_name,
    resolve_task_image_dir,
    resolve_within,
    validate_task_name,
)
from src.domain.models.task import TaskCreate, TaskUpdate

BASE_TASK = {
    "task_name": "Sony A7M4",
    "enabled": True,
    "keyword": "sony a7m4",
    "description": "body only",
    "decision_mode": "ai",
    "ai_prompt_base_file": "prompts/base_prompt.txt",
    "ai_prompt_criteria_file": "prompts/sony_criteria.txt",
}


def _payload(task_name):
    return {**BASE_TASK, "task_name": task_name}


@pytest.mark.parametrize(
    "bad_name",
    [
        "x/../../etc",          # C1 的原始利用载荷
        "../../x",
        "..",
        ".",
        "a/b",
        "a\\b",                  # Windows 分隔符
        "C:\\Windows\\System32",  # Windows 绝对路径
        "/etc/passwd",           # POSIX 绝对路径
        "task\x00name",          # 控制字符
        "   ",                   # 纯空白
        "",                      # 空
        "x" * 65,                # 超长
    ],
)
def test_task_create_rejects_unsafe_task_names(bad_name):
    with pytest.raises(ValidationError):
        TaskCreate(**_payload(bad_name))


@pytest.mark.parametrize(
    "good_name",
    ["MacBook Air M1", "索尼 A7M4 监控", "task-01", "任务_01", "a.b", "x" * 64],
)
def test_task_create_accepts_safe_task_names(good_name):
    task = TaskCreate(**_payload(good_name))
    assert task.task_name == good_name


def test_task_update_rejects_traversal_but_allows_none():
    with pytest.raises(ValidationError):
        TaskUpdate(task_name="x/../../y")
    assert TaskUpdate(task_name=None).task_name is None


def test_validate_task_name_strips_surrounding_space():
    assert validate_task_name("  Sony A7M4  ") == "Sony A7M4"


def test_is_safe_task_name_helper():
    assert is_safe_task_name("MacBook") is True
    assert is_safe_task_name("../etc") is False


def test_resolve_within_blocks_escape(tmp_path):
    (tmp_path / "images").mkdir()
    assert resolve_within(tmp_path / "images", "child").parent == tmp_path / "images"
    with pytest.raises(UnsafePathError):
        resolve_within(tmp_path / "images", "../outside")


def test_resolve_task_image_dir_refuses_escape(tmp_path):
    with pytest.raises(UnsafePathError):
        resolve_task_image_dir("evil/../../victim", base_dir=str(tmp_path / "images"))


def test_cleanup_task_images_refuses_traversal_and_keeps_victim(tmp_path, monkeypatch):
    """端到端复现 C1：受害者目录必须完好无损。"""
    monkeypatch.chdir(tmp_path)
    victim = tmp_path / "CANARY-VICTIM"
    (victim / "sub").mkdir(parents=True)
    (victim / "keep.txt").write_text("important", encoding="utf-8")
    (victim / "sub" / "also.txt").write_text("more", encoding="utf-8")

    # 爬虫会为这个任务名创建 images/task_images_evil（见 ai_handler.download_all_images）
    (tmp_path / "images" / "task_images_evil").mkdir(parents=True)

    from src.ai_handler import cleanup_task_images

    cleanup_task_images("evil/../../CANARY-VICTIM")

    assert victim.exists(), "C1 回归：受害者目录被删除了"
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "important"
    assert (victim / "sub" / "also.txt").exists()


def test_cleanup_task_images_still_deletes_its_own_directory(tmp_path, monkeypatch):
    """正向对照：合法任务名的清理功能不能被改坏。"""
    monkeypatch.chdir(tmp_path)
    own_dir = tmp_path / "images" / "task_images_good-task"
    own_dir.mkdir(parents=True)
    (own_dir / "img.jpg").write_bytes(b"x")

    from src.ai_handler import cleanup_task_images

    cleanup_task_images("good-task")

    assert not own_dir.exists()


def test_criteria_filename_guards(tmp_path, monkeypatch):
    """生成 criteria 的文件名守卫：拒绝退化关键词与系统保留文件。"""
    from src.services.task_generation_runner import build_criteria_filename

    assert build_criteria_filename("Sony A7M4") == "prompts/sony_a7m4_criteria.txt"

    # ".." / "!!!" 净化后为空 → 过去会共用 prompts/_criteria.txt 互相覆盖
    with pytest.raises(UnsafePathError):
        build_criteria_filename("..")
    with pytest.raises(UnsafePathError):
        build_criteria_filename("!!!")

    # 过去 keyword="macbook" 会覆盖 few-shot 参考文件，污染后续所有生成
    with pytest.raises(UnsafePathError):
        build_criteria_filename("macbook")

    # 注意：只有 criteria 命名空间内的冲突才可能发生；
    # "base prompt" 生成的是 base_prompt_criteria.txt（不冲突），应当放行。
    assert build_criteria_filename("base prompt") == "prompts/base_prompt_criteria.txt"
