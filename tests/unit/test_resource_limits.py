"""资源上限测试（P1：并发与页数封顶）。

这些旋钮可经未认证/低权限的任务接口设置，而它们直接决定 LLM 花费与抓取量
（= 风控暴露面）。历史实现只做 ``max(1, n)``，没有上限。
"""
import pytest
from pydantic import ValidationError

from src.ai_handler import (
    DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY,
    MAX_IMAGE_DOWNLOAD_CONCURRENCY,
)
from src.domain.models.task import TaskCreate, TaskGenerateRequest, TaskUpdate
from src.scraper import (
    MAX_AI_ANALYSIS_CONCURRENCY,
    MAX_TASK_PAGES,
    _get_ai_analysis_concurrency,
    _get_max_pages,
)

BASE_TASK = {
    "task_name": "Sony A7M4",
    "keyword": "sony a7m4",
    "description": "body only",
    "decision_mode": "keyword",
    "keyword_rules": ["a7m4"],
    "ai_prompt_base_file": "prompts/base_prompt.txt",
    "ai_prompt_criteria_file": "prompts/macbook_criteria.txt",
}


# --------------------------------------------------------------- max_pages


@pytest.mark.parametrize("value", [0, -1, 21, 100, 100000])
def test_task_create_rejects_out_of_range_max_pages(value):
    with pytest.raises(ValidationError):
        TaskCreate(**{**BASE_TASK, "max_pages": value})


@pytest.mark.parametrize("value", [1, 3, 20])
def test_task_create_accepts_reasonable_max_pages(value):
    assert TaskCreate(**{**BASE_TASK, "max_pages": value}).max_pages == value


def test_task_update_and_generate_are_bounded_too():
    with pytest.raises(ValidationError):
        TaskUpdate(max_pages=999)
    with pytest.raises(ValidationError):
        TaskGenerateRequest(task_name="t", keyword="k", max_pages=999)
    assert TaskUpdate(max_pages=5).max_pages == 5


def test_scraper_clamps_max_pages_for_legacy_rows():
    """历史数据库里可能存在超大值，运行时必须再夹一次。"""
    assert _get_max_pages({"max_pages": 9999}) == MAX_TASK_PAGES
    assert _get_max_pages({"max_pages": 3}) == 3
    assert _get_max_pages({}) == 1
    assert _get_max_pages({"max_pages": 0}) == 1
    assert _get_max_pages({"max_pages": "abc"}) == 1


# --------------------------------------------------------------- 并发上限


def test_ai_analysis_concurrency_is_capped():
    assert _get_ai_analysis_concurrency({"ai_analysis_concurrency": 999}) == MAX_AI_ANALYSIS_CONCURRENCY
    assert _get_ai_analysis_concurrency({"ai_analysis_concurrency": 3}) == 3
    # 低于 1 的值抬到 1
    assert _get_ai_analysis_concurrency({"ai_analysis_concurrency": 0}) == 1


def test_ai_analysis_concurrency_env_override_is_capped(monkeypatch):
    monkeypatch.setenv("AI_ANALYSIS_CONCURRENCY", "500")
    assert _get_ai_analysis_concurrency({}) == MAX_AI_ANALYSIS_CONCURRENCY


def test_image_download_concurrency_default_is_within_cap():
    assert 1 <= DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY <= MAX_IMAGE_DOWNLOAD_CONCURRENCY
