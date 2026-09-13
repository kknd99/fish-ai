"""降价提醒：发现"已见过的商品创出历史新低"时通知。

为什么单独做而不是沿用推荐通知：

1. **会降价的恰恰是"已存在"的商品**。爬虫对见过的商品会去重跳过（这是对的，
   避免重复推荐），但降价恰恰发生在这些商品上 —— 所以检测必须放在去重**之前**，
   否则永远等不到降价提醒。
2. **推荐通知不该重复发**。商品第一次出现时已经推过一次；价格不变时再推就是刷屏。
   这里用"创历史新低 + 降幅达标"两个条件同时满足才提醒：
   - 必须低于该商品此前**所有**观测价（`lowest_before`），避免"每次降一点就推"；
   - 相对**上一次观测价**的降幅要达到阈值（金额或比例满足其一即可）。
   因为快照会随每次运行落库，提醒过一次之后"此前最低价"就更新了，
   除非继续跌破，否则不会重复提醒 —— 去重是规则自带的，不需要额外状态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from src.services.price_history_service import parse_price_value

#: 默认阈值：降幅达到 5% 或 50 元（满足其一）才算值得提醒
DEFAULT_MIN_PERCENT = 5.0
DEFAULT_MIN_AMOUNT = 50.0


@dataclass(frozen=True)
class PriceDrop:
    """一次降价事件。"""

    item_id: str
    title: str
    link: str
    previous_price: float
    current_price: float
    lowest_before: float
    drop_amount: float
    drop_percent: float
    #: 主图链接（来自搜索列表的 picUrl）。有图时飞书会发成图文卡片。
    image_url: str = ""

    @property
    def is_new_low(self) -> bool:
        return self.current_price < self.lowest_before


def _history_prices_by_item(snapshots: Iterable[dict]) -> Dict[str, List[float]]:
    """把快照整理成 {item_id: [价格…]}，按传入顺序（即时间序）保留。"""
    history: Dict[str, List[float]] = {}
    for record in snapshots or []:
        item_id = str(record.get("item_id") or "").strip()
        if not item_id:
            continue
        price = parse_price_value(record.get("price"))
        if price is None or price <= 0:
            continue
        history.setdefault(item_id, []).append(price)
    return history


def detect_price_drops(
    items: Iterable[dict],
    snapshots: Iterable[dict],
    *,
    min_percent: float = DEFAULT_MIN_PERCENT,
    min_amount: float = DEFAULT_MIN_AMOUNT,
) -> List[PriceDrop]:
    """挑出本次抓取里"创新低且降幅达标"的商品。

    Args:
        items: 本次抓取解析出的商品（含 ``商品ID`` / ``当前售价`` 等字段）。
        snapshots: **本次之前**的历史价格快照（调用方需在记录本次快照前传入）。
        min_percent: 降幅百分比阈值。
        min_amount: 降幅金额阈值。

    Returns:
        降价事件列表；同一商品在同一批里只会出现一次。
    """
    history = _history_prices_by_item(snapshots)
    if not history:
        return []

    drops: List[PriceDrop] = []
    seen: set[str] = set()

    for item in items or []:
        item_id = str(item.get("商品ID") or "").strip()
        if not item_id or item_id in seen:
            continue

        past_prices = history.get(item_id)
        if not past_prices:
            # 第一次见到的商品不算降价（它刚被推荐过）
            continue

        current_price = parse_price_value(item.get("当前售价"))
        if current_price is None or current_price <= 0:
            continue

        previous_price = past_prices[-1]
        lowest_before = min(past_prices)

        # 没降（或涨价了）
        if current_price >= previous_price:
            continue

        drop_amount = round(previous_price - current_price, 2)
        drop_percent = round(drop_amount / previous_price * 100, 2) if previous_price else 0.0

        # 降幅不够：金额与比例满足其一即可
        if drop_amount < min_amount and drop_percent < min_percent:
            continue

        # 必须创历史新低，避免"每次降一点就提醒"
        if current_price >= lowest_before:
            continue

        seen.add(item_id)
        drops.append(
            PriceDrop(
                item_id=item_id,
                title=str(item.get("商品标题") or "未知标题"),
                link=str(item.get("商品链接") or "#"),
                previous_price=previous_price,
                current_price=current_price,
                lowest_before=lowest_before,
                drop_amount=drop_amount,
                drop_percent=drop_percent,
                image_url=str(item.get("商品主图链接") or ""),
            )
        )

    return drops


def build_drop_reason(drop: PriceDrop) -> str:
    """生成通知里的"原因"文案。"""
    lines = [
        f"降价提醒：¥{drop.previous_price:g} → ¥{drop.current_price:g}"
        f"（降 {drop.drop_amount:g} 元 / {drop.drop_percent:g}%）",
    ]
    if drop.is_new_low:
        lines.append(f"当前价已跌破此前最低价 ¥{drop.lowest_before:g}，创历史新低。")
    else:
        lines.append(f"此前最低价为 ¥{drop.lowest_before:g}。")
    lines.append("该商品此前已推送过，本次因降价再次提醒。")
    return "\n".join(lines)


def build_drop_product_data(drop: PriceDrop) -> dict:
    """构造通知渠道需要的扁平商品数据（与推荐通知保持同一结构）。"""
    return {
        "商品标题": drop.title,
        "当前售价": f"{drop.current_price:g}",
        "商品链接": drop.link,
        "商品ID": drop.item_id,
        # 始终带上这个键（缺失时为空串），webhook 模板引用它时不会渲染成 "None"
        "商品主图链接": drop.image_url,
    }


def resolve_thresholds(
    *,
    enabled: Optional[bool] = None,
    min_percent: Optional[float] = None,
    min_amount: Optional[float] = None,
) -> dict:
    """读取降价提醒的开关与阈值（未显式传入时回落到环境变量/默认值）。"""
    from src.config import (
        PRICE_DROP_ENABLED,
        PRICE_DROP_MIN_AMOUNT,
        PRICE_DROP_MIN_PERCENT,
    )

    def _positive(value, fallback: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if number > 0 else fallback

    return {
        "enabled": PRICE_DROP_ENABLED if enabled is None else bool(enabled),
        "min_percent": _positive(
            PRICE_DROP_MIN_PERCENT if min_percent is None else min_percent, DEFAULT_MIN_PERCENT
        ),
        "min_amount": _positive(
            PRICE_DROP_MIN_AMOUNT if min_amount is None else min_amount, DEFAULT_MIN_AMOUNT
        ),
    }
