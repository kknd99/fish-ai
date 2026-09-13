"""降价提醒端到端：真实链路打到本地假飞书服务。

覆盖的是「降价检测 → 通知构造 → 真实 HTTP 请求」这条完整路径，而不是只测
拼装函数：

    _notify_price_drops → send_ntfy_notification（真名兼容函数）
        → NotificationService → FeishuBotClient → requests.post

假服务同时扮演飞书的三段接口（换 token、传图、发消息），并托管一张真图，
所以「带缩略图的图文卡片」是被真的走了一遍，而不是断言一个 dict 长什么样。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.services.price_drop_service import detect_price_drops

#: 一张最小的合法 JPEG（假服务当成商品主图返回）
FAKE_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300"
    "080606070605080707070909080a0c140d0c0b0b0c1912130f"
    "141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30"
    "31343434"
    + "1f27393d38323c2e333432"
    + "ffc0000b080001000101011100ffc40014000100000000000000000000000000000009"
    + "ffda0008010100003f00d2cf20ffd9"
)


class _FakeFeishuHandler(BaseHTTPRequestHandler):
    """把收到的请求原样记进 server.requests，供断言。"""

    def log_message(self, *args):  # 静音测试输出
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # 商品主图
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(FAKE_JPEG)))
        self.end_headers()
        self.wfile.write(FAKE_JPEG)

    def do_POST(self):
        raw = self._read_body()
        record = {"path": self.path, "body": raw}
        self.server.requests.append(record)

        if self.path.endswith("/tenant_access_token/internal"):
            self._send_json(
                {"code": 0, "tenant_access_token": "t-fake-token", "expire": 7200}
            )
        elif self.path.endswith("/im/v1/images"):
            self._send_json({"code": 0, "data": {"image_key": "img-fake-key"}})
        else:  # 机器人 webhook
            self._send_json({"code": 0, "msg": "success"})


@pytest.fixture
def fake_feishu():
    server = HTTPServer(("127.0.0.1", 0), _FakeFeishuHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _feishu_message_payloads(server) -> list:
    """取出真正发到机器人 webhook 的消息体（排除换 token / 传图请求）。"""
    messages = []
    for record in server.requests:
        path = record["path"]
        if "tenant_access_token" in path or "/im/v1/images" in path:
            continue
        messages.append(json.loads(record["body"].decode("utf-8")))
    return messages


def _build_drop(image_url: str):
    """用真实的 detect_price_drops 造一个降价事件（不是手搓 PriceDrop）。"""
    rows = [
        {
            "商品ID": "8801",
            "当前售价": "3000",
            "商品标题": "大石 AERO 轮组 碳纤维",
            "商品链接": "https://www.goofish.com/item?id=8801",
            "商品主图链接": image_url,
        }
    ]
    drops = detect_price_drops(rows, [{"item_id": "8801", "price": "4200"}])
    assert len(drops) == 1, "前置条件：这笔应当被判为降价"
    return drops[0]


def _wire_real_feishu(monkeypatch, base_url: str) -> None:
    """把通知服务指向假飞书（替换 api_base + 机器人地址），其余全用真实实现。"""
    from src.infrastructure.external.notification_clients.feishu_bot_client import (
        FeishuBotClient,
    )
    from src.services import notification_service as ns_module

    real_build = ns_module.build_notification_service

    def build_with_fake_feishu(*args, **kwargs):
        service = real_build(*args, **kwargs)
        for client in service.clients:
            if isinstance(client, FeishuBotClient):
                client.api_base = base_url
                client.bot_url = f"{base_url}/open-apis/bot/v2/hook/fake"
        return service

    monkeypatch.setattr(ns_module, "build_notification_service", build_with_fake_feishu)
    # ai_handler 里 `from ... import build_notification_service` 是值导入，
    # 所以要同时替换它自己命名空间里的引用。
    import src.ai_handler as ai_handler

    monkeypatch.setattr(ai_handler, "build_notification_service", build_with_fake_feishu)

    monkeypatch.setenv("FEISHU_BOT_URL", f"{base_url}/open-apis/bot/v2/hook/fake")
    monkeypatch.setenv("FEISHU_APP_ID", "cli_fake_app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "fake_app_secret")


@pytest.mark.parametrize("with_image", [True, False])
def test_price_drop_reaches_feishu(monkeypatch, fake_feishu, with_image):
    """降价提醒真的发出去；有图即发图文卡片，无图自动降级为文本。"""
    import asyncio

    from src.scraper import _notify_price_drops

    base_url, server = fake_feishu
    _wire_real_feishu(monkeypatch, base_url)

    image_url = f"{base_url}/img/main.jpg" if with_image else ""
    drop = _build_drop(image_url)

    asyncio.run(_notify_price_drops([drop]))

    messages = _feishu_message_payloads(server)
    assert len(messages) == 1, f"应当恰好发出一条消息，实际 {len(messages)}"

    payload = messages[0]
    text = json.dumps(payload, ensure_ascii=False)

    # 标题、价格、降价原因都在
    assert "大石 AERO 轮组" in text
    assert "3000" in text and "4200" in text
    assert "历史新低" in text

    # 不要电脑端链接（用户明确要求）
    assert "电脑端链接" not in text

    if with_image:
        assert payload["msg_type"] == "interactive"
        assert payload["card"]["elements"][0]["img_key"] == "img-fake-key"
        # 传图接口确实被调用过，且带上了 Bearer token
        uploads = [r for r in server.requests if "/im/v1/images" in r["path"]]
        assert len(uploads) == 1
    else:
        assert payload["msg_type"] == "text"
        # 没图就不该白跑一趟上传接口
        assert not [r for r in server.requests if "/im/v1/images" in r["path"]]


def test_image_upload_failure_falls_back_to_text(monkeypatch, fake_feishu):
    """传图失败不能拖垮通知本身：降级成文本，消息照发。"""
    import asyncio

    from src.infrastructure.external.notification_clients import feishu_bot_client
    from src.scraper import _notify_price_drops

    base_url, server = fake_feishu
    _wire_real_feishu(monkeypatch, base_url)

    drop = _build_drop(f"{base_url}/img/main.jpg")

    # 主图下载 404 → 缩略图路径抛错 → 应降级
    def boom(self, image_url):
        raise RuntimeError("模拟图片下载失败")

    monkeypatch.setattr(
        feishu_bot_client.FeishuBotClient, "_download_image", boom, raising=True
    )

    asyncio.run(_notify_price_drops([drop]))

    messages = _feishu_message_payloads(server)
    assert len(messages) == 1
    assert messages[0]["msg_type"] == "text"
    assert "大石 AERO 轮组" in json.dumps(messages[0], ensure_ascii=False)


def test_no_notification_when_no_channel_configured(monkeypatch, fake_feishu):
    """没配任何渠道时只打印警告，不抛异常（不能因为缺少配置就让爬虫崩掉）。"""
    import asyncio

    from src.scraper import _notify_price_drops

    base_url, server = fake_feishu
    _wire_real_feishu(monkeypatch, base_url)
    for var in (
        "FEISHU_BOT_URL",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "NTFY_TOPIC_URL",
        "BARK_URL",
        "WX_BOT_URL",
        "GOTIFY_URL",
        "TELEGRAM_BOT_TOKEN",
        "WEBHOOK_URL",
    ):
        monkeypatch.setenv(var, "")

    asyncio.run(_notify_price_drops([_build_drop("")]))

    assert server.requests == []
