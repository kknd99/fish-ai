"""快照 headers 透传的过滤规则。

背景（实测）：增强快照里的 headers 是**浏览器自己生成**的，属于 Fetch 规范里的
forbidden header names。通过 CDP 强行设置（Playwright 的 extra_http_headers）会让
请求被网络栈判为非法——搜索页只要带上 `Sec-Fetch-*`，站点跨域资源
（g.alicdn.com 上的 JS/CSS）就全部以 `net::ERR_INVALID_ARGUMENT` 失败，SPA 渲染
不出来、正文为空，于是抓不到任何商品。

实测数据（同一份登录态，仅改请求头，交替重复 3 轮一致）：
    带 Sec-Fetch-*（三选一即可复现） → 13 个资源失败、正文 0 字符
    去掉全部 Sec-Fetch-*             → 6 个资源失败、正文约 6600 字符，正常渲染
"""
from src.scraper import _build_extra_headers, _is_forbidden_header


class TestIsForbiddenHeader:
    def test_browser_controlled_headers_are_forbidden(self):
        for name in (
            "Sec-Fetch-Site",
            "Sec-Fetch-Mode",
            "Sec-Fetch-Dest",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
        ):
            assert _is_forbidden_header(name) is True, name

    def test_spec_forbidden_names_are_forbidden(self):
        for name in (
            "Cookie",
            "Cookie2",
            "Host",
            "Connection",
            "Content-Length",
            "Referer",
            "Accept-Encoding",
            "Origin",
            "TE",
            "Trailer",
            "Transfer-Encoding",
            "Upgrade",
            "Via",
            "Keep-Alive",
            "Proxy-Authorization",
        ):
            assert _is_forbidden_header(name) is True, name

    def test_case_insensitive(self):
        assert _is_forbidden_header("SEC-FETCH-SITE") is True
        assert _is_forbidden_header("accept-encoding") is True

    def test_safe_headers_are_not_forbidden(self):
        for name in ("Accept-Language", "Accept", "User-Agent", "X-Custom-Header"):
            assert _is_forbidden_header(name) is False, name


class TestBuildExtraHeaders:
    def test_empty_input(self):
        assert _build_extra_headers(None) == {}
        assert _build_extra_headers({}) == {}

    def test_drops_everything_that_breaks_the_page(self):
        raw = {
            "sec-ch-ua": '"Google Chrome";v="117"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "User-Agent": "Mozilla/5.0 ...",
            "Accept": "*/*",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Referer": "https://www.goofish.com/personal",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        result = _build_extra_headers(raw)

        # 白屏的元凶必须被丢掉
        assert "Sec-Fetch-Site" not in result
        assert "Sec-Fetch-Mode" not in result
        assert "Sec-Fetch-Dest" not in result
        assert "Referer" not in result
        assert "Accept-Encoding" not in result
        assert not any(k.lower().startswith("sec-") for k in result)

        # 有意义且安全的保留下来
        assert result["Accept-Language"] == "zh-CN,zh;q=0.9"
        assert result["Accept"] == "*/*"
        assert result["User-Agent"] == "Mozilla/5.0 ..."

    def test_drops_lowercase_variants_too(self):
        raw = {"sec-fetch-site": "same-origin", "referer": "https://x", "accept-language": "zh-CN"}
        assert _build_extra_headers(raw) == {"accept-language": "zh-CN"}

    def test_skips_none_values_and_empty_keys(self):
        raw = {"Accept-Language": "zh-CN", "X-Empty": None, "": "value"}
        assert _build_extra_headers(raw) == {"Accept-Language": "zh-CN"}

    def test_cookie_is_never_forwarded(self):
        # cookie 由 storage_state 负责，作为请求头透传没有意义且会干扰
        assert _build_extra_headers({"Cookie": "a=b", "Accept-Language": "zh-CN"}) == {
            "Accept-Language": "zh-CN"
        }
