"""编排文件的回归测试。

用**真正的 Compose 二进制**解析 ``docker-compose.lan.yaml`` / ``.json``，而不是只做
一遍 YAML 语法检查——只有 Compose 自己才知道端口插值、卷写法、字段拼写这些规则，
这些正是手工改编排文件时最容易写错、又最难靠肉眼发现的地方。

未找到 Compose 时自动跳过。指定二进制：

    DOCKER_COMPOSE_BIN=/path/to/docker-compose pytest tests/integration/test_compose_files.py
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LAN_YAML = REPO_ROOT / "docker-compose.lan.yaml"
LAN_JSON = REPO_ROOT / "docker-compose.lan.json"

#: 期望挂载进容器的路径（单文件挂载点 + 数据目录）。
EXPECTED_MOUNTS = {
    "/app/.env",
    "/app/config.json",
    "/app/xianyu_state.json",
    "/app/data",
    "/app/state",
    "/app/prompts",
    "/app/jsonl",
    "/app/logs",
    "/app/images",
    "/app/price_history",
}


def _compose_command():
    """定位可用的 Compose 命令，找不到返回 None。"""
    explicit = os.environ.get("DOCKER_COMPOSE_BIN")
    if explicit and Path(explicit).exists():
        return [explicit]

    standalone = shutil.which("docker-compose")
    if standalone:
        return [standalone]

    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run(
            [docker, "compose", "version"], capture_output=True, text=True
        )
        if probe.returncode == 0:
            return [docker, "compose"]
    return None


COMPOSE = _compose_command()

pytestmark = pytest.mark.skipif(
    COMPOSE is None and not os.environ.get("DOCKER_COMPOSE_BIN"),
    reason="未找到 compose 二进制（可用 DOCKER_COMPOSE_BIN 指定）",
)


def _config(path: Path, **env_overrides) -> str:
    """跑一次 ``compose config``，返回解析后的规范化 YAML。"""
    env = dict(os.environ)
    env.pop("APP_PORT", None)
    env.update({k: str(v) for k, v in env_overrides.items()})

    result = subprocess.run(
        COMPOSE + ["-f", str(path), "config"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, f"compose config 失败:\n{result.stderr}"
    return result.stdout


def test_lan_yaml_is_accepted_by_compose():
    """lan 编排文件能被 Compose 正常解析。"""
    assert "services:" in _config(LAN_YAML)


def test_lan_json_is_accepted_by_compose():
    """JSON 版编排文件同样能被 Compose 解析。

    JSON 是 YAML 1.2 的子集，所以不需要特殊处理；这条用例把"NAS 面板里粘 JSON"
    这条路固定下来，避免哪天把 json 改成非 YAML 兼容的写法。
    """
    parsed = json.loads(LAN_JSON.read_text(encoding="utf-8"))
    assert parsed["services"]["app"]["build"] == "."

    assert "services:" in _config(LAN_JSON)


def test_lan_json_and_yaml_are_equivalent():
    """两份编排文件解析结果必须完全一致（否则就是改了一份忘了另一份）。"""
    assert _config(LAN_JSON) == _config(LAN_YAML)


def test_default_host_port_is_8000():
    assert 'published: "8000"' in _config(LAN_YAML)


def test_app_port_overrides_host_port_only():
    """APP_PORT 只改宿主端口，容器内仍然是 8000。

    容器内监听端口被 environment 里的 SERVER_PORT 钉死（其优先级高于 env_file），
    所以 .env 里写 SERVER_PORT 不会把映射弄错位。
    """
    resolved = _config(LAN_YAML, APP_PORT=9000)
    assert 'published: "9000"' in resolved
    assert "target: 8000" in resolved


def test_expected_mounts_are_declared():
    resolved = _config(LAN_YAML)
    for target in EXPECTED_MOUNTS:
        assert f"target: {target}" in resolved, f"缺少挂载点 {target}"


def test_state_and_env_use_selinux_shared_label():
    """SELinux 主机上缺 :z 会导致容器读不到挂载文件。"""
    source = LAN_YAML.read_text(encoding="utf-8")
    for entry in ("./.env:", "./config.json:", "./xianyu_state.json:", "./data:"):
        line = next(
            (l.strip() for l in source.splitlines() if l.strip().startswith(f"- {entry}")),
            None,
        )
        assert line is not None, f"没找到挂载项 {entry}"
        assert line.endswith(":z"), f"{entry} 缺少 :z 标签: {line}"


def test_init_not_set_because_entrypoint_already_is_tini():
    """镜像 ENTRYPOINT 就是 tini，再写 init: true 会出现双 tini 警告。

    断言按解析结果判断，不能直接搜文本——文件里的注释本身就在解释「刻意不写
    init: true」，搜文本会把注释也算成命中。
    """
    service = yaml.safe_load(LAN_YAML.read_text(encoding="utf-8"))["services"]["app"]
    assert "init" not in service

    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["tini", "--"]' in dockerfile


def test_frontend_region_data_exists():
    """前端构建依赖这个数据文件；打包/传输时漏掉它，构建会直接失败。

    （曾经因为 tar 排除模式没锚定路径，把 web-ui/src/data/ 整个排除了。）
    """
    assert (REPO_ROOT / "web-ui/src/data/goofishRegions.json").is_file()
