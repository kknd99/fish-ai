"""关键词判定策略：按关键词规则打分，不调用模型。

这是 ``decision_mode=keyword`` 的实现，逻辑与原 ``dispatcher._build_keyword_result``
完全一致（含 ``analysis_source`` 等字段形态），只是搬到了策略层。
"""
from __future__ import annotations

from src.keyword_rule_engine import build_search_text, evaluate_keyword_rules
from src.services.decision.base import DecisionContext, StrategyMeta


class KeywordDecisionStrategy:
    """基于关键词规则的判定。"""

    meta = StrategyMeta(
        name="keyword",
        display_name="关键词判断",
        requires_prompt=False,
        requires_description=False,
        requires_keyword_rules=True,
    )

    async def analyze(self, record: dict, context: DecisionContext) -> dict:
        search_text = build_search_text(record)
        return evaluate_keyword_rules(list(context.keyword_rules or ()), search_text)
