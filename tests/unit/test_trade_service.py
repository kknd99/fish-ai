"""交易服务与审计测试。

重点验证三件事：
1. 演练模式下**绝不会**调用适配器（不会有钱的动作）；
2. 幂等：已经成交过的商品再次进入时被判为 DUPLICATE，不会重复下单；
3. 每一次尝试（含被拒、失败）都留下审计行，且 ``execute`` 永不抛异常。

按本仓库约定，异步逻辑用 ``asyncio.run`` 驱动，不依赖 pytest-asyncio。
"""
import asyncio

import pytest

from src.services.trade.adapters import AdapterResult, NotifyLinkAdapter, build_adapter
from src.services.trade.audit import TradeAuditStore
from src.services.trade.models import TradeIntent, TradeOutcome
from src.services.trade.risk_gate import TradeLimits, TradeRiskGate
from src.services.trade.service import TradeService


class RecordingAdapter:
    """记录被调用次数的假适配器。"""

    name = "recording"

    def __init__(self, ok=True, raises=None):
        self.calls = []
        self._ok = ok
        self._raises = raises

    async def submit(self, intent):
        self.calls.append(intent)
        if self._raises is not None:
            raise self._raises
        return AdapterResult(ok=self._ok, detail="fake", external_ref="ref-1")


class FakeNotificationService:
    def __init__(self, results=None):
        self.clients = ["fake"]
        self.sent = []
        self._results = results or {"fake": {"success": True, "message": "ok"}}

    async def send_notification(self, product_data, reason):
        self.sent.append((product_data, reason))
        return self._results


def make_intent(**overrides):
    payload = {
        "task_name": "Sony A7M4",
        "item_id": "item-1",
        "title": "Sony A7M4 机身",
        "price": 100.0,
        "link": "https://www.goofish.com/item?id=item-1&spm=abc",
        "seller": "卖家A",
        "decision_source": "ai",
        "evidence": {"is_recommended": True},
    }
    payload.update(overrides)
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


def make_service(audit, adapter, **limit_overrides):
    return TradeService(
        audit=audit,
        gate=TradeRiskGate(make_limits(**limit_overrides)),
        adapter=adapter,
    )


@pytest.fixture()
def audit(tmp_path):
    return TradeAuditStore(db_path=str(tmp_path / "app.sqlite3"))


def test_dry_run_never_calls_adapter(audit):
    adapter = RecordingAdapter()
    service = make_service(audit, adapter, dry_run=True)

    result = asyncio.run(service.execute(make_intent()))

    assert result.outcome is TradeOutcome.DRY_RUN
    assert adapter.calls == [], "演练模式下不应该调用适配器"
    rows = audit.recent(limit=1)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "dry_run"
    assert rows[0]["allowed"] is True


def test_blocked_intent_does_not_call_adapter_and_is_audited(audit):
    adapter = RecordingAdapter()
    service = make_service(audit, adapter, enabled=False)

    result = asyncio.run(service.execute(make_intent()))

    assert result.outcome is TradeOutcome.BLOCKED
    assert adapter.calls == []
    rows = audit.recent(limit=1)
    assert rows[0]["outcome"] == "blocked"
    assert rows[0]["allowed"] is False
    assert rows[0]["reasons"], "拒绝原因必须写进审计"


def test_submits_when_dry_run_disabled(audit):
    adapter = RecordingAdapter()
    service = make_service(audit, adapter, dry_run=False)

    result = asyncio.run(service.execute(make_intent()))

    assert result.outcome is TradeOutcome.SUBMITTED
    assert len(adapter.calls) == 1
    assert audit.orders_today() == 1
    assert audit.spent_today() == pytest.approx(100.0)


def test_duplicate_is_blocked_after_a_real_submission(audit):
    adapter = RecordingAdapter()
    service = make_service(audit, adapter, dry_run=False)

    first = asyncio.run(service.execute(make_intent()))
    second = asyncio.run(service.execute(make_intent()))  # 同一商品：幂等键由 item_id 推导

    assert first.outcome is TradeOutcome.SUBMITTED
    assert second.outcome is TradeOutcome.DUPLICATE
    assert len(adapter.calls) == 1, "同一商品不得下单两次"


def test_dry_run_records_do_not_block_a_later_real_order(audit):
    adapter = RecordingAdapter()
    asyncio.run(make_service(audit, adapter, dry_run=True).execute(make_intent()))

    result = asyncio.run(make_service(audit, adapter, dry_run=False).execute(make_intent()))

    assert result.outcome is TradeOutcome.SUBMITTED


def test_adapter_failure_is_recorded_not_raised(audit):
    adapter = RecordingAdapter(raises=RuntimeError("浏览器崩了"))
    service = make_service(audit, adapter, dry_run=False)

    result = asyncio.run(service.execute(make_intent()))

    assert result.outcome is TradeOutcome.FAILED
    assert "浏览器崩了" in result.detail
    assert audit.recent(limit=1)[0]["outcome"] == "failed"


def test_unimplemented_checkout_adapter_fails_safely(audit):
    service = make_service(audit, build_adapter("playwright_checkout"), dry_run=False)

    result = asyncio.run(service.execute(make_intent()))

    assert result.outcome is TradeOutcome.FAILED
    assert "未实现" in result.detail
    assert audit.spent_today() == 0.0


def test_execute_never_raises_when_audit_is_broken(audit, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(audit, "append", boom)
    service = make_service(audit, RecordingAdapter(), dry_run=False)

    result = asyncio.run(service.execute(make_intent()))

    assert result.outcome is TradeOutcome.SUBMITTED
    assert "审计写入失败" in result.detail


def test_notify_link_adapter_pushes_real_link_only():
    fake = FakeNotificationService()
    adapter = NotifyLinkAdapter(notification_service=fake)

    result = asyncio.run(adapter.submit(make_intent()))

    assert result.ok is True
    product_data, reason = fake.sent[0]
    assert product_data["商品链接"] == "https://www.goofish.com/item?id=item-1&spm=abc"
    # 不得伪造下单 URL（上游 PR #474 的做法）
    assert "order/confirm" not in reason
    assert "不会自动提交订单" in reason


def test_notify_link_adapter_reports_failure_without_channels():
    fake = FakeNotificationService()
    fake.clients = []
    adapter = NotifyLinkAdapter(notification_service=fake)

    result = asyncio.run(adapter.submit(make_intent()))

    assert result.ok is False


def test_idempotency_key_ignores_task_and_query_string():
    a = TradeIntent(
        task_name="任务A", item_id="item-9", title="t", price=1.0,
        link="https://www.goofish.com/item?id=item-9&spm=aaa",
    )
    b = TradeIntent(
        task_name="任务B", item_id="item-9", title="t", price=1.0,
        link="https://www.goofish.com/item?id=item-9&spm=bbb",
    )
    assert a.idempotency_key == b.idempotency_key


def test_build_adapter_falls_back_to_dry_run_for_unknown_names():
    assert build_adapter("something-unknown").name == "dry_run"
    assert build_adapter("").name == "dry_run"
