"""资金闸门测试。

闸门是"自动下单"唯一的确定性防线（判定来自 LLM，而 LLM 的输入含卖家可控文本），
因此这里逐条覆盖限额规则，并专门验证 **fail-closed**：任何异常/未配置都必须是拒绝。
"""
from types import SimpleNamespace

import pytest

from src.services.trade.models import PRICE_SOURCE_DETAIL, TradeIntent
from src.services.trade.risk_gate import TradeLimits, TradeRiskGate


def make_intent(price=100.0, seller="卖家A", **kwargs):
    payload = {
        "task_name": "Sony A7M4",
        "item_id": "item-1",
        "title": "Sony A7M4 机身",
        "price": price,
        "link": "https://www.goofish.com/item?id=item-1&spm=xyz",
        "seller": seller,
        "decision_source": "ai",
        # 默认给出可信价格来源，否则会被"来源不可信"这条 fail-closed 规则拦下
        "price_source": PRICE_SOURCE_DETAIL,
        "evidence": {"is_recommended": True, "value_score": 80},
    }
    payload.update(kwargs)
    return TradeIntent(**payload)


def make_limits(**overrides):
    base = dict(
        enabled=True,
        dry_run=True,
        max_unit_price=1000.0,
        daily_budget=2000.0,
        max_orders_per_day=2,
        kill_switch_file="/nonexistent/kill-switch-for-tests",
        allowed_sellers=(),
        min_value_score=0.0,
    )
    base.update(overrides)
    return TradeLimits(**base)


def test_allows_when_every_limit_is_satisfied():
    verdict = TradeRiskGate(make_limits()).evaluate(make_intent())
    assert verdict.allowed is True
    assert verdict.reasons == ()


def test_disabled_feature_blocks():
    verdict = TradeRiskGate(make_limits(enabled=False)).evaluate(make_intent())
    assert verdict.allowed is False
    assert any("未启用" in reason for reason in verdict.reasons)


def test_kill_switch_blocks(tmp_path):
    switch = tmp_path / "TRADE_KILL_SWITCH"
    switch.write_text("stop", encoding="utf-8")
    verdict = TradeRiskGate(make_limits(kill_switch_file=str(switch))).evaluate(make_intent())
    assert verdict.allowed is False
    assert verdict.checks["kill_switch_active"] is True


def test_unset_price_ceiling_blocks_instead_of_allowing_everything():
    """未配置上限 ≠ 不限额。"""
    verdict = TradeRiskGate(make_limits(max_unit_price=0.0)).evaluate(make_intent())
    assert verdict.allowed is False
    assert any("单件价格上限" in reason for reason in verdict.reasons)


def test_unit_price_over_limit_blocks():
    verdict = TradeRiskGate(make_limits(max_unit_price=50.0)).evaluate(make_intent(price=100.0))
    assert verdict.allowed is False
    assert any("超过上限" in reason for reason in verdict.reasons)


def test_unparseable_price_blocks():
    verdict = TradeRiskGate(make_limits()).evaluate(make_intent(price="¥abc"))
    assert verdict.allowed is False


def test_zero_or_negative_price_blocks():
    verdict = TradeRiskGate(make_limits()).evaluate(make_intent(price=0.0))
    assert verdict.allowed is False
    assert any("价格异常" in reason for reason in verdict.reasons)


def test_daily_budget_accounts_for_already_spent_amount():
    gate = TradeRiskGate(make_limits(daily_budget=150.0))
    ok = gate.evaluate(make_intent(price=100.0), spent_today=40.0)
    assert ok.allowed is True
    blocked = gate.evaluate(make_intent(price=100.0), spent_today=60.0)
    assert blocked.allowed is False
    assert blocked.checks["projected_daily_spend"] == pytest.approx(160.0)


def test_unset_daily_budget_blocks():
    verdict = TradeRiskGate(make_limits(daily_budget=0.0)).evaluate(make_intent())
    assert verdict.allowed is False
    assert any("每日预算" in reason for reason in verdict.reasons)


def test_order_count_limit_blocks():
    gate = TradeRiskGate(make_limits(max_orders_per_day=1))
    assert gate.evaluate(make_intent(), orders_today=0).allowed is True
    assert gate.evaluate(make_intent(), orders_today=1).allowed is False


def test_zero_order_limit_blocks_everything():
    verdict = TradeRiskGate(make_limits(max_orders_per_day=0)).evaluate(make_intent())
    assert verdict.allowed is False
    assert any("每日下单数量上限" in reason for reason in verdict.reasons)


def test_duplicate_blocks():
    verdict = TradeRiskGate(make_limits()).evaluate(make_intent(), duplicate=True)
    assert verdict.allowed is False
    assert any("重复" in reason for reason in verdict.reasons)


def test_seller_whitelist():
    gate = TradeRiskGate(make_limits(allowed_sellers=("卖家A",)))
    assert gate.evaluate(make_intent(seller="卖家A")).allowed is True
    assert gate.evaluate(make_intent(seller="卖家B")).allowed is False


def test_value_score_floor():
    gate = TradeRiskGate(make_limits(min_value_score=70.0))
    assert gate.evaluate(make_intent()).allowed is True  # evidence.value_score = 80
    low = make_intent(evidence={"is_recommended": True, "value_score": 10})
    assert gate.evaluate(low).allowed is False
    # 缺字段 → 拒绝（fail-closed），而不是放行
    missing = make_intent(evidence={"is_recommended": True})
    assert gate.evaluate(missing).allowed is False


def test_gate_is_fail_closed_on_internal_error(monkeypatch):
    gate = TradeRiskGate(make_limits())

    def boom(*args, **kwargs):
        raise RuntimeError("闸门内部炸了")

    monkeypatch.setattr(gate, "_check_price", boom)
    verdict = gate.evaluate(make_intent())
    assert verdict.allowed is False
    assert any("闸门内部错误" in reason for reason in verdict.reasons)


def test_verdict_records_limits_snapshot_for_audit():
    verdict = TradeRiskGate(make_limits()).evaluate(make_intent())
    assert verdict.checks["limits"]["max_unit_price"] == 1000.0
    assert verdict.checks["price"] == 100.0


def test_limits_from_settings_parses_seller_list():
    fake = SimpleNamespace(
        enabled=True,
        dry_run=False,
        max_unit_price=500,
        daily_budget=1000,
        max_orders_per_day=3,
        kill_switch_file="KS",
        allowed_sellers=" 卖家A , 卖家B ,, ",
        min_value_score=60,
    )
    limits = TradeLimits.from_settings(fake)
    assert limits.allowed_sellers == ("卖家A", "卖家B")
    assert limits.max_orders_per_day == 3
    assert limits.dry_run is False


# ------------------------------------------------- 价格来源（P1：来源唯一）

def test_price_from_search_listing_is_rejected():
    """列表页摘要价不可信：可能是"低价引流、点进去改价"。"""
    verdict = TradeRiskGate(make_limits()).evaluate(
        make_intent(price_source="search")
    )
    assert verdict.allowed is False
    assert any("价格来源不可信" in reason for reason in verdict.reasons)


def test_unknown_price_source_is_rejected_by_default():
    """fail-closed：来源不明就不放行。"""
    verdict = TradeRiskGate(make_limits()).evaluate(
        make_intent(price_source="unknown")
    )
    assert verdict.allowed is False


def test_detail_price_source_is_accepted():
    verdict = TradeRiskGate(make_limits()).evaluate(
        make_intent(price_source=PRICE_SOURCE_DETAIL)
    )
    assert verdict.allowed is True
    assert verdict.checks["price_source"] == PRICE_SOURCE_DETAIL


# ------------------------------------------------- 任务价格区间二次夹取

def test_price_above_task_bounds_is_rejected():
    verdict = TradeRiskGate(make_limits()).evaluate(
        make_intent(price=900.0, task_min_price=100.0, task_max_price=500.0)
    )
    assert verdict.allowed is False
    assert any("超出任务上限" in reason for reason in verdict.reasons)


def test_price_below_task_bounds_is_rejected():
    verdict = TradeRiskGate(make_limits()).evaluate(
        make_intent(price=50.0, task_min_price=100.0, task_max_price=500.0)
    )
    assert verdict.allowed is False
    assert any("低于任务下限" in reason for reason in verdict.reasons)


def test_price_inside_task_bounds_is_accepted():
    verdict = TradeRiskGate(make_limits()).evaluate(
        make_intent(price=300.0, task_min_price=100.0, task_max_price=500.0)
    )
    assert verdict.allowed is True


def test_absent_task_bounds_do_not_block():
    """任务没配区间时不因此拦下——金额上限仍由闸门自身的上限负责。"""
    verdict = TradeRiskGate(make_limits()).evaluate(make_intent(price=300.0))
    assert verdict.allowed is True


# ------------------------------------------------- SKIP_AI_ANALYSIS 互斥

def test_skip_ai_analysis_blocks_trading():
    """SKIP_AI_ANALYSIS=true 会让所有商品都被判为推荐，与花钱互斥。"""
    verdict = TradeRiskGate(make_limits(ai_analysis_disabled=True)).evaluate(make_intent())
    assert verdict.allowed is False
    assert any("SKIP_AI_ANALYSIS" in reason for reason in verdict.reasons)


def test_limits_read_skip_analysis_from_ai_settings():
    trade = SimpleNamespace(
        enabled=True, dry_run=False, max_unit_price=500, daily_budget=1000,
        max_orders_per_day=3, kill_switch_file="KS", allowed_sellers=None,
        min_value_score=0,
    )
    limits_on = TradeLimits.from_settings(trade, SimpleNamespace(skip_analysis=True))
    limits_off = TradeLimits.from_settings(trade, SimpleNamespace(skip_analysis=False))
    assert limits_on.ai_analysis_disabled is True
    assert limits_off.ai_analysis_disabled is False
