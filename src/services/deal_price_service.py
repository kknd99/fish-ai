"""成交价参考：把卖家已售商品的价格，变成"这个型号大概卖多少"。

**数据从哪来**：卖家资料页的商品列表接口本来就会返回已售商品（``cardData.itemStatus``），
``_parse_user_items_data`` 也早就把它解析成 ``商品状态: 已售`` 并保留了价格。也就是说这
条数据一直在库里，只是从没被利用过 —— 所以本模块不需要新增任何采集。

**口径必须说清楚的两件事**：

1. 闲鱼"已售"显示的价格**不等于真实成交价**：它可能是挂牌价或最后一次改价，而且卖家
   卖完常常直接下架、页面不再保留。它是成交价的**代理指标**，用来看区间可以，不能当
   精确成交价。
2. 卖家已售列表是**这个卖家的全部历史**，不是这个型号。所以必须按关键词相关性过滤，
   否则会把同一个卖家的相机镜头价格混进 iPhone 的中位数里。

第 2 点的过滤**不能**用关键词子串，实测反例：任务关键词 ``大石轮组`` 匹配不到标题
``大石 AERO 轮组``（不是连续子串），而 ``iphone air`` 又会匹配到无关标题里的
``air``。所以这里复用任务自己的关键词规则引擎 ``evaluate_keyword_rules``，
让"什么算相关"与推荐判定保持同一套语义。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from src.keyword_rule_engine import evaluate_keyword_rules, normalize_text
from src.services.price_history_service import parse_price_value

#: 已售样本少于这个数量时不给出中位数结论 —— 太少的中位数只是噪声
MIN_SAMPLES_FOR_SUMMARY = 5


@dataclass(frozen=True)
class DealSample:
    """一条成交价样本（来自某个卖家已售列表里的一个商品）。"""

    item_id: str
    title: str
    price: float
    seller: str
    observed_at: str


def _sold_items_from_record(record: dict) -> list:
    """取出记录里卖家已售商品的原始条目。"""
    seller_info = record.get("卖家信息") or {}
    items = seller_info.get("卖家发布的商品列表") or []
    sold = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("商品状态") or "").strip() != "已售":
            continue
        sold.append(item)
    return sold


def _seller_name(record: dict) -> str:
    return str((record.get("卖家信息") or {}).get("卖家昵称") or "")


def is_relevant_title(title: str, *, keyword: str, keyword_rules: Iterable[str] = ()) -> bool:
    """判断一个已售商品标题是否与当前任务相关。

    优先用任务的关键词规则（与推荐判定同源）；没配规则时退回关键词子串，
    并在调用方记录用的是哪种口径，避免口径不明导致数字被误解。
    """
    rules = [str(rule) for rule in (keyword_rules or []) if str(rule).strip()]
    if rules:
        return bool(evaluate_keyword_rules(rules, title)["is_recommended"])
    normalized_keyword = normalize_text(keyword)
    if not normalized_keyword:
        return False
    return normalized_keyword in normalize_text(title)


def extract_deal_samples(
    record: dict,
    *,
    keyword: str,
    observed_at: str,
    keyword_rules: Iterable[str] = (),
    require_relevance: bool = True,
) -> List[DealSample]:
    """从一条结果记录里抽出可用的成交价样本。

    Args:
        record: 结果记录（含 ``卖家信息.卖家发布的商品列表``）。
        keyword: 任务关键词，用于相关性过滤。
        observed_at: **我们观测到该条目的时间**（ISO 字符串）。闲鱼列表接口不返回
            成交时间，所以"近期"只能用观测时间来界定，这一点在返回值里由调用方
            通过 ``observed_at`` 体现，不要误当成真实成交时间。
        keyword_rules: 任务的关键词规则，优先用于相关性判定。
        require_relevance: 关掉则收下该卖家全部已售商品（只用于诊断对比）。
    """
    seller = _seller_name(record)
    samples: List[DealSample] = []
    seen: set[str] = set()

    for item in _sold_items_from_record(record):
        item_id = str(item.get("商品ID") or "").strip()
        if not item_id or item_id in seen:
            continue
        title = str(item.get("商品标题") or "").strip()

        if require_relevance and not is_relevant_title(
            title, keyword=keyword, keyword_rules=keyword_rules
        ):
            continue

        price = parse_price_value(item.get("商品价格"))
        if price is None or price <= 0:
            continue

        seen.add(item_id)
        samples.append(
            DealSample(
                item_id=item_id,
                title=title,
                price=price,
                seller=seller,
                observed_at=observed_at,
            )
        )

    return samples


def _percentile(sorted_values: List[float], fraction: float) -> float:
    """线性插值分位数（样本量小时比最近邻更稳）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def summarize_deal_prices(samples: Iterable[DealSample]) -> Dict[str, object]:
    """把样本汇总成"这个型号大概卖多少"。

    样本不足时**不给中位数**：把一两个样本的中位数当成市场行情，比没有结论更危险。
    """
    prices = sorted(sample.price for sample in samples)
    count = len(prices)
    if count == 0:
        return {"sample_count": 0, "enough_samples": False}

    summary: Dict[str, object] = {
        "sample_count": count,
        "enough_samples": count >= MIN_SAMPLES_FOR_SUMMARY,
        "min": prices[0],
        "max": prices[-1],
        "median": round(_percentile(prices, 0.5), 2),
        "p25": round(_percentile(prices, 0.25), 2),
        "p75": round(_percentile(prices, 0.75), 2),
    }
    if not summary["enough_samples"]:
        summary["note"] = (
            f"样本仅 {count} 条（少于 {MIN_SAMPLES_FOR_SUMMARY} 条），"
            "不足以代表行情，仅供参考。"
        )
    return summary


def build_deal_reference(
    records: Iterable[dict],
    *,
    keyword: str,
    keyword_rules: Iterable[str] = (),
) -> Dict[str, object]:
    """从一批结果记录里汇总成交价参考（按商品去重）。"""
    rules = list(keyword_rules or ())
    by_item: Dict[str, DealSample] = {}
    for record in records or []:
        observed_at = str(record.get("抓取时间") or record.get("crawl_time") or "")
        for sample in extract_deal_samples(
            record,
            keyword=keyword,
            observed_at=observed_at,
            keyword_rules=rules,
        ):
            # 同一商品可能被多次观测到；保留最早那次，作为"已售"的最早可见时间
            existing = by_item.get(sample.item_id)
            if existing is None or (sample.observed_at and sample.observed_at < existing.observed_at):
                by_item[sample.item_id] = sample

    summary = summarize_deal_prices(by_item.values())
    summary["keyword"] = keyword
    summary["filter"] = "keyword_rules" if rules else "keyword_substring"
    summary["disclaimer"] = (
        "「已售」价格是成交价的代理指标（可能是挂牌价或最后一次改价），"
        "且只是该卖家历史成交的一个样本，不等于真实成交价。"
    )
    return summary


def is_bargain(
    current_price: Optional[float],
    summary: Dict[str, object],
    *,
    max_ratio: float = 0.85,
) -> bool:
    """当前要价是否明显低于已售价中位数（捡漏信号）。

    刻意要求样本足够：样本不足时中位数不可信，宁可漏报也不要误报。
    """
    if current_price is None or current_price <= 0:
        return False
    if not summary.get("enough_samples"):
        return False
    median = summary.get("median")
    if not isinstance(median, (int, float)) or median <= 0:
        return False
    return current_price <= median * max_ratio
