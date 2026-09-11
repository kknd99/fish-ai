"""轮换策略：什么时候该换账号/换代理，以及换不了时该怎么提示。

安全/可用性背景（上游最高频的痛点）：
``LoginRequiredError``（登录态失效）与 ``RiskControlError``（风控校验）此前都是
**直接中断重试循环**，于是账号轮换从来没有用在它被宣传的那个场景上——登录态一失效，
任务就被 ``FailureGuard`` 暂停 24 小时，而池子里的其他账号一次都没被尝试过。
（上游 issue #505/#492/#473/#472/#465 全是这类现象。）

这里把"能否轮换"的判断抽成纯函数，便于单测覆盖；判断依据就是三件事：
是否启用了轮换、是否被固定绑定、池子里是否还有**另一个**可用项。
"""
from __future__ import annotations

from typing import Iterable, Optional

#: 单次任务运行中，因轮换而允许的最大尝试次数（避免池子很大时长时间占用）。
MAX_ROTATION_ATTEMPTS = 6

#: 命中风控后的退避基数与上限（秒）。指数增长：30 → 60 → 120 → 240 → 480 → 600。
#: 为什么需要：命中风控后**立刻**换 IP 重试往往再次触发（对方看到的是短时间内
#: 同一账号的高频行为），而上游原实现要么立刻重试、要么直接中断并暂停 24 小时。
RISK_CONTROL_BACKOFF_BASE_SECONDS = 30
RISK_CONTROL_BACKOFF_MAX_SECONDS = 600


def compute_risk_control_backoff(
    hit_index: int,
    *,
    base_seconds: int = RISK_CONTROL_BACKOFF_BASE_SECONDS,
    max_seconds: int = RISK_CONTROL_BACKOFF_MAX_SECONDS,
) -> int:
    """第 ``hit_index`` 次命中风控后应等待的秒数（1 起算，指数增长并封顶）。

    ``hit_index <= 0`` 返回 0（表示不必等待）。
    """
    if hit_index <= 0:
        return 0
    base = max(1, int(base_seconds))
    ceiling = max(base, int(max_seconds))
    return min(ceiling, base * (2 ** (hit_index - 1)))


def _has_other(candidates: Iterable[str], current: Optional[str]) -> bool:
    """候选集合中是否存在与 ``current`` 不同的可用项。"""
    return any(value != current for value in candidates)


def can_rotate_account(
    rotation_settings: dict,
    *,
    available_accounts: Iterable[str],
    current_account: Optional[str],
    forced_account: Optional[str] = None,
) -> bool:
    """登录态失效时，能否换用账号池中的另一个账号。"""
    if forced_account:
        # 任务显式固定绑定了某个账号，擅自切换会违背运维意图
        return False
    if not rotation_settings.get("account_enabled"):
        return False
    return _has_other(available_accounts, current_account)


def can_rotate_proxy(
    rotation_settings: dict,
    *,
    available_proxies: Iterable[str],
    current_proxy: Optional[str],
) -> bool:
    """风控触发时，能否换用另一个出口 IP。"""
    if not rotation_settings.get("proxy_enabled"):
        return False
    return _has_other(available_proxies, current_proxy)


def rotation_unavailable_hint(
    rotation_settings: dict,
    *,
    forced_account: Optional[str] = None,
    kind: str = "account",
) -> str:
    """换不了时给出**可操作**的提示，而不是静默失败。"""
    if kind == "proxy":
        if not rotation_settings.get("proxy_enabled"):
            return (
                "[轮换] 代理轮换未启用（PROXY_ROTATION_ENABLED=false），"
                "风控只能靠降低频率或稍后重试；配置 PROXY_POOL 并开启轮换可自动切换出口 IP。"
            )
        return "[轮换] 代理池中没有其他可用代理（全部处于黑名单或池子为空），无法切换出口 IP。"

    if forced_account:
        return (
            f"[轮换] 任务固定绑定登录态 '{forced_account}'，不会自动切换；"
            "请更新该文件（例如重新扫码导出登录态）后重试。"
        )
    if not rotation_settings.get("account_enabled"):
        return (
            "[轮换] 账号轮换未启用（ACCOUNT_ROTATION_ENABLED=false），登录态失效只能等人工更新。"
            "启用方式：往账号目录（默认 state/）放入多个 *.json 登录态并开启账号轮换。"
        )
    return "[轮换] 账号池中没有其他可用登录态（全部处于黑名单或目录为空），无法切换账号。"


def blacklist_disabled_warning(rotation_settings: dict) -> Optional[str]:
    """``*_BLACKLIST_TTL=0`` 会关闭黑名单，失效项可能在重试中被重复选中。"""
    warnings = []
    if rotation_settings.get("account_enabled") and rotation_settings.get("account_blacklist_ttl", 0) <= 0:
        warnings.append("账号")
    if rotation_settings.get("proxy_enabled") and rotation_settings.get("proxy_blacklist_ttl", 0) <= 0:
        warnings.append("代理")
    if not warnings:
        return None
    joined = "、".join(warnings)
    return (
        f"[轮换] 提示：{joined}黑名单 TTL 为 0，失效项不会被排除，"
        "重试时可能被重复选中；建议设置 ACCOUNT_BLACKLIST_TTL / PROXY_BLACKLIST_TTL 为 300 以上。"
    )


def compute_attempt_limit(
    rotation_settings: dict,
    *,
    account_pool_size: int = 0,
    proxy_pool_size: int = 0,
    max_rotation_attempts: int = MAX_ROTATION_ATTEMPTS,
) -> int:
    """计算重试次数上限。

    除了配置里的 retry_limit，还要让尝试次数不少于**可用账号数**：
    否则 ``ACCOUNT_ROTATION_RETRY_LIMIT``（默认 2）会先于账号池耗尽，
    池子里的第 3 个账号永远轮不到。
    """
    limit = max(
        int(rotation_settings.get("account_retry_limit", 1) or 1),
        int(rotation_settings.get("proxy_retry_limit", 1) or 1),
        1,
    )
    if rotation_settings.get("account_enabled"):
        limit = max(limit, min(max(0, account_pool_size), max_rotation_attempts))
    if rotation_settings.get("proxy_enabled"):
        limit = max(limit, min(max(0, proxy_pool_size), max_rotation_attempts))
    return max(1, limit)
