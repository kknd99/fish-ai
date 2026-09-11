"""日志脱敏测试（安全审计：日志泄露一节）。

日志会落到 ``logs/<task>_<id>.log`` 并被日志接口读取，因此凡是打印外部数据的
地方都要先脱敏：带凭据的代理地址、``requests`` 异常里带 token 的 URL、
OpenAI 风格密钥等。
"""
import pytest

from src.core.redact import redact_text, redact_url


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("http://alice:s3cr3t@proxy.example:8080", "s3cr3t"),
        ("socks5://user:pw@127.0.0.1:1080", "pw@"),
        (
            "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendMessage",
            "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
        ),
        ("https://ntfy.sh/topic?token=abc123secret", "abc123secret"),
        ("Bearer sk-proj-abcdefghijklmnop12345", "abcdefghijklmnop12345"),
        ("api_key=live_abcdef123456", "live_abcdef123456"),
        ("password=hunter2", "hunter2"),
    ],
)
def test_redact_removes_credentials(raw, leaked):
    cleaned = redact_text(raw)
    assert leaked not in cleaned, f"脱敏后仍能读到凭据: {cleaned}"


def test_redact_masks_userinfo_but_keeps_host():
    cleaned = redact_url("http://alice:s3cr3t@proxy.example:8080/path")
    assert "proxy.example:8080" in cleaned
    assert "alice" not in cleaned
    assert "s3cr3t" not in cleaned


def test_redact_keeps_ordinary_text_intact():
    """不能过度脱敏——正常 URL 与文案必须原样保留，否则日志就没用了。"""
    plain = "正常文本 https://www.goofish.com/item?id=1 价格 1200"
    assert redact_text(plain) == plain


def test_redact_handles_non_string_input():
    assert "123" in redact_text(123)
    assert redact_text(None) == "None"
