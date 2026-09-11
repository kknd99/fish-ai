"""交易（自动下单）子包。

设计要点见各模块文档；一句话概括：**金钱动作必须过闸门、必须留审计、默认演练**。

当前状态：本子包**尚未接入**商品分析流程（``item_analysis_dispatcher``），
接入前请先确认：TRADE_ENABLED 显式开启、金额上限显式配置、且先在
TRADE_DRY_RUN=true 下跑通一段时间。
"""
from src.services.trade.adapters import (
    AdapterResult,
    DryRunAdapter,
    NotifyLinkAdapter,
    PlaywrightCheckoutAdapter,
    TradeAdapter,
    build_adapter,
)
from src.services.trade.audit import TradeAuditStore
from src.services.trade.models import (
    GateVerdict,
    TradeIntent,
    TradeOutcome,
    TradeResult,
    build_idempotency_key,
)
from src.services.trade.risk_gate import TradeLimits, TradeRiskGate
from src.services.trade.service import TradeService

__all__ = [
    "AdapterResult",
    "DryRunAdapter",
    "GateVerdict",
    "NotifyLinkAdapter",
    "PlaywrightCheckoutAdapter",
    "TradeAdapter",
    "TradeAuditStore",
    "TradeIntent",
    "TradeLimits",
    "TradeOutcome",
    "TradeResult",
    "TradeRiskGate",
    "TradeService",
    "build_adapter",
    "build_idempotency_key",
]
