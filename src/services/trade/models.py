"""交易（自动下单）领域模型。

这些类型刻意与爬虫/API 解耦：任何"要花钱的动作"都必须先被表示成
:class:`TradeIntent`，经 :class:`~src.services.trade.risk_gate.TradeRiskGate`
判定，再由 :class:`~src.services.trade.adapters.TradeAdapter` 执行，
最后落进 :class:`~src.services.trade.audit.TradeAuditStore` 的审计表。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


class TradeOutcome(str, Enum):
    """一次交易动作的最终结果。"""

    DRY_RUN = "dry_run"        # 闸门放行，但处于演练模式：未提交任何订单
    SUBMITTED = "submitted"    # 已提交订单
    BLOCKED = "blocked"        # 被闸门拒绝
    DUPLICATE = "duplicate"    # 同一商品已经处理过，拒绝重复
    FAILED = "failed"          # 执行阶段失败


def normalize_link(link: str | None) -> str:
    """去掉 query/fragment 后的链接，用作商品身份的一部分。"""
    if not link:
        return ""
    parts = urlsplit(str(link).strip())
    if not parts.scheme:
        return str(link).strip()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def build_idempotency_key(item_id: str | None, link: str | None) -> str:
    """生成幂等键。

    刻意**不包含任务名**：同一个商品被多个任务同时命中时，仍然只允许下单一次。
    商品 ID 优先，缺失时退回"去掉 query 的链接"。
    """
    identity = (str(item_id).strip() if item_id else "") or normalize_link(link)
    if not identity:
        raise ValueError("缺少 item_id 与 link，无法生成幂等键")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"trade:{digest}"


@dataclass(frozen=True)
class TradeIntent:
    """一次待执行的交易意图（不含任何资金动作）。"""

    task_name: str
    item_id: str
    title: str
    price: float
    link: str
    seller: Optional[str] = None
    decision_source: str = "unknown"          # "ai" / "keyword"
    evidence: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""                 # 留空则自动生成

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            object.__setattr__(
                self,
                "idempotency_key",
                build_idempotency_key(self.item_id, self.link),
            )

    def notification_payload(self) -> dict:
        """转成通知层认识的商品字段结构。"""
        return {
            "商品标题": self.title,
            "当前售价": str(self.price),
            "商品链接": self.link,
            "卖家昵称": self.seller or "未知",
        }


@dataclass(frozen=True)
class GateVerdict:
    """闸门判定结果。

    ``checks`` 会原样写进审计表，用于事后回答"当时为什么放行/为什么拦下"。
    """

    allowed: bool
    reasons: tuple[str, ...] = ()
    checks: Mapping[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.allowed:
            return "允许"
        return "拒绝: " + "; ".join(self.reasons) if self.reasons else "拒绝"


@dataclass(frozen=True)
class TradeResult:
    """交易动作的返回值（同时对应审计表里的一行）。"""

    outcome: TradeOutcome
    verdict: GateVerdict
    adapter: str
    audit_id: Optional[int] = None
    detail: str = ""
    external_ref: Optional[str] = None
