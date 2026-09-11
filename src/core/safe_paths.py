"""任务名与文件路径安全工具。

安全背景（对应安全审计报告中的 C1）：
任务名会被直接拼进文件系统路径，例如 ``images/task_images_<task_name>``
（见 ``src/ai_handler.py`` 的 ``download_all_images`` / ``cleanup_task_images``）。
``TaskCreate.task_name`` 历史上没有任何校验，因此一个形如 ``x/../../somewhere``
的任务名会让爬虫子进程在结束时对 ``images/task_images_x/../../somewhere``
执行 ``shutil.rmtree``，实现任意目录递归删除；同一个拼接还用于 ``os.makedirs``，
即任意目录写入。

本模块是任务名校验与"限定在目录内拼接路径"的唯一入口：
domain 模型的输入校验、services 与爬虫的路径构造都应从这里取，避免各处各写一份。
本模块只依赖标准库，可以被任何层安全导入（包括 domain）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

#: 任务名允许的字符。``\w`` 在 Python 3 默认按 Unicode 匹配，因此中文任务名照常可用；
#: 该集合刻意排除了 ``/``、``\``、``:`` 等路径分隔与控制字符。
TASK_NAME_PATTERN = re.compile(r"^[\w\-. ]+$")

#: 任务名长度上限。任务名会出现在日志文件名、图片目录名里，过长会顶到文件系统限制。
MAX_TASK_NAME_LENGTH = 64


class UnsafePathError(ValueError):
    """任务名非法，或路径拼接试图越出允许的目录。"""


def validate_task_name(value: object) -> str:
    """校验并规范化任务名；非法时抛出 :class:`UnsafePathError`。

    该校验用于**输入边界**（创建/更新任务），目的是让恶意任务名根本进不了数据库。
    运行时的路径构造仍需 :func:`resolve_within` 兜底，以防历史脏数据。
    """
    if value is None:
        raise UnsafePathError("任务名不能为空")

    name = str(value).strip()
    if not name:
        raise UnsafePathError("任务名不能为空")
    if len(name) > MAX_TASK_NAME_LENGTH:
        raise UnsafePathError(f"任务名长度不能超过 {MAX_TASK_NAME_LENGTH} 个字符")
    # "." 与 ".." 完全由合法字符组成，必须单独拒绝
    if name in {".", ".."}:
        raise UnsafePathError("任务名不能是 '.' 或 '..'")
    if not TASK_NAME_PATTERN.fullmatch(name):
        raise UnsafePathError(
            "任务名只能包含中文、字母、数字、下划线、短横线、点和空格，且不能包含路径分隔符"
        )
    return name


def is_safe_task_name(value: object) -> bool:
    """``validate_task_name`` 的非抛异常版本。"""
    try:
        validate_task_name(value)
    except UnsafePathError:
        return False
    return True


def resolve_within(base_dir: os.PathLike | str, *parts: str) -> Path:
    """在 ``base_dir`` 内拼接路径并做 containment 校验。

    返回解析后的绝对路径；若结果落在 ``base_dir`` 之外则抛出 :class:`UnsafePathError`。
    与 ``src/api/routes/prompts.py`` 中已有的写法保持一致（``resolve()`` + ``relative_to()``）。
    """
    base = Path(base_dir).resolve()
    candidate = base.joinpath(*parts).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise UnsafePathError(f"路径越界: {candidate} 不在 {base} 之内") from exc
    return candidate


def resolve_task_image_dir(
    task_name: object,
    *,
    base_dir: os.PathLike | str = "images",
    prefix: str = "task_images_",
) -> Path:
    """返回任务图片目录的绝对路径，并保证它落在 ``base_dir`` 之内。

    任务名先经 :func:`validate_task_name` 校验；即便历史数据里存在恶意名字，
    这里的 containment 校验也会拦住，使越界路径无法进入 ``makedirs``/``rmtree``。
    """
    safe_name = validate_task_name(task_name)
    return resolve_within(base_dir, f"{prefix}{safe_name}")
