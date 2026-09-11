"""判定策略注册表测试（P2 解耦）。

背景：``decision_mode`` 此前写死成 ``Literal["ai","keyword"]``，分派逻辑散在四处
（``spider_v2`` 决定是否加载 prompt、``scraper`` 归一化、``dispatcher`` 二选一、
``task.py`` 校验）。结果是"新增一种判定方式"要同时改四个文件，最容易漏改。

这里验证注册表接管之后的核心承诺：**新增策略 = 实现 + 注册**，其余四处自动跟随。
"""
import pytest
from pydantic import ValidationError

from src.domain.models.task import TaskCreate, TaskGenerateRequest, TaskUpdate
from src.services.decision import (
    DEFAULT_STRATEGY,
    DecisionContext,
    DecisionServices,
    StrategyMeta,
    available_strategies,
    get_strategy,
    is_registered,
    normalize_decision_mode,
    register_strategy,
    strategy_meta,
    strategy_names,
    strategy_requires_prompt,
    unregister_strategy,
)
from src.services.decision.ai_strategy import AIDecisionStrategy


# --------------------------------------------------------------- 注册表本身


def test_builtin_strategies_are_registered():
    assert set(strategy_names()) >= {"ai", "keyword"}
    assert is_registered("ai") and is_registered("keyword")
    assert is_registered("AI") is True  # 大小写不敏感
    assert is_registered("不存在的策略") is False


def test_normalize_decision_mode():
    assert normalize_decision_mode("ai") == "ai"
    assert normalize_decision_mode("KEYWORD") == "keyword"
    assert normalize_decision_mode("  ai  ") == "ai"
    # 未知值回退默认（与历史"非法值按 ai 处理"一致）
    assert normalize_decision_mode("bogus") == DEFAULT_STRATEGY
    assert normalize_decision_mode(None) == DEFAULT_STRATEGY
    assert normalize_decision_mode("") == DEFAULT_STRATEGY


def test_strategy_meta_declares_requirements():
    ai_meta = strategy_meta("ai")
    assert ai_meta.requires_prompt is True
    assert ai_meta.requires_description is True
    assert ai_meta.requires_keyword_rules is False

    keyword_meta = strategy_meta("keyword")
    assert keyword_meta.requires_prompt is False
    assert keyword_meta.requires_description is False
    assert keyword_meta.requires_keyword_rules is True


def test_strategy_requires_prompt_drives_prompt_loading():
    """spider_v2 用它决定是否读取 prompt 文件，替代写死的 "keyword" 判断。"""
    assert strategy_requires_prompt("ai") is True
    assert strategy_requires_prompt("keyword") is False
    # 未知值按默认策略处理
    assert strategy_requires_prompt("bogus") is True


def test_available_strategies_exposes_metadata():
    metas = available_strategies()
    assert all(isinstance(meta, StrategyMeta) for meta in metas)
    assert {meta.name for meta in metas} >= {"ai", "keyword"}


def test_register_rejects_strategy_without_meta():
    class Broken:
        pass

    with pytest.raises(ValueError):
        register_strategy(Broken())


# --------------------------------------------------------------- 可扩展性


class AlwaysRecommendStrategy:
    """测试用策略：永远推荐（用来证明"只需注册"）。"""

    meta = StrategyMeta(
        name="always",
        display_name="总是推荐",
        requires_prompt=False,
        requires_description=False,
        requires_keyword_rules=False,
    )

    async def analyze(self, record, context):
        return {
            "analysis_source": "always",
            "is_recommended": True,
            "reason": "测试策略",
            "keyword_hit_count": 0,
        }


@pytest.fixture()
def always_strategy():
    register_strategy(AlwaysRecommendStrategy())
    yield "always"
    unregister_strategy("always")


def test_new_strategy_becomes_available_everywhere(always_strategy):
    """核心承诺：注册之后，归一化 / prompt 判定 / 输入校验 / 分派都自动认它。"""
    import asyncio

    # 1) 归一化与注册查询
    assert normalize_decision_mode("always") == "always"
    assert strategy_requires_prompt("always") is False

    # 2) 输入校验：不需要 description、不需要关键词规则
    task = TaskCreate(
        task_name="自定义判定",
        keyword="k",
        decision_mode="always",
        ai_prompt_base_file="prompts/base_prompt.txt",
        ai_prompt_criteria_file="prompts/macbook_criteria.txt",
    )
    assert task.decision_mode == "always"

    # 3) 分派：dispatcher 走注册表，因此新策略直接被使用
    from src.services.item_analysis_dispatcher import ItemAnalysisDispatcher

    dispatcher = ItemAnalysisDispatcher(
        concurrency=1,
        skip_ai_analysis=False,
        seller_loader=lambda uid: {},
        image_downloader=lambda *args: [],
        ai_analyzer=lambda *args: None,
        notifier=lambda *args: None,
        saver=lambda *args: True,
    )
    result = asyncio.run(
        dispatcher._build_analysis_result(
            _job("always"), {"商品信息": {"商品ID": "1"}, "卖家信息": {}}
        )
    )
    assert result["analysis_source"] == "always"
    assert result["is_recommended"] is True


def _job(decision_mode: str):
    from src.services.item_analysis_dispatcher import ItemAnalysisJob

    return ItemAnalysisJob(
        keyword="k",
        task_name="demo",
        decision_mode=decision_mode,
        analyze_images=False,
        prompt_text="",
        keyword_rules=(),
        final_record={},
        seller_id=None,
        zhima_credit_text=None,
        registration_duration_text="",
    )


def test_unregistered_mode_is_rejected_at_api_boundary():
    """API 边界保持严格：写错的判定方式要报错，而不是静默按 AI 跑。"""
    with pytest.raises(ValidationError):
        TaskCreate(
            task_name="拼错的模式",
            keyword="k",
            description="d",
            decision_mode="kwrod",  # 拼写错误
            ai_prompt_base_file="prompts/base_prompt.txt",
            ai_prompt_criteria_file="prompts/macbook_criteria.txt",
        )
    with pytest.raises(ValidationError):
        TaskUpdate(decision_mode="nope")
    with pytest.raises(ValidationError):
        TaskGenerateRequest(task_name="t", keyword="k", description="d", decision_mode="nope")


def test_business_rules_follow_strategy_meta():
    """校验规则随策略走：AI 要求详细需求，关键词模式要求关键词规则。"""
    with pytest.raises(ValidationError) as ai_error:
        TaskCreate(
            task_name="缺描述",
            keyword="k",
            decision_mode="ai",
            ai_prompt_base_file="prompts/base_prompt.txt",
            ai_prompt_criteria_file="prompts/macbook_criteria.txt",
        )
    assert "AI 判断" in str(ai_error.value)

    with pytest.raises(ValidationError) as keyword_error:
        TaskCreate(
            task_name="缺规则",
            keyword="k",
            decision_mode="keyword",
            keyword_rules=[],
            ai_prompt_base_file="prompts/base_prompt.txt",
            ai_prompt_criteria_file="prompts/macbook_criteria.txt",
        )
    assert "关键词判断" in str(keyword_error.value)
