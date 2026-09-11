"""AI 判定策略：多模态模型分析商品。

逻辑自 ``item_analysis_dispatcher`` 平移而来，返回结构保持不变
（``is_recommended`` / ``reason`` / ``analysis_source`` / ``keyword_hit_count``，
失败时附带 ``error``），因为通知、入库与交易闸门都读这些字段。

两处刻意保留的既有语义：
- ``SKIP_AI_ANALYSIS=true`` 时**直接判定为推荐**（并且交易闸门会因该开关拒绝放行）；
- prompt 为空、模型返回 None、以及任意异常都不抛出，而是转成 ``is_recommended=False``
  的结果（fail-closed），让单条商品失败不至于拖垮整轮抓取。
"""
from __future__ import annotations

from src.services.decision.base import DecisionContext, StrategyMeta


class AIDecisionStrategy:
    """基于多模态模型的判定。"""

    meta = StrategyMeta(
        name="ai",
        display_name="AI 判断",
        requires_prompt=True,
        requires_description=True,
        requires_keyword_rules=False,
    )

    async def analyze(self, record: dict, context: DecisionContext) -> dict:
        services = context.services
        if services is None:
            return self._error("缺少判定所需的服务依赖，无法执行 AI 分析。")
        if services.skip_ai_analysis:
            return self._skipped()

        image_paths: list[str] = []
        try:
            image_paths = await services.download_images(record)
            if not context.prompt_text:
                return self._error("任务未配置AI prompt，跳过分析。")
            ai_result = await services.ai_analyzer(record, image_paths, context.prompt_text)
            if not ai_result:
                return self._error(
                    "AI analysis returned None after retries.",
                    error="AI analysis returned None after retries.",
                )
            ai_result.setdefault("analysis_source", "ai")
            ai_result.setdefault("keyword_hit_count", 0)
            return ai_result
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整轮
            return self._error(f"AI分析异常: {exc}", error=str(exc))
        finally:
            services.cleanup_images(image_paths)

    @staticmethod
    def _skipped() -> dict:
        return {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": "商品已跳过AI分析，直接通知",
            "keyword_hit_count": 0,
        }

    @staticmethod
    def _error(reason: str, *, error: str = "") -> dict:
        payload = {
            "analysis_source": "ai",
            "is_recommended": False,
            "reason": reason,
            "keyword_hit_count": 0,
        }
        if error:
            payload["error"] = error
        return payload
