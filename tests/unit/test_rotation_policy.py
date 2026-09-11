"""轮换策略测试（P3：登录态失效时真正启用账号轮换）。

上游最高频的痛点是"cookie 失效"：旧实现在 ``LoginRequiredError`` 上直接中断重试，
于是账号轮换从未用于它被宣传的场景——登录态一失效，任务就被 FailureGuard 暂停 24 小时，
池子里的其他账号一次都没被尝试。

这里分两层验证：
1. 纯决策函数（能否轮换、换不了时的提示、尝试次数上限）；
2. **真正驱动 scrape_xianyu 的重试循环**：打桩 Playwright 让它抛 LoginRequiredError，
   观察它是否依次换用账号池里的多个账号，而不是第一次就放弃。
"""
import asyncio
import os

import pytest

from src.services.rotation_policy import (
    MAX_ROTATION_ATTEMPTS,
    blacklist_disabled_warning,
    can_rotate_account,
    can_rotate_proxy,
    compute_attempt_limit,
    rotation_unavailable_hint,
)


def settings(**overrides):
    base = {
        "account_enabled": True,
        "account_mode": "on_failure",
        "account_retry_limit": 2,
        "account_blacklist_ttl": 300,
        "proxy_enabled": False,
        "proxy_mode": "on_failure",
        "proxy_retry_limit": 2,
        "proxy_blacklist_ttl": 300,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------- 决策函数


def test_can_rotate_account_when_pool_has_another():
    assert can_rotate_account(
        settings(), available_accounts=["state/a.json", "state/b.json"],
        current_account="state/a.json",
    ) is True


def test_cannot_rotate_when_pool_only_has_current():
    """池子里只有当前这个失效账号时，再试也是同一个死 cookie。"""
    assert can_rotate_account(
        settings(), available_accounts=["state/a.json"], current_account="state/a.json",
    ) is False


def test_cannot_rotate_when_rotation_disabled():
    assert can_rotate_account(
        settings(account_enabled=False),
        available_accounts=["state/a.json", "state/b.json"],
        current_account="state/a.json",
    ) is False


def test_cannot_rotate_when_task_pins_a_fixed_account():
    """固定绑定账号时不擅自切换，违背运维意图。"""
    assert can_rotate_account(
        settings(), available_accounts=["state/a.json", "state/b.json"],
        current_account="state/a.json", forced_account="state/a.json",
    ) is False


def test_can_rotate_proxy_requires_proxy_rotation_enabled():
    assert can_rotate_proxy(
        settings(proxy_enabled=True),
        available_proxies=["http://p1", "http://p2"], current_proxy="http://p1",
    ) is True
    assert can_rotate_proxy(
        settings(proxy_enabled=False),
        available_proxies=["http://p1", "http://p2"], current_proxy="http://p1",
    ) is False


def test_hints_are_actionable():
    assert "固定绑定" in rotation_unavailable_hint(settings(), forced_account="state/a.json")
    assert "未启用" in rotation_unavailable_hint(settings(account_enabled=False))
    assert "没有其他可用登录态" in rotation_unavailable_hint(settings())
    assert "代理轮换未启用" in rotation_unavailable_hint(settings(), kind="proxy")
    assert "无法切换出口 IP" in rotation_unavailable_hint(settings(proxy_enabled=True), kind="proxy")


def test_blacklist_disabled_warning():
    assert blacklist_disabled_warning(settings(account_blacklist_ttl=0)) is not None
    assert blacklist_disabled_warning(settings()) is None


def test_attempt_limit_covers_account_pool_size():
    """retry_limit=2 但池子里有 4 个账号时，不能让上限先于池子耗尽。"""
    limit = compute_attempt_limit(settings(account_retry_limit=2), account_pool_size=4)
    assert limit == 4

    # 但也不能因为池子巨大就跑很久
    huge = compute_attempt_limit(settings(account_retry_limit=2), account_pool_size=500)
    assert huge == MAX_ROTATION_ATTEMPTS


def test_attempt_limit_defaults_to_max_of_configured_retry_limits():
    """两个 retry_limit 取较大者（与既有实现一致）。"""
    assert compute_attempt_limit(
        settings(account_enabled=False, account_retry_limit=3, proxy_retry_limit=1)
    ) == 3
    assert compute_attempt_limit(
        settings(account_enabled=False, account_retry_limit=0, proxy_retry_limit=0)
    ) == 1


# --------------------------------------------------------------- 端到端：重试循环


class _StubPlaywright:
    """打桩 Playwright：进入上下文时抛指定的异常，模拟登录失效/风控。"""

    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *args):
        return False


def _prepare_task(tmp_path, monkeypatch, account_count=3):
    """准备一个最小可运行的任务环境，返回 task_config。"""
    from src.domain.models.task import validate_task_name

    validate_task_name("rotation-test")  # 触发导入，确保模块可用

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for index in range(account_count):
        (state_dir / f"acc_{index}.json").write_text('{"cookies": []}', encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "app.sqlite3"))
    monkeypatch.setenv("TASK_FAILURE_GUARD_PATH", str(tmp_path / "guard.json"))
    monkeypatch.delenv("ACCOUNT_ROTATION_ENABLED", raising=False)
    monkeypatch.delenv("ACCOUNT_STATE_DIR", raising=False)

    # 无通知渠道，避免测试里发外部请求
    for key in ("NTFY_TOPIC_URL", "BARK_URL", "WX_BOT_URL", "GOTIFY_URL",
                "TELEGRAM_BOT_TOKEN", "WEBHOOK_URL"):
        monkeypatch.delenv(key, raising=False)

    return {
        "task_name": "rotation-test",
        "keyword": "rotation-test-keyword",
        "enabled": True,
        "decision_mode": "keyword",
        "keyword_rules": ["x"],
        "max_pages": 1,
        "analyze_images": False,
        "account_rotation": {
            "enabled": True,
            "mode": "on_failure",
            "state_dir": str(state_dir),
            "retry_limit": 2,
            "blacklist_ttl_sec": 300,
        },
    }


def test_login_failure_rotates_through_accounts(tmp_path, monkeypatch, capsys):
    """核心回归：登录失效时必须换账号重试，而不是第一次就放弃。"""
    import src.scraper as scraper
    from src.scraper import LoginRequiredError, scrape_xianyu

    task_config = _prepare_task(tmp_path, monkeypatch, account_count=3)

    monkeypatch.setattr(
        scraper, "async_playwright",
        lambda: _StubPlaywright(LoginRequiredError("redirected to login")),
    )

    asyncio.run(scrape_xianyu(task_config))

    output = capsys.readouterr().out
    # 应当出现"换用账号池中的其他账号"以及实际使用的多个账号文件
    assert "换用账号池中的其他账号" in output
    used = {line.split("使用登录状态")[-1].strip() for line in output.splitlines() if "使用登录状态" in line}
    assert len(used) >= 2, f"没有发生有效的账号轮换，实际使用: {used}\n{output}"


def test_login_failure_without_pool_explains_why(tmp_path, monkeypatch, capsys):
    """只有一个账号时不能假装轮换，要给出可操作提示。"""
    import src.scraper as scraper
    from src.scraper import LoginRequiredError, scrape_xianyu

    task_config = _prepare_task(tmp_path, monkeypatch, account_count=1)

    monkeypatch.setattr(
        scraper, "async_playwright",
        lambda: _StubPlaywright(LoginRequiredError("redirected to login")),
    )

    asyncio.run(scrape_xianyu(task_config))

    output = capsys.readouterr().out
    assert "换用账号池中的其他账号" not in output
    assert "没有其他可用登录态" in output
