"""
AI 客户端封装
提供统一的 AI 调用接口
"""
import os
import json
import base64
from typing import Dict, List, Optional
from dotenv import load_dotenv
from openai import AsyncOpenAI
from src.ai_message_builder import build_analysis_messages
from src.infrastructure.config.settings import AISettings
from src.infrastructure.external.ai_client_factory import build_async_openai_client
from src.infrastructure.config.env_manager import env_manager
from src.services.ai_request_compat import (
    CHAT_COMPLETIONS_API_MODE,
    RESPONSES_API_MODE,
    build_ai_request_params,
    create_ai_response_async,
    is_chat_completions_api_unsupported_error,
    is_json_output_unsupported_error,
    is_responses_api_unsupported_error,
    is_temperature_unsupported_error,
    remove_temperature_param,
)
from src.services.ai_response_parser import (
    EmptyAIResponseError,
    extract_ai_response_content,
    parse_ai_response_json,
)


class AIClient:
    """AI 客户端封装"""

    def __init__(self):
        self.settings: Optional[AISettings] = None
        self.client: Optional[AsyncOpenAI] = None
        self.refresh()

    def _load_settings(self) -> None:
        load_dotenv(dotenv_path=env_manager.env_file, override=True)
        self.settings = AISettings()

    def refresh(self) -> None:
        self._load_settings()
        self.client = self._initialize_client()

    def _initialize_client(self) -> Optional[AsyncOpenAI]:
        """初始化 OpenAI 客户端。

        与抓取侧的 ``src/config.py`` 共用 ai_client_factory，两条路径的代理处理与
        NO_PROXY 修补不再各写一份（历史上正是因为各写一份，NO_PROXY 的修复只落在
        这一侧，抓取侧的 AI 分析在同样配置下会全挂）。
        """
        if not self.settings or not self.settings.is_configured():
            print("警告：AI 配置不完整，AI 功能将不可用")
            return None

        return build_async_openai_client(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            proxy_url=self.settings.proxy_url,
        )

    def is_available(self) -> bool:
        """检查 AI 客户端是否可用"""
        return self.client is not None

    async def close(self) -> None:
        """关闭底层异步客户端，避免事件循环结束后再触发清理。"""
        client = self.client
        self.client = None
        if client is None:
            return

        close = getattr(client, "close", None)
        if close is None:
            return
        await close()

    @staticmethod
    def encode_image(image_path: str) -> Optional[str]:
        """将图片编码为 Base64"""
        if not image_path or not os.path.exists(image_path):
            return None
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            print(f"编码图片失败: {e}")
            return None

    async def analyze(
        self,
        product_data: Dict,
        image_paths: List[str],
        prompt_text: str
    ) -> Optional[Dict]:
        """
        分析商品数据

        Args:
            product_data: 商品数据
            image_paths: 图片路径列表
            prompt_text: 分析提示词

        Returns:
            分析结果
        """
        if not self.is_available():
            print("AI 客户端不可用")
            return None

        try:
            messages = self._build_messages(product_data, image_paths, prompt_text)
            response = await self._call_ai(messages)
            return self._parse_response(response)
        except Exception as e:
            print(f"AI 分析失败: {e}")
            return None

    def _build_messages(self, product_data: Dict, image_paths: List[str], prompt_text: str) -> List[Dict]:
        """构建 AI 消息"""
        product_json = json.dumps(product_data, ensure_ascii=False, indent=2)
        image_data_urls: List[str] = []
        for path in image_paths:
            base64_img = self.encode_image(path)
            if base64_img:
                image_data_urls.append(f"data:image/jpeg;base64,{base64_img}")

        # 与 ai_handler 一致：规则进 system，不可信的卖家数据进 user
        return build_analysis_messages(
            product_json,
            prompt_text,
            image_data_urls=image_data_urls,
        )

    async def _call_ai(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.1,
        max_output_tokens: int = 4000,
        enable_json_output: Optional[bool] = None,
    ) -> str:
        """调用 AI API"""
        api_mode = CHAT_COMPLETIONS_API_MODE
        use_response_format = (
            self.settings.enable_response_format
            if enable_json_output is None
            else enable_json_output
        )
        use_temperature = True
        max_attempts = 4

        for attempt in range(max_attempts):
            request_params = build_ai_request_params(
                api_mode,
                model=self.settings.model_name,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                enable_json_output=use_response_format,
            )
            if not use_temperature:
                request_params = remove_temperature_param(request_params)

            if self.settings.enable_thinking:
                request_params["extra_body"] = {"enable_thinking": False}

            try:
                response = await create_ai_response_async(
                    self.client,
                    api_mode,
                    request_params,
                )
                return extract_ai_response_content(response)
            except EmptyAIResponseError as exc:
                if attempt < max_attempts - 1:
                    print(
                        f"AI响应为空，正在自动重试 ({attempt + 2}/{max_attempts})"
                    )
                    continue
                raise exc
            except Exception as exc:
                changed = False
                if (
                    api_mode == CHAT_COMPLETIONS_API_MODE
                    and is_chat_completions_api_unsupported_error(exc)
                ):
                    api_mode = RESPONSES_API_MODE
                    changed = True
                    print("当前服务未实现 Chat Completions API，正在自动回退到 Responses API")
                elif (
                    api_mode == RESPONSES_API_MODE
                    and is_responses_api_unsupported_error(exc)
                ):
                    api_mode = CHAT_COMPLETIONS_API_MODE
                    changed = True
                    print("当前服务未实现 Responses API，正在自动回退到 Chat Completions API")
                if use_response_format and is_json_output_unsupported_error(exc):
                    use_response_format = False
                    changed = True
                    print("当前模型不支持结构化 JSON 输出，正在自动重试并移除该参数")
                if use_temperature and is_temperature_unsupported_error(exc):
                    use_temperature = False
                    changed = True
                    print("当前模型不支持 temperature 参数，正在自动重试并移除该参数")
                if changed and attempt < max_attempts - 1:
                    continue
                raise

        raise RuntimeError("AI 调用在兼容性重试后仍未返回结果")

    def _parse_response(self, response_text: str) -> Optional[Dict]:
        """解析 AI 响应"""
        try:
            return parse_ai_response_json(response_text)
        except json.JSONDecodeError:
            print(f"无法解析 AI 响应: {response_text[:100]}")
            return None
