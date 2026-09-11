"""AI 客户端工厂测试（P2：合并双份实现，消除"修复只落一条路径"）。

真实缺陷：上游为 httpx 的 "NO_PROXY 带 IPv6 CIDR 会崩" 打过补丁，但只加在
``AIClient`` 上 —— 抓取侧的 ``src/config.py`` 没有。实测 ``NO_PROXY=[::1/128]`` 时
httpx 0.28.1 抛 ``InvalidURL: Invalid port: ':1'``，也就是这种配置下抓取侧的 AI 分析
会全挂，而任务生成看起来正常。现在两条路径共用同一份构造逻辑。
"""
import os

import pytest

from src.infrastructure.external.ai_client_factory import (
    build_async_openai_client,
    prepare_ai_environment,
    sanitize_no_proxy_env,
)


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "no_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------- NO_PROXY 修补


@pytest.mark.parametrize(
    "raw,expected",
    [
        # 用户正常写法（httpx 内部才会加方括号）
        ("::1/128,localhost", "::1,localhost"),
        # 照抄报错信息里带方括号的写法，同样处理
        ("[::1/128],localhost", "::1,localhost"),
        # 已经是裸地址：保持原样
        ("::1,localhost", "::1,localhost"),
    ],
)
def test_sanitize_strips_ipv6_cidr(monkeypatch, raw, expected):
    monkeypatch.setenv("NO_PROXY", raw)
    sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == expected


def test_sanitize_handles_lowercase_and_multiple_entries(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,[fe80::1/10],.internal")
    sanitize_no_proxy_env()
    assert os.environ["no_proxy"] == "127.0.0.1,fe80::1,.internal"


def test_sanitize_leaves_ipv4_cidr_alone(monkeypatch):
    """IPv4 CIDR 不受该 bug 影响，不要顺手改掉用户配置。"""
    monkeypatch.setenv("NO_PROXY", "10.0.0.0/8,localhost")
    sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "10.0.0.0/8,localhost"


def test_sanitize_is_noop_without_no_proxy(monkeypatch):
    sanitize_no_proxy_env()
    assert "NO_PROXY" not in os.environ


def test_ipv6_cidr_really_breaks_httpx_without_sanitizing(monkeypatch):
    """固化"为什么需要这个修补"：不打补丁时 httpx 无法使用该 NO_PROXY 配置。

    这里刻意不断言具体异常类型：不同 httpx 版本可能抛 InvalidURL 或裸 ValueError，
    但都会指向"端口解析失败"。修补后这类错误必须消失（连不上是另一回事）。
    """
    httpx = pytest.importorskip("httpx")
    # 真实用户写法：不带方括号
    monkeypatch.setenv("NO_PROXY", "::1/128")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")

    def attempt():
        try:
            with httpx.Client(trust_env=True) as client:
                client.get("http://example.invalid/", timeout=0.2)
            return None
        except Exception as exc:  # noqa: BLE001 - 只看现象
            return exc

    broken = attempt()
    assert broken is not None, "该配置本应让 httpx 报错"
    description = f"{type(broken).__name__}: {broken}".lower()
    assert "port" in description or "invalidurl" in description, (
        f"期望端口解析错误，实际 {type(broken).__name__}: {broken}"
    )

    sanitize_no_proxy_env()
    fixed = attempt()
    if fixed is not None:
        assert "port" not in str(fixed).lower(), f"修补后仍报端口错误: {fixed}"


# --------------------------------------------------------------- 代理环境


def test_prepare_environment_sets_proxy_and_masks_log(monkeypatch, capsys):
    prepare_ai_environment("http://alice:s3cr3t@proxy.example:8080")

    assert os.environ["HTTP_PROXY"] == "http://alice:s3cr3t@proxy.example:8080"
    assert os.environ["HTTPS_PROXY"] == "http://alice:s3cr3t@proxy.example:8080"
    output = capsys.readouterr().out
    assert "s3cr3t" not in output, "代理凭据不能出现在日志里"
    assert "***" in output


def test_prepare_environment_still_fixes_no_proxy_without_proxy_url(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "::1/128")
    prepare_ai_environment(None)
    assert os.environ["NO_PROXY"] == "::1"
    assert "HTTP_PROXY" not in os.environ


# --------------------------------------------------------------- 客户端构造


def test_build_client_returns_none_when_unconfigured():
    assert build_async_openai_client(api_key=None, base_url=None) is None
    assert build_async_openai_client(api_key="sk-x", base_url=None) is None
    assert build_async_openai_client(api_key=None, base_url="http://x/v1") is None


def test_build_client_creates_client_and_sanitizes(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "::1/128")
    client = build_async_openai_client(
        api_key="sk-canary",
        base_url="http://127.0.0.1:9/v1",
        proxy_url="http://proxy.local:8080",
    )
    try:
        assert client is not None
        assert os.environ["NO_PROXY"] == "::1", "构造客户端时必须修好 NO_PROXY"
        assert os.environ["HTTP_PROXY"] == "http://proxy.local:8080"
    finally:
        import asyncio

        asyncio.run(client.close())


def test_both_paths_use_the_same_factory(monkeypatch):
    """抓取侧（src/config.py 的模块级 client）与 API 侧（AIClient）必须同源。"""
    import src.infrastructure.external.ai_client as ai_client_module
    import src.infrastructure.external.ai_client_factory as factory_module

    assert ai_client_module.build_async_openai_client is factory_module.build_async_openai_client
    assert ai_client_module._sanitize_no_proxy_env is factory_module.sanitize_no_proxy_env

    import src.config as config_module

    # config.py 在导入时构造 client；这里只验证它引用的是同一个工厂函数
    assert "build_async_openai_client" in (
        __import__("pathlib").Path(config_module.__file__).read_text(encoding="utf-8")
    )
