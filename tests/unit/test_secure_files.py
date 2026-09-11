"""敏感文件权限测试（安全审计 M3）。

对应漏洞：项目里没有任何 chmod/umask，应用创建的文件都是默认 0644 ——
包括完整闲鱼会话 cookie（``xianyu_state.json``）、账号文件、含 token 的 ``.env``、
以及会被日志接口读走的任务日志。同机其他用户与共享卷容器可直接读走。
"""
import os
import stat
import sys

import pytest

from src.core.secure_files import (
    SECURE_DIR_MODE,
    SECURE_FILE_MODE,
    restrict_file,
    restrict_permissions,
    secure_makedirs,
)

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="Windows 无 POSIX 权限位"
)


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@posix_only
def test_secure_makedirs_creates_0700(tmp_path):
    target = tmp_path / "state"
    secure_makedirs(target)
    assert mode_of(target) == SECURE_DIR_MODE


@posix_only
def test_secure_makedirs_tightens_existing_permissive_directory(tmp_path):
    target = tmp_path / "state"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    secure_makedirs(target)
    assert mode_of(target) == SECURE_DIR_MODE


@posix_only
def test_restrict_file_tightens_world_readable_file(tmp_path):
    secret = tmp_path / "xianyu_state.json"
    secret.write_text('{"cookies": []}', encoding="utf-8")
    os.chmod(secret, 0o644)
    assert mode_of(secret) == 0o644

    restrict_file(secret)
    assert mode_of(secret) == SECURE_FILE_MODE


@posix_only
def test_restrict_permissions_tightens_even_slightly_wide_modes(tmp_path):
    """契约：敏感文件一律 owner-only。

    0640 这类"只是稍宽"的权限同样会被收紧——这些都是凭据文件，
    需要分组可读应当用 ACL，而不是放宽本函数的语义。
    """
    secret = tmp_path / "custom.json"
    secret.write_text("{}", encoding="utf-8")
    os.chmod(secret, 0o640)

    restrict_permissions(secret, SECURE_FILE_MODE)
    assert mode_of(secret) == SECURE_FILE_MODE


@posix_only
def test_restrict_permissions_leaves_correct_mode_untouched(tmp_path):
    secret = tmp_path / "custom.json"
    secret.write_text("{}", encoding="utf-8")
    os.chmod(secret, SECURE_FILE_MODE)

    restrict_permissions(secret, SECURE_FILE_MODE)
    assert mode_of(secret) == SECURE_FILE_MODE


def test_restrict_permissions_is_noop_for_missing_path(tmp_path):
    """文件不存在时不应抛异常（删除后调用等场景）。"""
    restrict_file(tmp_path / "does-not-exist.json")
    restrict_permissions(tmp_path / "does-not-exist.json", SECURE_FILE_MODE)


# --------------------------------------------------------- 端到端：接口写入


@posix_only
def test_login_state_endpoint_writes_0600(tmp_path, monkeypatch):
    """通过接口写入的 cookie 文件必须是 0600。"""
    import asyncio

    from src.api.routes import login_state

    monkeypatch.chdir(tmp_path)
    asyncio.run(
        login_state.update_login_state(
            login_state.LoginStateUpdate(content='{"cookies": [{"name": "unb"}]}')
        )
    )
    assert mode_of(tmp_path / "xianyu_state.json") == SECURE_FILE_MODE


@posix_only
def test_accounts_endpoint_writes_0600_and_dir_0700(tmp_path, monkeypatch):
    import asyncio

    from src.api.routes import accounts

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(accounts.env_manager, "get_value", lambda key, default=None: "state")

    asyncio.run(
        accounts.create_account(accounts.AccountCreate(name="acc_1", content='{"cookies": []}'))
    )
    assert mode_of(tmp_path / "state") == SECURE_DIR_MODE
    assert mode_of(tmp_path / "state" / "acc_1.json") == SECURE_FILE_MODE


@posix_only
def test_env_manager_creates_env_with_0600(tmp_path):
    from src.infrastructure.config.env_manager import EnvManager

    manager = EnvManager(env_file=str(tmp_path / ".env"))
    manager.update_values({"OPENAI_API_KEY": "sk-secret"})
    assert mode_of(tmp_path / ".env") == SECURE_FILE_MODE
