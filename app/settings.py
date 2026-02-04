from __future__ import annotations

import os
from pathlib import Path


# 分类后文件存放目录（恢复为 uploads）
UPLOAD_DIR = Path(os.getenv("DOCSENSE_FILE_STORE_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 上传暂存目录（请求处理期间使用）
TEMP_UPLOAD_DIR = Path(os.getenv("DOCSENSE_TEMP_UPLOAD_DIR", ".runtime/inbox"))
TEMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Web UI 限制：单次请求最大 500MB
MAX_CONTENT_LENGTH = 500 * 1024 * 1024
