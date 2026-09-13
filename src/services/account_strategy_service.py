"""
账号策略辅助函数
"""
from typing import Optional


ACCOUNT_STRATEGIES = {"auto", "fixed", "rotate"}


def clean_account_state_file(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"null", "undefined"}:
        return None
    return text


def normalize_account_strategy(
    strategy: Optional[str],
    account_state_file: Optional[str] = None,
) -> str:
    raw = str(strategy or "").strip().lower()
    if raw in ACCOUNT_STRATEGIES:
        return raw
    if clean_account_state_file(account_state_file):
        return "fixed"
    return "auto"


def resolve_account_runtime_plan(
    *,
    strategy: Optional[str],
    account_state_file: Optional[str],
    has_root_state_file: bool,
    available_account_files: list[str],
) -> dict:
    normalized_strategy = normalize_account_strategy(strategy, account_state_file)
    cleaned_account = clean_account_state_file(account_state_file)
    has_account_pool = len(available_account_files) > 0

    if normalized_strategy == "fixed":
        return {
            "strategy": normalized_strategy,
            "forced_account": cleaned_account,
            "use_account_pool": False,
            "prefer_root_state": False,
        }

    if normalized_strategy == "rotate":
        return {
            "strategy": normalized_strategy,
            "forced_account": None,
            "use_account_pool": has_account_pool,
            "prefer_root_state": False,
        }

    return {
        "strategy": normalized_strategy,
        "forced_account": None,
        "use_account_pool": (not has_root_state_file) and has_account_pool,
        "prefer_root_state": has_root_state_file,
    }


def resolve_rotation_plan(
    *,
    strategy: Optional[str],
    account_state_file: Optional[str] = None,
    has_root_state_file: bool,
    available_account_files: list[str],
    explicit_rotation_enabled: bool = False,
) -> dict:
    """在 ``resolve_account_runtime_plan`` 之上，把"显式开启轮换"也算进去。

    为什么需要这一层：``auto`` 策略只要根目录存在 ``xianyu_state.json`` 就优先用它，
    而**每个既有部署都有这个文件**。于是 ``ACCOUNT_ROTATION_ENABLED=true`` 会被
    静默地压成 False —— 设置页上那个开关点了没反应，日志里也什么都不说。
    这里把优先级写成一条明确的规则并返回 ``reason``，便于日志解释"为什么没轮换"。

    优先级（越具体的越优先）：
    1. 任务固定绑定登录态（``fixed`` / 指定了 ``account_state_file``）→ 不轮换；
    2. 任务策略显式要求轮换（``rotate``）→ 用账号池；
    3. ``auto`` + 显式开启轮换 + 池非空 → 用账号池（**这就是被修掉的那条**）；
    4. ``auto`` + 根目录有登录态 → 用它，不轮换（保持原行为）；
    5. ``auto`` + 池非空 → 用账号池。

    第 3 条要求池非空是有意的：只把开关打开却没往 ``state/`` 放账号时，回落到
    根目录登录态继续跑，比让任务直接报「未找到可用的登录状态文件」要好。
    """
    plan = resolve_account_runtime_plan(
        strategy=strategy,
        account_state_file=account_state_file,
        has_root_state_file=has_root_state_file,
        available_account_files=available_account_files,
    )
    has_account_pool = len(available_account_files) > 0

    # 唯一一处主动改变的行为：auto 策略下，显式开关能压过"根目录有登录态就用它"
    # 的默认偏好。没有这一条，ACCOUNT_ROTATION_ENABLED 在既有部署上永远无效。
    if (
        explicit_rotation_enabled
        and plan["strategy"] == "auto"
        and not plan["forced_account"]
        and has_account_pool
    ):
        return {
            **plan,
            "use_account_pool": True,
            "prefer_root_state": False,
            "reason": "explicit_flag",
        }

    if plan["prefer_root_state"]:
        reason = "root_state"
    elif plan["forced_account"]:
        reason = "fixed"
    elif plan["use_account_pool"]:
        reason = "strategy_rotate" if plan["strategy"] == "rotate" else "pool_fallback"
    else:
        reason = "no_state"

    return {**plan, "reason": reason}
