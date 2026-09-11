"""认证与会话测试。

对应安全审计 C3（全站无认证）与 C2（用 test 接口外带已存 API key）：
- 会话 token 的签名、过期、篡改、密钥轮换；
- 凭据的常数时间比较与登录限流；
- 管理面拦截：``/api/*`` 与 API 文档必须认证，登录页与静态资源保持公开；
- WebSocket 握手必须带有效会话。

按本仓库约定使用同步测试（TestClient），不依赖 pytest-asyncio。
"""
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from src.api import auth
from src.api.auth import (
    SESSION_COOKIE_NAME,
    LoginRateLimiter,
    auth_middleware,
    create_session_token,
    requires_auth_path,
    verify_credentials,
    verify_session_token,
)
from src.api.routes import websocket as websocket_routes
import importlib


def current_settings():
    """取"当前"的配置对象。

    不能写 ``import src.infrastructure.config.settings as m``：包 ``__init__`` 把
    模块级的 ``settings`` 实例再导出了，会遮蔽同名子模块，于是 ``m`` 拿到的是实例而不是模块。
    """
    return importlib.import_module("src.infrastructure.config.settings").settings


@pytest.fixture(autouse=True)
def _reset_auth_state(monkeypatch):
    """固定凭据与会话密钥，避免受开发机 .env / 环境变量影响。

    注意：``reload_settings()`` 会把模块级 ``settings`` 重新绑定到新实例，
    所以这里必须在**夹具执行时**取当前对象，不能使用导入时捕获的引用
    （否则全量跑测试时，前面某个用例触发的 reload 会让补丁打在被丢弃的旧对象上）。
    """
    current = current_settings()
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.setattr(current, "web_username", "admin", raising=False)
    monkeypatch.setattr(current, "web_password", "admin123", raising=False)
    monkeypatch.setattr(current, "web_session_secret", None, raising=False)
    monkeypatch.setattr(current, "web_session_ttl_seconds", 3600, raising=False)
    auth.login_rate_limiter._failures.clear()
    yield
    auth.login_rate_limiter._failures.clear()


def craft_token(username: str, exp_offset: int) -> str:
    """手工构造任意过期时间的 token（用于测过期分支）。"""
    payload = {"u": username, "exp": int(time.time()) + exp_offset}
    encoded = auth._b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signature = auth._hmac_signature(encoded)
    return f"{encoded}.{signature}"


# --------------------------------------------------------------- token 本身


def test_session_token_roundtrip():
    token = create_session_token("admin")
    assert verify_session_token(token) == "admin"


def test_session_token_rejects_tampering():
    token = create_session_token("admin")
    body, _, _ = token.rpartition(".")
    assert verify_session_token(f"{body}.{'0' * 64}") is None
    assert verify_session_token(body) is None
    assert verify_session_token("") is None
    assert verify_session_token(None) is None
    assert verify_session_token("garbage") is None


def test_session_token_expiry():
    assert verify_session_token(craft_token("admin", 60)) == "admin"
    assert verify_session_token(craft_token("admin", -1)) is None
    assert verify_session_token(craft_token("admin", -86400)) is None


def test_changing_password_invalidates_existing_sessions(monkeypatch):
    token = create_session_token("admin")
    assert verify_session_token(token) == "admin"

    monkeypatch.setattr(current_settings(), "web_password", "a-brand-new-password", raising=False)
    assert verify_session_token(token) is None, "改密码后旧会话必须立即失效"


def test_explicit_session_secret_overrides_password(monkeypatch):
    monkeypatch.setattr(current_settings(), "web_session_secret", "independent-secret", raising=False)
    token = create_session_token("admin")
    assert verify_session_token(token) == "admin"

    # 即便密码变了，显式配置的会话密钥仍保持有效（会话与密码解耦）
    monkeypatch.setattr(current_settings(), "web_password", "changed", raising=False)
    assert verify_session_token(token) == "admin"


# --------------------------------------------------------------- 路径策略


@pytest.mark.parametrize(
    "path,protected",
    [
        ("/api/tasks", True),
        ("/api/settings/ai/test", True),
        ("/api/login-state", True),
        ("/api/results/files/x.jsonl", True),
        ("/docs", True),
        ("/docs/oauth2-redirect", True),
        ("/openapi.json", True),
        ("/redoc", True),
        ("/health", False),
        ("/", False),
        ("/login", False),
        ("/dashboard", False),
        ("/assets/index-abc.js", False),
        ("/static/img.png", False),
        ("/auth/status", False),
        ("/auth/session", False),
        ("/auth/logout", False),
        ("/favicon.ico", False),
    ],
)
def test_requires_auth_path(path, protected):
    assert requires_auth_path(path) is protected


# --------------------------------------------------------------- 凭据与限流


def test_verify_credentials():
    assert verify_credentials("admin", "admin123") is True
    assert verify_credentials("admin", "wrong") is False
    assert verify_credentials("root", "admin123") is False
    assert verify_credentials("", "") is False


def test_rate_limiter_blocks_after_max_attempts():
    limiter = LoginRateLimiter(max_attempts=3, window_seconds=60)
    key = "10.0.0.9"
    assert limiter.is_blocked(key) is False
    for _ in range(3):
        limiter.record_failure(key)
    assert limiter.is_blocked(key) is True
    assert limiter.retry_after(key) > 0

    limiter.reset(key)
    assert limiter.is_blocked(key) is False


def test_rate_limiter_window_expiry():
    limiter = LoginRateLimiter(max_attempts=2, window_seconds=1)
    limiter.record_failure("k")
    limiter.record_failure("k")
    assert limiter.is_blocked("k") is True
    time.sleep(1.05)
    assert limiter.is_blocked("k") is False


# --------------------------------------------------------------- 中间件


def build_app() -> FastAPI:
    """带认证中间件的最小应用：一个受保护路由 + 一个公开路由。"""
    app = FastAPI()
    app.middleware("http")(auth_middleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/secret")
    async def secret():
        return {"secret": "value"}

    return app


def test_api_requires_authentication():
    client = TestClient(build_app())
    response = client.get("/api/secret")
    assert response.status_code == 401
    assert "未认证" in response.json()["detail"]


def test_api_rejects_forged_cookie():
    client = TestClient(build_app())
    client.cookies.set(SESSION_COOKIE_NAME, "forged.value")
    assert client.get("/api/secret").status_code == 401


def test_api_accepts_valid_session_cookie():
    client = TestClient(build_app())
    client.cookies.set(SESSION_COOKIE_NAME, create_session_token("admin"))
    response = client.get("/api/secret")
    assert response.status_code == 200
    assert response.json() == {"secret": "value"}


def test_public_paths_stay_open():
    client = TestClient(build_app())
    assert client.get("/health").status_code == 200


def test_api_docs_are_protected():
    """文档会完整暴露接口面，未认证时不应可读。"""
    app = build_app()
    client = TestClient(app)
    assert client.get("/openapi.json").status_code == 401


# --------------------------------------------------------------- WebSocket


def test_websocket_handshake_requires_session():
    app = FastAPI()
    app.include_router(websocket_routes.router)
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_websocket_handshake_accepts_valid_session():
    app = FastAPI()
    app.include_router(websocket_routes.router)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, create_session_token("admin"))

    with client.websocket_connect("/ws") as ws:
        assert ws is not None


def test_broadcast_survives_concurrent_disconnect():
    """回归：广播时必须遍历快照，否则集合变化会抛 RuntimeError。"""
    import asyncio

    from src.api.auth import SESSION_COOKIE_NAME as _cookie  # noqa: F401

    class MutatingConnection:
        """在被广播时把集合改掉，模拟并发断连。"""

        def __init__(self, store):
            self.store = store

        async def send_json(self, message):
            self.store.clear()

    store = websocket_routes.active_connections
    store.clear()
    store.add(MutatingConnection(store))
    try:
        asyncio.run(websocket_routes.broadcast_message("task_status_changed", {"id": 1}))
    finally:
        store.clear()
