from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")


def _resolve_runtime_dir() -> Path:
    raw_value = os.getenv("DOCSENSE_RUNTIME_DIR", "").strip()
    if not raw_value:
        return (PROJECT_ROOT / ".runtime").resolve()

    runtime_dir = Path(raw_value).expanduser()
    if not runtime_dir.is_absolute():
        raise RuntimeError("DOCSENSE_RUNTIME_DIR必须配置为绝对路径")
    return runtime_dir.resolve()


def _resolve_component_path(env_names: Iterable[str], relative_path: str) -> Path:
    """解析组件级覆盖路径；未覆盖时统一派生到 RUNTIME_DIR。"""
    for env_name in env_names:
        raw_value = os.getenv(env_name, "").strip()
        if not raw_value:
            continue
        path = Path(raw_value).expanduser()
        if not path.is_absolute():
            # 保留旧配置的兼容语义：组件级相对路径仍以项目根目录为基准。
            path = PROJECT_ROOT / path
        return path.resolve()
    return (RUNTIME_DIR / relative_path).resolve()


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name}必须是正整数") from exc
    if value < 1:
        raise RuntimeError(f"{name}必须是正整数")
    return value


# 统一运行时目录。显式配置 DOCSENSE_RUNTIME_DIR 时必须是绝对路径。
RUNTIME_DIR = _ensure_directory(_resolve_runtime_dir())

# 正式持久化数据库。组件级配置仅用于向后兼容，并拥有更高优先级。
LLM_TASK_DB_PATH = _ensure_parent(
    _resolve_component_path(("DOCSENSE_LLM_TASK_DB",), "llm_tasks.sqlite3")
)
KNOWLEDGE_BASE_DB_PATH = _ensure_parent(
    _resolve_component_path(
        ("DOCSENSE_KNOWLEDGE_BASE_DB", "KNOWLEDGE_BASE_DB_PATH"),
        "knowledge_base.sqlite3",
    )
)
CHAT_DB_PATH = _ensure_parent(
    _resolve_component_path(("DOCSENSE_CHAT_DB",), "chat_sessions.sqlite3")
)

# 当前仍由 Core Settings 明确拥有并在启动配置阶段创建的运行期目录。
SQLITE_EXPORT_DIR = _ensure_directory(RUNTIME_DIR / "sqlite")

# Web UI 限制：单次请求最大 500MB。
MAX_CONTENT_LENGTH = int(os.getenv("DOCSENSE_MAX_CONTENT_LENGTH", str(500 * 1024 * 1024)))

# SQLite 单实例文件对话的明确资源上限。它们是进程内保护措施，不宣称
# 具备跨实例配额语义；未来的网关/调度器可复用相同的应用层约束。
CHAT_MAX_FILES_PER_REQUEST = _positive_int_env("DOCSENSE_CHAT_MAX_FILES", 20)
CHAT_MAX_MESSAGE_CHARS = _positive_int_env("DOCSENSE_CHAT_MAX_MESSAGE_CHARS", 12_000)
CHAT_MAX_OUTPUT_CHARS = _positive_int_env("DOCSENSE_CHAT_MAX_OUTPUT_CHARS", 100_000)
CHAT_MAX_CONCURRENT_STREAMS = _positive_int_env(
    "DOCSENSE_CHAT_MAX_CONCURRENT_STREAMS",
    4,
)
