"""判定策略注册表：分派的唯一入口。

新增一种判定方式只需要：实现 ``DecisionStrategy`` + 调用 ``register_strategy``。
``spider_v2``（是否加载 prompt）、``scraper``（归一化）、``dispatcher``（分派）
与任务输入校验都从注册表读取，不再各写一份分支。
"""
from __future__ import annotations

from typing import Dict, Iterable

from src.services.decision.ai_strategy import AIDecisionStrategy
from src.services.decision.base import DecisionStrategy, StrategyMeta
from src.services.decision.keyword_strategy import KeywordDecisionStrategy

#: 未指定或无法识别时的默认策略。
DEFAULT_STRATEGY = "ai"

_registry: Dict[str, DecisionStrategy] = {}


def register_strategy(strategy: DecisionStrategy) -> DecisionStrategy:
    """注册（或覆盖）一个判定策略。"""
    meta = getattr(strategy, "meta", None)
    if meta is None or not getattr(meta, "name", ""):
        raise ValueError("判定策略必须提供带 name 的 meta")
    _registry[meta.name] = strategy
    return strategy


def unregister_strategy(name: str) -> None:
    """注销一个判定策略（供插件卸载与测试清理使用）。"""
    _registry.pop(str(name).strip().lower(), None)


def get_strategy(name: object) -> DecisionStrategy:
    """按名字取策略；未知名字回退到默认策略（与历史"非法值按 ai 处理"一致）。"""
    key = normalize_decision_mode(name)
    return _registry[key]


def strategy_names() -> tuple[str, ...]:
    return tuple(_registry.keys())


def available_strategies() -> tuple[StrategyMeta, ...]:
    return tuple(strategy.meta for strategy in _registry.values())


def normalize_decision_mode(value: object, *, default: str = DEFAULT_STRATEGY) -> str:
    """把外部输入（数据库字段、API 入参、任务配置）归一化为已注册的策略名。

    历史行为是"不认识就按 ai 处理"，这里保持一致，只是判断依据从写死的集合
    变成了注册表 —— 因此注册一个新策略后，它立刻可用，不需要改这里。
    """
    key = str(value or "").strip().lower()
    if key in _registry:
        return key
    fallback = str(default or "").strip().lower()
    return fallback if fallback in _registry else DEFAULT_STRATEGY


def strategy_meta(name: object) -> StrategyMeta:
    return get_strategy(name).meta


def strategy_requires_prompt(name: object) -> bool:
    """该策略是否需要读取 prompt 文件（爬虫子进程据此跳过 prompt 加载）。"""
    return bool(get_strategy(name).meta.requires_prompt)


def is_registered(name: object) -> bool:
    return str(name or "").strip().lower() in _registry


def iter_strategies() -> Iterable[DecisionStrategy]:
    return tuple(_registry.values())


# 内置策略在导入时注册
register_strategy(AIDecisionStrategy())
register_strategy(KeywordDecisionStrategy())
