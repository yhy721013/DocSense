from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.core.settings import RUNTIME_DIR


CALLBACK_PREVIEW_PATH = RUNTIME_DIR / "call_back.json"


def load_callback_preview(path: Path | None = None) -> dict[str, Any]:
    target = path or CALLBACK_PREVIEW_PATH
    if not target.exists():
        return {
            "ok": False,
            "message": "当前还没有回调结果文件",
            "payload": None,
        }

    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {
            "ok": False,
            "message": "回调文件读取失败",
            "payload": None,
        }

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "message": "回调文件不是合法 JSON",
            "payload": None,
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "message": "回调文件根节点必须为对象",
            "payload": None,
        }

    return {
        "ok": True,
        "message": "读取成功",
        "payload": payload,
    }
