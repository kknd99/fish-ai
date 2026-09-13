import asyncio
import json
import os
import random
import sys
from datetime import datetime
from typing import Optional

from playwright.async_api import (
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from src.ai_handler import (
    download_all_images,
    get_ai_analysis,
    send_ntfy_notification,
    cleanup_task_images,
)
from src.config import (
    AI_DEBUG_MODE,
    LOGIN_IS_EDGE,
    RUN_HEADLESS,
    RUNNING_IN_DOCKER,
    SKIP_AI_ANALYSIS,
    STATE_FILE,
)
from src.parsers import (
    _parse_search_results_json,
    _parse_user_items_data,
    calculate_reputation_from_ratings,
    parse_ratings_data,
    parse_user_head_data,
)
from src.utils import (
    format_registration_days,
    get_link_unique_key,
    log_time,
    random_sleep,
    safe_get,
    save_to_jsonl,
)
from src.core.safe_paths import UnsafePathError, safe_account_state_path
from src.rotation import RotationPool, load_state_files, parse_proxy_pool, RotationItem
from src.services.decision import normalize_decision_mode
from src.services.trade.pipeline import build_trade_runner, resolve_trade_db_path
from src.services.site_adapter import (
    detail_response_predicate,
    get_site_adapter,
    search_response_predicate,
)
from src.services.rotation_policy import (
    blacklist_disabled_warning,
    compute_risk_control_backoff,
    can_rotate_account,
    can_rotate_proxy,
    compute_attempt_limit,
    rotation_unavailable_hint,
)
from src.failure_guard import FailureGuard
from src.services.account_strategy_service import resolve_account_runtime_plan
from src.infrastructure.persistence.storage_names import build_result_filename
from src.services.item_analysis_dispatcher import (
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)
from src.services.price_drop_service import (
    build_drop_product_data,
    build_drop_reason,
    detect_price_drops,
    resolve_thresholds as resolve_price_drop_thresholds,
)
from src.services.price_history_service import (
    build_market_reference,
    load_price_snapshots,
    record_market_snapshots,
)
from src.services.result_storage_service import load_processed_link_keys
from src.services.seller_profile_cache import (
    DEFAULT_MAX_ENTRIES as DEFAULT_SELLER_PROFILE_CACHE_ENTRIES,
)
from src.services.seller_profile_cache import SellerProfileCache
from src.services.search_pagination import (
    advance_search_page,
)


class RiskControlError(Exception):
    pass


class LoginRequiredError(Exception):
    """Raised when Goofish redirects to the passport/mini_login flow."""


FAILURE_GUARD = FailureGuard()
EDGE_DOCKER_WARNING_PRINTED = False


def _is_login_url(url: str) -> bool:
    """登录态失效判定（站点事实来自适配器）。"""
    return get_site_adapter().is_login_url(url)


def _resolve_browser_channel() -> str:
    global EDGE_DOCKER_WARNING_PRINTED
    if RUNNING_IN_DOCKER:
        if LOGIN_IS_EDGE and not EDGE_DOCKER_WARNING_PRINTED:
            print(
                "检测到 LOGIN_IS_EDGE=true，但 Docker 镜像未内置 Edge，"
                "任务运行时将改用 Chromium。"
            )
            EDGE_DOCKER_WARNING_PRINTED = True
        return "chromium"
    return "msedge" if LOGIN_IS_EDGE else "chrome"


def wait_for_debug_close(debug_limit: int) -> bool:
    """调试模式下等用户按回车再关浏览器；**非交互式（没有 TTY）直接跳过**。

    历史实现是无条件 ``input()``，而它位于 ``finally`` 里、且排在
    ``browser.close()`` 之前。当爬虫由 GUI / 调度器以子进程方式拉起
    （``SPIDER_DEBUG_LIMIT`` 就是这么传的），stdin 不是终端，``input()`` 会抛
    ``EOFError`` —— 结果不仅等待失败，**浏览器也不会被关闭**，一次本来成功的
    抓取还会被记成失败。
    """
    if not debug_limit:
        return False
    stdin = getattr(sys, "stdin", None)
    if stdin is None or not stdin.isatty():
        print("[调试] 当前不是交互式终端，跳过「按回车关闭浏览器」等待。")
        return False
    input("按回车键关闭浏览器...")
    return True


def _should_analyze_images(task_config: dict) -> bool:
    raw_value = task_config.get("analyze_images", True)
    if isinstance(raw_value, bool):
        return raw_value
    return str(raw_value).strip().lower() not in {"false", "0", "no", "off"}


def _format_failure_reason(reason: str, limit: int = 500) -> str:
    if not reason:
        return "未知错误"
    cleaned = " ".join(str(reason).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


async def _notify_price_drops(drops) -> None:
    """为创出新低的商品发送降价提醒。

    与推荐通知走同一条通道（含飞书图文卡片）；因为搜索结果里没有主图，
    卡片会自动降级成纯文本（飞书客户端在主图缺失时就发文本）。
    """
    for drop in drops:
        reason = build_drop_reason(drop)
        product_data = build_drop_product_data(drop)
        try:
            await send_ntfy_notification(product_data, reason)
            print(
                f"   [降价] 已提醒: {drop.title[:24]}... "
                f"¥{drop.previous_price:g} → ¥{drop.current_price:g}"
            )
        except Exception as exc:
            print(f"   [降价] 发送降价提醒失败: {exc}")


async def _notify_task_failure(
    task_config: dict, reason: str, *, cookie_path: Optional[str]
) -> None:
    task_name = task_config.get("task_name", "未命名任务")
    keyword = task_config.get("keyword", "")
    formatted_reason = _format_failure_reason(reason)

    # Some failures are deterministic misconfiguration and should pause/notify immediately.
    pause_immediately = any(
        marker in formatted_reason
        for marker in (
            "未找到可用的代理地址",
            "未找到可用的登录状态文件",
        )
    )

    guard_result = FAILURE_GUARD.record_failure(
        task_name,
        formatted_reason,
        cookie_path=cookie_path,
        min_failures_to_pause=1 if pause_immediately else None,
    )

    if not guard_result.get("should_notify"):
        print(
            f"[FailureGuard] 任务 '{task_name}' 失败计数 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold}，暂不通知。"
        )
        return

    paused_until = guard_result.get("paused_until")
    paused_until_str = (
        paused_until.strftime("%Y-%m-%d %H:%M:%S") if paused_until else "N/A"
    )

    product_data = {
        "商品标题": f"[任务异常] {task_name}",
        "当前售价": "N/A",
        "商品链接": "#",
    }
    notify_reason = (
        f"任务运行失败(已连续 {guard_result.get('consecutive_failures')}/{FAILURE_GUARD.threshold} 次): {formatted_reason}"
        f"\n任务: {task_name}"
        f"\n关键词: {keyword or 'N/A'}"
        f"\n已自动暂停重试，暂停到: {paused_until_str}"
        f"\n修复后(更新登录态/cookies文件)将自动恢复。"
    )

    try:
        await send_ntfy_notification(product_data, notify_reason)
    except Exception as e:
        print(f"发送任务异常通知失败: {e}")


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_rotation_settings(task_config: dict) -> dict:
    account_cfg = task_config.get("account_rotation") or {}
    proxy_cfg = task_config.get("proxy_rotation") or {}

    account_enabled = _as_bool(
        account_cfg.get("enabled"),
        _as_bool(os.getenv("ACCOUNT_ROTATION_ENABLED"), False),
    )
    account_mode = (
        account_cfg.get("mode") or os.getenv("ACCOUNT_ROTATION_MODE", "per_task")
    ).lower()
    account_state_dir = account_cfg.get("state_dir") or os.getenv(
        "ACCOUNT_STATE_DIR", "state"
    )
    account_retry_limit = _as_int(
        account_cfg.get("retry_limit"),
        _as_int(os.getenv("ACCOUNT_ROTATION_RETRY_LIMIT"), 2),
    )
    account_blacklist_ttl = _as_int(
        account_cfg.get("blacklist_ttl_sec"),
        _as_int(os.getenv("ACCOUNT_BLACKLIST_TTL"), 300),
    )

    proxy_enabled = _as_bool(
        proxy_cfg.get("enabled"), _as_bool(os.getenv("PROXY_ROTATION_ENABLED"), False)
    )
    proxy_mode = (
        proxy_cfg.get("mode") or os.getenv("PROXY_ROTATION_MODE", "per_task")
    ).lower()
    proxy_pool = proxy_cfg.get("proxy_pool") or os.getenv("PROXY_POOL", "")
    proxy_retry_limit = _as_int(
        proxy_cfg.get("retry_limit"),
        _as_int(os.getenv("PROXY_ROTATION_RETRY_LIMIT"), 2),
    )
    proxy_blacklist_ttl = _as_int(
        proxy_cfg.get("blacklist_ttl_sec"),
        _as_int(os.getenv("PROXY_BLACKLIST_TTL"), 300),
    )

    return {
        "account_enabled": account_enabled,
        "account_mode": account_mode,
        "account_state_dir": account_state_dir,
        "account_retry_limit": max(1, account_retry_limit),
        "account_blacklist_ttl": max(0, account_blacklist_ttl),
        "proxy_enabled": proxy_enabled,
        "proxy_mode": proxy_mode,
        "proxy_pool": proxy_pool,
        "proxy_retry_limit": max(1, proxy_retry_limit),
        "proxy_blacklist_ttl": max(0, proxy_blacklist_ttl),
    }


#: AI 分析并发上限。该值可由任务配置给出（历史上没有上限），
#: 它直接决定 LLM 并发与花费，因此在这里封顶（安全审计：可经 API 设成任意大）。
MAX_AI_ANALYSIS_CONCURRENCY = 8

#: 单任务最多翻页数。上限用于约束抓取量与风控暴露面。
MAX_TASK_PAGES = 20

#: 卖家主页/评价列表的最大滚动次数。
#: 原实现只靠 8 秒空闲超时兜底：页面若持续分页就会一直滚动，
#: 同时把抓到的条目全堆在内存里（all_items / all_ratings）。
MAX_SELLER_PROFILE_SCROLLS = 30


def _get_ai_analysis_concurrency(task_config: dict) -> int:
    configured = task_config.get("ai_analysis_concurrency")
    default = _as_int(os.getenv("AI_ANALYSIS_CONCURRENCY"), 2)
    return min(MAX_AI_ANALYSIS_CONCURRENCY, max(1, _as_int(configured, default)))


def _get_max_pages(task_config: dict) -> int:
    """取单任务翻页数并封顶（历史数据里可能存在超大值）。"""
    return min(MAX_TASK_PAGES, max(1, _as_int(task_config.get("max_pages"), 1)))


MAX_SELLER_PROFILE_CACHE_ENTRIES = 2000


def _get_seller_profile_cache_max_entries(task_config: dict) -> int:
    """卖家资料缓存容量上限（封顶，避免长时间运行堆积深拷贝）。"""
    configured = task_config.get("seller_profile_cache_max_entries")
    default = _as_int(os.getenv("SELLER_PROFILE_CACHE_MAX_ENTRIES"), DEFAULT_SELLER_PROFILE_CACHE_ENTRIES)
    return min(MAX_SELLER_PROFILE_CACHE_ENTRIES, max(1, _as_int(configured, default)))


def _get_seller_profile_cache_ttl(task_config: dict) -> int:
    configured = task_config.get("seller_profile_cache_ttl")
    default = _as_int(os.getenv("SELLER_PROFILE_CACHE_TTL"), 1800)
    return max(0, _as_int(configured, default))


def _default_context_options() -> dict:
    return {
        "user_agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
        "viewport": {"width": 412, "height": 915},
        "device_scale_factor": 2.625,
        "is_mobile": True,
        "has_touch": True,
        "locale": "zh-CN",
        "timezone_id": "Asia/Shanghai",
        "permissions": ["geolocation"],
        "geolocation": {"longitude": 121.4737, "latitude": 31.2304},
        "color_scheme": "light",
    }


def _clean_kwargs(options: dict) -> dict:
    return {k: v for k, v in options.items() if v is not None}


def _looks_like_mobile(ua: str) -> Optional[bool]:
    if not ua:
        return None
    ua_lower = ua.lower()
    if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
        return True
    if "windows" in ua_lower or "macintosh" in ua_lower:
        return False
    return None


def _build_context_overrides(snapshot: dict) -> dict:
    env = snapshot.get("env") or {}
    headers = snapshot.get("headers") or {}
    navigator = env.get("navigator") or {}
    screen = env.get("screen") or {}
    intl = env.get("intl") or {}

    overrides = {}

    ua = (
        headers.get("User-Agent")
        or headers.get("user-agent")
        or navigator.get("userAgent")
    )
    if ua:
        overrides["user_agent"] = ua

    accept_language = headers.get("Accept-Language") or headers.get("accept-language")
    locale = None
    if accept_language:
        locale = accept_language.split(",")[0].strip()
    elif navigator.get("language"):
        locale = navigator["language"]
    if locale:
        overrides["locale"] = locale

    tz = intl.get("timeZone")
    if tz:
        overrides["timezone_id"] = tz

    width = screen.get("width")
    height = screen.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        overrides["viewport"] = {"width": int(width), "height": int(height)}

    dpr = screen.get("devicePixelRatio")
    if isinstance(dpr, (int, float)):
        overrides["device_scale_factor"] = float(dpr)

    touch_points = navigator.get("maxTouchPoints")
    if isinstance(touch_points, (int, float)):
        overrides["has_touch"] = touch_points > 0

    mobile_flag = _looks_like_mobile(ua or "")
    if mobile_flag is not None:
        overrides["is_mobile"] = mobile_flag

    return _clean_kwargs(overrides)


#: 不能透传给浏览器的请求头。
#:
#: 快照里的 headers 是**浏览器自己生成**的，属于 Fetch 规范里的 forbidden header
#: names。通过 CDP 强行设置（Playwright 的 extra_http_headers 就是这么做的）会让
#: 请求被网络栈判为非法：实测在搜索页只要带上 `Sec-Fetch-*`，站点的跨域资源
#: （g.alicdn.com 上的 JS/CSS）全部以 `net::ERR_INVALID_ARGUMENT` 失败，SPA 渲染
#: 不出来、正文为空，于是抓不到任何商品。
#:
#: 实测（同一份登录态，仅改请求头）：
#:   带 Sec-Fetch-*（三选一即可复现）→ 13 个资源失败、正文 0 字符
#:   去掉全部 Sec-Fetch-*            → 6 个资源失败、正文约 6600 字符，正常渲染
#: 该结论交替重复 3 轮均一致，非站点抖动。
_FORBIDDEN_HEADER_NAMES = {
    "accept-charset",
    "accept-encoding",
    "access-control-request-headers",
    "access-control-request-method",
    "connection",
    "content-length",
    "cookie",
    "cookie2",
    "date",
    "dnt",
    "expect",
    "host",
    "keep-alive",
    "origin",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "via",
}


def _is_forbidden_header(name: str) -> bool:
    """判断某个请求头是否由浏览器控制、不允许透传。"""
    lowered = name.lower()
    if lowered in _FORBIDDEN_HEADER_NAMES:
        return True
    # `Sec-` 前缀（Sec-Fetch-*、sec-ch-ua*）本身就是规范规定的保留前缀。
    if lowered.startswith("sec-"):
        return True
    return lowered.startswith("proxy-")


def _build_snapshot_init_script(snapshot: dict) -> str:
    """按快照恢复浏览器状态：localStorage / sessionStorage 与指纹字段。

    为什么需要这个（实测）：

    1. **快照里的 storage 从来没被用上**。扩展导出的 `storage.local` 里带着风控引擎
       自己的状态（`baxia_entry_config`、`baxia_entry_config_time`）；代码此前只用
       `storage` 这个键判断"这是增强快照"，却从不把内容恢复到浏览器里。于是每次
       启动爬虫，站点看到的是一个**完全没有风控状态的陌生客户端**，直接弹验证框。
    2. **指纹自相矛盾**。UA 被覆盖成"Windows Chrome 117"，但容器里实际是 Linux 上的
       Chromium，`navigator.platform` 仍是 `Linux x86_64`、`userAgentData.brands`
       仍是真实引擎版本（实测 `Chromium 151`）。这类"嘴上一个样、身上另一个样"的
       组合正是无头检测最常抓的信号。

    这里把 `navigator.platform`、`navigator.userAgentData` 对齐到快照声明，
    并把 storage 里的键值写回对应的存储。

    注意：Client Hints **请求头**（`sec-ch-ua`）由浏览器网络栈生成，脚本改不了；
    要让它也一致需要装真正的 Chrome（见 docs）。
    """
    env = snapshot.get("env") or {}
    navigator_info = env.get("navigator") or {}
    storage = snapshot.get("storage") or {}

    payload = {
        "platform": navigator_info.get("platform"),
        "uaData": navigator_info.get("userAgentData"),
        "languages": navigator_info.get("languages"),
        "hardwareConcurrency": navigator_info.get("hardwareConcurrency"),
        "deviceMemory": navigator_info.get("deviceMemory"),
        "maxTouchPoints": navigator_info.get("maxTouchPoints"),
        "local": storage.get("local") or {},
        "session": storage.get("session") or {},
    }

    return f"""
(() => {{
  const data = {json.dumps(payload, ensure_ascii=False)};
  const define = (name, value) => {{
    try {{ if (value !== null && value !== undefined) Object.defineProperty(navigator, name, {{get: () => value}}); }} catch (e) {{}}
  }};

  // 1) 对齐 navigator.platform（快照声明 Win32，容器里实际是 Linux x86_64）
  define('platform', data.platform);

  // 2) 对齐语言、CPU/内存、触摸点数。
  //    旧脚本把它们写死成移动端值（maxTouchPoints=5、4 种语言），
  //    与"Windows 桌面 Chrome"的 UA 自相矛盾。
  if (Array.isArray(data.languages) && data.languages.length) define('languages', data.languages);
  define('hardwareConcurrency', data.hardwareConcurrency);
  define('deviceMemory', data.deviceMemory);
  define('maxTouchPoints', data.maxTouchPoints);

  // 3) 对齐 userAgentData（快照声明 Chrome 117，实际是 Chromium 151）
  try {{
    const ua = data.uaData;
    if (ua && typeof ua === 'object') {{
      const brands = (ua.brands || []).map((b) => ({{brand: b.brand, version: String(b.version)}}));
      const uaData = {{
        brands: brands,
        mobile: !!ua.mobile,
        platform: ua.platform,
        getHighEntropyValues: () => Promise.resolve({{
          architecture: '', bitness: '', brands: brands,
          mobile: !!ua.mobile, model: '', platform: ua.platform,
          platformVersion: '', uaFullVersion: '', wow64: false,
        }}),
        toJSON: () => ({{brands: brands, mobile: !!ua.mobile, platform: ua.platform}}),
      }};
      Object.defineProperty(navigator, 'userAgentData', {{get: () => uaData}});
    }}
  }} catch (e) {{}}

  // 4) 恢复 storage（含风控引擎状态）——opaque origin 上访问会抛异常，忽略即可
  const restore = (store, items) => {{
    try {{
      for (const key of Object.keys(items || {{}})) {{
        store.setItem(key, items[key]);
      }}
    }} catch (e) {{}}
  }};
  try {{ restore(window.localStorage, data.local); }} catch (e) {{}}
  try {{ restore(window.sessionStorage, data.session); }} catch (e) {{}}
}})();
"""


def _build_extra_headers(raw_headers: Optional[dict]) -> dict:
    """从快照的 headers 里挑出可以安全透传的部分。

    只保留浏览器不控制、且确实有意义的那些（例如 Accept-Language）。
    其余一律丢弃：它们要么由浏览器按当前上下文自动生成，要么会破坏请求。
    """
    if not raw_headers:
        return {}
    headers = {}
    for key, value in raw_headers.items():
        if not key or value is None:
            continue
        if _is_forbidden_header(key):
            continue
        headers[key] = value
    return headers


async def scrape_user_profile(context, user_id: str) -> dict:
    """
    【新版】访问指定用户的个人主页，按顺序采集其摘要信息、完整的商品列表和完整的评价列表。
    """
    print(f"   -> 开始采集用户ID: {user_id} 的完整信息...")
    profile_data = {}
    page = await context.new_page()

    # 为各项异步任务准备Future和数据容器
    head_api_future = asyncio.get_event_loop().create_future()

    all_items, all_ratings = [], []
    stop_item_scrolling, stop_rating_scrolling = asyncio.Event(), asyncio.Event()

    async def handle_response(response: Response):
        # 捕获头部摘要API
        if (
            "mtop.idle.web.user.page.head" in response.url
            and not head_api_future.done()
        ):
            try:
                head_api_future.set_result(await response.json())
                print(f"      [API捕获] 用户头部信息... 成功")
            except Exception as e:
                if not head_api_future.done():
                    head_api_future.set_exception(e)

        # 捕获商品列表API
        elif "mtop.idle.web.xyh.item.list" in response.url:
            try:
                data = await response.json()
                all_items.extend(data.get("data", {}).get("cardList", []))
                print(f"      [API捕获] 商品列表... 当前已捕获 {len(all_items)} 件")
                if not data.get("data", {}).get("nextPage", True):
                    stop_item_scrolling.set()
            except Exception as e:
                stop_item_scrolling.set()

        # 捕获评价列表API
        elif "mtop.idle.web.trade.rate.list" in response.url:
            try:
                data = await response.json()
                all_ratings.extend(data.get("data", {}).get("cardList", []))
                print(f"      [API捕获] 评价列表... 当前已捕获 {len(all_ratings)} 条")
                if not data.get("data", {}).get("nextPage", True):
                    stop_rating_scrolling.set()
            except Exception as e:
                stop_rating_scrolling.set()

    page.on("response", handle_response)

    try:
        # --- 任务1: 导航并采集头部信息 ---
        await page.goto(
            get_site_adapter().seller_profile_url(user_id),
            wait_until="domcontentloaded",
            timeout=20000,
        )
        head_data = await asyncio.wait_for(head_api_future, timeout=15)
        profile_data = await parse_user_head_data(head_data)

        # --- 任务2: 滚动加载所有商品 (默认页面) ---
        print("      [采集阶段] 开始采集该用户的商品列表...")
        await random_sleep(2, 4)  # 等待第一页商品API完成
        scroll_rounds = 0
        while not stop_item_scrolling.is_set():
            if scroll_rounds >= MAX_SELLER_PROFILE_SCROLLS:
                print(
                    f"      [滚动上限] 商品列表已滚动 {MAX_SELLER_PROFILE_SCROLLS} 次，停止加载。"
                )
                break
            scroll_rounds += 1
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            try:
                await asyncio.wait_for(stop_item_scrolling.wait(), timeout=8)
            except asyncio.TimeoutError:
                print("      [滚动超时] 商品列表可能已加载完毕。")
                break
        profile_data["卖家发布的商品列表"] = await _parse_user_items_data(all_items)

        # --- 任务3: 点击并采集所有评价 ---
        print("      [采集阶段] 开始采集该用户的评价列表...")
        rating_tab_locator = page.locator("//div[text()='信用及评价']/ancestor::li")
        if await rating_tab_locator.count() > 0:
            await rating_tab_locator.click()
            await random_sleep(3, 5)  # 等待第一页评价API完成

            rating_scroll_rounds = 0
            while not stop_rating_scrolling.is_set():
                if rating_scroll_rounds >= MAX_SELLER_PROFILE_SCROLLS:
                    print(
                        f"      [滚动上限] 评价列表已滚动 {MAX_SELLER_PROFILE_SCROLLS} 次，停止加载。"
                    )
                    break
                rating_scroll_rounds += 1
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try:
                    await asyncio.wait_for(stop_rating_scrolling.wait(), timeout=8)
                except asyncio.TimeoutError:
                    print("      [滚动超时] 评价列表可能已加载完毕。")
                    break

            profile_data["卖家收到的评价列表"] = await parse_ratings_data(all_ratings)
            reputation_stats = await calculate_reputation_from_ratings(all_ratings)
            profile_data.update(reputation_stats)
        else:
            print("      [警告] 未找到评价选项卡，跳过评价采集。")

    except Exception as e:
        print(f"   [错误] 采集用户 {user_id} 信息时发生错误: {e}")
    finally:
        page.remove_listener("response", handle_response)
        await page.close()
        print(f"   -> 用户 {user_id} 信息采集完成。")

    return profile_data


async def scrape_xianyu(task_config: dict, debug_limit: int = 0):
    """
    【核心执行器】
    根据单个任务配置，异步爬取闲鱼商品数据，并对每个新发现的商品进行实时的、独立的AI分析和通知。
    """
    keyword = task_config["keyword"]
    max_pages = _get_max_pages(task_config)
    personal_only = task_config.get("personal_only", False)
    min_price = task_config.get("min_price")
    max_price = task_config.get("max_price")
    ai_prompt_text = task_config.get("ai_prompt_text", "")
    analyze_images = _should_analyze_images(task_config)
    # 归一化依据是判定策略注册表（新增策略后无需改这里）
    decision_mode = normalize_decision_mode(task_config.get("decision_mode"))
    keyword_rules = task_config.get("keyword_rules") or []
    free_shipping = task_config.get("free_shipping", False)
    raw_new_publish = task_config.get("new_publish_option") or ""
    new_publish_option = raw_new_publish.strip()
    if new_publish_option == "__none__":
        new_publish_option = ""
    region_filter = (task_config.get("region") or "").strip()

    processed_links = set()
    history_run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    history_seen_item_ids: set[str] = set()
    historical_snapshots = load_price_snapshots(keyword)
    result_filename = build_result_filename(keyword)
    processed_links = load_processed_link_keys(keyword)
    if processed_links:
        print(
            f"LOG: 该关键词在数据库中有历史记录（去重标识「{result_filename}」），"
            f"已加载 {len(processed_links)} 个商品用于去重。"
        )
    else:
        # 措辞刻意点明这是"数据库里的去重标识"：此前写的是
        # "结果集 xxx.jsonl 当前为空"，读起来像是在讲一个 jsonl 文件，
        # 害得人去 jsonl/ 目录里翻（实际结果只写 SQLite，那个后缀只是标识的一部分）。
        print(
            f"LOG: 该关键词在数据库中没有历史记录（去重标识「{result_filename}」），"
            "本次结果将写入数据库。"
        )

    rotation_settings = _get_rotation_settings(task_config)
    account_items = load_state_files(rotation_settings["account_state_dir"])
    runtime_plan = resolve_account_runtime_plan(
        strategy=task_config.get("account_strategy"),
        account_state_file=task_config.get("account_state_file"),
        has_root_state_file=os.path.exists(STATE_FILE),
        available_account_files=account_items,
    )
    forced_account = runtime_plan["forced_account"]
    if runtime_plan["prefer_root_state"]:
        account_items = [STATE_FILE]
        rotation_settings["account_enabled"] = False
    elif runtime_plan["use_account_pool"]:
        rotation_settings["account_enabled"] = True
    else:
        rotation_settings["account_enabled"] = False

    account_pool = RotationPool(
        account_items, rotation_settings["account_blacklist_ttl"], "account"
    )
    proxy_pool = RotationPool(
        parse_proxy_pool(rotation_settings["proxy_pool"]),
        rotation_settings["proxy_blacklist_ttl"],
        "proxy",
    )

    selected_account: Optional[RotationItem] = None
    selected_proxy: Optional[RotationItem] = None

    def _select_account(force_new: bool = False) -> Optional[RotationItem]:
        nonlocal selected_account
        if forced_account:
            return RotationItem(value=forced_account)
        if not rotation_settings["account_enabled"]:
            if os.path.exists(STATE_FILE):
                return RotationItem(value=STATE_FILE)
            return None
        if (
            rotation_settings["account_mode"] == "per_task"
            and selected_account
            and not force_new
        ):
            return selected_account
        picked = account_pool.pick_random()
        return picked or selected_account

    def _select_proxy(force_new: bool = False) -> Optional[RotationItem]:
        nonlocal selected_proxy
        if not rotation_settings["proxy_enabled"]:
            return None
        if (
            rotation_settings["proxy_mode"] == "per_task"
            and selected_proxy
            and not force_new
        ):
            return selected_proxy
        picked = proxy_pool.pick_random()
        return picked or selected_proxy

    async def _run_scrape_attempt(state_file: str, proxy_server: Optional[str]) -> int:
        processed_item_count = 0
        stop_scraping = False

        # 账号登录态文件必须落在受控目录内：它会被当作 Playwright 的
        # storage_state 读取，任意路径等于"任意可读 JSON 都能被当成 cookie 使用"。
        try:
            state_file = str(safe_account_state_path(state_file))
        except UnsafePathError as exc:
            raise FileNotFoundError(
                f"登录状态文件路径不合法，已拒绝使用: {state_file} ({exc})"
            ) from exc

        if not os.path.exists(state_file):
            raise FileNotFoundError(f"登录状态文件不存在: {state_file}")

        snapshot_data = None
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                snapshot_data = json.load(f)
        except Exception as e:
            print(f"警告：读取登录状态文件失败，将直接按路径使用: {e}")

        async with async_playwright() as p:
            # 反检测启动参数
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ]

            launch_kwargs = {"headless": RUN_HEADLESS, "args": launch_args}
            if proxy_server:
                launch_kwargs["proxy"] = {"server": proxy_server}

            launch_kwargs["channel"] = _resolve_browser_channel()

            browser = await p.chromium.launch(**launch_kwargs)

            context_kwargs = _default_context_options()
            storage_state_arg = state_file
            analysis_dispatcher: Optional[ItemAnalysisDispatcher] = None

            if isinstance(snapshot_data, dict):
                # 新版扩展导出的增强快照，包含环境和Header
                if any(
                    key in snapshot_data
                    for key in ("env", "headers", "page", "storage")
                ):
                    print(f"检测到增强浏览器快照，应用环境参数: {state_file}")
                    storage_state_arg = {"cookies": snapshot_data.get("cookies", [])}
                    context_kwargs.update(_build_context_overrides(snapshot_data))
                    extra_headers = _build_extra_headers(snapshot_data.get("headers"))
                    if extra_headers:
                        context_kwargs["extra_http_headers"] = extra_headers
                else:
                    storage_state_arg = snapshot_data

            context_kwargs = _clean_kwargs(context_kwargs)
            context = await browser.new_context(
                storage_state=storage_state_arg, **context_kwargs
            )
            seller_profile_cache = SellerProfileCache(
                ttl_seconds=_get_seller_profile_cache_ttl(task_config),
                max_entries=_get_seller_profile_cache_max_entries(task_config),
            )
            analysis_dispatcher = ItemAnalysisDispatcher(
                concurrency=_get_ai_analysis_concurrency(task_config),
                skip_ai_analysis=SKIP_AI_ANALYSIS,
                seller_loader=lambda user_id: seller_profile_cache.get_or_load(
                    str(user_id),
                    lambda seller_key: scrape_user_profile(context, seller_key),
                ),
                image_downloader=download_all_images,
                ai_analyzer=get_ai_analysis,
                notifier=send_ntfy_notification,
                saver=save_to_jsonl,
                # 交易链路：任务未开启交易时为 None，等于完全不接（默认部署行为不变）
                trade_runner=build_trade_runner(
                    task_config,
                    db_path=resolve_trade_db_path(),
                ),
            )

            # 增强反检测脚本（模拟真实移动设备）
            await context.add_init_script("""
                // 移除webdriver标识
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

                // 模拟真实移动设备的navigator属性
                Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});

                // 添加chrome对象
                window.chrome = {runtime: {}, loadTimes: function() {}, csi: function() {}};

                // 模拟触摸支持
                Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});

                /*
                 * 覆盖permissions查询（避免暴露自动化）
                 */
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                        Promise.resolve({state: Notification.permission}) :
                        originalQuery(parameters)
                );
            """)

            # 按快照恢复 storage 与指纹字段（详见 _build_snapshot_init_script）
            if isinstance(snapshot_data, dict) and any(
                key in snapshot_data for key in ("env", "headers", "page", "storage")
            ):
                await context.add_init_script(_build_snapshot_init_script(snapshot_data))

            page = await context.new_page()

            try:
                # 步骤 0 - 模拟真实用户：先访问首页（重要的反检测措施）
                log_time("步骤 0 - 模拟真实用户访问首页...")
                await page.goto(
                    get_site_adapter().home_url(),
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                log_time("[反爬] 在首页停留，模拟浏览...")
                await random_sleep(1, 2)

                # 模拟随机滚动（移动设备的触摸滚动）
                await page.evaluate("window.scrollBy(0, Math.random() * 500 + 200)")
                await random_sleep(1, 2)

                log_time("步骤 1 - 导航到搜索结果页...")
                # 使用 'q' 参数构建正确的搜索URL，并进行URL编码
                search_url = get_site_adapter().search_url(keyword)
                log_time(f"目标URL: {search_url}")

                # 先监听搜索接口响应，再执行导航，避免错过首次请求
                async with page.expect_response(
                    search_response_predicate(), timeout=30000
                ) as initial_response_info:
                    await page.goto(
                        search_url, wait_until="domcontentloaded", timeout=60000
                    )
                if _is_login_url(page.url):
                    raise LoginRequiredError(
                        f"Login required: redirected to {page.url} (cookies/state likely expired)"
                    )

                # 捕获初始搜索的API数据
                initial_response = await initial_response_info.value

                # 等待页面加载出关键筛选元素，以确认已成功进入搜索结果页
                try:
                    await page.wait_for_selector("text=新发布", timeout=15000)
                except PlaywrightTimeoutError as e:
                    if _is_login_url(page.url):
                        raise LoginRequiredError(
                            f"Login required: redirected to {page.url} (cookies/state likely expired)"
                        ) from e
                    raise

                # 模拟真实用户行为：页面加载后的初始停留和浏览
                log_time("[反爬] 模拟用户查看页面...")
                await random_sleep(1, 3)

                # --- 新增：检查是否存在验证弹窗 ---
                adapter = get_site_adapter()
                baxia_dialog = page.locator(adapter.dialog_risk_selector())
                middleware_widget = page.locator(adapter.middleware_risk_selector())
                try:
                    # 等待弹窗在2秒内出现。如果出现，则执行块内代码。
                    await baxia_dialog.wait_for(state="visible", timeout=2000)
                    print(
                        "\n==================== CRITICAL BLOCK DETECTED ===================="
                    )
                    print("检测到闲鱼反爬虫验证弹窗 (baxia-dialog)，无法继续操作。")
                    print("这通常是因为操作过于频繁或被识别为机器人。")
                    print("建议：")
                    print("1. 停止脚本一段时间再试。")
                    print(
                        "2. (推荐) 在 .env 文件中设置 RUN_HEADLESS=false，以非无头模式运行，这有助于绕过检测。"
                    )
                    print(f"任务 '{keyword}' 将在此处中止。")
                    print(
                        "==================================================================="
                    )
                    raise RiskControlError("baxia-dialog")
                except PlaywrightTimeoutError:
                    # 2秒内弹窗未出现，这是正常情况，继续执行
                    pass

                # 检查是否有J_MIDDLEWARE_FRAME_WIDGET覆盖层
                try:
                    await middleware_widget.wait_for(state="visible", timeout=2000)
                    print(
                        "\n==================== CRITICAL BLOCK DETECTED ===================="
                    )
                    print(
                        "检测到闲鱼反爬虫验证弹窗 (J_MIDDLEWARE_FRAME_WIDGET)，无法继续操作。"
                    )
                    print("这通常是因为操作过于频繁或被识别为机器人。")
                    print("建议：")
                    print("1. 停止脚本一段时间再试。")
                    print("2. (推荐) 更新登录状态文件，确保登录状态有效。")
                    print("3. 降低任务执行频率，避免被识别为机器人。")
                    print(f"任务 '{keyword}' 将在此处中止。")
                    print(
                        "==================================================================="
                    )
                    raise RiskControlError("J_MIDDLEWARE_FRAME_WIDGET")
                except PlaywrightTimeoutError:
                    # 2秒内弹窗未出现，这是正常情况，继续执行
                    pass
                # --- 结束新增 ---

                try:
                    await page.click("div[class*='closeIconBg']", timeout=3000)
                    print("LOG: 已关闭广告弹窗。")
                except PlaywrightTimeoutError:
                    print("LOG: 未检测到广告弹窗。")

                final_response = None
                log_time("步骤 2 - 应用筛选条件...")
                if new_publish_option:
                    try:
                        await page.click("text=新发布")
                        await random_sleep(1, 2)  # 原来是 (1.5, 2.5)
                        async with page.expect_response(
                            search_response_predicate(), timeout=20000
                        ) as response_info:
                            await page.click(f"text={new_publish_option}")
                            # --- 修改: 增加排序后的等待时间 ---
                            await random_sleep(2, 4)  # 原来是 (3, 5)
                        final_response = await response_info.value
                    except PlaywrightTimeoutError:
                        log_time(
                            f"新发布筛选 '{new_publish_option}' 请求超时，继续执行。"
                        )
                    except Exception as e:
                        print(f"LOG: 应用新发布筛选失败: {e}")

                if personal_only:
                    async with page.expect_response(
                        search_response_predicate(), timeout=20000
                    ) as response_info:
                        await page.click("text=个人闲置")
                        # --- 修改: 将固定等待改为随机等待，并加长 ---
                        await random_sleep(2, 4)  # 原来是 asyncio.sleep(5)
                    final_response = await response_info.value

                if free_shipping:
                    try:
                        async with page.expect_response(
                            search_response_predicate(), timeout=20000
                        ) as response_info:
                            await page.click("text=包邮")
                            await random_sleep(2, 4)
                        final_response = await response_info.value
                    except PlaywrightTimeoutError:
                        log_time("包邮筛选请求超时，继续执行。")
                    except Exception as e:
                        print(f"LOG: 应用包邮筛选失败: {e}")

                if region_filter:
                    try:
                        area_trigger = page.get_by_text("区域", exact=True)
                        if await area_trigger.count():
                            await area_trigger.first.click()
                            await random_sleep(1.5, 2)
                            popover_candidates = page.locator("div.ant-popover")
                            popover = popover_candidates.filter(
                                has=page.locator(
                                    ".areaWrap--FaZHsn8E, [class*='areaWrap']"
                                )
                            ).last
                            if not await popover.count():
                                popover = popover_candidates.filter(
                                    has=page.get_by_text("重新定位")
                                ).last
                            if not await popover.count():
                                popover = popover_candidates.filter(
                                    has=page.get_by_text("查看")
                                ).last
                            if not await popover.count():
                                print("LOG: 未找到区域弹窗，跳过区域筛选。")
                                raise PlaywrightTimeoutError("region-popover-not-found")
                            await popover.wait_for(state="visible", timeout=5000)

                            # 列表容器：第一层 children 即省/市/区三列，不再强依赖具体类名，提升鲁棒性
                            area_wrap = popover.locator(
                                ".areaWrap--FaZHsn8E, [class*='areaWrap']"
                            ).first
                            await area_wrap.wait_for(state="visible", timeout=3000)
                            columns = area_wrap.locator(":scope > div")
                            col_prov = columns.nth(0)
                            col_city = columns.nth(1)
                            col_dist = columns.nth(2)

                            region_parts = [
                                p.strip() for p in region_filter.split("/") if p.strip()
                            ]

                            async def _click_in_column(
                                column_locator, text_value: str, desc: str
                            ) -> None:
                                option = column_locator.locator(
                                    ".provItem--QAdOx8nD", has_text=text_value
                                ).first
                                if await option.count():
                                    await option.click()
                                    await random_sleep(1.5, 2)
                                    try:
                                        await option.wait_for(
                                            state="attached", timeout=1500
                                        )
                                        await option.wait_for(
                                            state="visible", timeout=1500
                                        )
                                    except PlaywrightTimeoutError:
                                        pass
                                else:
                                    print(f"LOG: 未找到{desc} '{text_value}'，跳过。")

                            if len(region_parts) >= 1:
                                await _click_in_column(
                                    col_prov, region_parts[0], "省份"
                                )
                                await random_sleep(1, 2)
                            if len(region_parts) >= 2:
                                await _click_in_column(
                                    col_city, region_parts[1], "城市"
                                )
                                await random_sleep(1, 2)
                            if len(region_parts) >= 3:
                                await _click_in_column(
                                    col_dist, region_parts[2], "区/县"
                                )
                                await random_sleep(1, 2)

                            search_btn = popover.locator(
                                "div.searchBtn--Ic6RKcAb"
                            ).first
                            if await search_btn.count():
                                try:
                                    async with page.expect_response(
                                        search_response_predicate(),
                                        timeout=20000,
                                    ) as response_info:
                                        await search_btn.click()
                                        await random_sleep(2, 3)
                                    final_response = await response_info.value
                                except PlaywrightTimeoutError:
                                    log_time("区域筛选提交超时，继续执行。")
                            else:
                                print(
                                    "LOG: 未找到区域弹窗的“查看XX件宝贝”按钮，跳过提交。"
                                )
                        else:
                            print("LOG: 未找到区域筛选触发器。")
                    except PlaywrightTimeoutError:
                        log_time(f"区域筛选 '{region_filter}' 请求超时，继续执行。")
                    except Exception as e:
                        print(f"LOG: 应用区域筛选 '{region_filter}' 失败: {e}")

                if min_price or max_price:
                    price_container = page.locator(
                        'div[class*="search-price-input-container"]'
                    ).first
                    if await price_container.is_visible():
                        if min_price:
                            await price_container.get_by_placeholder("¥").first.fill(
                                min_price
                            )
                            # --- 修改: 将固定等待改为随机等待 ---
                            await random_sleep(1, 2.5)  # 原来是 asyncio.sleep(5)
                        if max_price:
                            await (
                                price_container.get_by_placeholder("¥")
                                .nth(1)
                                .fill(max_price)
                            )
                            # --- 修改: 将固定等待改为随机等待 ---
                            await random_sleep(1, 2.5)  # 原来是 asyncio.sleep(5)

                        async with page.expect_response(
                            search_response_predicate(), timeout=20000
                        ) as response_info:
                            await page.keyboard.press("Tab")
                            # --- 修改: 增加确认价格后的等待时间 ---
                            await random_sleep(2, 4)  # 原来是 asyncio.sleep(5)
                        final_response = await response_info.value
                    else:
                        print("LOG: 警告 - 未找到价格输入容器。")

                log_time("所有筛选已完成，开始处理商品列表...")

                current_response = (
                    final_response
                    if final_response and final_response.ok
                    else initial_response
                )
                for page_num in range(1, max_pages + 1):
                    if stop_scraping:
                        break
                    log_time(f"开始处理第 {page_num}/{max_pages} 页 ...")

                    if page_num > 1:
                        page_advance_result = await advance_search_page(
                            page=page,
                            page_num=page_num,
                        )
                        if not page_advance_result.advanced:
                            break
                        current_response = page_advance_result.response

                    if not (current_response and current_response.ok):
                        log_time(f"第 {page_num} 页响应无效，跳过。")
                        continue

                    basic_items = await _parse_search_results_json(
                        await current_response.json(), f"第 {page_num} 页"
                    )
                    if not basic_items:
                        break
                    # 降价检测必须放在去重之前：会降价的恰恰是"已存在"的商品，
                    # 而它们在下面的循环里会被直接 continue 跳过。这里用的是
                    # 本次运行之前的历史快照（还没记录本次），所以对比基准正确。
                    price_drops = []
                    try:
                        drop_settings = resolve_price_drop_thresholds()
                        if drop_settings["enabled"]:
                            price_drops = detect_price_drops(
                                basic_items,
                                historical_snapshots,
                                min_percent=drop_settings["min_percent"],
                                min_amount=drop_settings["min_amount"],
                            )
                    except Exception as exc:
                        print(f"   [降价] 检测降价时出错（已跳过）: {exc}")

                    historical_snapshots.extend(
                        record_market_snapshots(
                            keyword=keyword,
                            task_name=task_config.get("task_name", "Untitled Task"),
                            items=basic_items,
                            run_id=history_run_id,
                            snapshot_time=datetime.now().isoformat(),
                            seen_item_ids=history_seen_item_ids,
                        )
                    )

                    total_items_on_page = len(basic_items)
                    for i, item_data in enumerate(basic_items, 1):
                        if debug_limit > 0 and processed_item_count >= debug_limit:
                            log_time(
                                f"已达到调试上限 ({debug_limit})，停止获取新商品。"
                            )
                            stop_scraping = True
                            break

                        unique_key = get_link_unique_key(item_data["商品链接"])
                        if unique_key in processed_links:
                            log_time(
                                f"[页内进度 {i}/{total_items_on_page}] 商品 '{item_data['商品标题'][:20]}...' 已存在，跳过。"
                            )
                            continue

                        log_time(
                            f"[页内进度 {i}/{total_items_on_page}] 发现新商品，获取详情: {item_data['商品标题'][:30]}..."
                        )
                        # --- 修改: 访问详情页前的等待时间，模拟用户在列表页上看了一会儿 ---
                        await random_sleep(2, 4)  # 原来是 (2, 4)

                        detail_page = await context.new_page()
                        try:
                            async with detail_page.expect_response(
                                detail_response_predicate(), timeout=25000
                            ) as detail_info:
                                await detail_page.goto(
                                    item_data["商品链接"],
                                    wait_until="domcontentloaded",
                                    timeout=25000,
                                )

                            detail_response = await detail_info.value
                            if detail_response.ok:
                                detail_json = await detail_response.json()

                                ret_string = str(
                                    await safe_get(detail_json, "ret", default=[])
                                )
                                if "FAIL_SYS_USER_VALIDATE" in ret_string:
                                    print(
                                        "\n==================== CRITICAL BLOCK DETECTED ===================="
                                    )
                                    print(
                                        "检测到闲鱼反爬虫验证 (FAIL_SYS_USER_VALIDATE)，程序将终止。"
                                    )
                                    long_sleep_duration = random.randint(3, 60)
                                    print(
                                        f"为避免账户风险，将执行一次长时间休眠 ({long_sleep_duration} 秒) 后再退出..."
                                    )
                                    await asyncio.sleep(long_sleep_duration)
                                    print("长时间休眠结束，现在将安全退出。")
                                    print(
                                        "==================================================================="
                                    )
                                    raise RiskControlError("FAIL_SYS_USER_VALIDATE")

                                # 解析商品详情数据并更新 item_data
                                item_do = await safe_get(
                                    detail_json, "data", "itemDO", default={}
                                )
                                seller_do = await safe_get(
                                    detail_json, "data", "sellerDO", default={}
                                )

                                reg_days_raw = await safe_get(
                                    seller_do, "userRegDay", default=0
                                )
                                registration_duration_text = format_registration_days(
                                    reg_days_raw
                                )

                                # --- START: 新增代码块 ---

                                # 1. 提取卖家的芝麻信用信息
                                zhima_credit_text = await safe_get(
                                    seller_do, "zhimaLevelInfo", "levelName"
                                )

                                # 2. 提取该商品的完整图片列表
                                image_infos = await safe_get(
                                    item_do, "imageInfos", default=[]
                                )
                                if image_infos:
                                    # 使用列表推导式获取所有有效的图片URL
                                    all_image_urls = [
                                        img.get("url")
                                        for img in image_infos
                                        if img.get("url")
                                    ]
                                    if all_image_urls:
                                        # 用新的字段存储图片列表，替换掉旧的单个链接
                                        item_data["商品图片列表"] = all_image_urls
                                        # (可选) 仍然保留主图链接，以防万一
                                        item_data["商品主图链接"] = all_image_urls[0]

                                # --- END: 新增代码块 ---
                                item_data["“想要”人数"] = await safe_get(
                                    item_do,
                                    "wantCnt",
                                    default=item_data.get("“想要”人数", "NaN"),
                                )
                                item_data["浏览量"] = await safe_get(
                                    item_do, "browseCnt", default="-"
                                )
                                # ...[此处可添加更多从详情页解析出的商品信息]...

                                user_id = await safe_get(seller_do, "sellerId")

                                # 构建基础记录
                                final_record = {
                                    "爬取时间": datetime.now().isoformat(),
                                    "搜索关键字": keyword,
                                    "任务名称": task_config.get(
                                        "task_name", "Untitled Task"
                                    ),
                                    "商品信息": item_data,
                                    "卖家信息": {},
                                }
                                price_reference = build_market_reference(
                                    keyword=keyword,
                                    item=item_data,
                                    current_market_items=basic_items,
                                    historical_snapshots=historical_snapshots,
                                )
                                final_record["价格参考"] = price_reference
                                final_record["price_insight"] = price_reference.get(
                                    "本商品价格位置", {}
                                )

                                analysis_dispatcher.submit(
                                    ItemAnalysisJob(
                                        keyword=keyword,
                                        task_name=task_config.get(
                                            "task_name", "Untitled Task"
                                        ),
                                        decision_mode=decision_mode,
                                        analyze_images=analyze_images,
                                        prompt_text=ai_prompt_text,
                                        keyword_rules=tuple(keyword_rules or []),
                                        final_record=final_record,
                                        seller_id=str(user_id) if user_id else None,
                                        zhima_credit_text=zhima_credit_text,
                                        registration_duration_text=registration_duration_text,
                                    )
                                )

                                processed_links.add(unique_key)
                                processed_item_count += 1
                                log_time(
                                    f"商品已提交后台分析。累计处理 {processed_item_count} 个新商品。"
                                )

                                # --- 修改: 增加单个商品处理后的主要延迟 ---
                                log_time(
                                    "[反爬] 执行一次主要的随机延迟以模拟用户浏览间隔..."
                                )
                                await random_sleep(5, 10)
                            else:
                                print(
                                    f"   错误: 获取商品详情API响应失败，状态码: {detail_response.status}"
                                )
                                if AI_DEBUG_MODE:
                                    print(
                                        f"--- [DETAIL DEBUG] FAILED RESPONSE from {item_data['商品链接']} ---"
                                    )
                                    try:
                                        print(await detail_response.text())
                                    except Exception as e:
                                        print(f"无法读取响应内容: {e}")
                                    print(
                                        "----------------------------------------------------"
                                    )

                        except PlaywrightTimeoutError:
                            print(f"   错误: 访问商品详情页或等待API响应超时。")
                        except Exception as e:
                            print(f"   错误: 处理商品详情时发生未知错误: {e}")
                        finally:
                            await detail_page.close()
                            # --- 修改: 增加关闭页面后的短暂整理时间 ---
                            await random_sleep(2, 4)  # 原来是 (1, 2.5)

                    # 本页处理完统一发降价提醒（放在循环外：循环里有 continue/break 分支）
                    if price_drops:
                        await _notify_price_drops(price_drops)

                    # --- 新增: 在处理完一页所有商品后，翻页前，增加一个更长的“休息”时间 ---
                    if not stop_scraping and page_num < max_pages:
                        print(
                            f"--- 第 {page_num} 页处理完毕，准备翻页。执行一次页面间的长时休息... ---"
                        )
                        await random_sleep(10, 15)

            except PlaywrightTimeoutError as e:
                if _is_login_url(page.url):
                    raise LoginRequiredError(
                        f"Login required: redirected to {page.url} (cookies/state likely expired)"
                    ) from e
                print(f"\n操作超时错误: 页面元素或网络响应未在规定时间内出现。\n{e}")
                raise
            except asyncio.CancelledError:
                log_time("收到取消信号，正在终止当前爬虫任务...")
                raise
            except Exception as e:
                if type(e).__name__ == "TargetClosedError":
                    log_time("浏览器已关闭，忽略后续异常（可能是任务被停止）。")
                    return processed_item_count
                if get_site_adapter().is_login_url(str(e)):
                    raise LoginRequiredError(
                        f"Login required: redirected to passport flow ({e})"
                    ) from e
                print(f"\n爬取过程中发生未知错误: {e}")
                raise
            finally:
                if analysis_dispatcher is not None:
                    log_time("等待后台分析任务完成...")
                    await analysis_dispatcher.join()
                log_time("任务执行完毕，浏览器将在5秒后自动关闭...")
                await asyncio.sleep(5)
                wait_for_debug_close(debug_limit)
                await browser.close()

        return processed_item_count

    processed_item_count = 0
    attempt_limit = compute_attempt_limit(
        rotation_settings,
        account_pool_size=len(account_pool.available_items()),
        proxy_pool_size=len(proxy_pool.available_items()),
    )
    rotation_warning = blacklist_disabled_warning(rotation_settings)
    if rotation_warning:
        print(rotation_warning)
    last_error = ""
    last_state_path: Optional[str] = None

    # If this task is already in a paused state, skip immediately.
    task_name_for_guard = task_config.get("task_name", "未命名任务")
    pause_cookie_path = None
    if (
        isinstance(task_config.get("account_state_file"), str)
        and task_config.get("account_state_file").strip()
    ):
        pause_cookie_path = task_config.get("account_state_file").strip()
    elif os.path.exists(STATE_FILE):
        pause_cookie_path = STATE_FILE

    decision = FAILURE_GUARD.should_skip_start(
        task_name_for_guard, cookie_path=pause_cookie_path
    )
    if decision.skip:
        print(
            f"[FailureGuard] 任务 '{task_name_for_guard}' 已暂停重试 (连续失败 {decision.consecutive_failures}/{FAILURE_GUARD.threshold})"
        )
        if decision.should_notify:
            try:
                await send_ntfy_notification(
                    {
                        "商品标题": f"[任务暂停] {task_name_for_guard}",
                        "当前售价": "N/A",
                        "商品链接": "#",
                    },
                    "任务处于暂停状态，将跳过执行。\n"
                    f"原因: {decision.reason}\n"
                    f"连续失败: {decision.consecutive_failures}/{FAILURE_GUARD.threshold}\n"
                    f"暂停到: {decision.paused_until.strftime('%Y-%m-%d %H:%M:%S') if decision.paused_until else 'N/A'}\n"
                    "修复方法: 更新登录态/cookies文件后会自动恢复。",
                )
            except Exception as e:
                print(f"发送任务暂停通知失败: {e}")

        cleanup_task_images(task_config.get("task_name", "default"))
        return 0

    # 由"失败类型"触发的强制轮换（登录失效换账号、风控换出口 IP），
    # 与配置里 on_failure 模式的轮换走同一段逻辑，避免两处各改一份。
    force_rotate_account = False
    force_rotate_proxy = False
    #: 本次运行命中风控的次数，用于计算退避（越大等越久，封顶 10 分钟）
    risk_control_hits = 0

    for attempt in range(1, attempt_limit + 1):
        if attempt == 1:
            selected_account = _select_account()
            selected_proxy = _select_proxy()
        else:
            should_rotate_account = force_rotate_account or (
                rotation_settings["account_enabled"]
                and rotation_settings["account_mode"] == "on_failure"
            )
            should_rotate_proxy = force_rotate_proxy or (
                rotation_settings["proxy_enabled"]
                and rotation_settings["proxy_mode"] == "on_failure"
            )
            force_rotate_account = False
            force_rotate_proxy = False

            if should_rotate_account:
                account_pool.mark_bad(selected_account, last_error)
                selected_account = _select_account(force_new=True)
            if should_rotate_proxy:
                proxy_pool.mark_bad(selected_proxy, last_error)
                selected_proxy = _select_proxy(force_new=True)

        if rotation_settings["account_enabled"] and not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            print(last_error)
            break
        if not rotation_settings["account_enabled"] and not selected_account:
            last_error = "未找到可用的登录状态文件，无法继续执行任务。"
            print(last_error)
            break
        if rotation_settings["proxy_enabled"] and not selected_proxy:
            last_error = "未找到可用的代理地址，无法继续执行任务。"
            print(last_error)
            break

        state_path = selected_account.value if selected_account else STATE_FILE
        last_state_path = state_path
        proxy_server = selected_proxy.value if selected_proxy else None
        if rotation_settings["account_enabled"]:
            print(f"账号轮换：使用登录状态 {state_path}")
        if rotation_settings["proxy_enabled"] and proxy_server:
            print(f"IP 轮换：使用代理 {proxy_server}")

        try:
            processed_item_count += await _run_scrape_attempt(state_path, proxy_server)
            last_error = ""
            FAILURE_GUARD.record_success(task_name_for_guard)
            break
        except LoginRequiredError as e:
            last_error = str(e)
            print(f"检测到登录失效/重定向: {e}")
            # 这正是账号轮换该生效的场景：旧实现直接 break，于是池子里的其他
            # 账号一次都没被用过，任务反而被 FailureGuard 暂停 24 小时。
            if can_rotate_account(
                rotation_settings,
                available_accounts=[i.value for i in account_pool.available_items()],
                current_account=selected_account.value if selected_account else None,
                forced_account=forced_account,
            ):
                print("[轮换] 该登录态已失效，停用它并换用账号池中的其他账号重试...")
                force_rotate_account = True
                continue
            print(rotation_unavailable_hint(rotation_settings, forced_account=forced_account))
            break
        except RiskControlError as e:
            last_error = str(e)
            risk_control_hits += 1
            print(f"检测到风控或验证触发: {e}")
            # 风控常与出口 IP 绑定，换代理是有意义的；换不了就按原行为中断，
            # 交给 FailureGuard 计数暂停，避免无意义地反复触发风控。
            if can_rotate_proxy(
                rotation_settings,
                available_proxies=[i.value for i in proxy_pool.available_items()],
                current_proxy=selected_proxy.value if selected_proxy else None,
            ):
                # 换 IP 之前先退避：命中风控后立刻重试往往再次被拦（对方看到的是
                # 同一账号在极短时间内的连续行为），等一会儿再换更划算。
                backoff = compute_risk_control_backoff(risk_control_hits)
                if backoff > 0:
                    print(
                        f"[风控] 第 {risk_control_hits} 次命中，退避 {backoff} 秒后再换出口 IP 重试..."
                    )
                    await asyncio.sleep(backoff)
                print("[轮换] 停用当前代理并换用其他代理重试...")
                force_rotate_proxy = True
                continue
            print(rotation_unavailable_hint(rotation_settings, kind="proxy"))
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"本次尝试失败: {last_error}")
            if attempt < attempt_limit:
                print("将尝试轮换账号/IP 后重试...")

    if last_error:
        await _notify_task_failure(task_config, last_error, cookie_path=last_state_path)

    # 清理任务图片目录
    cleanup_task_images(task_config.get("task_name", "default"))

    return processed_item_count
