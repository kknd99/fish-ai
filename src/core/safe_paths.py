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


def _normalize_reference(value: object, *, field_label: str) -> str:
    """规范化用户填写的路径：统一分隔符、去掉前导 ``./``。

    刻意**不在这里**拒绝绝对路径：Docker 部署下 ``ACCOUNT_STATE_DIR=/app/state``、
    ``ai_prompt_base_file=/app/prompts/base_prompt.txt`` 都是合法的绝对写法。
    真正的安全性质是"必须落在允许的根目录内"，由 :func:`_resolve_reference` 负责。
    """
    raw = str(value or "").strip()
    if not raw:
        raise UnsafePathError(f"{field_label}不能为空")

    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_windows_drive_reference(raw: str) -> bool:
    """Windows 盘符写法（``C:\\path``）——在任意平台上都按绝对路径对待。"""
    return len(raw) > 1 and raw[1] == ":"


def _is_absolute_reference(raw: str) -> bool:
    """认绝对写法：POSIX 绝对路径、UNC（``\\\\server`` 或 ``//server``）、盘符。"""
    return (
        os.path.isabs(raw)
        or Path(raw).is_absolute()
        or raw.startswith("//")
        or raw.startswith("\\\\")
        or _is_windows_drive_reference(raw)
    )


def _resolve_reference(
    value: object,
    base_dir: os.PathLike | str,
    *,
    field_label: str,
) -> Path:
    """把引用解析到 ``base_dir`` 之内（相对与绝对写法都支持）。

    契约：
    - 相对写法不得含 ``..``，且允许省略 ``base_dir`` 前缀
      （``foo.txt`` 与 ``prompts/foo.txt`` 等价）；
    - 绝对写法允许，但解析结果必须落在 ``base_dir`` 之内；
    - 结果必须指向**具体文件**，不能退化回目录本身。
    """
    raw = str(value or "").strip()
    normalized = _normalize_reference(value, field_label=field_label)
    base = Path(base_dir).resolve()

    if _is_absolute_reference(raw):
        resolved = Path(raw).resolve()
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise UnsafePathError(
                f"{field_label}必须位于 {base} 之内，实际为 {resolved}"
            ) from exc
        if resolved == base:
            raise UnsafePathError(f"{field_label}必须指向具体文件：{raw}")
        return resolved

    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise UnsafePathError(f"{field_label}不能包含 '..'：{normalized}")
    if parts and parts[0] == Path(base_dir).name:
        parts = parts[1:]
    if not parts:
        raise UnsafePathError(f"{field_label}必须指向具体文件，而不是目录：{normalized}")

    resolved = resolve_within(base_dir, *parts)
    if resolved == base:
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
    ``OPENAI_BASE_URL`` 的请求里。因此这里强制：解析后必须落在 ``prompts/`` 之内
    ——``/etc/passwd``、``../../.env`` 一律拒绝。

    兼容三种写法：``base_prompt.txt``、``prompts/base_prompt.txt``，以及落在
    ``prompts/`` 之内的绝对路径（Docker 下 ``/app/prompts/base_prompt.txt`` 是常见配置）。
    """
    return _resolve_reference(value, base_dir, field_label="prompt 文件路径")


def safe_account_state_path(
    value: object,
    *,
    state_dir: os.PathLike | str = DEFAULT_ACCOUNT_STATE_DIR,
    root_state_file: os.PathLike | str = ROOT_STATE_FILE,
) -> Path:
    """把 ``account_state_file`` 解析为受控路径。

    允许三类取值：
    1. 根目录的单账号文件 ``xianyu_state.json``（含其绝对路径写法）；
    2. ``ACCOUNT_STATE_DIR`` 内的文件（``acc_1.json`` 或 ``state/acc_1.json``）；
    3. 上述文件的绝对路径写法——只要确实落在账号目录之内
       （Docker 下 ``ACCOUNT_STATE_DIR=/app/state`` 会让池子里全是绝对路径）。

    其余一律拒绝：这个值会被当作 Playwright 的 ``storage_state`` 读取，
    任意路径等于"任意可读 JSON 都能当 cookie 用"。
    """
    normalized = _normalize_reference(value, field_label="账号登录态文件")
    raw = str(value or "").strip()

    root = Path(root_state_file)
    if normalized in {root.name, root.as_posix()}:
        return Path(root).resolve()
    if _is_absolute_reference(raw) and Path(raw).resolve() == root.resolve():
        return root.resolve()

    return _resolve_reference(value, state_dir, field_label="账号登录态文件")


def validate_account_state_dir(value: object) -> str:
    """校验 ``ACCOUNT_STATE_DIR`` 设置项。

    该值可通过设置接口改写，因此必须限制在**项目目录之内**（相对或绝对写法都可以，
    以兼容 Docker 里的 ``/app/state``）。允许绝对路径不是为了放宽安全边界，而是因为
    容器部署下它本来就是绝对路径；真正的边界是"不许指向项目之外"。
    """
    raw = str(value or "").strip()
    normalized = _normalize_reference(value, field_label="账号登录态目录").rstrip("/")
    if not normalized:
        raise UnsafePathError("账号登录态目录不能为空")

    if _is_windows_drive_reference(raw):
        # 盘符写法在 POSIX 上会被当成普通文件名（项目里多出一个叫 "C:\\state" 的目录），
        # 语义混乱且无正当用途，直接拒绝。
        raise UnsafePathError(f"账号登录态目录不能使用盘符写法: {raw}")

    if _is_absolute_reference(raw):
        # 绝对路径写法允许，但必须落在项目目录（当前工作目录）之内：
        # Docker 下 WORKDIR=/app、ACCOUNT_STATE_DIR=/app/state 是正常配置；
        # /tmp、/home/app/.config 这类则被拒绝。
        resolved = Path(raw).resolve()
        root = Path(os.getcwd()).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise UnsafePathError(
                f"账号登录态目录必须位于项目目录 {root} 之内，实际为 {resolved}"
            ) from exc
        return normalized

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
