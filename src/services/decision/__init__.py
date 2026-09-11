"""判定策略子包（P2 解耦）。

对外只需要两样东西：``DecisionContext`` / ``DecisionServices``（调用方提供能力），
以及 ``get_strategy`` / ``register_strategy``（注册表）。
"""
from src.services.decision.base import (
    DecisionContext,
    DecisionServices,
    DecisionStrategy,
    StrategyMeta,
)
from src.services.decision.registry import (
    DEFAULT_STRATEGY,
    available_strategies,
    get_strategy,
    is_registered,
    iter_strategies,
    normalize_decision_mode,
    register_strategy,
    strategy_meta,
    strategy_names,
    strategy_requires_prompt,
    unregister_strategy,
)

__all__ = [
    "DEFAULT_STRATEGY",
    "DecisionContext",
    "DecisionServices",
    "DecisionStrategy",
    "StrategyMeta",
    "available_strategies",
    "get_strategy",
    "is_registered",
    "iter_strategies",
    "normalize_decision_mode",
    "register_strategy",
    "strategy_meta",
    "strategy_names",
    "strategy_requires_prompt",
    "unregister_strategy",
]
