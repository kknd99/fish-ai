"""判定策略行为测试（P2：AI / 关键词两种策略）。

这些行为自 ``item_analysis_dispatcher`` 平移而来，返回结构必须保持原样，
因为通知、入库与交易闸门都读 ``is_recommended`` / ``reason`` /
``analysis_source`` / ``keyword_hit_count``。
"""
import asyncio

import pytest

from src.services.decision import DecisionContext, DecisionServices, get_strategy
from src.services.decision.ai_strategy import AIDecisionStrategy
from src.services.decision.keyword_strategy import KeywordDecisionStrategy


def make_services(*, analyzer=None, images=None, skip=False, cleanup=None):
    async def download_images(record):
        return list(images or [])

    async def default_analyzer(record, image_paths, prompt_text):
        return {
            "is_recommended": True,
            "reason": "看起来不错",
            "prompt_version": "v1",
        }

    return DecisionServices(
        download_images=download_images,
        cleanup_images=cleanup or (lambda paths: None),
        ai_analyzer=analyzer or default_analyzer,
        skip_ai_analysis=skip,
    )


def context(services, **overrides):
    payload = {
        "task_name": "demo",
        "prompt_text": "只接受个人卖家。",
        "keyword_rules": ("sony", "a7m4"),
        "services": services,
    }
    payload.update(overrides)
    return DecisionContext(**payload)


RECORD = {
    "商品信息": {"商品ID": "1", "商品标题": "Sony A7M4 机身", "当前售价": "3000"},
    "卖家信息": {"卖家昵称": "卖家A"},
}


# --------------------------------------------------------------- 关键词策略


def test_keyword_strategy_recommends_on_hit():
    result = asyncio.run(
        KeywordDecisionStrategy().analyze(RECORD, context(make_services()))
    )
    assert result["analysis_source"] == "keyword"
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] >= 2


def test_keyword_strategy_rejects_on_miss():
    result = asyncio.run(
        KeywordDecisionStrategy().analyze(
            RECORD, context(make_services(), keyword_rules=("尼康", "canon"))
        )
    )
    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


def test_keyword_strategy_never_calls_the_model():
    called = []

    async def analyzer(*args):
        called.append(args)
        return None

    asyncio.run(
        KeywordDecisionStrategy().analyze(
            RECORD, context(make_services(analyzer=analyzer))
        )
    )
    assert called == [], "关键词模式不应调用模型"


# --------------------------------------------------------------- AI 策略


def test_ai_strategy_passes_through_success():
    result = asyncio.run(AIDecisionStrategy().analyze(RECORD, context(make_services())))
    assert result["is_recommended"] is True
    assert result["reason"] == "看起来不错"
    assert result["analysis_source"] == "ai"       # 缺省补齐
    assert result["keyword_hit_count"] == 0        # 缺省补齐
    assert result["prompt_version"] == "v1"        # 模型字段保留


def test_ai_strategy_uses_prompt_and_images():
    seen = {}

    async def analyzer(record, image_paths, prompt_text):
        seen["images"] = image_paths
        seen["prompt"] = prompt_text
        return {"is_recommended": True, "reason": "ok"}

    asyncio.run(
        AIDecisionStrategy().analyze(
            RECORD,
            context(make_services(analyzer=analyzer, images=["a.jpg", "b.jpg"])),
        )
    )
    assert seen["images"] == ["a.jpg", "b.jpg"]
    assert seen["prompt"] == "只接受个人卖家。"


def test_ai_strategy_fails_closed_without_prompt():
    result = asyncio.run(
        AIDecisionStrategy().analyze(RECORD, context(make_services(), prompt_text=""))
    )
    assert result["is_recommended"] is False
    assert "prompt" in result["reason"]


def test_ai_strategy_fails_closed_when_model_returns_none():
    async def analyzer(*args):
        return None

    result = asyncio.run(
        AIDecisionStrategy().analyze(RECORD, context(make_services(analyzer=analyzer)))
    )
    assert result["is_recommended"] is False
    assert result["error"] == "AI analysis returned None after retries."


def test_ai_strategy_fails_closed_on_exception():
    async def analyzer(*args):
        raise RuntimeError("模型网关 500")

    result = asyncio.run(
        AIDecisionStrategy().analyze(RECORD, context(make_services(analyzer=analyzer)))
    )
    assert result["is_recommended"] is False
    assert "模型网关 500" in result["error"]


def test_ai_strategy_cleans_up_images_even_on_failure():
    cleaned = []

    async def analyzer(*args):
        raise RuntimeError("boom")

    services = make_services(analyzer=analyzer, images=["tmp1.jpg"])
    services = DecisionServices(
        download_images=services.download_images,
        cleanup_images=cleaned.append,
        ai_analyzer=services.ai_analyzer,
        skip_ai_analysis=False,
    )
    asyncio.run(AIDecisionStrategy().analyze(RECORD, context(services)))
    assert cleaned == [["tmp1.jpg"]], "失败路径也必须清理临时图片"


def test_ai_strategy_skip_flag_recommends_everything():
    """既有语义：SKIP_AI_ANALYSIS=true 时直接判定为推荐。

    注意这与交易互斥（由资金闸门拦住），策略层保持不变以兼容历史行为。
    """
    result = asyncio.run(
        AIDecisionStrategy().analyze(RECORD, context(make_services(skip=True)))
    )
    assert result["is_recommended"] is True
    assert result["analysis_source"] == "ai"
    assert "跳过AI分析" in result["reason"]


def test_ai_strategy_without_services_is_safe():
    result = asyncio.run(
        AIDecisionStrategy().analyze(RECORD, DecisionContext(task_name="demo"))
    )
    assert result["is_recommended"] is False


# --------------------------------------------------------------- 通过注册表取用


@pytest.mark.parametrize(
    "name,expected",
    [("ai", AIDecisionStrategy), ("keyword", KeywordDecisionStrategy), ("AI", AIDecisionStrategy)],
)
def test_registry_resolves_expected_strategy(name, expected):
    assert isinstance(get_strategy(name), expected)
