"""判定策略的公共类型。

安全/设计背景（P2 解耦）：``decision_mode`` 此前被写死成 ``Literal["ai","keyword"]``，
分派逻辑散在三处 —— ``spider_v2`` 决定要不要加载 prompt、``scraper`` 做一次兜底归一化、
``item_analysis_dispatcher`` 用 ``if job.decision_mode == "keyword"`` 二选一。
结果是"新增一种判定方式"要同时改三个文件加上 task.py 的校验，很容易漏。

现在判定方式是一个**带元信息的策略对象**：

- 策略自己声明是否需要 prompt / 详细需求 / 关键词规则，因此输入校验也随策略走；
- 分派只发生在注册表里，新增策略 = 实现 + 注册，不再改散落的分支。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol


@dataclass(frozen=True)
class StrategyMeta:
    """策略的声明式元信息（注册表与输入校验都读它）。"""

    name: str
    display_name: str
    #: 是否需要读取 prompt 文件（爬虫子进程据此决定是否加载 prompt）
    requires_prompt: bool = False
    #: 创建任务时是否必须填写"详细需求"
    requires_description: bool = False
    #: 创建任务时是否必须填写关键词规则
    requires_keyword_rules: bool = False


@dataclass(frozen=True)
class DecisionServices:
    """策略执行时需要的外部能力，由 dispatcher 注入。

    用注入而不是让策略自己 import，是为了策略层可被单测替换（传假函数即可）。
    """

    download_images: Callable[[dict], Awaitable[list[str]]]
    cleanup_images: Callable[[list[str]], None]
    ai_analyzer: Callable[[dict, list[str], str], Awaitable[Optional[dict]]]
    skip_ai_analysis: bool = False


@dataclass(frozen=True)
class DecisionContext:
    """一次判定所需的上下文。"""

    task_name: str = ""
    prompt_text: str = ""
    keyword_rules: tuple[str, ...] = field(default_factory=tuple)
    services: Optional[DecisionServices] = None


class DecisionStrategy(Protocol):
    """判定策略接口。

    ``analyze`` 必须返回统一结构：至少含 ``is_recommended`` / ``reason`` /
    ``analysis_source`` / ``keyword_hit_count``，因为下游（通知、入库、交易闸门）
    都依赖这几个字段。
    """

    meta: StrategyMeta

    async def analyze(self, record: dict, context: DecisionContext) -> dict:
        ...
