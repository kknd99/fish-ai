"""飞书自定义机器人通知客户端。

与企业微信机器人同构：都是一个 webhook 地址，POST 一段 JSON。

飞书自定义机器人的接口要点（`https://open.feishu.cn/open-apis/bot/v2/hook/<token>`）：
- 文本消息体为 ``{"msg_type": "text", "content": {"text": "..."}}``；
- 成功响应是 ``{"code": 0, "msg": "success"}``；老一些的网关返回
  ``{"StatusCode": 0, "StatusMessage": "success"}``，因此两者都认；
- 机器人可以开启**签名校验**（设置里那个开关）。开启后请求体必须带
  ``timestamp`` 与 ``sign``，否则消息会被拒。签名算法见
  :func:`sign_feishu_request` —— 注意飞书这里把 ``timestamp\\nsecret`` 当作
  **HMAC 的 key**、消息体为空，与常见用法相反，容易写错。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from typing import Callable, Dict, Optional

import requests

from .base import NotificationClient


def sign_feishu_request(timestamp: str, secret: str) -> str:
    """计算飞书自定义机器人的签名。

    官方算法：以 ``f"{timestamp}\\n{secret}"`` 作为 HMAC-SHA256 的 **key**、
    空串作为消息，再做 base64。参数顺序写反是最常见的坑。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"),
        b"",
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class FeishuBotClient(NotificationClient):
    """飞书自定义机器人通知客户端。"""

    channel_key = "feishu"
    display_name = "飞书"

    def __init__(
        self,
        bot_url: Optional[str] = None,
        bot_secret: Optional[str] = None,
        pcurl_to_mobile: bool = True,
        *,
        time_source: Optional[Callable[[], float]] = None,
    ):
        super().__init__(enabled=bool(bot_url), pcurl_to_mobile=pcurl_to_mobile)
        self.bot_url = bot_url
        self.bot_secret = bot_secret
        # 便于测试注入固定时间，避免签名断言依赖真实时钟
        self._time_source = time_source or time.time

    def build_payload(self, product_data: Dict, reason: str) -> dict:
        """构造请求体（单独成方法以便单测直接断言内容与签名）。"""
        message = self._build_message(product_data, reason)

        lines = [
            message.notification_title,
            f"价格: {message.price}",
            f"原因: {message.reason}",
        ]
        if message.mobile_link:
            lines.append(f"手机端链接: {message.mobile_link}")
        lines.append(f"电脑端链接: {message.desktop_link}")

        payload: dict = {
            "msg_type": "text",
            "content": {"text": "\n".join(lines)},
        }

        if self.bot_secret:
            timestamp = str(int(self._time_source()))
            payload["timestamp"] = timestamp
            payload["sign"] = sign_feishu_request(timestamp, self.bot_secret)

        return payload

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("飞书 未启用")

        payload = self.build_payload(product_data, reason)
        headers = {"Content-Type": "application/json"}
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: requests.post(
                self.bot_url,
                json=payload,
                headers=headers,
                timeout=10,
            ),
        )
        response.raise_for_status()

        try:
            result = response.json()
        except ValueError as exc:  # 非 JSON 响应（例如被网关拦截）
            raise RuntimeError("飞书返回了非 JSON 响应") from exc

        # 新旧两种响应字段都兼容
        code = result.get("code", result.get("StatusCode", 0))
        if code not in (0, None):
            detail = result.get("msg") or result.get("StatusMessage") or "飞书返回未知错误"
            raise RuntimeError(detail)
