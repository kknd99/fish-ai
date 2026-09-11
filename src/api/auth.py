"""会话认证与访问控制。

安全背景（对应安全审计报告中的 C3 / C2 一族问题）：
本应用此前**没有任何服务端认证**——``POST /auth/status`` 只做一次明文比较并返回布尔值，
不签发任何会话；前端把 ``auth_logged_in`` 存进 localStorage 当路由守卫。也就是说
``/api/*`` 全部匿名可达：可以读改配置、读写闲鱼 cookie、启动/停止爬虫、删任务删结果，
还能用 ``POST /api/settings/ai/test`` 把已存的 ``OPENAI_API_KEY`` 送到任意地址。

本模块提供最小但完整的会话机制：

- 登录成功后在 ``Set-Cookie`` 里下发**签名 token**，属性为
  ``HttpOnly``（JS 读不到）+ ``SameSite=Lax``（跨站 POST 不带 cookie，顺带挡掉 CSRF）
  + ``Path=/``；Vue 端不需要任何 token 处理，同源请求会自动带上。
- 签名密钥默认由 ``WEB_PASSWORD`` 派生，因此**改密码即失效所有会话**，且零额外配置；
  需要独立轮换时可显式设置 ``WEB_SESSION_SECRET``。
- 比较凭据用 ``hmac.compare_digest``（常数时间），并对登录失败做限流。
- 管理面拦截收敛在 ``auth_middleware`` 一处：``/api/*`` 与 API 文档路径必须认证，
  其余（SPA 外壳、静态资源、``/health``）保持公开，否则连登录页都加载不出来。

WebSocket（``/ws``）不经过 HTTP 中间件，由 ``websocket_is_authenticated`` 在握手阶段单独校验。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

SESSION_COOKIE_NAME = "xianyu_session"

#: 需要认证的路径前缀。所有 API 路由都在 /api 之下。
PROTECTED_PREFIXES = ("/api/",)

#: 需要认证的精确路径：API 文档会完整暴露接口面与参数结构。
PROTECTED_EXACT_PATHS = ("/docs", "/redoc", "/openapi.json")

#: 永远公开的路径（登录页需要它们才能加载）。
PUBLIC_EXACT_PATHS = (
    "/",
    "/health",
    "/auth/status",
    "/auth/session",
    "/auth/logout",
)


def requires_auth_path(path: str) -> bool:
    """判断某个 HTTP 路径是否必须认证。"""
    if not path:
        return False
    if path in PUBLIC_EXACT_PATHS:
        return False
    if path in PROTECTED_EXACT_PATHS or path.startswith("/docs/") or path.startswith("/redoc/"):
        return True
    return path.startswith(PROTECTED_PREFIXES)


# --------------------------------------------------------------------------- 密钥


def web_password() -> str:
    from src.infrastructure.config.settings import settings

    return settings.web_password or ""


def session_secret() -> str:
    from src.infrastructure.config.settings import settings

    explicit = (settings.web_session_secret or os.getenv("WEB_SESSION_SECRET") or "").strip()
    if explicit:
        return explicit
    return f"ai-goofish-session:{web_password()}"


def session_ttl_seconds() -> int:
    """会话有效期。

    必须每次现取配置：``reload_settings()`` 会**重新绑定**模块级 ``settings``，
    因此在模块导入时 ``from ... import settings`` 拿到的是旧对象，读到的会是过期配置。
    """
    from src.infrastructure.config.settings import settings

    try:
        return max(60, int(settings.web_session_ttl_seconds))
    except (TypeError, ValueError):
        return 7 * 24 * 3600


def _signing_key() -> bytes:
    return hashlib.sha256(session_secret().encode("utf-8")).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _hmac_signature(encoded_payload: str) -> str:
    """对 base64url 载荷计算 HMAC-SHA256（十六进制）。"""
    return hmac.new(
        _signing_key(), encoded_payload.encode("ascii"), hashlib.sha256
    ).hexdigest()


# --------------------------------------------------------------------------- token


def create_session_token(username: str, *, ttl_seconds: Optional[int] = None) -> str:
    """生成签名会话 token：``<base64url(payload)>.<hex hmac>``。"""
    from src.infrastructure.config.settings import settings

    ttl = int(ttl_seconds if ttl_seconds is not None else settings.web_session_ttl_seconds)
    payload = {
        "u": username or "",
        "exp": int(time.time()) + max(60, ttl),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded}.{_hmac_signature(encoded)}"


def verify_session_token(token: Optional[str]) -> Optional[str]:
    """校验 token，返回用户名；无效/被篡改/过期一律返回 ``None``。"""
    if not token or "." not in token:
        return None
    encoded, _, signature = token.rpartition(".")
    if not encoded or not signature:
        return None

    if not hmac.compare_digest(_hmac_signature(encoded), signature):
        return None

    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except Exception:
        return None

    try:
        expiry = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if expiry <= int(time.time()):
        return None

    username = payload.get("u")
    return username if isinstance(username, str) else None


# --------------------------------------------------------------------------- 凭据


def verify_credentials(username: str, password: str) -> bool:
    """常数时间比较用户名与密码。"""
    from src.infrastructure.config.settings import settings

    user_ok = hmac.compare_digest(str(username or ""), str(settings.web_username or ""))
    pass_ok = hmac.compare_digest(str(password or ""), str(settings.web_password or ""))
    return bool(user_ok and pass_ok)


def uses_default_password() -> bool:
    """是否仍在使用文档里的默认密码（用于启动告警）。"""
    from src.infrastructure.config.settings import settings

    return (settings.web_password or "") in {"", "admin123"}


class LoginRateLimiter:
    """极简登录失败限流（按来源地址计数）。

    只做本地内存计数，够挡住"对着局域网内实例慢慢猜密码"这一档；
    进程重启即清零，符合单用户自用工具的定位。
    """

    def __init__(self, max_attempts: int = 8, window_seconds: int = 300) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1, int(window_seconds))
        self._failures: dict[str, list[float]] = {}

    def _prune(self, key: str, now: float) -> list[float]:
        kept = [ts for ts in self._failures.get(key, []) if now - ts < self.window_seconds]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)
        return kept

    def is_blocked(self, key: str) -> bool:
        now = time.time()
        return len(self._prune(key, now)) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        now = time.time()
        attempts = self._prune(key, now)
        attempts.append(now)
        self._failures[key] = attempts

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def retry_after(self, key: str) -> int:
        now = time.time()
        attempts = self._prune(key, now)
        if len(attempts) < self.max_attempts:
            return 0
        return max(1, int(self.window_seconds - (now - min(attempts))))


login_rate_limiter = LoginRateLimiter()


# --------------------------------------------------------------------------- 请求侧


def client_key(request) -> str:
    """限流用的来源标识。"""
    if request is None:
        return "unknown"
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) or "unknown"
    return str(host)


def session_username_from_cookies(cookies) -> Optional[str]:
    if not cookies:
        return None
    try:
        token = cookies.get(SESSION_COOKIE_NAME)
    except Exception:
        return None
    return verify_session_token(token)


def request_is_authenticated(request) -> bool:
    return session_username_from_cookies(getattr(request, "cookies", None)) is not None


def websocket_is_authenticated(websocket) -> bool:
    return session_username_from_cookies(getattr(websocket, "cookies", None)) is not None


async def auth_middleware(request, call_next):
    """管理面访问控制：``/api/*`` 与 API 文档必须带有效会话。"""
    from fastapi.responses import JSONResponse

    if requires_auth_path(request.url.path) and not request_is_authenticated(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "未认证或会话已过期，请重新登录。"},
            headers={"Cache-Control": "no-store"},
        )
    return await call_next(request)
