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

关于**缩略图**：飞书的图片消息不能直接用图片 URL，必须先上传换取 ``image_key``，
而上传接口需要 ``tenant_access_token``（即自建应用的 app_id/app_secret）。
因此缩略图是**可选增强**：

- 配了 ``FEISHU_APP_ID`` + ``FEISHU_APP_SECRET`` → 下载商品主图 → 上传 → 用
  **交互式卡片**把图片和文字一起发出去（一条消息，图文合一）；
- 没配（或上传/发送失败）→ 退回到纯文本消息，通知本身不会丢。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from typing import Callable, Dict, Optional, Tuple

import requests

from src.config import IMAGE_DOWNLOAD_HEADERS

from .base import NotificationClient

#: 飞书开放平台地址。单独抽出来便于测试时指向本地假服务。
DEFAULT_API_BASE = "https://open.feishu.cn"

#: 上传图片的大小上限（飞书限制 10MB，这里留点余量）。
MAX_IMAGE_BYTES = 9 * 1024 * 1024

#: tenant_access_token 有效期 7200 秒，提前 5 分钟刷新，避免边界过期。
TOKEN_TTL_SECONDS = 7200
TOKEN_REFRESH_MARGIN = 300


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
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
        pcurl_to_mobile: bool = True,
        *,
        time_source: Optional[Callable[[], float]] = None,
        api_base: str = DEFAULT_API_BASE,
    ):
        super().__init__(enabled=bool(bot_url), pcurl_to_mobile=pcurl_to_mobile)
        self.bot_url = bot_url
        self.bot_secret = bot_secret
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        # 便于测试注入固定时间，避免签名断言依赖真实时钟
        self._time_source = time_source or time.time
        # tenant_access_token 缓存：(token, 过期时间戳)
        self._token_cache: Optional[Tuple[str, float]] = None

    # ---------- 消息构造 ----------

    def _body_lines(self, message) -> list:
        """消息正文（不含标题）。

        刻意**不带电脑端链接**：商品链接用手机端那份（``PCURL_TO_MOBILE`` 默认开启），
        两个链接内容重复且占版面。若没生成手机端链接，则退回用电脑端链接，
        以免消息里没有任何可点的入口。
        """
        lines = [
            f"价格: {message.price}",
            f"原因: {message.reason}",
        ]
        link = message.mobile_link or message.desktop_link
        if link:
            lines.append(f"链接: {link}")
        return lines

    def build_payload(self, product_data: Dict, reason: str) -> dict:
        """构造纯文本请求体（未配置应用凭证时的形态，也是上传失败时的兜底）。"""
        message = self._build_message(product_data, reason)

        lines = [message.notification_title, *self._body_lines(message)]
        payload: dict = {
            "msg_type": "text",
            "content": {"text": "\n".join(lines)},
        }
        return self._with_signature(payload)

    def build_card_payload(self, image_key: str, product_data: Dict, reason: str) -> dict:
        """构造带缩略图的交互式卡片（图文一条消息）。

        图片元素用 ``img_key``；正文用 ``lark_md``，这样链接可点击。
        """
        message = self._build_message(product_data, reason)

        body_lines = []
        link = message.mobile_link or message.desktop_link
        if link:
            body_lines.append(f"**价格**: {message.price}")
            body_lines.append(f"**原因**: {message.reason}")
            body_lines.append(f"[查看商品]({link})")
        else:
            body_lines.append(f"**价格**: {message.price}")
            body_lines.append(f"**原因**: {message.reason}")

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": message.notification_title},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "img",
                        "img_key": image_key,
                        "alt": {"tag": "plain_text", "content": "商品主图"},
                    },
                    {
                        "tag": "div",
                        "text": {"tag": "lark_md", "content": "\n".join(body_lines)},
                    },
                ],
            },
        }
        return self._with_signature(payload)

    def _with_signature(self, payload: dict) -> dict:
        if self.bot_secret:
            timestamp = str(int(self._time_source()))
            payload["timestamp"] = timestamp
            payload["sign"] = sign_feishu_request(timestamp, self.bot_secret)
        return payload

    # ---------- 缩略图（可选增强） ----------

    @property
    def image_enabled(self) -> bool:
        """是否具备发图能力（需要应用凭证）。"""
        return bool(self.app_id and self.app_secret)

    def _get_tenant_access_token(self) -> str:
        """获取并缓存 tenant_access_token。"""
        cached = self._token_cache
        if cached and cached[1] > self._time_source():
            return cached[0]

        response = requests.post(
            f"{self.api_base}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=10,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("飞书鉴权返回了非 JSON 响应") from exc

        if result.get("code") not in (0, None) or not result.get("tenant_access_token"):
            detail = result.get("msg") or "飞书鉴权失败"
            raise RuntimeError(detail)

        token = result["tenant_access_token"]
        expire = int(result.get("expire") or TOKEN_TTL_SECONDS)
        self._token_cache = (token, self._time_source() + max(60, expire - TOKEN_REFRESH_MARGIN))
        return token

    def _download_image(self, image_url: str) -> bytes:
        """下载商品主图（阿里 CDN 需要带浏览器头，复用应用既有常量）。"""
        response = requests.get(
            image_url, headers=IMAGE_DOWNLOAD_HEADERS, timeout=20, stream=True
        )
        response.raise_for_status()

        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=8192):
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise RuntimeError("商品图片超过飞书 10MB 限制，跳过缩略图")
            chunks.append(chunk)
        return b"".join(chunks)

    def _upload_image(self, image_bytes: bytes) -> str:
        """上传图片换取 image_key。"""
        token = self._get_tenant_access_token()
        response = requests.post(
            f"{self.api_base}/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("product.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("飞书上传图片返回了非 JSON 响应") from exc

        image_key = (result.get("data") or {}).get("image_key")
        if result.get("code") not in (0, None) or not image_key:
            detail = result.get("msg") or "飞书上传图片失败"
            raise RuntimeError(detail)
        return image_key

    def _build_payload_with_thumbnail(self, product_data: Dict, reason: str) -> Optional[dict]:
        """尝试构造带缩略图的卡片；任何一步失败都返回 None 由调用方降级。"""
        message = self._build_message(product_data, reason)
        if not message.image_url:
            return None

        image_key = self._upload_image(self._download_image(message.image_url))
        return self.build_card_payload(image_key, product_data, reason)

    # ---------- 发送 ----------

    def _post(self, payload: dict) -> None:
        headers = {"Content-Type": "application/json"}
        response = requests.post(self.bot_url, json=payload, headers=headers, timeout=10)
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

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("飞书 未启用")

        loop = asyncio.get_running_loop()

        payload = None
        if self.image_enabled:
            try:
                payload = await loop.run_in_executor(
                    None, self._build_payload_with_thumbnail, product_data, reason
                )
            except Exception as exc:
                # 缩略图是锦上添花，不能让它拖垮通知本身
                print(f"   [飞书] 缩略图处理失败，改用纯文本推送: {exc}")
                payload = None

        if payload is None:
            payload = self.build_payload(product_data, reason)

        await loop.run_in_executor(None, self._post, payload)
