from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_DIST = "/dist"


def read_repo_file(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_frontend_build_output_path_is_consistent_across_configs():
    vite_config = read_repo_file("web-ui/vite.config.ts")
    dockerfile = read_repo_file("Dockerfile")
    frontend_dockerfile = read_repo_file("web-ui/Dockerfile")
    dockerignore = read_repo_file(".dockerignore")
    start_script = read_repo_file("start.sh")
    dockerignore_lines = dockerignore.splitlines()

    assert "path.resolve(__dirname, '../dist')" in vite_config
    assert (
        f"COPY --from=frontend-builder {ROOT_DIST} /app/dist" in dockerfile
    ), "Docker multi-stage copy must use the Vite build output path."
    assert (
        f"COPY --from=builder {ROOT_DIST} /usr/share/nginx/html"
        in frontend_dockerfile
    ), "Frontend-only Docker build must use the Vite build output path."
    assert "dist/" in dockerignore_lines
    # 说明（修正上游的陈旧断言）：这里原本断言 "web-ui/dist" **不在** .dockerignore 里，
    # 但它同样是构建产物 —— 放进 build context 只会把宿主机的旧构建带进镜像。
    # 两条排除都保留，才是"构建产物一律不进上下文"的一致性。
    assert "web-ui/dist" in dockerignore_lines, (
        "web-ui/dist 属于构建产物，应从 build context 中排除"
    )
    assert '[ ! -d "dist" ]' in start_script
    assert "cp -r web-ui/dist ./" not in start_script
