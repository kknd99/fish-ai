"""按快照恢复浏览器状态（storage + 指纹字段）。

背景（实测）：
1. 快照的 `storage.local` 里有风控引擎自己的状态（`baxia_entry_config` 等），
   而代码此前只用 `storage` 这个键判断"这是增强快照"，从不恢复内容 ——
   每次启动爬虫，站点看到的是一个没有风控状态的陌生客户端。
2. UA 被覆盖成 Windows Chrome 117，但容器里实际是 Linux 上的 Chromium：
   `navigator.platform` 仍是 Linux、`userAgentData.brands` 仍是真实引擎版本。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from src.scraper import _build_snapshot_init_script

NODE = shutil.which("node")


def _snapshot() -> dict:
    return {
        "env": {
            "navigator": {
                "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0",
                "platform": "Win32",
                "languages": ["zh-CN", "zh"],
                "hardwareConcurrency": 12,
                "deviceMemory": 8,
                "maxTouchPoints": 0,
                "userAgentData": {
                    "brands": [
                        {"brand": "Google Chrome", "version": "117"},
                        {"brand": "Not;A=Brand", "version": "8"},
                        {"brand": "Chromium", "version": "117"},
                    ],
                    "mobile": False,
                    "platform": "Windows",
                },
            }
        },
        "storage": {
            "local": {"baxia_entry_config": "TOKEN123", "tfstk__": "zzz", "APLUS_CNA": "x"},
            "session": {"__accs_device": "1082469912_382"},
        },
        "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0"},
    }


class TestSnapshotInitScript:
    def test_restores_platform_and_ua_data(self):
        script = _build_snapshot_init_script(_snapshot())
        assert "Win32" in script
        assert "Google Chrome" in script and "117" in script
        assert "userAgentData" in script

    def test_aligns_languages_cores_memory_and_touch(self):
        """旧脚本把这些写死成移动端值，与 Windows 桌面 UA 自相矛盾。"""
        script = _build_snapshot_init_script(_snapshot())
        assert "hardwareConcurrency" in script
        assert "deviceMemory" in script
        assert "maxTouchPoints" in script
        assert "languages" in script

    def test_restores_both_storages(self):
        script = _build_snapshot_init_script(_snapshot())
        assert "baxia_entry_config" in script
        assert "TOKEN123" in script
        assert "tfstk__" in script
        assert "__accs_device" in script
        assert "localStorage" in script
        assert "sessionStorage" in script

    def test_handles_missing_fields_gracefully(self):
        script = _build_snapshot_init_script({})
        assert "localStorage" in script
        # 缺字段时不该出现 Python 的 None 字面量污染 JS
        assert "None" not in script

    def test_payload_is_json_escaped(self):
        snapshot = _snapshot()
        snapshot["storage"]["local"]["weird"] = 'quote " and backslash \\ and </script>'
        script = _build_snapshot_init_script(snapshot)
        # 值必须能被 JSON 正确转义（否则会截断 JS 字符串）
        assert '\\"' in script
        assert "</script>" in script  # 内容保留，但已被 JSON 转义为字符串字面量

    @pytest.mark.skipif(NODE is None, reason="未安装 node，无法校验 JS 语法")
    def test_generated_javascript_is_syntactically_valid(self, tmp_path: Path):
        """生成的脚本必须是合法 JS —— 语法错误会让注入静默失效。"""
        script = _build_snapshot_init_script(_snapshot())
        target = tmp_path / "init.js"
        target.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [NODE, "--check", str(target)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.skipif(NODE is None, reason="未安装 node，无法校验 JS 语法")
    def test_empty_snapshot_javascript_is_syntactically_valid(self, tmp_path: Path):
        target = tmp_path / "init_empty.js"
        target.write_text(_build_snapshot_init_script({}), encoding="utf-8")
        result = subprocess.run(
            [NODE, "--check", str(target)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
