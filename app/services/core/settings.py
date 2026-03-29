from __future__ import annotations

import os
from pathlib import Path


# 统一运行时目录
RUNTIME_DIR = Path(".runtime")
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# 正式 llm 任务持久化
LLM_TASK_DB_PATH = Path(os.getenv("DOCSENSE_LLM_TASK_DB", str(RUNTIME_DIR / "llm_tasks.sqlite3")))
LLM_TASK_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 知识库管理持久化
KNOWLEDGE_BASE_DB_PATH = Path(os.getenv("DOCSENSE_KNOWLEDGE_BASE_DB", str(RUNTIME_DIR / "knowledge_base.sqlite3")))
KNOWLEDGE_BASE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Web UI 限制：单次请求最大 500MB
MAX_CONTENT_LENGTH = int(os.getenv("DOCSENSE_MAX_CONTENT_LENGTH", str(500 * 1024 * 1024)))
