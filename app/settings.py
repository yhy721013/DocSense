from __future__ import annotations

from pathlib import Path


# 上传文件存放目录
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

# Web UI 限制：单次请求最大 500MB
MAX_CONTENT_LENGTH = 500 * 1024 * 1024
