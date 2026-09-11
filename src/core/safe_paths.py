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


#: prompt 文件所在目录（与 src/api/routes/prompts.py 的 _PROMPTS_DIR 保持一致）。
PROMPTS_DIR = "prompts"

#: 账号登录态目录的默认值（可被 ACCOUNT_STATE_DIR 覆盖）。
DEFAULT_ACCOUNT_STATE_DIR = "state"

#: 根目录下的单账号登录态文件。
ROOT_STATE_FILE = "xianyu_state.json"


def _normalize_relative_reference(value: object, *, field_label: str) -> str:
    """把用户填写的相对路径规范化：统一分隔符、去掉前导 ``./``。"""
    raw = str(value or "").strip()
    if not raw:
        raise UnsafePathError(f"{field_label}不能为空")
    if os.path.isabs(raw) or Path(raw).is_absolute():
        raise UnsafePathError(f"{field_label}必须使用相对路径，不能是绝对路径: {raw}")
    if raw.startswith("\\\\") or (len(raw) > 1 and raw[1] == ":"):
        raise UnsafePathError(f"{field_label}必须是相对路径: {raw}")

    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _resolve_under_base(
    normalized: str,
    base_dir: os.PathLike | str,
    *,
    field_label: str,
    allow_bare_name: bool = False,
) -> Path:
    """把已规范化的相对路径解析到 ``base_dir`` 之下。

    比单纯的 containment 检查更严一档，目的是让契约一眼可审：
    - 任何 ``..`` 组件直接拒绝（不依赖 ``resolve()`` 的归一化语义）；
    - 允许省略 ``base_dir`` 前缀（``foo.txt`` 与 ``prompts/foo.txt`` 等价）；
    - 解析结果必须指向**具体文件**，不能退化回目录本身。
    """
    base = Path(base_dir)
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise UnsafePathError(f"{field_label}不能包含 '..'：{normalized}")
    if parts and parts[0] == base.name:
        parts = parts[1:]
    if not parts:
        raise UnsafePathError(f"{field_label}必须指向具体文件，而不是目录：{normalized}")

    resolved = resolve_within(base, *parts)
    if resolved == base.resolve():
        raise UnsafePathError(f"{field_label}必须指向具体文件：{normalized}")
    return resolved


def safe_prompt_path(
    value: object,
    *,
    base_dir: os.PathLike | str = PROMPTS_DIR,
) -> Path:
    """把任务里的 prompt 文件引用解析为 ``prompts/`` 内的绝对路径。

    ``ai_prompt_base_file`` / ``ai_prompt_criteria_file`` / ``ai_prompt_file`` 都是
    用户可控字符串，而爬虫子进程会用 ``open()`` 直接读它们，再原样塞进发往
    ``OPENAI_BASE_URL`` 的请求里。因此这里强制：只接受相对路径、不得含 ``..``，
    且解析后必须落在 ``prompts/`` 之内——``/etc/passwd``、``../../.env`` 一律拒绝。

    兼容两种写法：``base_prompt.txt`` 与 ``prompts/base_prompt.txt``。
    """
    normalized = _normalize_relative_reference(value, field_label="prompt 文件路径")
    return _resolve_under_base(
        normalized, base_dir, field_label="prompt 文件路径"
    )


def safe_account_state_path(
    value: object,
    *,
    state_dir: os.PathLike | str = DEFAULT_ACCOUNT_STATE_DIR,
    root_state_file: os.PathLike | str = ROOT_STATE_FILE,
) -> Path:
    """把 ``account_state_file`` 解析为受控路径。

    允许两类取值：
    1. 根目录的单账号文件 ``xianyu_state.json``；
    2. ``ACCOUNT_STATE_DIR`` 内的文件（``acc_1.json`` 或 ``state/acc_1.json``）。

    其余一律拒绝：这个值会被当作 Playwright 的 ``storage_state`` 读取，
    任意路径等于"任意可读 JSON 都能当 cookie 用"。
    """
    normalized = _normalize_relative_reference(value, field_label="账号登录态文件")

    root = Path(root_state_file)
    if normalized in {root.name, root.as_posix()}:
        return Path(root).resolve()

    return _resolve_under_base(
        normalized,
        state_dir,
        field_label="账号登录态文件",
    )


def validate_account_state_dir(value: object) -> str:
    """校验 ``ACCOUNT_STATE_DIR`` 设置项。

    该值可通过设置接口改写，若放任绝对路径就等于"把账号文件写到任意目录/从任意目录读"，
    因此只接受项目内的相对目录。
    """
    normalized = _normalize_relative_reference(value, field_label="账号登录态目录")
    normalized = normalized.rstrip("/")
    if not normalized:
        raise UnsafePathError("账号登录态目录不能为空")
    if ".." in normalized.split("/"):
        raise UnsafePathError("账号登录态目录不能包含 '..'")
    return normalized


def validate_prompt_reference(value: object) -> str:
    """校验任务里的 prompt 文件引用，并返回**保持原有写法**的字符串。

    只做校验、不改写存储格式：``prompts/foo.txt`` 与 ``foo.txt`` 都保留原样，
    因为数据库、前端表单与 ``spider_v2`` 都依赖这个既有形态。
    """
    safe_prompt_path(value)
    return str(value).strip()


def validate_account_state_reference(value: object) -> str:
    """校验任务的 ``account_state_file``，并返回保持原有写法的字符串。"""
    safe_account_state_path(value)
    return str(value).strip()
