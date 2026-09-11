"""
任务领域模型
定义任务实体及其业务逻辑
"""
import re
from enum import Enum
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.cron_utils import validate_cron_expression
from src.services.decision import (
    is_registered,
    normalize_decision_mode,
    strategy_meta,
)
from src.core.safe_paths import (
    validate_account_state_reference,
    validate_prompt_reference,
    validate_task_name,
)
from src.services.account_strategy_service import (
    clean_account_state_file,
    normalize_account_strategy,
)


class TaskStatus(str, Enum):
    """任务状态枚举"""

    STOPPED = "stopped"
    RUNNING = "running"
    SCHEDULED = "scheduled"


def _normalize_keyword_values(value) -> List[str]:
    if value is None:
        return []

    raw_values = []
    if isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    elif isinstance(value, str):
        raw_values = re.split(r"[\n,]+", value)
    else:
        raw_values = [value]

    normalized: List[str] = []
    seen = set()
    for item in raw_values:
        text = str(item).strip()
        if not text:
            continue
        dedup_key = text.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        normalized.append(text)
    return normalized


def _extract_keywords_from_legacy_groups(groups) -> List[str]:
    if not groups:
        return []

    merged: List[str] = []
    for group in groups:
        include_keywords = []
        if isinstance(group, dict):
            include_keywords = group.get("include_keywords") or []
        else:
            include_keywords = getattr(group, "include_keywords", []) or []
        merged.extend(_normalize_keyword_values(include_keywords))
    return _normalize_keyword_values(merged)


def _normalize_payload_keywords(payload: Any) -> Any:
    if payload is None or not isinstance(payload, dict):
        return payload
    values = dict(payload)
    values["account_state_file"] = clean_account_state_file(values.get("account_state_file"))
    values["account_strategy"] = normalize_account_strategy(
        values.get("account_strategy"),
        values.get("account_state_file"),
    )
    if "keyword_rules" in values:
        values["keyword_rules"] = _normalize_keyword_values(values.get("keyword_rules"))
    elif "keyword_rule_groups" in values:
        values["keyword_rules"] = _extract_keywords_from_legacy_groups(
            values.get("keyword_rule_groups")
        )
    return values


def _has_keyword_rules(keyword_rules: List[str]) -> bool:
    return bool(keyword_rules and len(keyword_rules) > 0)


def _normalize_optional_string(value):
    if value == "" or value == "null" or value == "undefined" or value is None:
        return None
    return value


def _validate_cron_expression(value: Optional[str]) -> Optional[str]:
    return validate_cron_expression(value)


def _normalize_price_value(value):
    if _normalize_optional_string(value) is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    return value


class Task(BaseModel):
    """任务实体"""

    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    id: Optional[int] = None
    task_name: str
    enabled: bool
    keyword: str
    description: Optional[str] = ""
    analyze_images: bool = True
    max_pages: int
    personal_only: bool
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: str
    ai_prompt_criteria_file: str
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: Literal["ai", "keyword"] = "ai"
    keyword_rules: List[str] = Field(default_factory=list)
    is_running: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    def can_start(self) -> bool:
        """检查任务是否可以启动"""
        return self.enabled and not self.is_running

    def can_stop(self) -> bool:
        """检查任务是否可以停止"""
        return self.is_running

    def apply_update(self, update: "TaskUpdate") -> "Task":
        """应用更新并返回新的任务实例"""
        update_data = update.model_dump(exclude_unset=True)
        return self.model_copy(update=update_data)


class TaskCreate(BaseModel):
    """创建任务的DTO"""

    model_config = ConfigDict(extra="ignore")

    @field_validator("task_name", mode="before")
    @classmethod
    def check_task_name(cls, value):
        """任务名会进入文件系统路径（``images/task_images_<name>``），
        必须在输入边界拦住穿越型名字，否则可导致任意目录删除/写入。"""
        return validate_task_name(value)

    @field_validator("ai_prompt_base_file", "ai_prompt_criteria_file", mode="before")
    @classmethod
    def check_prompt_reference(cls, value):
        """prompt 文件路径必须落在 prompts/ 内。

        这些内容会被爬虫子进程读取并原样送进发往 OPENAI_BASE_URL 的请求，
        任意路径等于"任意本地文件都能被外带"（安全审计 H2）。空值放行，
        由后续业务校验（AI 模式必须给出判断标准）负责报错。
        """
        if value is None or not str(value).strip():
            return value
        return validate_prompt_reference(value)

    task_name: str
    enabled: bool = True
    keyword: str
    description: Optional[str] = ""
    analyze_images: bool = True
    # 上限与 scraper.MAX_TASK_PAGES 保持一致：该值决定抓取量与风控暴露面
    max_pages: int = Field(default=3, ge=1, le=20)
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: str = "prompts/base_prompt.txt"
    ai_prompt_criteria_file: str = ""
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: str = "ai"
    keyword_rules: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", mode="before")
    @classmethod
    def normalize_cron(cls, value):
        return _normalize_optional_string(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def normalize_account_state_file(cls, value):
        cleaned = clean_account_state_file(value)
        if cleaned:
            # 该文件会被当作 Playwright 的 storage_state 读取，
            # 任意路径等于"任意可读 JSON 都能当 cookie 用"。
            validate_account_state_reference(cleaned)
        return cleaned

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron_expression(value)

    @field_validator("decision_mode", mode="before")
    @classmethod
    def check_decision_mode(cls, value):
        """API 边界保持严格：只接受已注册的判定策略名。

        运行时对历史脏数据的宽松归一化在 scraper / spider_v2 里（回退到默认策略），
        这里则拒绝拼写错误，避免"写错了却静默按 AI 跑"。
        """
        if value is None:
            return None
        if not is_registered(value):
            raise ValueError(f"不支持的判定方式: {value}")
        return normalize_decision_mode(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @model_validator(mode="after")
    def validate_decision_mode_payload(self):
        # 校验依据来自判定策略声明的元信息，而不是硬编码 "ai"/"keyword"：
        # 新增一种判定方式时，"是否需要详细需求 / 关键词规则"跟着策略走。
        meta = strategy_meta(normalize_decision_mode(self.decision_mode))
        description = str(self.description or "").strip()
        if meta.requires_description and not description:
            raise ValueError(f"{meta.display_name}模式下，详细需求(description)不能为空。")
        if meta.requires_keyword_rules and not _has_keyword_rules(self.keyword_rules):
            raise ValueError(f"{meta.display_name}模式下，至少需要一个关键词。")
        if self.account_strategy == "fixed" and not self.account_state_file:
            raise ValueError("固定账号模式下必须选择账号。")
        return self


class TaskUpdate(BaseModel):
    """更新任务的DTO"""

    model_config = ConfigDict(extra="ignore")

    @field_validator("task_name", mode="before")
    @classmethod
    def check_task_name(cls, value):
        """同 TaskCreate：任务名进入文件系统路径前必须校验。"""
        if value is None:
            return None
        return validate_task_name(value)

    @field_validator("ai_prompt_base_file", "ai_prompt_criteria_file", mode="before")
    @classmethod
    def check_prompt_reference(cls, value):
        """prompt 文件路径必须落在 prompts/ 内。

        这些内容会被爬虫子进程读取并原样送进发往 OPENAI_BASE_URL 的请求，
        任意路径等于"任意本地文件都能被外带"（安全审计 H2）。空值放行，
        由后续业务校验（AI 模式必须给出判断标准）负责报错。
        """
        if value is None or not str(value).strip():
            return value
        return validate_prompt_reference(value)

    task_name: Optional[str] = None
    enabled: Optional[bool] = None
    keyword: Optional[str] = None
    description: Optional[str] = None
    analyze_images: Optional[bool] = None
    max_pages: Optional[int] = Field(default=None, ge=1, le=20)
    personal_only: Optional[bool] = None
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    cron: Optional[str] = None
    ai_prompt_base_file: Optional[str] = None
    ai_prompt_criteria_file: Optional[str] = None
    account_state_file: Optional[str] = None
    account_strategy: Optional[Literal["auto", "fixed", "rotate"]] = None
    free_shipping: Optional[bool] = None
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: Optional[str] = None
    keyword_rules: Optional[List[str]] = None
    is_running: Optional[bool] = None

    @field_validator("decision_mode", mode="before")
    @classmethod
    def check_decision_mode(cls, value):
        """同 TaskCreate：API 边界只接受已注册的判定策略名。"""
        if value is None:
            return None
        if not is_registered(value):
            raise ValueError(f"不支持的判定方式: {value}")
        return normalize_decision_mode(value)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", mode="before")
    @classmethod
    def normalize_cron(cls, value):
        return _normalize_optional_string(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def normalize_account_state_file(cls, value):
        cleaned = clean_account_state_file(value)
        if cleaned:
            # 该文件会被当作 Playwright 的 storage_state 读取，
            # 任意路径等于"任意可读 JSON 都能当 cookie 用"。
            validate_account_state_reference(cleaned)
        return cleaned

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron_expression(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @model_validator(mode="after")
    def validate_partial_keyword_payload(self):
        if self.decision_mode == "keyword" and self.keyword_rules is not None:
            if not _has_keyword_rules(self.keyword_rules):
                raise ValueError("关键词判断模式下，至少需要一个关键词。")
        if self.decision_mode == "ai" and self.description is not None:
            if not str(self.description).strip():
                raise ValueError("AI 判断模式下，详细需求(description)不能为空。")
        return self


class TaskGenerateRequest(BaseModel):
    """任务创建请求DTO（AI模式支持自动生成标准）"""

    model_config = ConfigDict(extra="ignore")

    task_name: str
    keyword: str
    description: Optional[str] = ""
    analyze_images: bool = True
    personal_only: bool = True
    min_price: Optional[str] = None
    max_price: Optional[str] = None
    max_pages: int = Field(default=3, ge=1, le=20)
    cron: Optional[str] = None
    account_state_file: Optional[str] = None
    account_strategy: Literal["auto", "fixed", "rotate"] = "auto"
    free_shipping: bool = True
    new_publish_option: Optional[str] = None
    region: Optional[str] = None
    decision_mode: str = "ai"
    keyword_rules: List[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_keyword_payload(cls, values):
        return _normalize_payload_keywords(values)

    @field_validator("min_price", "max_price", mode="before")
    @classmethod
    def convert_price_to_str(cls, value):
        return _normalize_price_value(value)

    @field_validator("cron", mode="before")
    @classmethod
    def empty_str_to_none(cls, value):
        return _normalize_optional_string(value)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value):
        return _validate_cron_expression(value)

    @field_validator("account_state_file", mode="before")
    @classmethod
    def empty_account_to_none(cls, value):
        return _normalize_optional_string(value)

    @field_validator("new_publish_option", "region", mode="before")
    @classmethod
    def empty_str_to_none_for_strings(cls, value):
        return _normalize_optional_string(value)

    @field_validator("decision_mode", mode="before")
    @classmethod
    def check_decision_mode(cls, value):
        """API 边界保持严格：只接受已注册的判定策略名。

        运行时对历史脏数据的宽松归一化在 scraper / spider_v2 里（回退到默认策略），
        这里则拒绝拼写错误，避免"写错了却静默按 AI 跑"。
        """
        if value is None:
            return None
        if not is_registered(value):
            raise ValueError(f"不支持的判定方式: {value}")
        return normalize_decision_mode(value)

    @field_validator("keyword_rules", mode="before")
    @classmethod
    def normalize_keyword_rules(cls, value):
        return _normalize_keyword_values(value)

    @model_validator(mode="after")
    def validate_decision_mode_payload(self):
        # 校验依据来自判定策略声明的元信息，而不是硬编码 "ai"/"keyword"
        meta = strategy_meta(normalize_decision_mode(self.decision_mode))
        description = str(self.description or "").strip()
        if meta.requires_description and not description:
            raise ValueError(f"{meta.display_name}模式下，详细需求(description)不能为空。")
        if meta.requires_keyword_rules and not _has_keyword_rules(self.keyword_rules):
            raise ValueError(f"{meta.display_name}模式下，至少需要一个关键词。")
        if self.account_strategy == "fixed" and not self.account_state_file:
            raise ValueError("固定账号模式下必须选择账号。")
        return self
