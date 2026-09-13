import json
import os
import re
import sys
from typing import Awaitable, Callable, Optional

import aiofiles

from src.infrastructure.external.ai_client import AIClient

# The meta-prompt to instruct the AI
META_PROMPT_TEMPLATE = """
你是一位世界级的AI提示词工程大师。你的任务是根据用户提供的【购买需求】，模仿一个【参考范例】，为闲鱼监控机器人的AI分析模块（代号 EagleEye）生成一份全新的【分析标准】文本。

你的输出必须严格遵循【参考范例】的结构、语气和核心原则，但内容要完全针对用户的【购买需求】进行定制。最终生成的文本将作为AI分析模块的思考指南。

---
这是【参考范例】（`macbook_criteria.txt`）：
```text
{reference_text}
```
---

这是用户的【购买需求】：
```text
{user_description}
```
---

请现在开始生成全新的【分析标准】文本。请注意：
1.  **只输出新生成的文本内容**，不要包含任何额外的解释、标题或代码块标记。
2.  保留范例中的 `[V6.3 核心升级]`、`[V6.4 逻辑修正]` 等版本标记，这有助于保持格式一致性。
3.  将范例中所有与 "MacBook" 相关的内容，替换为与用户需求商品相关的内容。
4.  思考并生成针对新商品类型的“一票否决硬性原则”和“危险信号清单”。
5.  **不要输出思考过程**：不要包含 <think>、<thinking>、<reasoning> 之类的标签，
    也不要写“用户要求我……”这类自述，直接给出最终文本。
"""

ProgressCallback = Callable[[str, str], Awaitable[None]]

#: 分析标准文件的最小合理长度。参考范例 macbook_criteria.txt 有 2262 字符；
#: 低于这个值通常意味着文件被截断、是空壳，或者干脆写错了内容（实测遇到过
#: 只有 33 字符的文件，以及整份文件其实是模型思维链的情况）。
MIN_CRITERIA_LENGTH = 200


def criteria_warnings(task_name: str, criteria_path: str, criteria_text: str) -> list:
    """检查某个任务的分析标准是否可疑，返回告警文案（空列表 = 没问题）。

    两类问题实测都遇到过：
    - 文件只有 33 字符的空壳；
    - 整份文件其实是模型思维链（生成时没剥离 <think>），文件里写着"你的任务是
      重新生成一份分析标准"，于是 AI 收到错误指令、响应缺字段。
    这两种情况表面看都只是"AI 分析失败"，很难往文件本身想，所以提前告警。
    """
    stripped = (criteria_text or "").strip()
    if len(stripped) < MIN_CRITERIA_LENGTH:
        return [
            f"⚠️  警告: 任务 '{task_name}' 的分析标准过短"
            f"（{len(stripped)} 字符，建议至少 {MIN_CRITERIA_LENGTH}）：{criteria_path}",
            "     可能是文件被截断、内容为空壳，或生成时把模型思维链写了进去。",
            "     建议在 Web UI 里重新生成该任务的分析标准，或手工补齐。",
        ]
    if strip_reasoning(criteria_text or "") != stripped:
        return [
            f"⚠️  警告: 任务 '{task_name}' 的分析标准里疑似混入了模型思维链"
            f"（<think> 标签）：{criteria_path}",
            "     这会让 AI 收到错误的指令，建议重新生成或手工清理该文件。",
        ]
    return []

#: 模型可能把推理过程一并吐在正文里（实测遇到：整份 criteria 文件里只有一段
#: 英文思维链，且因输出上限被截断在半句），必须剥离干净再存盘。
_REASONING_BLOCK_RE = re.compile(
    r"<\s*(think|thinking|reasoning|analysis)\s*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
#: 没有闭合标签时（被 max_output_tokens 截断），从开标签起全部视为推理过程。
_REASONING_OPEN_RE = re.compile(
    r"<\s*(think|thinking|reasoning|analysis)\s*>",
    re.IGNORECASE,
)


def strip_reasoning(text: str) -> str:
    """剥离模型输出里的推理过程，只留下最终正文。

    两种情况都要处理：
    - ``<think>...</think>`` 这类闭合块，直接删除；
    - 只有开标签、没有闭合标签（输出被截断），从开标签起整段丢弃。
    """
    if not text:
        return ""
    cleaned = _REASONING_BLOCK_RE.sub("", text)
    open_match = _REASONING_OPEN_RE.search(cleaned)
    if open_match:
        cleaned = cleaned[: open_match.start()]
    return cleaned.strip()


async def _report_progress(
    progress_callback: Optional[ProgressCallback],
    step_key: str,
    message: str,
) -> None:
    if progress_callback:
        await progress_callback(step_key, message)


def _read_reference_text(reference_file_path: str) -> str:
    try:
        with open(reference_file_path, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"参考文件未找到: {reference_file_path}")
    except IOError as exc:
        raise IOError(f"读取参考文件失败: {exc}")


async def _request_generated_text(ai_client: AIClient, prompt: str) -> str:
    print("正在调用AI生成新的分析标准，请稍候...")
    try:
        generated_text = await ai_client._call_ai(
            [{"role": "user", "content": prompt}],
            temperature=0.5,
            # 800 太小：推理型模型会先写一大段思考，正文还没开始就被截断，
            # 于是存下来的是半截思维链（实测踩过）。
            max_output_tokens=4000,
            enable_json_output=False,
        )
    except Exception as exc:
        print(f"调用 OpenAI API 时出错: {exc}")
        raise

    cleaned = strip_reasoning(generated_text)
    if not cleaned:
        # 宁可失败也不要把垃圾落进 prompts/：那会让该任务的每次 AI 分析都缺字段。
        raise RuntimeError(
            "AI 只返回了思考过程（或输出被截断），没有可用的分析标准正文。"
            "请重试，或改用更遵循指令的模型。"
        )

    print("AI已成功生成内容。")
    return cleaned


async def _close_ai_client(
    ai_client: AIClient,
    active_error: BaseException | None,
) -> None:
    try:
        await ai_client.close()
    except Exception as close_error:
        print(f"关闭 AI 客户端时出错: {close_error}")
        if active_error is None:
            raise


async def generate_criteria(
    user_description: str,
    reference_file_path: str,
    progress_callback: Optional[ProgressCallback] = None,
) -> str:
    """
    Generates a new criteria file content using AI.
    """
    ai_client = AIClient()
    active_error: BaseException | None = None
    try:
        if not ai_client.is_available():
            ai_client.refresh()
        if not ai_client.is_available():
            raise RuntimeError("AI客户端未初始化，无法生成分析标准。请检查.env配置。")

        await _report_progress(progress_callback, "reference", "正在读取参考文件。")
        print(f"正在读取参考文件: {reference_file_path}")
        reference_text = _read_reference_text(reference_file_path)

        await _report_progress(progress_callback, "prompt", "正在构建发送给 AI 的指令。")
        print("正在构建发送给AI的指令...")
        prompt = META_PROMPT_TEMPLATE.format(
            reference_text=reference_text,
            user_description=user_description,
        )

        await _report_progress(progress_callback, "llm", "正在调用 AI 生成分析标准。")
        return await _request_generated_text(ai_client, prompt)
    except Exception as exc:
        active_error = exc
        raise
    finally:
        await _close_ai_client(ai_client, active_error)


async def update_config_with_new_task(new_task: dict, config_file: str = "config.json"):
    """
    将一个新任务添加到指定的JSON配置文件中。
    """
    print(f"正在更新配置文件: {config_file}")
    try:
        # 读取现有配置
        config_data = []
        if os.path.exists(config_file):
            async with aiofiles.open(config_file, 'r', encoding='utf-8') as f:
                content = await f.read()
                # 处理空文件的情况
                if content.strip():
                    try:
                        config_data = json.loads(content)
                        print(f"成功读取现有配置，当前任务数量: {len(config_data)}")
                    except json.JSONDecodeError as e:
                        print(f"解析配置文件失败，将创建新配置: {e}")
                        config_data = []
        else:
            print(f"配置文件不存在，将创建新文件: {config_file}")

        # 追加新任务
        config_data.append(new_task)

        # 写回配置文件
        async with aiofiles.open(config_file, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(config_data, ensure_ascii=False, indent=2))
            print("配置文件写入完成")

        print(f"成功！新任务 '{new_task.get('task_name')}' 已添加到 {config_file} 并已启用。")
        return True
    except json.JSONDecodeError as e:
        error_msg = f"错误: 配置文件 {config_file} 格式错误，无法解析: {e}"
        sys.stderr.write(error_msg + "\n")
        print(error_msg)
        return False
    except IOError as e:
        error_msg = f"错误: 读写配置文件失败: {e}"
        sys.stderr.write(error_msg + "\n")
        print(error_msg)
        return False
    except Exception as e:
        error_msg = f"错误: 更新配置文件时发生未知错误: {e}"
        sys.stderr.write(error_msg + "\n")
        print(error_msg)
        import traceback
        print(traceback.format_exc())
        return False
