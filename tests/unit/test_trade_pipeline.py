"""交易管线测试（L1 接入主流程）。

三层默认关闭，接上之后行为必须与从前完全一致：
1. 任务级 ``trade_enabled`` 默认 false → ``build_trade_runner`` 返回 None，整条链路不接；
2. 全局 ``TRADE_ENABLED`` 默认 false → 闸门拒绝；
3. 全局 ``TRADE_DRY_RUN`` 默认 true → 即便放行也只写审计、不产生外部动作。

本文件覆盖前两层与"接上之后真正发生了什么"，并用审计表作为最终证据。
"""
import asyncio

import pytest

from src.services.item_analysis_dispatcher import (
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)
from src.services.trade.audit import TradeAuditStore
from src.services.trade.models import PRICE_SOURCE_DETAIL, TradeOutcome
from src.services.trade.pipeline import (
    build_trade_intent,
    build_trade_runner,
    is_trade_enabled,
)


class FakeTradeSettings:
    """避免依赖 .env：直接给出闸门参数。"""

    def __init__(self, **overrides):
        self.enabled = True
        self.dry_run = True
        self.adapter = "dry_run"
        self.max_unit_price = 5000.0
        self.daily_budget = 8000.0
        self.max_orders_per_day = 2
        self.kill_switch_file = "/nonexistent/kill-switch-for-tests"
        self.allowed_sellers = None
        self.min_value_score = 0.0
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeAiSettings:
    skip_analysis = False


def make_record(**overrides):
    record = {
        "搜索关键字": "sony a7m4",
        "任务名称": "Sony A7M4",
        "爬取时间": "2026-01-01T10:00:00",
        "商品信息": {
            "商品ID": "item-1",
            "商品标题": "Sony A7M4 机身",
            "商品链接": "https://www.goofish.com/item?id=item-1",
            "当前售价": "¥3,800",
            "卖家昵称": "卖家A",
        },
        "卖家信息": {"卖家昵称": "卖家A"},
    }
    record.update(overrides)
    return record


def make_task_config(**overrides):
    config = {
        "task_name": "Sony A7M4",
        "keyword": "sony a7m4",
        "min_price": "1000",
        "max_price": "4000",
    }
    config.update(overrides)
    return config


def recommended(**overrides):
    payload = {"is_recommended": True, "reason": "合适", "analysis_source": "ai"}
    payload.update(overrides)
    return payload


def audit_rows(db_path):
    store = TradeAuditStore(db_path=str(db_path))
    return store.recent(limit=50)


# --------------------------------------------------------------- 开关判定


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        (None, False),
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("", False),
        (0, False),
    ],
)
def test_is_trade_enabled(raw, expected):
    assert is_trade_enabled({"trade_enabled": raw}) is expected


def test_is_trade_enabled_defaults_to_false():
    assert is_trade_enabled({}) is False
    assert is_trade_enabled(None) is False


# --------------------------------------------------------------- 意图构造


def test_build_trade_intent_maps_record_fields():
    intent = build_trade_intent(make_task_config(), make_record(), recommended())

    assert intent is not None
    assert intent.task_name == "Sony A7M4"
    assert intent.item_id == "item-1"
    assert intent.title == "Sony A7M4 机身"
    assert intent.price == pytest.approx(3800.0)          # "¥3,800" → 3800
    assert intent.seller == "卖家A"
    assert intent.decision_source == "ai"
    assert intent.price_source == PRICE_SOURCE_DETAIL      # 硬约定：详情页价格
    assert intent.task_min_price == pytest.approx(1000.0)
    assert intent.task_max_price == pytest.approx(4000.0)
    assert intent.evidence["is_recommended"] is True


def test_build_trade_intent_requires_an_identifier():
    """ID 与链接都为空时无法做幂等，直接不进入交易流程。"""
    record = make_record()
    record["商品信息"] = dict(record["商品信息"], **{"商品链接": ""})
    record["商品信息"]["商品ID"] = ""

    assert build_trade_intent(make_task_config(), record, recommended()) is None


def test_build_trade_intent_keeps_unparseable_price_for_the_gate():
    """价格解析不出来时以 0 进闸门 —— 审计表里要留下"被拒"的痕迹，而不是静默跳过。"""
    record = make_record()
    record["商品信息"] = dict(record["商品信息"], **{"当前售价": "价格异常"})

    intent = build_trade_intent(make_task_config(), record, recommended())
    assert intent is not None
    assert intent.price == 0.0


def test_build_trade_intent_tolerates_missing_task_bounds():
    config = make_task_config()
    config.pop("min_price")
    config.pop("max_price")

    intent = build_trade_intent(config, make_record(), recommended())
    assert intent.task_min_price is None
    assert intent.task_max_price is None


# --------------------------------------------------------------- runner


def test_runner_is_not_built_when_task_does_not_enable_trade(tmp_path):
    """默认部署完全不接：这是"接上之后行为不变"的第一层保证。"""
    assert build_trade_runner(make_task_config(), db_path=str(tmp_path / "a.sqlite3")) is None
    assert build_trade_runner(
        make_task_config(trade_enabled=False), db_path=str(tmp_path / "a.sqlite3")
    ) is None


def test_runner_skips_non_recommended_items(tmp_path):
    db = tmp_path / "app.sqlite3"
    runner = build_trade_runner(
        make_task_config(trade_enabled=True),
        db_path=str(db),
        trade_settings=FakeTradeSettings(),
        ai_settings=FakeAiSettings(),
    )
    assert runner is not None

    result = asyncio.run(runner(make_record(), recommended(is_recommended=False)))

    assert result is None
    assert audit_rows(db) == [], "未推荐的商品不应产生任何交易记录"


def test_runner_writes_audit_row_when_enabled_with_dry_run(tmp_path):
    """核心证据：启用后（演练模式）会产生一条 dry_run 审计行，但不产生外部动作。"""
    db = tmp_path / "app.sqlite3"
    runner = build_trade_runner(
        make_task_config(trade_enabled=True),
        db_path=str(db),
        trade_settings=FakeTradeSettings(dry_run=True),
        ai_settings=FakeAiSettings(),
    )

    result = asyncio.run(runner(make_record(), recommended()))

    assert result is not None
    assert result.outcome is TradeOutcome.DRY_RUN

    rows = audit_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "dry_run"
    assert row["allowed"] is True
    assert row["item_id"] == "item-1"
    assert row["price"] == pytest.approx(3800.0)
    assert row["checks"]["price_source"] == PRICE_SOURCE_DETAIL


def test_runner_blocks_when_global_trade_disabled(tmp_path):
    """第二层保证：任务开了但全局没开，闸门仍然拒绝。"""
    db = tmp_path / "app.sqlite3"
    runner = build_trade_runner(
        make_task_config(trade_enabled=True),
        db_path=str(db),
        trade_settings=FakeTradeSettings(enabled=False),
        ai_settings=FakeAiSettings(),
    )

    result = asyncio.run(runner(make_record(), recommended()))

    assert result.outcome is TradeOutcome.BLOCKED
    assert audit_rows(db)[0]["allowed"] is False


def test_runner_blocks_when_skip_ai_analysis_is_on(tmp_path):
    """SKIP_AI_ANALYSIS 与交易互斥（否则所有商品都会被判为推荐）。"""
    db = tmp_path / "app.sqlite3"
    runner = build_trade_runner(
        make_task_config(trade_enabled=True),
        db_path=str(db),
        trade_settings=FakeTradeSettings(),
        ai_settings=type("AI", (), {"skip_analysis": True})(),
    )

    result = asyncio.run(runner(make_record(), recommended()))

    assert result.outcome is TradeOutcome.BLOCKED
    assert "SKIP_AI_ANALYSIS" in " ".join(audit_rows(db)[0]["reasons"])


def test_runner_uses_task_level_trade_action(tmp_path):
    """任务级动作覆盖全局 TRADE_ADAPTER。"""
    db = tmp_path / "app.sqlite3"
    runner = build_trade_runner(
        make_task_config(trade_enabled=True, trade_action="dry_run"),
        db_path=str(db),
        trade_settings=FakeTradeSettings(adapter="notify_link"),
        ai_settings=FakeAiSettings(),
    )

    result = asyncio.run(runner(make_record(), recommended()))
    assert result.adapter == "dry_run"


# --------------------------------------------------------------- dispatcher 接线


def make_job(**overrides):
    payload = {
        "keyword": "sony a7m4",
        "task_name": "Sony A7M4",
        "decision_mode": "keyword",
        "analyze_images": False,
        "prompt_text": "",
        "keyword_rules": ("sony",),
        "final_record": make_record(),
        "seller_id": None,
        "zhima_credit_text": None,
        "registration_duration_text": "",
    }
    payload.update(overrides)
    return ItemAnalysisJob(**payload)


def build_dispatcher(**overrides):
    async def seller_loader(user_id):
        return {}

    async def image_downloader(*args):
        return []

    async def ai_analyzer(*args):
        return None

    async def notifier(*args):
        return None

    async def saver(*args):
        return True

    kwargs = {
        "concurrency": 1,
        "skip_ai_analysis": False,
        "seller_loader": seller_loader,
        "image_downloader": image_downloader,
        "ai_analyzer": ai_analyzer,
        "notifier": notifier,
        "saver": saver,
    }
    kwargs.update(overrides)
    return ItemAnalysisDispatcher(**kwargs)


def test_dispatcher_does_not_touch_trade_by_default():
    """不传 trade_runner 时，交易流程完全不存在（默认行为不变）。"""
    dispatcher = build_dispatcher()
    job = make_job()

    asyncio.run(dispatcher._process_job(job))

    assert dispatcher.completed_count == 1
    assert dispatcher._trade_runner is None


def test_dispatcher_calls_trade_runner_after_saving_and_notifying():
    calls = []

    async def runner(record, analysis):
        calls.append((record.get("商品信息", {}).get("商品ID"), analysis.get("is_recommended")))

    dispatcher = build_dispatcher(trade_runner=runner)
    asyncio.run(dispatcher._process_job(make_job()))

    assert calls == [("item-1", True)]


def test_dispatcher_survives_a_broken_trade_runner(capsys):
    """交易侧出错不能把一次成功的抓取变成失败。"""

    async def boom(record, analysis):
        raise RuntimeError("交易侧炸了")

    dispatcher = build_dispatcher(trade_runner=boom)
    asyncio.run(dispatcher._process_job(make_job()))

    assert dispatcher.completed_count == 1
    assert "交易流程异常" in capsys.readouterr().out


# --------------------------------------------------------------- 端到端：审计落库


def test_end_to_end_default_off_produces_no_trade_rows(tmp_path):
    """默认配置（任务未开启交易）跑完整流程：审计表必须为空。"""
    db = tmp_path / "app.sqlite3"
    task_config = make_task_config()  # 不带 trade_enabled

    runner = build_trade_runner(task_config, db_path=str(db))
    dispatcher = build_dispatcher(trade_runner=runner)
    asyncio.run(dispatcher._process_job(make_job()))

    assert runner is None
    assert audit_rows(db) == []


def test_end_to_end_enabled_writes_exactly_one_row(tmp_path):
    db = tmp_path / "app.sqlite3"
    runner = build_trade_runner(
        make_task_config(trade_enabled=True),
        db_path=str(db),
        trade_settings=FakeTradeSettings(),
        ai_settings=FakeAiSettings(),
    )
    dispatcher = build_dispatcher(trade_runner=runner)

    asyncio.run(dispatcher._process_job(make_job()))

    rows = audit_rows(db)
    assert len(rows) == 1
    assert rows[0]["task_name"] == "Sony A7M4"
    assert rows[0]["checks"]["price_source"] == PRICE_SOURCE_DETAIL
