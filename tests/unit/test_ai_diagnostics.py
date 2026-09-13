"""AI 失败时落盘原始响应 + 分析标准可疑时告警。

这两条都是这轮线上排查总结出来的：当时 AI 一直报「响应缺少必需字段
'prompt_version'」，而 `logs/ai/*.log` 里**只有请求摘要、没有响应内容**，
只能靠反复试；同时那个任务的 criteria 文件其实是模型思维链，却没有任何提示。
"""
import json
from pathlib import Path

from src.ai_handler import (
    MAX_LOGGED_RESPONSE_CHARS,
    _append_ai_failure_log,
)
from src.prompt_utils import MIN_CRITERIA_LENGTH, criteria_warnings


class TestAiFailureLog:
    def test_appends_raw_response_after_request_summary(self, tmp_path: Path):
        log = tmp_path / "20260101_120000.log"
        log.write_text('{"timestamp": "20260101_120000"}\n', encoding="utf-8")

        _append_ai_failure_log(
            str(log), 1, "格式验证失败：缺少必需字段或字段类型不正确",
            '{"is_recommended": true, "reason": "缺了 prompt_version"}',
        )

        lines = log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2, "请求摘要之后应追加一条失败记录（JSONL）"
        assert json.loads(lines[0])["timestamp"] == "20260101_120000"
        entry = json.loads(lines[1])
        assert entry["attempt"] == 1
        assert "缺少必需字段" in entry["failure_reason"]
        assert entry["raw_response"] == '{"is_recommended": true, "reason": "缺了 prompt_version"}'
        assert entry["response_chars"] == len(entry["raw_response"])
        assert entry["response_truncated"] is False

    def test_multiple_failures_append_multiple_lines(self, tmp_path: Path):
        log = tmp_path / "ai.log"
        log.write_text('{"timestamp": "x"}\n', encoding="utf-8")

        _append_ai_failure_log(str(log), 1, "格式验证失败", "第一次响应")
        _append_ai_failure_log(str(log), 2, "JSON解析失败", "第二次响应")

        entries = [json.loads(line) for line in log.read_text(encoding="utf-8").strip().splitlines()]
        assert [e.get("attempt") for e in entries[1:]] == [1, 2]
        assert entries[2]["raw_response"] == "第二次响应"

    def test_truncates_oversized_response(self, tmp_path: Path):
        log = tmp_path / "ai.log"
        huge = "x" * (MAX_LOGGED_RESPONSE_CHARS + 5000)

        _append_ai_failure_log(str(log), 1, "格式验证失败", huge)

        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["response_truncated"] is True
        assert entry["response_chars"] == len(huge)
        assert len(entry["raw_response"]) == MAX_LOGGED_RESPONSE_CHARS

    def test_handles_non_string_response(self, tmp_path: Path):
        log = tmp_path / "ai.log"
        _append_ai_failure_log(str(log), 1, "返回空响应", None)
        entry = json.loads(log.read_text(encoding="utf-8").strip())
        assert entry["raw_response"] == "None"

    def test_empty_path_is_noop(self, capsys):
        # 日志写入本身失败时 log_filepath 是空串，此时不能抛异常打断分析
        _append_ai_failure_log("", 1, "格式验证失败", "x")
        assert capsys.readouterr().out == ""

    def test_get_ai_analysis_wires_all_failure_branches(self):
        """接线守卫：三个失败分支都要调用落盘，否则又会退化成"只有请求摘要"。"""
        import inspect

        from src.ai_handler import get_ai_analysis

        source = inspect.getsource(get_ai_analysis)
        assert source.count("_append_ai_failure_log") == 3, (
            "格式验证失败 / JSON解析失败 / 空响应 三个分支都应落盘原始响应"
        )
        assert 'log_filepath = ""' in source, "log_filepath 需要在 try 之前先初始化"


class TestCriteriaWarnings:
    def test_flags_short_criteria(self):
        warnings = criteria_warnings("我的任务", "prompts/x_criteria.txt", "[V6.3] 简短")
        assert len(warnings) == 3
        assert "过短" in warnings[0]
        assert "prompts/x_criteria.txt" in warnings[0]
        assert "我的任务" in warnings[0]

    def test_flags_leaked_chain_of_thought(self):
        text = "<think>The user asks ...</think>" + "正常标准。" * 60
        warnings = criteria_warnings("我的任务", "prompts/x_criteria.txt", text)
        assert len(warnings) == 2
        assert "思维链" in warnings[0]

    def test_short_takes_priority_over_think(self):
        """又短又含思维链时，先报"过短"（更根本的问题）。"""
        warnings = criteria_warnings("t", "p", "<think>推理</think>短")
        assert "过短" in warnings[0]

    def test_clean_criteria_produces_no_warning(self):
        assert criteria_warnings("t", "p", "正常标准。" * 60) == []

    def test_threshold_is_reasonable(self):
        assert 100 <= MIN_CRITERIA_LENGTH <= 500, "阈值应贴近参考范例量级（2000+ 字符）的下限"

    def test_empty_and_none(self):
        assert criteria_warnings("t", "p", "") != []
        assert criteria_warnings("t", "p", None) != []
