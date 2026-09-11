"""抓取导航的离线验证（假浏览器驱动真实 scrape_xianyu）。

这是"浏览器驱动层"的第一片覆盖：``SiteAdapter`` 抽取把首页/搜索地址与登录判定
从 scraper 里挪走了，所以必须验证**真实流程里实际访问的 URL 与判定结果**没有变化 ——
只靠"适配器返回值正确"是不够的，还得证明抓取流程真的用了它。

做法：造一个假 Playwright，让页面在"等待关键元素"时按超时失败（真实环境中这是
正常的失败点），从而把流程停在导航之后；然后断言流程访问过的 URL 序列。
"""
import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.services.site_adapter import GoofishSiteAdapter, set_site_adapter
from src.scraper import scrape_xianyu


class FakeResponse:
    def __init__(self, url: str):
        self.url = url
        self.request = SimpleNamespace(method="POST")

    async def json(self):
        return {"data": {"resultList": []}}


class FakeExpectResponse:
    """模拟 ``page.expect_response()`` 的异步上下文管理器。"""

    def __init__(self, response: FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    @property
    def value(self):
        async def _get():
            return self._response

        return _get()


class FakeLocator:
    def __init__(self, page):
        self._page = page

    def first(self):
        return self

    async def count(self):
        return 0

    async def wait_for(self, **kwargs):
        raise PlaywrightTimeoutError("no dialog")

    async def click(self, **kwargs):
        return None


class FakePage:
    def __init__(self, current_url: str):
        self.gotos: list[str] = []
        self.predicates: list = []
        self._url = current_url

    @property
    def url(self) -> str:
        return self._url

    async def goto(self, url, **kwargs):
        self.gotos.append(url)
        return FakeResponse(url)

    async def evaluate(self, script):
        return None

    def expect_response(self, predicate, timeout=None):
        self.predicates.append(predicate)
        return FakeExpectResponse(FakeResponse(self._url))

    async def wait_for_selector(self, selector, timeout=None):
        raise PlaywrightTimeoutError("selector not found")

    def locator(self, selector):
        return FakeLocator(self)

    def on(self, event, handler):
        return None

    async def close(self):
        return None


class FakeContext:
    def __init__(self, page: FakePage):
        self._page = page

    async def add_init_script(self, script):
        return None

    async def new_page(self):
        return self._page

    async def close(self):
        return None


class FakeBrowser:
    def __init__(self, page: FakePage):
        self._page = page

    async def new_context(self, **kwargs):
        return FakeContext(self._page)

    async def close(self):
        return None


class FakeChromium:
    def __init__(self, browser: FakeBrowser):
        self._browser = browser

    async def launch(self, **kwargs):
        return self._browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser):
        self.chromium = FakeChromium(browser)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _restore_adapter():
    yield
    set_site_adapter(None)


@pytest.fixture(autouse=True)
def _instant_sleeps(monkeypatch):
    """把反检测延时与"关浏览器前等 5 秒"改成瞬时。

    这些等待是给真实站点看的，测试里没必要付这个时间（否则一个用例要十几秒）。
    """
    import asyncio

    import src.scraper as scraper

    async def _no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr(scraper, "random_sleep", _no_sleep)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def prepare(tmp_path, monkeypatch, page_url="https://www.goofish.com/search?q=x"):
    """准备最小可运行环境，返回 (task_config, FakePage)。"""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    account = state_dir / "acc.json"
    account.write_text('{"cookies": []}', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("TASK_FAILURE_GUARD_PATH", str(tmp_path / "guard.json"))
    for key in ("NTFY_TOPIC_URL", "BARK_URL", "WX_BOT_URL", "GOTIFY_URL",
                "TELEGRAM_BOT_TOKEN", "WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)

    page = FakePage(page_url)
    import src.scraper as scraper

    monkeypatch.setattr(
        scraper, "async_playwright", lambda: FakePlaywright(FakeBrowser(page))
    )

    task_config = {
        "task_name": "nav-test",
        "keyword": "MacBook Air M1",
        "enabled": True,
        "decision_mode": "keyword",
        "keyword_rules": ["macbook"],
        "max_pages": 1,
        "analyze_images": False,
        "account_state_file": str(account),
        "new_publish_option": "",
        "personal_only": False,
        "free_shipping": False,
    }
    return task_config, page


def test_navigation_uses_adapter_urls(tmp_path, monkeypatch):
    """真实流程访问的 URL 必须与适配器给出的一致（首页 → 搜索页）。"""
    task_config, page = prepare(tmp_path, monkeypatch)
    adapter = GoofishSiteAdapter()

    asyncio.run(scrape_xianyu(task_config))

    assert page.gotos[0] == adapter.home_url(), "第一步应访问首页（反检测预热）"
    assert page.gotos[1] == adapter.search_url("MacBook Air M1"), (
        f"第二步应访问搜索页，实际 {page.gotos[1]}"
    )


def test_navigation_url_matches_legacy_literal(tmp_path, monkeypatch):
    """对照重构前的写法，确保 URL 逐字一致。"""
    from urllib.parse import urlencode

    task_config, page = prepare(tmp_path, monkeypatch)
    asyncio.run(scrape_xianyu(task_config))

    assert page.gotos[1] == f"https://www.goofish.com/search?{urlencode({'q': 'MacBook Air M1'})}"


def test_search_response_predicate_comes_from_adapter(tmp_path, monkeypatch):
    """`page.expect_response` 用的谓词应当是适配器提供的，且能命中真实响应形状。"""
    task_config, page = prepare(tmp_path, monkeypatch)
    asyncio.run(scrape_xianyu(task_config))

    assert page.predicates, "流程应当注册过搜索响应谓词"
    predicate = page.predicates[0]
    fragment = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
    assert predicate(FakeResponse(f"https://h5api.m.goofish.com{fragment}")) is True
    assert predicate(FakeResponse("https://example.com/other")) is False


def test_login_redirect_is_detected_through_adapter(tmp_path, monkeypatch, capsys):
    """登录态失效判定必须走适配器。

    注意：``scrape_xianyu`` 会**在内部**捕获 LoginRequiredError 并处理（轮换账号或
    给出提示），所以这里断言的是"检测到了"，而不是"异常抛出到外层"。
    """
    task_config, page = prepare(
        tmp_path, monkeypatch, page_url="https://passport.goofish.com/login"
    )

    asyncio.run(scrape_xianyu(task_config))

    output = capsys.readouterr().out
    assert "检测到登录失效/重定向" in output, f"适配器的登录判定没有生效:\n{output}"

    # 走的是"固定绑定账号、无法轮换"的提示分支（该用例显式绑定了账号文件）
    assert "固定绑定登录态" in output


def test_switching_adapter_changes_navigation(tmp_path, monkeypatch):
    """换成另一个站点适配器后，真实流程访问的地址随之改变（扩展点的意义所在）。"""

    class StubAdapter(GoofishSiteAdapter):
        name = "stub"
        host = "https://example.com"

        def search_url(self, keyword: str) -> str:
            return f"{self.host}/find?kw={keyword}"

    set_site_adapter(StubAdapter())
    task_config, page = prepare(tmp_path, monkeypatch)

    asyncio.run(scrape_xianyu(task_config))

    assert page.gotos[0] == "https://example.com/"
    assert page.gotos[1] == "https://example.com/find?kw=MacBook Air M1"
