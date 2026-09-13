"""站点适配器：把"这个站点的具体事实"从抓取流程里抽出来。

为什么要有这一层（P2 解耦）：``src/scraper.py`` 把"驱动浏览器"与"闲鱼这个站点的
具体知识"揉在一起 —— 登录 URL 特征、首页/搜索/卖家主页的地址、风控弹窗的选择器、
搜索与详情接口的片段、商品链接的改写规则，全都是散落在 1300 行里的字面量。
于是"接第二个平台"或"换一个抓取方式"的成本约等于把那 1300 行复制一遍。

这一层先把**站点知识**收拢（都是可离线单测的纯逻辑），浏览器驱动主体暂留原处；
后续把它抽成完整适配器时，接口已经在这里了。

注册表默认注册闲鱼适配器；``set_site_adapter`` 供测试与多平台切换使用。
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol
from urllib.parse import urlencode

from src.services.search_pagination import (
    NEXT_PAGE_SELECTOR,
    is_search_results_response,
)

#: 闲鱼详情接口片段（原先硬编码在 src/config.py 与 scraper 内联 lambda 里）
DETAIL_API_URL_FRAGMENT = "h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail"

#: 卖家主页商品列表接口片段（原先硬编码在 scrape_user_profile 的响应监听里）。
#: 抽出来是为了让"探查已售标签"的诊断脚本与实际采集用同一个匹配串，避免两边走偏。
ITEM_LIST_API_URL_FRAGMENT = "mtop.idle.web.xyh.item.list"

#: 风控/验证弹窗选择器：出现即认为被拦住，任务应当中止而不是继续瞎点。
#: 两个选择器各有名字，抓取流程按名字取用，避免出现 selectors[0] 这种下标访问。
DIALOG_RISK_SELECTOR = "div.baxia-dialog-mask"
MIDDLEWARE_RISK_SELECTOR = "div.J_MIDDLEWARE_FRAME_WIDGET"

#: 登录页特征：命中说明登录态失效。
LOGIN_URL_MARKERS = ("passport.goofish.com", "mini_login")

GOOFISH_HOST = "https://www.goofish.com"
#: 站内商品链接的协议前缀，抓回来的链接需要改写才能给浏览器/通知使用。
ITEM_LINK_SCHEME = "fleamarket://"


class SiteAdapter(Protocol):
    """站点适配器接口：站点事实的唯一来源。"""

    name: str

    def search_url(self, keyword: str) -> str:
        ...

    def home_url(self) -> str:
        ...

    def seller_profile_url(self, user_id: str) -> str:
        ...

    def is_login_url(self, url: str) -> bool:
        ...

    def is_search_response(self, response: object) -> bool:
        ...

    def is_detail_response(self, response: object) -> bool:
        ...

    def dialog_risk_selector(self) -> str:
        ...

    def middleware_risk_selector(self) -> str:
        ...

    def next_page_selector(self) -> str:
        ...

    def normalize_item_link(self, link: str) -> str:
        ...


class GoofishSiteAdapter:
    """闲鱼（goofish.com）适配器。"""

    name = "goofish"
    host = GOOFISH_HOST

    def search_url(self, keyword: str) -> str:
        # 与历史实现完全一致：使用 q 参数并做 URL 编码
        return f"{self.host}/search?{urlencode({'q': keyword})}"

    def home_url(self) -> str:
        return f"{self.host}/"

    def seller_profile_url(self, user_id: str) -> str:
        return f"{self.host}/personal?userId={user_id}"

    def is_login_url(self, url: str) -> bool:
        lowered = str(url or "").lower()
        return any(marker in lowered for marker in LOGIN_URL_MARKERS)

    def is_search_response(self, response: object) -> bool:
        return is_search_results_response(response)

    def is_detail_response(self, response: object) -> bool:
        return DETAIL_API_URL_FRAGMENT in str(getattr(response, "url", "") or "")

    def dialog_risk_selector(self) -> str:
        return DIALOG_RISK_SELECTOR

    def middleware_risk_selector(self) -> str:
        return MIDDLEWARE_RISK_SELECTOR

    def next_page_selector(self) -> str:
        return NEXT_PAGE_SELECTOR

    def normalize_item_link(self, link: str) -> str:
        return str(link or "").replace(ITEM_LINK_SCHEME, f"{self.host}/")


_adapter: SiteAdapter = GoofishSiteAdapter()


def get_site_adapter() -> SiteAdapter:
    """当前生效的站点适配器。"""
    return _adapter


def set_site_adapter(adapter: Optional[SiteAdapter]) -> SiteAdapter:
    """切换站点适配器；传 None 恢复默认（闲鱼）。"""
    global _adapter
    _adapter = adapter if adapter is not None else GoofishSiteAdapter()
    return _adapter


def detail_response_predicate() -> Callable[[object], bool]:
    """给 ``page.expect_response`` 用的详情响应判定（避免在抓取流程里写内联 lambda）。"""
    adapter = get_site_adapter()
    return adapter.is_detail_response


def search_response_predicate() -> Callable[[object], bool]:
    """给 ``page.expect_response`` 用的搜索响应判定。"""
    adapter = get_site_adapter()
    return adapter.is_search_response
