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

# 运行期目录。
LLM_DOWNLOAD_DIR = _ensure_directory(
    _resolve_component_path(("FILE_DOWNLOAD_DIR",), "llm_downloads")
)
OCR_CACHE_DIR = _ensure_directory(
    _resolve_component_path(("DOCSENSE_OCR_CACHE_DIR",), "ocr_markdown")
)
MINERU_CACHE_DIR = _ensure_directory(
    _resolve_component_path(("DOCSENSE_MINERU_CACHE_DIR",), "mineru_markdown")
)
SQLITE_EXPORT_DIR = _ensure_directory(RUNTIME_DIR / "sqlite")

# Web UI 限制：单次请求最大 500MB。
MAX_CONTENT_LENGTH = int(os.getenv("DOCSENSE_MAX_CONTENT_LENGTH", str(500 * 1024 * 1024)))
