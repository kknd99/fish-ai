"""降价提醒：只对"创历史新低且降幅达标"的商品提醒一次。

设计要点（都有对应用例）：
- 首次见到的商品不算降价（它刚被推荐过）；
- 必须低于此前**所有**观测价，避免"每次降一点就推"；
- 相对上一次观测价的降幅要达标（金额或比例满足其一）；
- 因为快照每次运行都会落库，提醒过一次后"此前最低价"就更新了 —— 去重是规则
  自带的，不需要额外记录已提醒状态。
"""
import pytest

from src.services.price_drop_service import (
    DEFAULT_MIN_AMOUNT,
    DEFAULT_MIN_PERCENT,
    PriceDrop,
    build_drop_product_data,
    build_drop_reason,
    detect_price_drops,
)


def item(item_id: str, price, title: str = "测试商品", link: str = "https://e.com/i"):
    return {"商品ID": item_id, "当前售价": price, "商品标题": title, "商品链接": link}


def snap(item_id: str, price) -> dict:
    return {"item_id": item_id, "price": price}


class TestDetectPriceDrops:
    def test_first_sighting_is_not_a_drop(self):
        """第一次见到 → 不是降价（它刚被推荐过，不该再提醒）。"""
        assert detect_price_drops([item("A", "1000")], []) == []

    def test_new_low_with_big_enough_drop(self):
        drops = detect_price_drops(
            [item("A", "900")], [snap("A", 1000), snap("A", 950)]
        )
        assert len(drops) == 1
        drop = drops[0]
        assert drop.current_price == 900
        assert drop.previous_price == 950      # 对比"上一次观测价"
        assert drop.lowest_before == 950       # 创了新低
        assert drop.drop_amount == 50
        assert drop.drop_percent == pytest.approx(5.26, abs=0.01)
        assert drop.is_new_low is True

    def test_price_increase_is_ignored(self):
        assert detect_price_drops([item("A", "1200")], [snap("A", 1000)]) == []

    def test_unchanged_price_is_ignored(self):
        assert detect_price_drops([item("A", "1000")], [snap("A", 1000)]) == []

    def test_drop_below_thresholds_is_ignored(self):
        """降了 10 元 / 1%，两个阈值都不满足 → 不提醒。"""
        assert detect_price_drops([item("A", "990")], [snap("A", 1000)]) == []

    def test_amount_threshold_alone_is_enough(self):
        """降 60 元但比例不到 5%（基数大）→ 金额达标即提醒。"""
        drops = detect_price_drops([item("A", "1940")], [snap("A", 2000)])
        assert len(drops) == 1, "金额 60 ≥ 50，应当提醒"
        assert drops[0].drop_percent < DEFAULT_MIN_PERCENT

    def test_percent_threshold_alone_is_enough(self):
        """降 30 元但比例达 6%（基数小）→ 比例达标即提醒。"""
        drops = detect_price_drops([item("A", "470")], [snap("A", 500)])
        assert len(drops) == 1, "比例 6% ≥ 5%，应当提醒"
        assert drops[0].drop_amount < DEFAULT_MIN_AMOUNT

    def test_not_a_new_low_is_ignored(self):
        """曾以 800 成交过，现在 900 虽然比上次(1000)降了，但不是新低 → 不提醒。"""
        assert detect_price_drops(
            [item("A", "900")], [snap("A", 800), snap("A", 1000)]
        ) == []

    def test_repeated_small_drops_do_not_spam(self):
        """模拟连续几轮：每轮只降一点点 → 只有达标且创新低的那轮提醒。"""
        history = [snap("A", 1000)]
        assert detect_price_drops([item("A", "995")], history) == []   # 降 5 元,不提醒
        history.append(snap("A", 995))
        assert detect_price_drops([item("A", "990")], history) == []   # 累计仍不达标
        history.append(snap("A", 990))
        drops = detect_price_drops([item("A", "900")], history)        # 这次降 90 元
        assert len(drops) == 1
        # 提醒后快照更新，同样价格再跑不会重复提醒
        history.append(snap("A", 900))
        assert detect_price_drops([item("A", "900")], history) == []

    def test_handles_prices_with_currency_and_commas(self):
        drops = detect_price_drops(
            [item("A", "¥1,200")], [snap("A", 1500.0)]
        )
        assert len(drops) == 1
        assert drops[0].current_price == 1200

    def test_handles_wan_unit(self):
        drops = detect_price_drops([item("A", "1.2万")], [snap("A", 15000)])
        assert len(drops) == 1
        assert drops[0].current_price == 12000

    def test_invalid_current_price_is_skipped(self):
        for bad in ("价格异常", "暂无", "", None):
            assert detect_price_drops([item("A", bad)], [snap("A", 1000)]) == []

    def test_items_without_id_are_skipped(self):
        assert detect_price_drops([{"当前售价": "100"}], [snap("A", 1000)]) == []

    def test_multiple_items_each_reported_once(self):
        drops = detect_price_drops(
            [item("A", "900"), item("B", "800"), item("A", "880")],
            [snap("A", 1000), snap("B", 1000)],
        )
        assert [d.item_id for d in drops] == ["A", "B"], "同一商品只报一次"

    def test_history_without_matching_item(self):
        assert detect_price_drops([item("X", "900")], [snap("A", 1000)]) == []


class TestDropMessage:
    def _drop(self, **overrides) -> PriceDrop:
        base = dict(
            item_id="A", title="Sony A7M4 机身", link="https://e.com/i",
            previous_price=1299.0, current_price=1099.0,
            lowest_before=1299.0, drop_amount=200.0, drop_percent=15.4,
        )
        base.update(overrides)
        return PriceDrop(**base)

    def test_reason_mentions_prices_and_percent(self):
        reason = build_drop_reason(self._drop())
        assert "降价提醒" in reason
        assert "1299" in reason and "1099" in reason
        assert "200" in reason and "15.4" in reason
        assert "历史新低" in reason

    def test_reason_notes_repeat_send(self):
        assert "已推送过" in build_drop_reason(self._drop())

    def test_product_data_is_flat_for_notification_clients(self):
        """通知渠道读的是扁平键（与推荐通知保持同一结构）。"""
        data = build_drop_product_data(self._drop())
        assert data["商品标题"] == "Sony A7M4 机身"
        assert data["当前售价"] == "1099"
        assert data["商品链接"] == "https://e.com/i"
        assert "商品信息" not in data

    def test_image_key_always_present(self):
        """`商品主图链接` 始终存在（无图为空串），webhook 模板不会渲染出 None。"""
        assert build_drop_product_data(self._drop())["商品主图链接"] == ""
        data = build_drop_product_data(self._drop(image_url="https://img.e.com/a.jpg"))
        assert data["商品主图链接"] == "https://img.e.com/a.jpg"


class TestDropImage:
    """降价提醒要能配图：主图取自搜索列表的 picUrl。

    被去重跳过的商品不会再进详情页，列表里的 picUrl 是唯一图源。
    """

    def test_image_url_carried_from_search_item(self):
        rows = [
            {
                "商品ID": "A",
                "当前售价": "900",
                "商品标题": "Sony A7M4 机身",
                "商品链接": "https://e.com/i",
                "商品主图链接": "https://img.e.com/a.jpg",
            }
        ]
        drops = detect_price_drops(rows, [snap("A", 1000)])
        assert len(drops) == 1
        assert drops[0].image_url == "https://img.e.com/a.jpg"
        # 一路传到通知渠道读的字段上
        assert build_drop_product_data(drops[0])["商品主图链接"] == "https://img.e.com/a.jpg"

    def test_missing_image_yields_empty_string(self):
        drops = detect_price_drops([item("A", "900")], [snap("A", 1000)])
        assert drops[0].image_url == ""
        assert build_drop_product_data(drops[0])["商品主图链接"] == ""

    def test_none_image_yields_empty_string(self):
        """字段存在但为 None（接口偶尔这么给）不能变成 "None" 字符串。"""
        rows = [dict(item("A", "900"), **{"商品主图链接": None})]
        drops = detect_price_drops(rows, [snap("A", 1000)])
        assert drops[0].image_url == ""
