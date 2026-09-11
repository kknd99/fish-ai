"""交易管线：把"抓取 → 判定"的结果接进交易闸门（L1 半自动）。

分工：
- 这个模块负责**把一条已判定的商品记录翻译成 TradeIntent**，以及决定"这个任务
  到底要不要走交易"；
- 资金闸门与审计仍然在 TradeService 里（金额上限、幂等、卖家白名单、急停开关）。

三层默认关闭，接上之后行为与从前完全一致：
1. 任务级 ``trade_enabled`` 默认 false（本模块据此直接不建 runner）；
2. 全局 ``TRADE_ENABLED`` 默认 false（闸门会拒绝）；
3. 全局 ``TRADE_DRY_RUN`` 默认 true（即便放行也只写审计、不产生外部动作）。

价格来源是硬约定：``price_source=detail`` —— 取自**详情接口解析后的记录**，
不使用搜索列表页的摘要价（那正是"低价引流、点进去改价"的利用点）。
"""
from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional

from src.services.price_history_service import parse_price_value
from src.services.trade.models import PRICE_SOURCE_DETAIL, TradeIntent, TradeResult
from src.services.trade.service import TradeService

#: 交易 runner 的签名：(记录, 判定结果) -> TradeResult | None
TradeRunner = Callable[[dict, dict], Any]


def is_trade_enabled(task_config: Mapping[str, Any]) -> bool:
    """任务的交易开关。只认真正的布尔真值，避免 "false" 这类字符串被当成真。"""
    raw = (task_config or {}).get("trade_enabled", False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _as_float_price(value: Any) -> Optional[float]:
    """把任务配置里的价格（"4000"、"¥4,000" 等）转成浮点；无法解析返回 None。"""
    parsed = parse_price_value(value)
    if parsed is None:
        return None
    try:
        return float(parsed)
    except (TypeError, ValueError):
        return None


def build_trade_intent(
    task_config: Mapping[str, Any],
    record: Mapping[str, Any],
    analysis_result: Mapping[str, Any] | None,
) -> Optional[TradeIntent]:
    """把一条商品记录翻译成交易意图；缺少必要标识时返回 None。

    价格无法解析时**不跳过**，而是以 0 进入闸门 —— 这样审计表里会留下一条
    "价格异常被拒"的记录，而不是无声无息地什么都不做。
    """
    item = (record or {}).get("商品信息") or {}
    seller_info = (record or {}).get("卖家信息") or {}
    analysis = dict(analysis_result or {})

    item_id = str(item.get("商品ID") or "").strip()
    link = str(item.get("商品链接") or "").strip()
    if not item_id and not link:
        # 没有商品标识就无法做幂等（可能重复下单），直接不进入交易流程
        return None

    seller = seller_info.get("卖家昵称") or item.get("卖家昵称")
    price = _as_float_price(item.get("当前售价"))

    return TradeIntent(
        task_name=str(
            (task_config or {}).get("task_name") or (record or {}).get("任务名称") or ""
        ),
        item_id=item_id or link,
        title=str(item.get("商品标题") or ""),
        price=float(price or 0.0),
        link=link,
        seller=str(seller).strip() if seller else None,
        decision_source=str(analysis.get("analysis_source") or "unknown"),
        # 硬约定：价格取自详情接口解析结果
        price_source=PRICE_SOURCE_DETAIL,
        task_min_price=_as_float_price((task_config or {}).get("min_price")),
        task_max_price=_as_float_price((task_config or {}).get("max_price")),
        evidence=analysis,
    )


def build_trade_runner(
    task_config: Mapping[str, Any],
    *,
    db_path: Optional[str] = None,
    trade_settings: Any = None,
    ai_settings: Any = None,
    notification_service: Any = None,
) -> Optional[TradeRunner]:
    """按任务配置构造交易 runner；任务未开启交易时返回 None。

    返回 None 是"什么都没接上"的意思 —— 调用方（dispatcher）据此完全跳过交易流程，
    因此默认部署下这条链路不产生任何行为变化。
    """
    if not is_trade_enabled(task_config):
        return None

    action = str((task_config or {}).get("trade_action") or "").strip().lower() or None
    service = TradeService.from_settings(
        trade_settings,
        db_path=db_path,
        notification_service=notification_service,
        ai_settings=ai_settings,
        adapter_name=action,
    )

    async def runner(record: dict, analysis_result: dict) -> Optional[TradeResult]:
        # 只有"判定为推荐"的商品才进入交易流程：闸门管的是"能不能花钱"，
        # 这里管的是"值不值得进入花钱流程"，两者都不应被绕过。
        if not (analysis_result or {}).get("is_recommended"):
            return None

        intent = build_trade_intent(task_config, record, analysis_result)
        if intent is None:
            print("   [交易] 缺少商品标识（ID 与链接都为空），已跳过交易流程。")
            return None

        result = await service.execute(intent)
        print(
            f"   [交易] {result.outcome.value} | 适配器={result.adapter} | {result.detail[:120]}"
        )
        return result

    return runner


def resolve_trade_db_path() -> Optional[str]:
    """交易审计与结果共用同一个 SQLite 库（默认取 APP_DATABASE_FILE）。"""
    return os.getenv("APP_DATABASE_FILE") or None
