"""离线回放：抓取 → 判定 → 入库 全链路（P3 的测试夹具层）。

为什么要有这一层：``src/scraper.py`` 是 1300 行、把"驱动浏览器 / 操作筛选 UI /
解析 / 调度"揉在一起的单文件，而它**没有任何自动化测试**（上游 CI 也不跑 pytest）。
在没有这层网的前提下做抽取重构（P2 的 SiteAdapter）等于闭眼改。

这里用录制的 mtop 响应做"回放"：不启浏览器、不联网、不碰闲鱼，只把响应喂给解析层，
再走判定与入库，验证整条链路的数据形态。真实响应可用 ``tests/README.md`` 里的方法录制。

fixture 说明：
- ``search_results.json``：正常单条（既有）；
- ``search_results_mixed.json``：**坏条目 + "万" 价格 + 缺发布时间 + 正常条目**混合；
- ``search_results_empty.json``：页面确实没有商品；
- ``search_results_schema_changed.json``：字段结构变化（没有 resultList）。
"""
import asyncio
import json

import pytest

from src.infrastructure.persistence.storage_names import build_result_filename
from src.keyword_rule_engine import build_search_text, evaluate_keyword_rules
from src.parsers import _parse_search_results_json

pytestmark = pytest.mark.usefixtures("load_json_fixture")


def query_records(filename, limit=100):
    import src.services.result_storage_service as storage

    return storage.query_result_records(
        filename,
        ai_recommended_only=False,
        keyword_recommended_only=False,
        sort_by="crawl_time",
        sort_order="desc",
        page=1,
        limit=limit,
    )


def parse(fixture, load_json_fixture, source="fixture"):
    raw = load_json_fixture(fixture)
    return asyncio.run(_parse_search_results_json(raw, source))


# --------------------------------------------------------------- 解析层


def test_replay_normal_response(load_json_fixture):
    items = parse("search_results.json", load_json_fixture)
    assert len(items) == 1
    item = items[0]
    assert item["商品标题"] == "Sony A7M4 Body"
    assert item["当前售价"] == "¥13999"
    assert item["商品ID"] == "123456"
    assert item["包邮" if False else "商品标签"] == ["包邮", "验货宝"]
    assert item["商品链接"].startswith("https://www.goofish.com/")
    assert item["发布时间"] == "2024-03-10 00:00"


def test_replay_one_broken_item_does_not_kill_the_page(load_json_fixture, capsys):
    """回归：单个畸形条目曾经会让整页返回 []（被调用方当成"没有更多结果"）。"""
    items = parse("search_results_mixed.json", load_json_fixture)

    titles = [item["商品标题"] for item in items]
    assert "坏条目-价格异常" not in titles, "坏条目应被跳过"
    assert "iPhone 15 Pro" in titles, "坏条目之后的正常条目必须仍然解析出来"
    assert "缺发布时间的商品" in titles
    assert "Sony A7M4 Body" in titles
    assert len(items) >= 3, f"坏条目不应影响其它条目，实际只解析出 {titles}"

    output = capsys.readouterr().out
    assert "跳过解析失败的条目" in output, "跳过坏条目时必须留下日志"


def test_replay_wan_price_is_normalized(load_json_fixture):
    items = parse("search_results_mixed.json", load_json_fixture)
    wan = next(item for item in items if item["商品ID"] == "999")
    assert wan["当前售价"] == "¥12000", "1.2万 应换算为 12000"


def test_replay_missing_publish_time_is_tolerated(load_json_fixture):
    items = parse("search_results_mixed.json", load_json_fixture)
    item = next(i for i in items if i["商品ID"] == "888")
    assert item["发布时间"] == "未知时间"


def test_replay_empty_page_returns_empty(load_json_fixture, capsys):
    assert parse("search_results_empty.json", load_json_fixture) == []
    assert "未找到商品列表" in capsys.readouterr().out


def test_replay_schema_change_is_logged(load_json_fixture, capsys):
    """字段结构变化时当前契约是"返回空 + 打日志"。

    这里把这个契约固定下来：将来若要改成"抛错让任务失败得更明显"，
    会先看到这条测试失败，从而是有意识的改动而不是顺手改掉。
    """
    assert parse("search_results_schema_changed.json", load_json_fixture) == []
    assert "未找到商品列表" in capsys.readouterr().out


# --------------------------------------------------------------- 判定层


def make_record(item: dict, price: str | None = None) -> dict:
    product = dict(item)
    if price is not None:
        product["当前售价"] = price
    return {
        "搜索关键字": "sony a7m4",
        "任务名称": "Sony A7M4",
        "爬取时间": "2026-01-01T10:00:00",
        "商品信息": product,
        "卖家信息": {"卖家昵称": product.get("卖家昵称", "")},
    }


def test_replay_keyword_decision_on_parsed_item(load_json_fixture):
    item = parse("search_results.json", load_json_fixture)[0]
    record = make_record(item)

    result = evaluate_keyword_rules(["sony", "a7m4", "佳能"], build_search_text(record))

    assert result["analysis_source"] == "keyword"
    assert result["is_recommended"] is True
    assert result["keyword_hit_count"] >= 2
    assert "sony" in [k.lower() for k in result["matched_keywords"]]


def test_replay_keyword_decision_rejects_unrelated_item(load_json_fixture):
    item = parse("search_results.json", load_json_fixture)[0]
    record = make_record(item)

    result = evaluate_keyword_rules(["尼康", "canon"], build_search_text(record))

    assert result["is_recommended"] is False
    assert result["keyword_hit_count"] == 0


# --------------------------------------------------------------- 入库层


def test_replay_persists_parsed_records(monkeypatch, tmp_path, load_json_fixture):
    """回放整条链路：解析 → 判定 → 入库 → 读回。"""
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))

    import src.services.result_storage_service as storage
    from src.utils import save_to_jsonl

    items = parse("search_results_mixed.json", load_json_fixture)
    assert len(items) >= 3

    for item in items:
        record = make_record(item)
        record["ai_analysis"] = evaluate_keyword_rules(["sony", "iphone"], build_search_text(record))
        assert asyncio.run(save_to_jsonl(record, "sony a7m4")) is True

    total, rows = asyncio.run(query_records(build_result_filename("sony a7m4")))
    assert total == len(items)
    # 查询返回的是原始记录（含 商品信息 / ai_analysis），不是数据库列
    stored_ids = {row.get("商品信息", {}).get("商品ID") for row in rows}
    assert {"123456", "999", "888"} <= stored_ids

    recommended = [
        row for row in rows if (row.get("ai_analysis") or {}).get("is_recommended")
    ]
    assert recommended, "至少应有一条被判为推荐（标题里含 sony / iphone）"


def test_replay_is_idempotent_for_same_item(monkeypatch, tmp_path, load_json_fixture):
    """同一商品重复回放不应产生重复行（UNIQUE(result_filename, link_unique_key)）。"""
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))

    import src.services.result_storage_service as storage
    from src.utils import save_to_jsonl

    item = parse("search_results.json", load_json_fixture)[0]
    record = make_record(item)

    assert asyncio.run(save_to_jsonl(record, "sony a7m4")) is True
    assert asyncio.run(save_to_jsonl(record, "sony a7m4")) is True

    total, _ = asyncio.run(query_records(build_result_filename("sony a7m4")))
    assert total == 1
