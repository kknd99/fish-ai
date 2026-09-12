"""飞书缩略图推送：上传换 image_key、发图文卡片、失败时降级。

飞书的图片消息不能直接用图片 URL，必须先上传换 ``image_key``（上传接口需要
``tenant_access_token``，即自建应用的 app_id/app_secret）。因此缩略图是可选增强：
配了凭证才发卡片，任何一步失败都退回纯文本，**通知本身不能丢**。
"""
import asyncio
import json

import pytest

import src.infrastructure.external.notification_clients.feishu_bot_client as module
from src.infrastructure.external.notification_clients.feishu_bot_client import (
    FeishuBotClient,
)

PRODUCT = {
    "商品标题": "Sony A7M4 机身",
    "当前售价": "¥3800",
    "商品链接": "https://www.goofish.com/item?id=1",
    "商品主图链接": "https://img.alicdn.com/bao/uploaded/example.jpg",
    "卖家昵称": "卖家A",
}

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/token"


class _Resp:
    def __init__(self, payload=None, status=200, content=b""):
        self._payload = payload
        self.status_code = status
        self._content = content
        self.text = json.dumps(payload) if isinstance(payload, dict) else ""

    def json(self):
        if not isinstance(self._payload, dict):
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        yield self._content


def _install(monkeypatch, *, token_ok=True, upload_ok=True, image=b"x" * 512,
             image_bytes_over_limit=False, download_status=200):
    calls = {"auth": 0, "upload": 0, "download": 0, "webhooks": []}

    def fake_post(url, json=None, headers=None, timeout=None, data=None, files=None):
        if "tenant_access_token" in url:
            calls["auth"] += 1
            if not token_ok:
                return _Resp({"code": 10003, "msg": "app not found"})
            return _Resp({"code": 0, "tenant_access_token": "t-abc", "expire": 7200})
        if "/im/v1/images" in url:
            calls["upload"] += 1
            calls["upload_auth"] = headers
            if not upload_ok:
                return _Resp({"code": 234001, "msg": "upload failed"})
            return _Resp({"code": 0, "data": {"image_key": "img_abc"}})
        calls["webhooks"].append((url, json))
        return _Resp({"code": 0, "msg": "success"})

    def fake_get(url, headers=None, timeout=None, stream=None):
        calls["download"] += 1
        if download_status >= 400:
            return _Resp(None, status=download_status)
        payload = b"y" * (11 * 1024 * 1024) if image_bytes_over_limit else image
        return _Resp(content=payload)

    monkeypatch.setattr(module.requests, "post", fake_post)
    monkeypatch.setattr(module.requests, "get", fake_get)
    return calls


def _client(**kwargs):
    return FeishuBotClient(WEBHOOK, kwargs.pop("bot_secret", None),
                           kwargs.pop("app_id", "cli_test"), kwargs.pop("app_secret", "secret"),
                           pcurl_to_mobile=kwargs.pop("pcurl_to_mobile", True), **kwargs)


# ------------------------------------------------------------ 能力判定


def test_image_disabled_without_app_credentials():
    assert FeishuBotClient(WEBHOOK).image_enabled is False
    assert FeishuBotClient(WEBHOOK, None, "cli_x", None).image_enabled is False
    assert FeishuBotClient(WEBHOOK, None, None, "sec").image_enabled is False
    assert FeishuBotClient(WEBHOOK, None, "cli_x", "sec").image_enabled is True


# ------------------------------------------------------------ 卡片构造


def test_card_payload_shape():
    payload = FeishuBotClient(WEBHOOK).build_card_payload("img_abc", PRODUCT, "价格合适")

    assert payload["msg_type"] == "interactive"
    card = payload["card"]
    assert card["header"]["title"]["content"].startswith("🚨 新推荐!")
    elements = card["elements"]
    assert elements[0]["tag"] == "img"
    assert elements[0]["img_key"] == "img_abc"
    body = elements[1]["text"]["content"]
    assert elements[1]["text"]["tag"] == "lark_md"
    assert "**价格**: ¥3800" in body
    assert "价格合适" in body
    assert "[查看商品](" in body


def test_card_and_text_both_carry_signature_when_secret_set():
    client = FeishuBotClient(WEBHOOK, "my-secret", time_source=lambda: 1700000000.0)

    card = client.build_card_payload("img_abc", PRODUCT, "ok")
    text = client.build_payload(PRODUCT, "ok")

    assert card["timestamp"] == "1700000000"
    assert card["sign"] == text["sign"]
    assert "sign" in text


# ------------------------------------------------------------ 上传流程


def test_send_uploads_image_and_posts_card(monkeypatch):
    calls = _install(monkeypatch)

    asyncio.run(_client().send(PRODUCT, "价格合适"))

    assert calls["auth"] == 1
    assert calls["upload"] == 1
    assert calls["download"] == 1
    assert calls["upload_auth"]["Authorization"] == "Bearer t-abc"
    url, payload = calls["webhooks"][-1]
    assert url == WEBHOOK
    assert payload["msg_type"] == "interactive"
    assert payload["card"]["elements"][0]["img_key"] == "img_abc"


def test_access_token_is_cached_across_sends(monkeypatch):
    calls = _install(monkeypatch)
    client = _client()

    asyncio.run(client.send(PRODUCT, "第一次"))
    asyncio.run(client.send(PRODUCT, "第二次"))

    # 7200 秒有效期，第二次不该再去换 token
    assert calls["auth"] == 1
    assert calls["upload"] == 2
    assert len(calls["webhooks"]) == 2


def test_token_is_refreshed_after_expiry(monkeypatch):
    calls = _install(monkeypatch)
    now = {"t": 1_000_000.0}
    client = _client(time_source=lambda: now["t"])

    asyncio.run(client.send(PRODUCT, "第一次"))
    now["t"] += 8000.0  # 超过 7200 秒有效期
    asyncio.run(client.send(PRODUCT, "第二次"))

    assert calls["auth"] == 2


# ------------------------------------------------------------ 降级路径


def test_falls_back_to_text_without_app_credentials(monkeypatch):
    calls = _install(monkeypatch)
    client = FeishuBotClient(WEBHOOK)

    asyncio.run(client.send(PRODUCT, "价格合适"))

    assert calls["upload"] == 0 and calls["download"] == 0
    payload = calls["webhooks"][-1][1]
    assert payload["msg_type"] == "text"
    assert "电脑端链接" not in payload["content"]["text"]


def test_falls_back_to_text_when_upload_fails(monkeypatch):
    calls = _install(monkeypatch, upload_ok=False)

    asyncio.run(_client().send(PRODUCT, "价格合适"))

    payload = calls["webhooks"][-1][1]
    assert payload["msg_type"] == "text"   # 通知照发，不抛异常
    assert "价格合适" in payload["content"]["text"]


def test_falls_back_to_text_when_token_fails(monkeypatch):
    calls = _install(monkeypatch, token_ok=False)

    asyncio.run(_client().send(PRODUCT, "价格合适"))

    assert calls["webhooks"][-1][1]["msg_type"] == "text"


def test_falls_back_to_text_when_image_download_fails(monkeypatch):
    calls = _install(monkeypatch, download_status=404)

    asyncio.run(_client().send(PRODUCT, "价格合适"))

    assert calls["upload"] == 0
    assert calls["webhooks"][-1][1]["msg_type"] == "text"


def test_falls_back_to_text_when_image_exceeds_limit(monkeypatch):
    calls = _install(monkeypatch, image_bytes_over_limit=True)

    asyncio.run(_client().send(PRODUCT, "价格合适"))

    # 超过飞书 10MB 限制时不该硬发，降级为文本
    assert calls["upload"] == 0
    assert calls["webhooks"][-1][1]["msg_type"] == "text"


def test_send_without_image_url_uses_text(monkeypatch):
    calls = _install(monkeypatch)
    product = {k: v for k, v in PRODUCT.items() if k != "商品主图链接"}

    asyncio.run(_client().send(product, "价格合适"))

    assert calls["download"] == 0
    assert calls["webhooks"][-1][1]["msg_type"] == "text"


def test_webhook_error_still_raises(monkeypatch):
    """降级只针对缩略图；webhook 本身报错必须抛出，让上层记录失败。"""
    def fake_post(url, json=None, headers=None, timeout=None, data=None, files=None):
        if "tenant_access_token" in url:
            return _Resp({"code": 0, "tenant_access_token": "t", "expire": 7200})
        if "/im/v1/images" in url:
            return _Resp({"code": 0, "data": {"image_key": "img_abc"}})
        return _Resp({"code": 19021, "msg": "sign match fail"})

    monkeypatch.setattr(module.requests, "post", fake_post)
    monkeypatch.setattr(module.requests, "get", lambda *a, **k: _Resp(content=b"x" * 10))

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(_client().send(PRODUCT, "ok"))
    assert "sign match fail" in str(exc.value)
