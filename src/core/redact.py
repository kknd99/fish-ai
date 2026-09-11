"""日志脱敏。

安全背景（安全审计 H7/日志一节）：任务日志会落到 ``logs/<task>_<id>.log``，
而 ``GET /api/logs`` 曾把日志原文返回给任何能访问端口的人。日志里会出现的敏感值：

- 带凭据的代理地址：``src/config.py`` 与 ``ai_client.py`` 直接打印 ``PROXY_URL``，
  认证代理形如 ``http://user:pass@host:port``，等于把凭据写进日志；
- ``requests`` 抛出的异常文本里带完整 URL，而通知渠道的 URL 本身可能就是凭据
  （Telegram 的 ``/bot<token>/sendMessage``、带 ``?token=`` 的 webhook 等）；
- OpenAI 风格的 ``sk-...`` 密钥。

这里提供一个"宽进严出"的文本清洗函数：所有要打印的外部数据都过一遍。
脱敏只影响日志，不影响真实请求。
"""
from __future__ import annotations

import re
from typing import Any

_MASK = "***"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # URL 里的 userinfo：scheme://user:pass@host
    (re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), rf"\g<scheme>{_MASK}:{_MASK}@"),
    # Telegram Bot API：/bot<id>:<token>/method
    (re.compile(r"/bot\d+:[A-Za-z0-9_\-]+"), f"/bot{_MASK}"),
    # OpenAI 风格密钥
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), f"sk-{_MASK}"),
    # 常见 query 参数里的凭据
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|passwd|pwd)=([^&\s\"']+)"
        ),
        rf"\1={_MASK}",
    ),
    # Authorization 头（异常文本里偶尔会带上）
    (re.compile(r"(?i)\b(authorization|bearer)\s*[:=]?\s*[A-Za-z0-9._\-]{12,}"), rf"\1 {_MASK}"),
)


def redact_text(value: Any) -> str:
    """把文本中的凭据替换成 ``***``。非字符串输入会被 ``str()`` 化。"""
    text = value if isinstance(value, str) else str(value)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_url(value: Any) -> str:
    """URL 专用别名（语义更清晰；实现与 :func:`redact_text` 相同）。"""
    return redact_text(value)
