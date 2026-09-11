"""静态分析回归测试：禁止"未定义名字"这类运行时必崩的问题。

来由：给 scraper 加账号登录态路径校验时，代码里用了 ``UnsafePathError`` 与
``safe_account_state_path`` 却**忘了 import**。结果是每一次真实抓取尝试都会立刻
``NameError`` —— 而当时的验证只覆盖了 HTTP 接口，没有驱动抓取路径，于是这个
"抓取功能整体失效"的回归漏了过去，直到为轮换逻辑写端到端测试才暴露。

所以这里把 pyflakes 的 "undefined name" 检查固化成测试：它专门抓这类
"写起来像对、跑起来必崩"的错误。其余告警（未使用的导入、无占位符的 f-string）
不属于本测试范围，避免噪音导致没人看。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import pyflakes  # noqa: F401
except ImportError:  # pragma: no cover - 取决于本地环境
    pyflakes = None

if pyflakes is None:
    if os.environ.get("CI"):
        # CI 里静默跳过等于这道防线不存在：requirements.txt 已包含 pyflakes，
        # 因此 CI 上必须装好，缺了就是配置问题，要显式失败。
        pytest.fail("CI 环境缺少 pyflakes（requirements.txt 应已包含）")
    pytest.skip("需要 pyflakes（见 requirements.txt）", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[2]
TARGETS = ["src", "spider_v2.py", "desktop_launcher.py"]


def _run_pyflakes() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", *TARGETS],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_no_undefined_names_in_source():
    findings = [
        line for line in _run_pyflakes()
        if "undefined name" in line or "undefined local" in line
    ]
    assert not findings, (
        "源码中存在未定义名字（运行时必然报错）:\n" + "\n".join(findings)
    )


def test_pyflakes_is_actually_running():
    """防止 pyflakes 因路径/版本问题静默失效，让上面的断言变成永远通过。"""
    probe = subprocess.run(
        [sys.executable, "-m", "pyflakes", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
