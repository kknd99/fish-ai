"""AI 客户端的公共构造与环境准备。

为什么要有这个模块（P2：合并双份实现）：
项目里存在两条 AI 调用路径 —— 抓取侧走 ``src/config.py`` 的模块级 ``client``
（``ai_handler.get_ai_analysis``），API 侧走 ``AIClient``（任务生成等）。
两者各自建客户端、各自处理代理，结果**修复只落在其中一条路径上**：

上游为 `httpx` 的 "NO_PROXY 里带 IPv6 CIDR" 崩溃打过补丁，但补丁只加在
``AIClient`` 上。实测 ``NO_PROXY=[::1/128]`` 时 httpx 0.28.1 抛
``InvalidURL: Invalid port: ':1'`` —— 也就是说这种配置下抓取侧的 AI 分析会**全挂**，
而任务生成却看起来正常。所以把"建客户端 + 准备代理环境"收敛到这里，两条路径共用。
"""
from __future__ import annotations

import ipaddress
import os
from typing import Optional

from openai import AsyncOpenAI

from src.core.redact import redact_url


def sanitize_no_proxy_env() -> None:
    """修掉 NO_PROXY / no_proxy 里的 IPv6 CIDR 写法。

    httpx ≤ 0.28.1 会把 NO_PROXY 中的 IPv6 条目连同 CIDR 前缀一起塞进方括号，
    于是 URL 解析把它当成非法端口（实测 ``NO_PROXY=::1/128`` 时抛
    ``InvalidURL: Invalid port: ':1'``）。去掉 ``/prefix`` 是安全的：httpx 本来
    也不支持 CIDR 匹配，只做精确主机比较。

    两种写法都处理：``::1/128``（正常配置）与 ``[::1/128]``（照抄报错信息里
    带方括号的写法）。非 IPv6 条目一律原样保留，不动用户配置。

    见 https://github.com/encode/httpx/pull/3741
    """
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        parts = [host.strip() for host in value.split(",")]
        cleaned: list[str] = []
        changed = False
        for part in parts:
            bare = part[1:-1] if part.startswith("[") and part.endswith("]") else part
            host = bare.partition("/")[0]
            try:
                ipaddress.IPv6Address(host)
            except ValueError:
                cleaned.append(part)
                continue
            cleaned.append(host)
            if host != part:
                changed = True
        if changed:
            os.environ[key] = ",".join(cleaned)


def prepare_ai_environment(proxy_url: Optional[str]) -> None:
    """按配置设置代理环境变量，并修好 NO_PROXY。

    ``openai`` 客户端内部的 httpx 会自己读 ``HTTP_PROXY`` / ``HTTPS_PROXY``，
    因此这里用环境变量而不是构造参数（与既有行为一致）。
    """
    if proxy_url:
        # 认证代理形如 http://user:pass@host，原文打印等于把凭据写进日志
        print(f"正在为AI请求使用代理: {redact_url(proxy_url)}")
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url

    sanitize_no_proxy_env()


def build_async_openai_client(
    *,
    api_key: Optional[str],
    base_url: Optional[str],
    proxy_url: Optional[str] = None,
) -> Optional[AsyncOpenAI]:
    """构造异步 OpenAI 兼容客户端；配置不完整或失败时返回 None。"""
    if not base_url or not api_key:
        print("警告：未在 .env 文件中完整设置 OPENAI_BASE_URL 和 OPENAI_MODEL_NAME。AI相关功能可能无法使用。")
        return None

    try:
        prepare_ai_environment(proxy_url)
        return AsyncOpenAI(api_key=api_key, base_url=base_url)
    except Exception as exc:  # noqa: BLE001 - 初始化失败不应让进程起不来
        print(f"初始化 AI 客户端失败: {exc}")
        return None
