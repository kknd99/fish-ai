"""生成分析标准时必须剥离模型的思维链。

实测踩到的坑：用「AI 生成任务」功能生成 criteria 文件时，推理型模型把思考过程
一并吐在正文里，而代码直接 ``generated_text.strip()`` 存盘，于是 ``prompts/`` 下
出现了一份**只有英文思维链**的文件（末尾还被 800 token 的输出上限截断在半句）。
之后该任务的每次 AI 分析都会把这段"重新生成一份标准"的指令当成分析标准，
表现为响应缺少 ``prompt_version`` 等必需字段。
"""
import pytest

from src.prompt_utils import strip_reasoning


class TestStripReasoning:
    def test_removes_closed_think_block(self):
        assert strip_reasoning("<think>推理过程</think>真正的正文") == "真正的正文"

    def test_removes_reasoning_when_body_comes_first(self):
        assert strip_reasoning("真正的正文\n<think>推理</think>") == "真正的正文"

    def test_removes_multiple_blocks(self):
        raw = "<think>a</think>正文一<thinking>b</thinking>正文二"
        assert strip_reasoning(raw) == "正文一正文二"

    def test_truncated_open_tag_drops_everything_after(self):
        """被 max_output_tokens 截断时只有开标签，从开标签起全是推理。"""
        assert strip_reasoning("正文<think>写到一半就断了") == "正文"

    def test_multiline_and_case_insensitive(self):
        raw = "<THINK>\n多行\n推理\n</THINK>\n\n最终文本\n"
        assert strip_reasoning(raw) == "最终文本"

    def test_reasoning_tags_variants(self):
        for tag in ("think", "thinking", "reasoning", "analysis"):
            assert strip_reasoning(f"<{tag}>推理</{tag}>正文") == "正文"

    def test_only_reasoning_yields_empty(self):
        """整份文件都是思维链（实测就是这样）——必须识别为空，不能存盘。"""
        raw = "<think>The user asks: You are a world-class AI prompt engineer.</think>"
        assert strip_reasoning(raw) == ""

    def test_plain_text_untouched(self):
        text = "[V6.3 核心升级]\n硬性原则：……"
        assert strip_reasoning(text) == text

    def test_empty_and_none(self):
        assert strip_reasoning("") == ""
        assert strip_reasoning(None) == ""

    def test_does_not_eat_unrelated_angle_brackets(self):
        text = "价格 < 1000 且 成色 > 九成新"
        assert strip_reasoning(text) == text


class TestGeneratedCriteriaGuard:
    """保存环节的兜底：过短或全是思维链时必须报错，而不是落盘。"""

    def test_rejects_reasoning_only(self):
        from src.services.task_generation_runner import save_generated_criteria

        import asyncio

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(save_generated_criteria("prompts/x_criteria.txt", "<think>只有推理</think>"))
        assert "思考过程" in str(exc.value) or "为空" in str(exc.value)

    def test_rejects_too_short(self):
        from src.services.task_generation_runner import save_generated_criteria

        import asyncio

        with pytest.raises(RuntimeError) as exc:
            asyncio.run(save_generated_criteria("prompts/x_criteria.txt", "太短了"))
        assert "过短" in str(exc.value)

    def test_accepts_clean_long_text(self, tmp_path, monkeypatch):
        import asyncio
        import os

        from src.services.task_generation_runner import save_generated_criteria

        monkeypatch.chdir(tmp_path)
        body = "[V6.3 核心升级]\n" + "硬性原则与危险信号清单。" * 30
        asyncio.run(save_generated_criteria("prompts/x_criteria.txt", f"<think>推理</think>{body}"))

        written = (tmp_path / "prompts/x_criteria.txt").read_text(encoding="utf-8")
        assert written == body.strip()
        assert "<think>" not in written
