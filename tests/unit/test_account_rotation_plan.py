"""账号轮换的启用判定：谁说了算，以及为什么。

背景：``ACCOUNT_ROTATION_ENABLED`` 曾被无声吞掉 —— ``auto`` 策略只要根目录存在
``xianyu_state.json`` 就优先用它（而每个既有部署都有这个文件），于是这个开关
怎么设都没用，设置页上的开关点了也不生效，日志里还一个字都不提。

这组用例把优先级钉死，避免以后再退化。
"""
from src.services.account_strategy_service import (
    resolve_account_runtime_plan,
    resolve_rotation_plan,
)

POOL = ["state/acc1.json", "state/acc2.json", "state/acc3.json"]


def plan(strategy=None, account_state_file=None, root=True, pool=POOL, flag=False):
    return resolve_rotation_plan(
        strategy=strategy,
        account_state_file=account_state_file,
        has_root_state_file=root,
        available_account_files=list(pool),
        explicit_rotation_enabled=flag,
    )


class TestExplicitSwitch:
    """本轮修掉的核心问题：开关必须真的算数。"""

    def test_flag_on_beats_root_state_file(self):
        """既有部署都有 xianyu_state.json —— 这正是开关以前失效的场景。"""
        result = plan(strategy="auto", root=True, flag=True)
        assert result["use_account_pool"] is True
        assert result["prefer_root_state"] is False
        assert result["reason"] == "explicit_flag"

    def test_flag_off_keeps_using_root_state_file(self):
        """没开开关时，原有行为一个字都不能变。"""
        result = plan(strategy="auto", root=True, flag=False)
        assert result["use_account_pool"] is False
        assert result["prefer_root_state"] is True
        assert result["reason"] == "root_state"

    def test_flag_on_with_empty_pool_falls_back_to_root_state(self):
        """只开了开关却没放账号：回落到根目录登录态，好过任务直接报错停摆。"""
        result = plan(strategy="auto", root=True, pool=[], flag=True)
        assert result["use_account_pool"] is False
        assert result["prefer_root_state"] is True
        assert result["reason"] == "root_state"

    def test_flag_on_with_empty_pool_and_no_root_state(self):
        result = plan(strategy="auto", root=False, pool=[], flag=True)
        assert result["use_account_pool"] is False
        assert result["prefer_root_state"] is False
        assert result["reason"] == "no_state"


class TestStrategyWins:
    """任务级策略比全局开关更具体，必须优先。"""

    def test_fixed_strategy_ignores_flag(self):
        result = plan(
            strategy="fixed", account_state_file="state/pinned.json", root=True, flag=True
        )
        assert result["forced_account"] == "state/pinned.json"
        assert result["use_account_pool"] is False

    def test_rotate_strategy_works_without_flag(self):
        """策略里明确写了 rotate 就不该再要求开开关。"""
        result = plan(strategy="rotate", root=True, flag=False)
        assert result["use_account_pool"] is True
        assert result["prefer_root_state"] is False
        assert result["reason"] == "strategy_rotate"

    def test_account_state_file_implies_fixed(self):
        """只填了 account_state_file 没写策略 → 视为 fixed，不动账号池。"""
        result = plan(account_state_file="state/pinned.json", root=True, flag=True)
        assert result["forced_account"] == "state/pinned.json"
        assert result["use_account_pool"] is False

    def test_rotate_strategy_with_empty_pool(self):
        result = plan(strategy="rotate", root=True, pool=[], flag=False)
        assert result["use_account_pool"] is False
        assert result["reason"] == "no_state"


class TestAutoHeuristic:
    """原本就有的启发式（没有根目录登录态时用账号池）不能被改坏。"""

    def test_no_root_state_uses_pool(self):
        result = plan(strategy="auto", root=False, flag=False)
        assert result["use_account_pool"] is True
        assert result["reason"] == "pool_fallback"

    def test_no_root_state_and_no_pool(self):
        result = plan(strategy="auto", root=False, pool=[], flag=False)
        assert result["use_account_pool"] is False
        assert result["reason"] == "no_state"


class TestScraperMapping:
    """scraper 用 ``use_account_pool`` 直接决定 account_enabled。

    以前是三分支手写覆盖，三种写法必须等价 —— 否则"开关失灵"会以另一种形式回来。
    """

    def test_matches_base_plan_for_every_combination(self):
        for strategy in (None, "auto", "fixed", "rotate"):
            for root in (True, False):
                for pool in ([], POOL):
                    for flag in (True, False):
                        base = resolve_account_runtime_plan(
                            strategy=strategy,
                            account_state_file=None,
                            has_root_state_file=root,
                            available_account_files=list(pool),
                        )
                        result = plan(
                            strategy=strategy, root=root, pool=pool, flag=flag
                        )
                        # 老的映射：prefer_root_state → False，否则看 use_account_pool
                        legacy = (
                            False if base["prefer_root_state"] else bool(base["use_account_pool"])
                        )
                        if flag and base["strategy"] == "auto" and pool:
                            # 唯一一处有意改变的行为
                            expected = True
                        else:
                            expected = legacy
                        assert result["use_account_pool"] is expected, (
                            strategy,
                            root,
                            bool(pool),
                            flag,
                        )


class TestDecisionLog:
    """判定结果必须说出来。

    这次的问题就是"开关没生效但日志一声不吭"，只能靠读源码才发现。
    """

    def _log(self, result, capsys, mode="per_task", enabled=None):
        from src.scraper import _log_rotation_decision

        settings = {
            "account_enabled": (
                result["use_account_pool"] if enabled is None else enabled
            ),
            "account_mode": mode,
            "account_state_dir": "state",
        }
        items = POOL if result["use_account_pool"] else []
        _log_rotation_decision(result, settings, items)
        return capsys.readouterr().out

    def test_enabled_log_says_mode_and_pool_size(self, capsys):
        out = self._log(plan(root=True, flag=True), capsys)
        assert "账号轮换已启用" in out
        assert "per_task" in out
        assert "3" in out
        # 提醒用户根目录登录态已不参与，避免"UI 里续了登录却没用"
        assert "xianyu_state.json" in out
        assert "账号管理" in out

    def test_disabled_by_root_state_tells_how_to_enable(self, capsys):
        out = self._log(plan(root=True, flag=False), capsys)
        assert "未启用" in out
        assert "ACCOUNT_ROTATION_ENABLED=true" in out
        assert "rotate" in out

    def test_disabled_by_fixed_strategy_names_the_account(self, capsys):
        result = plan(strategy="fixed", account_state_file="state/pinned.json")
        out = self._log(result, capsys)
        assert "固定绑定" in out
        assert "state/pinned.json" in out

    def test_disabled_with_no_state_explains_where_to_put_it(self, capsys):
        out = self._log(plan(root=False, pool=[]), capsys)
        assert "没有可用的登录态" in out
        assert "state/" in out
