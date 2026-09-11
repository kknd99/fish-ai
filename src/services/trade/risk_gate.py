"""资金闸门：所有交易动作的唯一入场检查。

这是"自动下单"这个功能里最重要的一块代码。判定由 LLM 给出，而 LLM 的输入
包含**卖家可控的文本**（商品标题/描述），也就是说：判定是可以被卖家影响的。
因此花钱这个动作不能直接信任 ``is_recommended``，必须再过一道与模型无关的、
确定性的、fail-closed 的限额检查。

约定：
- 任何一项检查异常 → **拒绝**（fail-closed），并把异常写进 ``checks`` 供审计；
- 未配置上限 ≠ 不限额。``TRADE_MAX_UNIT_PRICE`` / ``TRADE_DAILY_BUDGET`` /
  ``TRADE_MAX_ORDERS_PER_DAY`` 为 0 时一律拒绝，迫使部署者显式设定金额边界。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from src.services.trade.models import GateVerdict, TradeIntent


@dataclass(frozen=True)
class TradeLimits:
    """闸门使用的限额快照（会写进审计）。"""

    enabled: bool = False
    dry_run: bool = True
    max_unit_price: float = 0.0
    daily_budget: float = 0.0
    max_orders_per_day: int = 0
    kill_switch_file: str = "TRADE_KILL_SWITCH"
    allowed_sellers: tuple[str, ...] = ()
    min_value_score: float = 0.0

    @classmethod
    def from_settings(cls, trade_settings: Any) -> "TradeLimits":
        """由 :class:`~src.infrastructure.config.settings.TradeSettings` 构造。"""
        raw_sellers = getattr(trade_settings, "allowed_sellers", None) or ""
        sellers = tuple(
            part.strip() for part in str(raw_sellers).split(",") if part.strip()
        )
        return cls(
            enabled=bool(getattr(trade_settings, "enabled", False)),
            dry_run=bool(getattr(trade_settings, "dry_run", True)),
            max_unit_price=float(getattr(trade_settings, "max_unit_price", 0.0) or 0.0),
            daily_budget=float(getattr(trade_settings, "daily_budget", 0.0) or 0.0),
            max_orders_per_day=int(getattr(trade_settings, "max_orders_per_day", 0) or 0),
            kill_switch_file=str(
                getattr(trade_settings, "kill_switch_file", "TRADE_KILL_SWITCH")
                or "TRADE_KILL_SWITCH"
            ),
            allowed_sellers=sellers,
            min_value_score=float(getattr(trade_settings, "min_value_score", 0.0) or 0.0),
        )

    def as_audit_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "max_unit_price": self.max_unit_price,
            "daily_budget": self.daily_budget,
            "max_orders_per_day": self.max_orders_per_day,
            "allowed_sellers": list(self.allowed_sellers),
            "min_value_score": self.min_value_score,
        }


class TradeRiskGate:
    """Deterministic, fail-closed 的限额闸门。"""

    def __init__(self, limits: TradeLimits, *, env: Optional[dict] = None) -> None:
        self.limits = limits
        self._env = env

    # ------------------------------------------------------------------ 内部检查
    def _check_enabled(self, reasons: list[str], checks: dict) -> None:
        checks["enabled"] = self.limits.enabled
        if not self.limits.enabled:
            reasons.append("交易功能未启用 (TRADE_ENABLED=false)")

    def _check_kill_switch(self, reasons: list[str], checks: dict) -> None:
        path = self.limits.kill_switch_file
        exists = bool(path) and os.path.exists(path)
        checks["kill_switch_file"] = path
        checks["kill_switch_active"] = exists
        if exists:
            reasons.append(f"急停开关生效（存在文件 {path}），已停止所有交易动作")

    def _check_price(self, intent: TradeIntent, reasons: list[str], checks: dict) -> None:
        price = intent.price
        checks["price"] = price
        checks["max_unit_price"] = self.limits.max_unit_price
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            reasons.append(f"无法解析商品价格: {price!r}")
            return
        if price_value <= 0:
            reasons.append(f"商品价格异常: {price_value}")
            return
        if self.limits.max_unit_price <= 0:
            reasons.append("未配置单件价格上限 (TRADE_MAX_UNIT_PRICE)，拒绝放行")
        elif price_value > self.limits.max_unit_price:
            reasons.append(
                f"单价 {price_value} 超过上限 {self.limits.max_unit_price}"
            )

    def _check_daily_budget(
        self,
        intent: TradeIntent,
        spent_today: float,
        reasons: list[str],
        checks: dict,
    ) -> None:
        checks["spent_today"] = spent_today
        checks["daily_budget"] = self.limits.daily_budget
        if self.limits.daily_budget <= 0:
            reasons.append("未配置每日预算上限 (TRADE_DAILY_BUDGET)，拒绝放行")
            return
        projected = float(spent_today or 0.0) + float(intent.price or 0.0)
        checks["projected_daily_spend"] = projected
        if projected > self.limits.daily_budget:
            reasons.append(
                f"本单后当日累计支出 {projected} 将超过预算 {self.limits.daily_budget}"
            )

    def _check_order_count(
        self,
        orders_today: int,
        reasons: list[str],
        checks: dict,
    ) -> None:
        checks["orders_today"] = orders_today
        checks["max_orders_per_day"] = self.limits.max_orders_per_day
        if self.limits.max_orders_per_day <= 0:
            reasons.append("未配置每日下单数量上限 (TRADE_MAX_ORDERS_PER_DAY)，拒绝放行")
            return
        if orders_today >= self.limits.max_orders_per_day:
            reasons.append(
                f"当日已下单 {orders_today} 次，达到上限 {self.limits.max_orders_per_day}"
            )

    def _check_duplicate(
        self,
        intent: TradeIntent,
        duplicate: bool,
        reasons: list[str],
        checks: dict,
    ) -> None:
        checks["idempotency_key"] = intent.idempotency_key
        checks["duplicate"] = duplicate
        if duplicate:
            reasons.append("该商品此前已处理过（幂等键命中），拒绝重复下单")

    def _check_seller(
        self,
        intent: TradeIntent,
        reasons: list[str],
        checks: dict,
    ) -> None:
        allow = self.limits.allowed_sellers
        checks["allowed_sellers"] = list(allow)
        checks["seller"] = intent.seller
        if not allow:
            return
        if (intent.seller or "").strip() not in allow:
            reasons.append(f"卖家 '{intent.seller}' 不在白名单内")

    def _check_value_score(
        self,
        intent: TradeIntent,
        reasons: list[str],
        checks: dict,
    ) -> None:
        threshold = self.limits.min_value_score
        if threshold <= 0:
            return
        raw = (intent.evidence or {}).get("value_score")
        checks["value_score"] = raw
        checks["min_value_score"] = threshold
        try:
            score = float(raw)
        except (TypeError, ValueError):
            reasons.append("判定结果缺少可信的 value_score，拒绝放行")
            return
        if score < threshold:
            reasons.append(f"价值评分 {score} 低于下限 {threshold}")

    # ------------------------------------------------------------------ 对外入口
    def evaluate(
        self,
        intent: TradeIntent,
        *,
        spent_today: float = 0.0,
        orders_today: int = 0,
        duplicate: bool = False,
    ) -> GateVerdict:
        """执行全部检查并返回判定。任何内部异常都视为拒绝。"""
        checks: dict[str, Any] = {"limits": self.limits.as_audit_dict()}
        reasons: list[str] = []
        try:
            self._check_enabled(reasons, checks)
            self._check_kill_switch(reasons, checks)
            self._check_price(intent, reasons, checks)
            self._check_daily_budget(intent, spent_today, reasons, checks)
            self._check_order_count(orders_today, reasons, checks)
            self._check_duplicate(intent, duplicate, reasons, checks)
            self._check_seller(intent, reasons, checks)
            self._check_value_score(intent, reasons, checks)
        except Exception as exc:  # fail-closed：闸门自身出错时绝不放行
            reasons.append(f"闸门内部错误，按拒绝处理: {exc!r}")
        return GateVerdict(allowed=not reasons, reasons=tuple(reasons), checks=checks)


def collect_reasons(verdicts: Iterable[GateVerdict]) -> tuple[str, ...]:
    """辅助函数：合并多个判定的拒绝原因。"""
    merged: list[str] = []
    for verdict in verdicts:
        merged.extend(verdict.reasons)
    return tuple(merged)
