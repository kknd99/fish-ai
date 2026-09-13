"""探查「已售」标签的数据契约（一次性诊断工具，不属于运行时）。

**为什么需要它**：解析器 ``_parse_user_items_data`` 里有 ``itemStatus → 在售/已售``
的映射，但采集端只点击过「信用及评价」标签，**从没点过「已售」**（见
``scrape_user_profile``）。所以"最近成交价"目前没有数据源，而那个接口
（``mtop.idle.web.xyh.item.list``）的已售参数与返回结构不能靠猜 —— 猜错了就会写出一套
永远解析不出数据的代码。

本脚本用**与真实爬虫完全相同的浏览器上下文**（复用 ``src.scraper`` 的指纹、请求头
过滤与增强快照恢复）打开卖家主页，把看到的所有 ``mtop.*`` 请求与响应落盘，并尝试
点击「已售」标签。跑完把输出文件交回分析，据此定型解析。

用法（在容器内跑，避免另建一套环境）::

    docker cp tools/probe_sold_tab.py ai-goofish-monitor-app:/tmp/probe.py
    docker exec -it ai-goofish-monitor-app python /tmp/probe.py \\
        --state state/1450.json --item 812345678901 --out /app/logs/sold_probe.json

``--item`` 与 ``--seller`` 二选一：给 ``--item`` 时会先打开商品详情页，从详情接口里
取 ``sellerDO.sellerId`` 再跳主页（卖家 ID 没有落库，只能这样拿）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# 允许 `python tools/probe_sold_tab.py` 与 `/tmp/probe.py` 两种跑法
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, "/app")

from playwright.async_api import async_playwright  # noqa: E402

from src.scraper import (  # noqa: E402
    _build_context_overrides,
    _build_extra_headers,
    _clean_kwargs,
    _default_context_options,
    _resolve_browser_channel,
)
from src.services.site_adapter import (  # noqa: E402
    DETAIL_API_URL_FRAGMENT,
    ITEM_LIST_API_URL_FRAGMENT,
    detail_response_predicate,
    get_site_adapter,
)

#: 「已售」标签的可能写法。不同版本/端上的文案不一样，逐个试，记录哪个命中。
SOLD_TAB_CANDIDATES = (
    "//div[text()='已售']/ancestor::li",
    "//span[text()='已售']/ancestor::li",
    "//div[contains(text(),'已售')]/ancestor::li",
    "//*[contains(text(),'卖出的')]/ancestor::li",
    "//*[contains(text(),'已卖出')]/ancestor::li",
)

#: 单个响应最多留多少字符（够看清结构即可，避免 dump 出几十 MB）
MAX_RESPONSE_CHARS = 120_000


def _load_snapshot(state_file: str) -> tuple[dict, object]:
    """把登录态文件读成 (context_kwargs_额外项, storage_state 参数)。

    与 ``scrape_xianyu`` 里那段保持同一逻辑：增强快照要用它自己的 cookies + 环境参数，
    普通 storage_state 直接整份交给 Playwright。
    """
    with open(state_file, encoding="utf-8") as fh:
        snapshot_data = json.load(fh)

    context_kwargs = _default_context_options()
    storage_state_arg: object = state_file

    if isinstance(snapshot_data, dict):
        if any(k in snapshot_data for k in ("env", "headers", "page", "storage")):
            print(f"[探查] 检测到增强快照，应用环境参数: {state_file}")
            storage_state_arg = {"cookies": snapshot_data.get("cookies", [])}
            context_kwargs.update(_build_context_overrides(snapshot_data))
            extra_headers = _build_extra_headers(snapshot_data.get("headers"))
            if extra_headers:
                context_kwargs["extra_http_headers"] = extra_headers
        else:
            storage_state_arg = snapshot_data

    return context_kwargs, storage_state_arg


def _item_status_summary(payload: object) -> dict:
    """统计 cardList 里 itemStatus 的取值分布 —— 直接回答"默认页有没有已售"。"""
    try:
        cards = payload["data"]["cardList"]  # type: ignore[index]
    except Exception:
        return {}
    counts: dict[str, int] = {}
    for card in cards or []:
        data = (card or {}).get("cardData") or {}
        key = str(data.get("itemStatus"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _dump_item_list_cards(payload: object) -> list:
    """抽出 cardList 的前几条，供人工看字段名（价格/状态/时间到底叫什么）。"""
    try:
        cards = payload["data"]["cardList"]  # type: ignore[index]
    except Exception:
        return []
    return cards[:3]


async def probe(args) -> int:
    state_file = args.state
    if not os.path.exists(state_file):
        print(f"错误：登录态文件不存在: {state_file}")
        return 2

    records: list[dict] = []
    item_list_responses: list[dict] = []
    seller_id = args.seller or ""

    context_kwargs, storage_state_arg = _load_snapshot(state_file)
    context_kwargs = _clean_kwargs(context_kwargs)

    async with async_playwright() as p:
        launch_kwargs = {"headless": not args.headful, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
        channel = _resolve_browser_channel()
        if channel:
            launch_kwargs["channel"] = channel
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            context = await browser.new_context(
                storage_state=storage_state_arg, **context_kwargs
            )
            page = await context.new_page()

            async def handle_response(response):
                url = str(response.url or "")
                if "mtop" not in url:
                    return
                entry = {
                    "url": url,
                    "status": response.status,
                    "request_post_data": None,
                }
                try:
                    entry["request_post_data"] = response.request.post_data
                except Exception:
                    pass
                # 只对关心的接口留响应体
                if ITEM_LIST_API_URL_FRAGMENT in url or DETAIL_API_URL_FRAGMENT in url:
                    try:
                        payload = await response.json()
                    except Exception as exc:
                        entry["parse_error"] = str(exc)
                    else:
                        entry["item_status_counts"] = _item_status_summary(payload)
                        entry["card_samples"] = _dump_item_list_cards(payload)
                        entry["response_json"] = json.dumps(
                            payload, ensure_ascii=False
                        )[:MAX_RESPONSE_CHARS]
                        item_list_responses.append(entry)
                records.append(entry)

            page.on("response", handle_response)

            # 第一步：拿到卖家 ID
            if not seller_id and args.item:
                item_url = (
                    args.item
                    if str(args.item).startswith("http")
                    else f"https://www.goofish.com/item?id={args.item}"
                )
                print(f"[探查] 打开商品详情页取卖家 ID: {item_url}")
                detail_payload = {}
                try:
                    async with page.expect_response(
                        detail_response_predicate(), timeout=25000
                    ) as detail_info:
                        await page.goto(item_url, wait_until="domcontentloaded", timeout=25000)
                    detail_payload = await (await detail_info.value).json()
                except Exception as exc:
                    print(f"[探查] 未能捕获详情接口: {exc}")
                seller_id = str(
                    ((detail_payload.get("data") or {}).get("sellerDO") or {}).get("sellerId") or ""
                )
                if seller_id:
                    print(f"[探查] 卖家 ID = {seller_id}")
                else:
                    print("[探查] 详情接口里没拿到 sellerId，请改用 --seller 直接指定")
                    return 3

            if not seller_id:
                print("错误：需要 --item 或 --seller 之一")
                return 2

            # 第二步：打开卖家主页，先看默认页
            profile_url = get_site_adapter().seller_profile_url(seller_id)
            print(f"[探查] 打开卖家主页: {profile_url}")
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(4000)

            print(f"[探查] 滚动默认页 {args.scrolls} 次…")
            for _ in range(args.scrolls):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2500)

            default_tab_counts = [
                r.get("item_status_counts") for r in item_list_responses if r.get("item_status_counts")
            ]
            print(f"[探查] 默认页 itemStatus 分布: {default_tab_counts}")

            # 第三步：尝试点「已售」标签
            clicked_locator = None
            if not args.no_tab_click:
                for locator in SOLD_TAB_CANDIDATES:
                    try:
                        node = page.locator(locator)
                        if await node.count() > 0:
                            await node.first.click()
                            clicked_locator = locator
                            print(f"[探查] 已点击「已售」标签，命中定位器: {locator}")
                            await page.wait_for_timeout(4000)
                            for _ in range(args.scrolls):
                                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                await page.wait_for_timeout(2500)
                            break
                    except Exception as exc:
                        print(f"[探查] 定位器失败 {locator}: {exc}")
                if not clicked_locator:
                    print("[探查] 没找到「已售」标签，页面上的可点标签：")
                    for label in ("在售", "已售", "卖出的宝贝", "信用及评价", "Ta的宝贝"):
                        n = await page.locator(f"//*[text()='{label}']").count()
                        print(f"         「{label}」出现 {n} 次")

            if args.screenshot:
                await page.screenshot(path=args.screenshot, full_page=True)

            report = {
                "seller_id": seller_id,
                "state_file": state_file,
                "clicked_locator": clicked_locator,
                "item_list_api_fragment": ITEM_LIST_API_URL_FRAGMENT,
                "requests": records,
                "item_list_responses": item_list_responses,
            }
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=2)
            print(f"\n[探查] 已写出 {args.out}")
            print(f"[探查] 共捕获 {len(records)} 个 mtop 请求，其中商品列表响应 {len(item_list_responses)} 个")
            print("[探查] 商品列表相关的请求 URL（去重）：")
            seen = set()
            for record in records:
                if ITEM_LIST_API_URL_FRAGMENT in record["url"] and record["url"] not in seen:
                    seen.add(record["url"])
                    print(f"         {record['url'][:200]}")
        finally:
            await browser.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="探查闲鱼卖家主页「已售」标签的数据契约")
    parser.add_argument("--state", default="xianyu_state.json", help="登录态文件路径")
    parser.add_argument("--item", default="", help="商品 ID 或商品 URL（用它反查卖家 ID）")
    parser.add_argument("--seller", default="", help="卖家 user_id（已知时直接用）")
    parser.add_argument("--out", default="logs/sold_probe.json", help="输出 JSON 路径")
    parser.add_argument("--scrolls", type=int, default=3, help="每个标签滚动次数")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口（排错用）")
    parser.add_argument("--no-tab-click", action="store_true", help="只采集默认页，不点已售标签")
    parser.add_argument("--screenshot", default="", help="截图保存路径（可选）")
    args = parser.parse_args()
    return asyncio.run(probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
