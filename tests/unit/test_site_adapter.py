"""站点适配器测试（P2 之三：把"站点知识"从抓取流程里抽出来）。

``src/scraper.py`` 原本把闲鱼这个站点的具体事实写成散落的字面量：登录 URL 特征、
首页/搜索/卖家主页地址、风控弹窗选择器、搜索与详情接口片段、商品链接改写规则。
这些现在都在 ``GoofishSiteAdapter`` 里，因此可以**离线单测** —— 而重构的前提正是
"先能验证行为没变"。

本文件的重点是**行为保持**：适配器产出的 URL、谓词与选择器必须与重构前的字面量
逐字一致（下面的用例直接写出重构前的表达式作对照）。
"""
import pytest
from urllib.parse import urlencode

from src.services.site_adapter import (
    DETAIL_API_URL_FRAGMENT,
    DIALOG_RISK_SELECTOR,
    MIDDLEWARE_RISK_SELECTOR,
    GoofishSiteAdapter,
    detail_response_predicate,
    get_site_adapter,
    search_response_predicate,
    set_site_adapter,
)
from src.services.search_pagination import NEXT_PAGE_SELECTOR


class FakeResponse:
    """模拟 Playwright 的 Response（只需 url 与 request.method）。"""

    def __init__(self, url: str, method: str = "POST"):
        self.url = url
        self.request = type("Request", (), {"method": method})()


@pytest.fixture()
def adapter():
    return GoofishSiteAdapter()


@pytest.fixture(autouse=True)
def _restore_adapter():
    """避免用例切换适配器后污染其它测试。"""
    yield
    set_site_adapter(None)


# --------------------------------------------------------------- 行为保持：URL


@pytest.mark.parametrize(
    "keyword",
    ["MacBook Air M1", "索尼 A7M4", "iPhone 15 Pro", "a&b=c", "中文 关键词/带符号"],
)
def test_search_url_matches_legacy_expression(adapter, keyword):
    """与重构前的内联写法逐字一致。"""
    legacy = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"
    assert adapter.search_url(keyword) == legacy


def test_search_url_encodes_special_characters(adapter):
    url = adapter.search_url("a&b=c")
    assert "&" not in url.split("?", 1)[1].replace("%26", "")
    assert url.startswith("https://www.goofish.com/search?q=")


def test_home_url(adapter):
    assert adapter.home_url() == "https://www.goofish.com/"


def test_seller_profile_url(adapter):
    assert adapter.seller_profile_url("2200000000") == (
        "https://www.goofish.com/personal?userId=2200000000"
    )


def test_normalize_item_link_rewrites_app_scheme(adapter):
    assert adapter.normalize_item_link("fleamarket://item?id=1") == (
        "https://www.goofish.com/item?id=1"
    )
    assert adapter.normalize_item_link("https://www.goofish.com/item?id=1") == (
        "https://www.goofish.com/item?id=1"
    )
    assert adapter.normalize_item_link("") == ""


# --------------------------------------------------------------- 行为保持：谓词


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://passport.goofish.com/login", True),
        ("https://www.goofish.com/mini_login.htm?x=1", True),
        ("https://www.goofish.com/search?q=a", False),
        ("", False),
    ],
)
def test_is_login_url(adapter, url, expected):
    assert adapter.is_login_url(url) is expected


def test_is_login_url_is_case_insensitive(adapter):
    assert adapter.is_login_url("https://PASSPORT.GOOFISH.COM/x") is True


def test_is_search_response_requires_post_and_fragment(adapter):
    fragment = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
    assert adapter.is_search_response(FakeResponse(f"https://h5api.m.goofish.com{fragment}")) is True
    assert adapter.is_search_response(FakeResponse(f"https://h5api.m.goofish.com{fragment}", "GET")) is False
    assert adapter.is_search_response(FakeResponse("https://example.com/other")) is False


def test_is_detail_response(adapter):
    assert adapter.is_detail_response(
        FakeResponse(f"https://h5api.m.goofish.com/{DETAIL_API_URL_FRAGMENT}/1.0/")
    ) is True
    assert adapter.is_detail_response(FakeResponse("https://example.com/item")) is False


# --------------------------------------------------------------- 选择器


def test_risk_control_selectors_match_legacy_literals(adapter):
    assert adapter.dialog_risk_selector() == "div.baxia-dialog-mask"
    assert adapter.middleware_risk_selector() == "div.J_MIDDLEWARE_FRAME_WIDGET"
    assert DIALOG_RISK_SELECTOR == "div.baxia-dialog-mask"
    assert MIDDLEWARE_RISK_SELECTOR == "div.J_MIDDLEWARE_FRAME_WIDGET"


def test_next_page_selector_matches_pagination_module(adapter):
    assert adapter.next_page_selector() == NEXT_PAGE_SELECTOR


# --------------------------------------------------------------- 注册表


class StubAdapter(GoofishSiteAdapter):
    """测试用站点：把站点事实整体换掉，验证它们确实来自适配器而不是硬编码。"""

    name = "stub"
    host = "https://example.com"
    search_fragment = "/api/stub.search/1.0/"

    def is_login_url(self, url: str) -> bool:
        return "example.com/login" in str(url)

    def is_search_response(self, response) -> bool:
        return self.search_fragment in str(getattr(response, "url", "") or "")

    def is_detail_response(self, response) -> bool:
        return "/api/stub.detail/1.0/" in str(getattr(response, "url", "") or "")


def test_set_site_adapter_switches_all_site_facts():
    stub = set_site_adapter(StubAdapter())

    assert get_site_adapter() is stub
    assert stub.search_url("x") == "https://example.com/search?q=x"
    assert stub.is_login_url("https://example.com/login") is True
    assert stub.is_login_url("https://passport.goofish.com/login") is False


def test_set_none_restores_goofish():
    set_site_adapter(StubAdapter())
    restored = set_site_adapter(None)
    assert isinstance(restored, GoofishSiteAdapter)
    assert restored.search_url("x").startswith("https://www.goofish.com/")


def test_predicate_helpers_follow_active_adapter():
    """``page.expect_response`` 用的谓词必须跟随当前适配器。"""
    assert search_response_predicate()(FakeResponse(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
    )) is True
    assert detail_response_predicate()(FakeResponse(
        f"https://h5api.m.goofish.com/{DETAIL_API_URL_FRAGMENT}/1.0/"
    )) is True

    set_site_adapter(StubAdapter())
    assert search_response_predicate()(FakeResponse(
        "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
    )) is False, "切换适配器后旧站点的谓词不应再命中"


# --------------------------------------------------------------- scraper 已接线


def test_scraper_login_check_delegates_to_adapter():
    """scraper 的登录判定必须走适配器（而不是自己再写一份字符串匹配）。"""
    import src.scraper as scraper

    assert scraper._is_login_url("https://passport.goofish.com/login") is True

    set_site_adapter(StubAdapter())
    assert scraper._is_login_url("https://example.com/login") is True
    assert scraper._is_login_url("https://passport.goofish.com/login") is False


def test_scraper_no_longer_hardcodes_site_literals():
    """回归护栏：站点事实不应再以字面量形式散落在 scraper 里。"""
    import pathlib

    import src.scraper as scraper

    source = pathlib.Path(scraper.__file__).read_text(encoding="utf-8")
    forbidden = [
        "https://www.goofish.com/",
        "https://www.goofish.com/search?",
        "passport.goofish.com",
        "div.baxia-dialog-mask",
        "div.J_MIDDLEWARE_FRAME_WIDGET",
        "h5api.m.goofish.com",
        "fleamarket://",
    ]
    leftover = [literal for literal in forbidden if literal in source]
    assert leftover == [], f"这些站点字面量应已移入适配器: {leftover}"
