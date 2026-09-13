"""成交价参考：从卖家已售列表里算出"这个型号大概卖多少"。

覆盖的是本模块最容易出错的几处：

- **相关性过滤**：卖家已售列表是"这个卖家的全部历史"，不是这个型号。实测反例：
  关键词 ``大石轮组`` 用子串匹配不到标题 ``大石 AERO 轮组``，而 ``iphone air`` 又会
  匹配到无关标题。所以判定要复用任务的关键词规则引擎。
- **缺失/异常价格**：``商品价格`` 可能是 None、空串、``¥4,800``、``1.2万``。
- **样本不足不给结论**：一两条样本的中位数当行情比没有结论更危险。
- **同商品重复观测**：同一卖家多次被推荐时，同一个已售商品会重复出现。
"""
import pytest

from src.services.deal_price_service import (
    MIN_SAMPLES_FOR_SUMMARY,
    build_deal_reference,
    extract_deal_samples,
    is_bargain,
    is_relevant_title,
    summarize_deal_prices,
)


def record(items, seller="卖家A", crawl_time="2026-09-13T20:05"):
    return {
        "抓取时间": crawl_time,
        "卖家信息": {"卖家昵称": seller, "卖家发布的商品列表": items},
    }


def item(item_id, title, price, status="已售"):
    return {"商品ID": item_id, "商品标题": title, "商品价格": price, "商品状态": status}


class TestRelevanceFilter:
    """相关性判定：这里错了，中位数就是垃圾。"""

    def test_substring_misses_real_match(self):
        """实测反例：'大石轮组' 不是 '大石 AERO 轮组' 的连续子串。"""
        assert is_relevant_title("大石 AERO 轮组 碳纤维", keyword="大石轮组") is False

    def test_keyword_rules_catch_it(self):
        """配了规则就能命中 —— 所以生产路径必须优先用规则。"""
        rules = ["大石", "AERO", "轮组", "碳纤维"]
        assert is_relevant_title(
            "大石 AERO 轮组 碳纤维", keyword="大石轮组", keyword_rules=rules
        )

    def test_rules_reject_unrelated_model(self):
        rules = ["iphone air"]
        assert is_relevant_title("佳能镜头 24-70", keyword="iphone air", keyword_rules=rules) is False

    def test_no_rules_falls_back_to_substring(self):
        assert is_relevant_title("iPhone Air 256G", keyword="iphone air")
        assert not is_relevant_title("佳能镜头 24-70", keyword="iphone air")

    def test_empty_keyword_and_no_rules_rejects_everything(self):
        assert not is_relevant_title("随便什么标题", keyword="")


class TestExtraction:
    def test_only_sold_items(self):
        rec = record([
            item("1", "iPhone Air 256G", "5200", status="已售"),
            item("2", "iPhone Air 512G", "6500", status="在售"),
        ])
        samples = extract_deal_samples(rec, keyword="iphone air", observed_at="T1")
        assert [s.item_id for s in samples] == ["1"]

    def test_price_formats(self):
        rec = record([
            item("1", "iPhone Air A", "5200"),
            item("2", "iPhone Air B", "¥4,800"),
            item("3", "iPhone Air C", "1.2万"),
        ])
        samples = extract_deal_samples(rec, keyword="iphone air", observed_at="T1")
        assert sorted(s.price for s in samples) == [4800.0, 5200.0, 12000.0]

    @pytest.mark.parametrize("bad_price", [None, "", "面议", "0", "  "])
    def test_bad_prices_are_skipped(self, bad_price):
        rec = record([item("1", "iPhone Air", bad_price)])
        assert extract_deal_samples(rec, keyword="iphone air", observed_at="T1") == []

    def test_duplicate_item_ids_collapse(self):
        rec = record([
            item("1", "iPhone Air 256G", "5200"),
            item("1", "iPhone Air 256G", "5200"),
        ])
        assert len(extract_deal_samples(rec, keyword="iphone air", observed_at="T1")) == 1

    def test_missing_seller_item_list(self):
        assert extract_deal_samples({}, keyword="x", observed_at="T1") == []

    def test_unknown_status_ignored(self):
        rec = record([item("1", "iPhone Air", "5200", status="未知状态 (7)")])
        assert extract_deal_samples(rec, keyword="iphone air", observed_at="T1") == []


class TestSummary:
    def _samples(self, prices):
        rec = record(
            [item(str(i), f"iPhone Air {i}", str(p)) for i, p in enumerate(prices)]
        )
        return extract_deal_samples(rec, keyword="iphone air", observed_at="T1")

    def test_no_samples_has_no_median(self):
        summary = summarize_deal_prices([])
        assert summary == {"sample_count": 0, "enough_samples": False}

    def test_too_few_samples_withholds_median(self):
        """样本不足时仍算出数字，但明确标记不可用并给出原因。"""
        summary = summarize_deal_prices(self._samples([5000, 5200]))
        assert summary["sample_count"] == 2
        assert summary["enough_samples"] is False
        assert "不足" in summary["note"]

    def test_enough_samples_gives_percentiles(self):
        summary = summarize_deal_prices(self._samples([4000, 4500, 5000, 5500, 6000]))
        assert summary["enough_samples"] is True
        assert summary["min"] == 4000 and summary["max"] == 6000
        assert summary["median"] == 5000
        assert summary["p25"] < summary["median"] < summary["p75"]
        assert "note" not in summary


class TestBuildReference:
    def test_dedupes_across_records_and_keeps_earliest(self):
        recs = [
            record([item("1", "iPhone Air 256G", "5200")], crawl_time="2026-09-10T00:00"),
            record([item("1", "iPhone Air 256G", "5200")], crawl_time="2026-09-13T00:00"),
        ]
        summary = build_deal_reference(recs, keyword="iphone air")
        assert summary["sample_count"] == 1
        assert summary["filter"] == "keyword_substring"
        assert "代理指标" in summary["disclaimer"]

    def test_reports_which_filter_was_used(self):
        recs = [record([item("1", "大石 AERO 轮组", "3000")])]
        assert build_deal_reference(recs, keyword="大石轮组")["sample_count"] == 0
        with_rules = build_deal_reference(
            recs, keyword="大石轮组", keyword_rules=["大石", "轮组"]
        )
        assert with_rules["sample_count"] == 1
        assert with_rules["filter"] == "keyword_rules"

    def test_empty_input(self):
        summary = build_deal_reference([], keyword="iphone air")
        assert summary["sample_count"] == 0
        assert summary["enough_samples"] is False


class TestBargainSignal:
    def _summary(self, prices):
        rec = record(
            [item(str(i), f"iPhone Air {i}", str(p)) for i, p in enumerate(prices)]
        )
        samples = extract_deal_samples(rec, keyword="iphone air", observed_at="T1")
        return summarize_deal_prices(samples)

    def test_well_below_median_is_bargain(self):
        assert is_bargain(4000, self._summary([5000, 5000, 5000, 5000, 5000]))

    def test_at_median_is_not(self):
        assert not is_bargain(5000, self._summary([5000, 5000, 5000, 5000, 5000]))

    def test_threshold_is_configurable(self):
        summary = self._summary([5000, 5000, 5000, 5000, 5000])
        assert not is_bargain(4800, summary, max_ratio=0.9)
        assert is_bargain(4800, summary, max_ratio=0.97)

    def test_insufficient_samples_never_alerts(self):
        """样本不足宁可漏报：中位数不可信时提醒就是误报。"""
        summary = self._summary([5000, 5000])
        assert summary["enough_samples"] is False
        assert not is_bargain(1000, summary)

    def test_bad_inputs(self):
        summary = self._summary([5000] * MIN_SAMPLES_FOR_SUMMARY)
        assert not is_bargain(None, summary)
        assert not is_bargain(0, summary)
        assert not is_bargain(-100, summary)
        assert not is_bargain(4000, {})
