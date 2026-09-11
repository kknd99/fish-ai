"""路径收口测试（安全审计 H2 / M2 / account_state_file）。

对应漏洞：``ai_prompt_base_file`` / ``ai_prompt_criteria_file`` / ``ai_prompt_file``
与 ``account_state_file`` 都是用户可控字符串，会被爬虫子进程直接 ``open()``——
prompt 内容随模型请求发往 ``OPENAI_BASE_URL``，账号文件被当作 Playwright 的
``storage_state``。因此这些值必须被限制在 ``prompts/`` 与账号目录内。
"""
import pytest
from pydantic import ValidationError

from src.core.safe_paths import (
    UnsafePathError,
    safe_account_state_path,
    safe_prompt_path,
    validate_account_state_dir,
    validate_account_state_reference,
    validate_prompt_reference,
)
from src.domain.models.task import TaskCreate, TaskUpdate

BASE_TASK = {
    "task_name": "Sony A7M4",
    "keyword": "sony a7m4",
    "description": "body only",
    "decision_mode": "ai",
}


# --------------------------------------------------------------- prompt 路径


@pytest.mark.parametrize(
    "good",
    ["prompts/base_prompt.txt", "base_prompt.txt", "prompts/sony_a7m4_criteria.txt"],
)
def test_safe_prompt_path_accepts_prompts_dir(good):
    resolved = safe_prompt_path(good)
    assert resolved.name.endswith(".txt")
    assert "prompts" in resolved.parts


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/etc/hosts",
        "../../.env",
        "../prompts/base_prompt.txt",
        "prompts/../../.env",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "\\\\server\\share\\x.txt",
        "",
        "   ",
        "prompts",
        "prompts/",
        ".",
    ],
)
def test_safe_prompt_path_rejects_arbitrary_paths(bad):
    with pytest.raises(UnsafePathError):
        safe_prompt_path(bad)


def test_prompt_subdirectory_inside_prompts_is_allowed():
    """containment 才是契约：prompts/ 内的子目录属于合法范围。"""
    assert safe_prompt_path("prompts/archive/old.txt").name == "old.txt"


def test_task_create_rejects_absolute_prompt_paths():
    """原始 PoC 形态：把 prompt 指向系统文件。"""
    with pytest.raises(ValidationError):
        TaskCreate(**{**BASE_TASK, "ai_prompt_base_file": "/etc/passwd"})
    with pytest.raises(ValidationError):
        TaskCreate(**{**BASE_TASK, "ai_prompt_base_file": "prompts/base_prompt.txt",
                      "ai_prompt_criteria_file": "../../.env"})
    with pytest.raises(ValidationError):
        TaskUpdate(ai_prompt_criteria_file="/etc/passwd")


def test_task_create_accepts_normal_prompt_paths():
    task = TaskCreate(**{**BASE_TASK, "ai_prompt_base_file": "prompts/base_prompt.txt",
                         "ai_prompt_criteria_file": "prompts/macbook_criteria.txt"})
    assert task.ai_prompt_criteria_file == "prompts/macbook_criteria.txt"


def test_empty_criteria_is_left_to_business_validation():
    """空值不在这里报错——"AI 模式必须给出判断标准"由业务校验负责。"""
    task = TaskCreate(**{**BASE_TASK, "ai_prompt_base_file": "prompts/base_prompt.txt",
                         "ai_prompt_criteria_file": ""})
    assert task.ai_prompt_criteria_file == ""
    assert validate_prompt_reference("prompts/base_prompt.txt") == "prompts/base_prompt.txt"


# --------------------------------------------------------------- 账号登录态路径


@pytest.mark.parametrize(
    "good",
    ["xianyu_state.json", "state/acc_1.json", "acc_1.json"],
)
def test_safe_account_state_path_accepts_controlled_locations(good):
    resolved = safe_account_state_path(good)
    assert resolved.name == "xianyu_state.json" or resolved.parent.name == "state"


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/home/user/.config/credentials.json",
        "../../secrets.json",
        "state/../../etc/hosts",
        "C:\\Users\\x\\state.json",
        "",
        "state",
    ],
)
def test_safe_account_state_path_rejects_arbitrary_paths(bad):
    with pytest.raises(UnsafePathError):
        safe_account_state_path(bad)


def test_task_create_rejects_arbitrary_account_state_file():
    with pytest.raises(ValidationError):
        TaskCreate(**{**BASE_TASK, "account_state_file": "/etc/passwd"})
    with pytest.raises(ValidationError):
        TaskUpdate(account_state_file="../../etc/hosts")


def test_task_create_accepts_state_dir_account():
    task = TaskCreate(**{**BASE_TASK, "account_state_file": "state/acc_1.json"})
    assert task.account_state_file == "state/acc_1.json"
    assert validate_account_state_reference("state/acc_1.json") == "state/acc_1.json"


# --------------------------------------------------------------- 账号目录设置


@pytest.mark.parametrize("good", ["state", "state/accounts", "accounts"])
def test_validate_account_state_dir_accepts_relative(good):
    assert validate_account_state_dir(good) == good


@pytest.mark.parametrize(
    "bad",
    ["/tmp", "/home/app/.config", "../outside", "state/../../etc", "", "C:\\state"],
)
def test_validate_account_state_dir_rejects_absolute_and_traversal(bad):
    with pytest.raises(UnsafePathError):
        validate_account_state_dir(bad)
