"""飞书机器人通知客户端测试。

除了消息体形态，重点覆盖**签名校验**：飞书这里把 ``timestamp\\nsecret`` 当作
HMAC 的 key、消息体为空，与常见写法相反，最容易写反且线上才暴露
（写反 = 消息被拒，但本地看不出任何异常）。
"""
import asyncio
import base64
import hashlib
import hmac
import json

import pytest

from src.infrastructure.external.notification_clients.feishu_bot_client import (
    FeishuBotClient,
    sign_feishu_request,
)

PRODUCT = {
    "商品标题": "Sony A7M4 机身",
    "当前售价": "¥3800",
    "商品链接": "https://www.goofish.com/item?id=1",
    "商品主图链接": "https://img.alicdn.com/bao/uploaded/example.jpg",
    "卖家昵称": "卖家A",
}


# --------------------------------------------------------------- 签名


def test_signature_matches_documented_algorithm():
    """独立按官方算法复算一遍，确保 key/消息体没有写反。"""
    timestamp, secret = "1700000000", "s3cr3t"
    expected = base64.b64encode(
        hmac.new(
            f"{timestamp}\n{secret}".encode("utf-8"),
            b"",
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")

    assert sign_feishu_request(timestamp, secret) == expected


def test_signature_differs_for_different_inputs():
    assert sign_feishu_request("1", "a") != sign_feishu_request("1", "b")
    assert sign_feishu_request("1", "a") != sign_feishu_request("2", "a")


def test_signature_is_deterministic():
    assert sign_feishu_request("1700000000", "x") == sign_feishu_request("1700000000", "x")


# --------------------------------------------------------------- 请求体


def test_payload_without_secret_has_no_signature_fields():
    client = FeishuBotClient(bot_url="https://open.feishu.cn/open-apis/bot/v2/hook/token")

    payload = client.build_payload(PRODUCT, "价格合适")

    assert payload["msg_type"] == "text"
    text = payload["content"]["text"]
    assert "Sony A7M4 机身" in text
    assert "¥3800" in text
    assert "价格合适" in text
    # 默认开启链接转换，因此消息里是手机端链接；电脑端链接已被去掉
    assert "https://pages.goofish.com/sharexy" in text
    assert "电脑端链接" not in text
    assert "timestamp" not in payload
    assert "sign" not in payload


def test_payload_with_secret_includes_timestamp_and_signature():
    client = FeishuBotClient(
        bot_url="https://open.feishu.cn/open-apis/bot/v2/hook/token",
        bot_secret="s3cr3t",
        time_source=lambda: 1700000000.9,
    )

    payload = client.build_payload(PRODUCT, "价格合适")

    assert payload["timestamp"] == "1700000000"
    assert payload["sign"] == sign_feishu_request("1700000000", "s3cr3t")


def test_payload_includes_mobile_link_when_convertible():
    client = FeishuBotClient(bot_url="https://example.com/hook", pcurl_to_mobile=True)

    payload = client.build_payload(PRODUCT, "ok")
    text = payload["content"]["text"]
    assert "链接: https://" in text
    # 电脑端链接已按要求去掉：两个链接内容重复，只保留手机端那份
    assert "电脑端链接" not in text
    assert "手机端链接" not in text


def test_payload_omits_mobile_link_when_disabled():
    client = FeishuBotClient(bot_url="https://example.com/hook", pcurl_to_mobile=False)

    text = client.build_payload(PRODUCT, "ok")["content"]["text"]
    # 没有手机端链接时退回电脑端链接，保证消息里仍有可点入口
    assert "电脑端链接" not in text
    assert "https://www.goofish.com/item?id=1" in text


def test_client_is_disabled_without_url():
    assert FeishuBotClient(bot_url=None).is_enabled() is False
    assert FeishuBotClient(bot_url="https://x/hook").is_enabled() is True


# --------------------------------------------------------------- 发送


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if not isinstance(self._payload, dict):
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_post(monkeypatch, response):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return response

    import src.infrastructure.external.notification_clients.feishu_bot_client as module

    monkeypatch.setattr(module.requests, "post", fake_post)
    return captured


def test_send_posts_text_payload(monkeypatch):
    captured = _patch_post(monkeypatch, _FakeResponse({"code": 0, "msg": "success"}))
    client = FeishuBotClient(bot_url="https://open.feishu.cn/open-apis/bot/v2/hook/token")

    asyncio.run(client.send(PRODUCT, "价格合适"))

    assert captured["url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/token"
    assert captured["json"]["msg_type"] == "text"
    assert captured["timeout"] == 10


def test_send_accepts_legacy_status_code_field(monkeypatch):
    """老网关返回 StatusCode/StatusMessage，也应视为成功。"""
    _patch_post(monkeypatch, _FakeResponse({"StatusCode": 0, "StatusMessage": "success"}))
    client = FeishuBotClient(bot_url="https://example.com/hook")

    asyncio.run(client.send(PRODUCT, "ok"))  # 不抛异常即通过


def test_send_raises_on_business_error(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse({"code": 19021, "msg": "sign match fail"}))
    client = FeishuBotClient(bot_url="https://example.com/hook")

    with pytest.raises(RuntimeError, match="sign match fail"):
        asyncio.run(client.send(PRODUCT, "ok"))


def test_send_raises_on_non_json_response(monkeypatch):
    _patch_post(monkeypatch, _FakeResponse("<html>blocked</html>"))
    client = FeishuBotClient(bot_url="https://example.com/hook")

    with pytest.raises(RuntimeError, match="非 JSON"):
        asyncio.run(client.send(PRODUCT, "ok"))


def test_send_raises_when_disabled():
    with pytest.raises(RuntimeError, match="未启用"):
        asyncio.run(FeishuBotClient(bot_url=None).send(PRODUCT, "ok"))


# --------------------------------------------------------------- 工厂接线


def test_factory_registers_feishu_channel():
    from src.infrastructure.config.settings import NotificationSettings
    from src.infrastructure.external.notification_clients.factory import (
        build_notification_clients,
    )

    settings = NotificationSettings.model_construct(
        ntfy_topic_url=None,
        gotify_url=None,
        gotify_token=None,
        bark_url=None,
        wx_bot_url=None,
        feishu_bot_url="https://open.feishu.cn/open-apis/bot/v2/hook/token",
        feishu_bot_secret=None,
        feishu_app_id="cli_from_settings",
        feishu_app_secret="secret_from_settings",
        telegram_bot_token=None,
        telegram_chat_id=None,
        telegram_api_base_url="https://api.telegram.org",
        webhook_url=None,
        webhook_method="POST",
        webhook_headers=None,
        webhook_content_type="JSON",
        webhook_query_parameters=None,
        webhook_body=None,
        pcurl_to_mobile=True,
    )

    clients = build_notification_clients(settings)
    keys = {client.channel_key for client in clients}
    assert "feishu" in keys

    enabled = [client for client in clients if client.is_enabled()]
    assert [client.channel_key for client in enabled] == ["feishu"]
    assert enabled[0].display_name == "飞书"


def test_factory_passes_app_credentials_for_thumbnail():
    """应用凭证必须从设置传到客户端，否则缩略图功能形同虚设。"""
    from src.infrastructure.config.settings import NotificationSettings
    from src.infrastructure.external.notification_clients.factory import (
        build_notification_clients,
    )

    settings = NotificationSettings.model_construct(
        ntfy_topic_url=None, gotify_url=None, gotify_token=None, bark_url=None,
        wx_bot_url=None,
        feishu_bot_url="https://open.feishu.cn/open-apis/bot/v2/hook/token",
        feishu_bot_secret=None,
        feishu_app_id="cli_from_settings",
        feishu_app_secret="secret_from_settings",
        telegram_bot_token=None, telegram_chat_id=None,
        telegram_api_base_url="https://api.telegram.org",
        webhook_url=None, webhook_method="POST", webhook_headers=None,
        webhook_content_type="JSON", webhook_query_parameters=None,
        webhook_body=None, pcurl_to_mobile=True,
    )

    client = next(c for c in build_notification_clients(settings) if c.channel_key == "feishu")
    assert client.app_id == "cli_from_settings"
    assert client.app_secret == "secret_from_settings"
    assert client.image_enabled is True
