"""交易服务：把"意图 → 闸门 → 执行 → 审计"串成一条不可绕过的链路。

调用方（将来接进商品分析流程时）只需要：

    result = await trade_service.execute(intent)

``execute`` **不会抛异常**：任何失败都被收敛成 FAILED 结果并写进审计，
这样一次交易异常不会让爬虫循环崩掉，也不会出现"失败了但没人知道"的情况。
"""
from __future__ import annotations

import time
from typing import Optional

from src.infrastructure.config.settings import TradeSettings, trade_settings
from src.services.trade.adapters import TradeAdapter, build_adapter
from src.services.trade.audit import TradeAuditStore
from src.services.trade.models import TradeIntent, TradeOutcome, TradeResult
from src.services.trade.risk_gate import TradeLimits, TradeRiskGate


class TradeService:
    """交易编排服务。"""

    def __init__(
        self,
        audit: TradeAuditStore,
        gate: TradeRiskGate,
        adapter: TradeAdapter,
    ) -> None:
        self.audit = audit
        self.gate = gate
        self.adapter = adapter

    @classmethod
    def from_settings(
        cls,
        settings: Optional[TradeSettings] = None,
        *,
        db_path: Optional[str] = None,
        notification_service=None,
        ai_settings=None,
    ) -> "TradeService":
        """按当前配置装配一套交易服务。"""
        # 现取配置：reload_settings() 会重新绑定模块级实例，导入时的引用会过期
        from src.infrastructure.config.settings import ai_settings as current_ai_settings

        resolved = settings or trade_settings
        adapter = build_adapter(
            getattr(resolved, "adapter", "dry_run"),
            notification_service=notification_service,
        )
        return cls(
            audit=TradeAuditStore(db_path=db_path),
            gate=TradeRiskGate(
                TradeLimits.from_settings(
                    resolved,
                    ai_settings if ai_settings is not None else current_ai_settings,
                )
            ),
            adapter=adapter,
        )

    @property
    def dry_run(self) -> bool:
        return self.gate.limits.dry_run

    async def execute(self, intent: TradeIntent) -> TradeResult:
        """执行一次交易尝试；永不抛异常。"""
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        # 1) 先取事实：是否重复、今日已花多少
        try:
            duplicate = self.audit.has_idempotency_key(intent.idempotency_key)
            spent_today = self.audit.spent_today()
            orders_today = self.audit.orders_today()
        except Exception as exc:
            return self._record(
                intent=intent,
                verdict=None,
                outcome=TradeOutcome.FAILED,
                detail=f"读取审计数据失败，按失败处理: {exc!r}",
                checks={"audit_error": repr(exc)},
                duration_ms=elapsed_ms(),
            )

        # 2) 闸门判定
        verdict = self.gate.evaluate(
            intent,
            spent_today=spent_today,
            orders_today=orders_today,
            duplicate=duplicate,
        )
        if not verdict.allowed:
            outcome = TradeOutcome.DUPLICATE if duplicate else TradeOutcome.BLOCKED
            return self._record(
                intent=intent,
                verdict=verdict,
                outcome=outcome,
                detail=verdict.summary(),
                duration_ms=elapsed_ms(),
            )

        # 3) 演练模式：闸门放行也不产生外部动作
        if self.dry_run:
            return self._record(
                intent=intent,
                verdict=verdict,
                outcome=TradeOutcome.DRY_RUN,
                detail="dry-run：闸门放行，未提交订单",
                duration_ms=elapsed_ms(),
            )

        # 4) 真正执行
        try:
            adapter_result = await self.adapter.submit(intent)
        except NotImplementedError as exc:
            return self._record(
                intent=intent,
                verdict=verdict,
                outcome=TradeOutcome.FAILED,
                detail=f"适配器未实现: {exc}",
                duration_ms=elapsed_ms(),
            )
        except Exception as exc:
            return self._record(
                intent=intent,
                verdict=verdict,
                outcome=TradeOutcome.FAILED,
                detail=f"交易执行异常: {exc!r}",
                duration_ms=elapsed_ms(),
            )

        return self._record(
            intent=intent,
            verdict=verdict,
            outcome=TradeOutcome.SUBMITTED if adapter_result.ok else TradeOutcome.FAILED,
            detail=adapter_result.detail,
            external_ref=adapter_result.external_ref,
            duration_ms=elapsed_ms(),
        )

    def _record(
        self,
        *,
        intent: TradeIntent,
        verdict,
        outcome: TradeOutcome,
        detail: str,
        duration_ms: int,
        external_ref: Optional[str] = None,
        checks: Optional[dict] = None,
    ) -> TradeResult:
        """写审计并返回结果；写审计失败也不能让异常冒出去。"""
        from src.services.trade.models import GateVerdict

        if verdict is None:
            verdict = GateVerdict(allowed=False, reasons=("内部错误",), checks=checks or {})

        audit_id: Optional[int] = None
        try:
            audit_id = self.audit.append(
                intent=intent,
                verdict=verdict,
                outcome=outcome,
                adapter=self.adapter.name,
                detail=detail,
                external_ref=external_ref,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            detail = f"{detail} | 审计写入失败: {exc!r}"

        return TradeResult(
            outcome=outcome,
            verdict=verdict,
            adapter=self.adapter.name,
            audit_id=audit_id,
            detail=detail,
            external_ref=external_ref,
        )
