"""量一量「成交价参考」在真实数据上到底能用成什么样（诊断工具）。

在正式把它接进推荐判定之前，必须先回答一个问题：**过滤后还剩多少样本？**
3570 条已售条目听起来很多，但那是"卖家 × 商品"的原始计数 —— 卖家会被重复推荐、
同一个已售商品会反复出现，而且卖家已售列表里大量商品跟当前型号无关。样本只有几十条
时中位数就是噪声，接进去只会污染推荐。

本脚本用 ``src.services.deal_price_service`` 本身的代码路径来统计，所以它给出的样本数
就是接入后的实际样本数，不是一个估算。

用法（容器内）::

    docker cp tools/deal_price_report.py ai-goofish-monitor-app:/tmp/report.py
    docker exec ai-goofish-monitor-app python /tmp/report.py
    # 需要机器可读输出时：--json /app/logs/deal_report.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "/app")

from src.services import deal_price_service as dps  # noqa: E402
from src.services.deal_price_service import build_deal_reference  # noqa: E402

DEFAULT_DB = "/app/data/app.sqlite3"
DEFAULT_CONFIG = "/app/config.json"


def load_task_rules(config_path: str) -> dict:
    """关键词 → 该任务的关键词规则（用于相关性过滤）。"""
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except Exception as exc:
        print(f"读取 {config_path} 失败（将退回子串过滤）: {exc}")
        return {}

    mapping = {}
    for task in config.get("tasks") or []:
        keyword = str(task.get("keyword") or "").strip()
        if not keyword:
            continue
        rules = [str(r) for r in (task.get("keyword_rules") or []) if str(r).strip()]
        mapping[keyword] = rules
    return mapping


def load_records(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "select keyword, crawl_time, raw_json from result_items"
        ).fetchall()
    finally:
        conn.close()

    records = []
    for keyword, crawl_time, raw in rows:
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        payload["抓取时间"] = crawl_time
        records.append((str(keyword or ""), payload))
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="统计成交价参考的真实可用样本量")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--json", default="", help="把结果写到 JSON 文件")
    parser.add_argument("--samples", type=int, default=8, help="每个关键词打印几条样例")
    args = parser.parse_args()

    rules_by_keyword = load_task_rules(args.config)
    records = load_records(args.db)
    if not records:
        print("result_items 里没有记录。")
        return 1

    by_keyword = {}
    for keyword, record in records:
        by_keyword.setdefault(keyword, []).append(record)

    print("共 %d 条结果记录，覆盖 %d 个关键词\n" % (len(records), len(by_keyword)))

    report = {}
    for keyword in sorted(by_keyword):
        recs = by_keyword[keyword]
        rules = rules_by_keyword.get(keyword, [])

        strict = build_deal_reference(recs, keyword=keyword, keyword_rules=rules)

        # 不过滤的对照：看相关性过滤到底砍掉了多少（也是"卖家历史≠该型号"的证据）
        unfiltered_samples = {}
        for record in recs:
            observed = str(record.get("抓取时间") or "")
            for sample in dps.extract_deal_samples(
                record,
                keyword=keyword,
                observed_at=observed,
                keyword_rules=rules,
                require_relevance=False,
            ):
                unfiltered_samples.setdefault(sample.item_id, sample)

        print("=" * 74)
        print("关键词: %s" % keyword)
        print("  任务关键词规则: %s" % (rules if rules else "（未配置，退回子串匹配）"))
        print("  该卖家全部已售（去重后）: %d 个商品" % len(unfiltered_samples))
        print("  与「%s」相关（去重后）: %d 个商品" % (keyword, strict["sample_count"]))

        if strict["sample_count"] == 0:
            print("  → 没有可用样本，无法给出成交价参考。")
        elif not strict["enough_samples"]:
            print("  → 样本不足（%d 条），中位数不可信，暂不能用于判断。" % strict["sample_count"])
            print("     %s" % strict.get("note", ""))
        else:
            print(
                "  → 中位数 ¥%g（P25 ¥%g / P75 ¥%g，区间 ¥%g~¥%g，样本 %d 条）"
                % (
                    strict["median"],
                    strict["p25"],
                    strict["p75"],
                    strict["min"],
                    strict["max"],
                    strict["sample_count"],
                )
            )

        if args.samples and strict["sample_count"]:
            print("  样例（判断过滤是否可靠）:")
            # 按商品去重后再打印：同一卖家被多次推荐时同一件已售会被反复抽到，
            # 直接打印会让人以为样本比实际多（计数是按商品去重的，展示口径要一致）
            printed: set[str] = set()
            shown = 0
            for record in recs:
                observed = str(record.get("抓取时间") or "")
                for sample in dps.extract_deal_samples(
                    record, keyword=keyword, observed_at=observed, keyword_rules=rules
                ):
                    if sample.item_id in printed:
                        continue
                    printed.add(sample.item_id)
                    print(
                        "    ¥%-8g %-40s [%s]"
                        % (sample.price, sample.title[:40], sample.seller[:10])
                    )
                    shown += 1
                    if shown >= args.samples:
                        break
                if shown >= args.samples:
                    break

        report[keyword] = {
            "rules": rules,
            "unfiltered_sold_items": len(unfiltered_samples),
            "relevant": strict,
        }

    print("=" * 74)
    print("\n提示：「已售」价格是成交价的代理指标（可能是挂牌价或最后一次改价），")
    print("      且卖家已售列表是**该卖家的全部历史**，不等于该型号的真实成交价分布。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print("已写出 %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
