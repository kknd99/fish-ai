"""敏感文件的权限收口。

安全背景（安全审计 M3，已实测）：项目里没有任何 ``chmod``/``umask``，
于是应用自己创建的文件都是默认的 0644 —— 包括**完整的闲鱼会话 cookie**
（``xianyu_state.json``）、``state/*.json`` 账号文件、含各类 token 的 ``.env``，
以及会被无认证接口读走的任务日志。同一台机器上的其他用户、备份工具、
共享同一卷的容器都能直接读走这些凭据。

这里提供统一的落盘辅助函数：文件 0600、目录 0700。所有 ``chmod`` 都是
best-effort（Windows 上无意义，失败不抛异常），但**创建时就指定 mode** 才是
真正的第一道防线——先创建再 chmod 会有一个短暂的可读窗口。
"""
from __future__ import annotations

import os
import stat

#: 敏感文件权限：仅属主可读写。
SECURE_FILE_MODE = 0o600

#: 敏感目录权限：仅属主可进入。
SECURE_DIR_MODE = 0o700


def secure_makedirs(path: os.PathLike | str, *, exist_ok: bool = True) -> None:
    """以 0700 创建目录（含父目录）。

    注意：``os.makedirs(mode=...)`` 的 mode 只作用于**最后一级**，且受进程
    umask 削弱，因此创建后再显式 ``chmod`` 一次，确保结果确定。
    """
    os.makedirs(path, mode=SECURE_DIR_MODE, exist_ok=exist_ok)
    restrict_permissions(path, SECURE_DIR_MODE)


def restrict_permissions(path: os.PathLike | str, mode: int) -> None:
    """尽力把 ``path`` 的权限设为 ``mode``。

    契约很直接：**敏感文件一律 owner-only**（文件 0600、目录 0700），
    即便原文件是 0640 这种"稍宽"的权限也会被收紧——这些都是凭据文件，
    需要分组可读时应使用 ACL，而不是放宽本函数的语义。
    """
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return
    if current == mode:
        return

    try:
        os.chmod(path, mode)
    except OSError:
        # Windows 或只读文件系统：忽略
        pass


def restrict_file(path: os.PathLike | str) -> None:
    """把已存在的敏感文件收紧到 0600。"""
    restrict_permissions(path, SECURE_FILE_MODE)


def default_umask_is_permissive() -> bool:
    """当前进程 umask 是否会让新建文件默认可被他人读取（用于启动告警）。"""
    current = os.umask(0)
    os.umask(current)
    return bool(~current & 0o077)
