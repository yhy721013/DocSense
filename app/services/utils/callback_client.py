from __future__ import annotations

from datetime import datetime
import logging
import json
from pathlib import PurePosixPath
from pathlib import Path
import re
from typing import Any, Mapping

import requests

from app.services.core.settings import RUNTIME_DIR

logger = logging.getLogger(__name__)

CALLBACK_HISTORY_DIR = RUNTIME_DIR / "callback"
_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _strip_path_and_extension(value: Any) -> str:
    text = _as_text(value).replace("\\", "/")
    if not text:
        return ""
    return PurePosixPath(text).stem


def _safe_filename_component(value: Any, fallback: str = "callback") -> str:
    text = _strip_path_and_extension(value)
    text = _INVALID_FILENAME_CHARS.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip(" .-")
    return text or fallback


def _limit_utf8_bytes(value: str, max_bytes: int) -> str:
    text = value
    while len(text.encode("utf-8")) > max_bytes and text:
        text = text[:-1]
    return text.rstrip(" .-") or "callback"


def _payload_data(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else {}


def build_callback_history_stem(
    payload: Mapping[str, Any],
    callback_context: Mapping[str, Any] | None = None,
    *,
    timestamp: datetime | None = None,
) -> str:
    context = callback_context or {}
    data = _payload_data(payload)
    business_type = _as_text(context.get("businessType") or payload.get("businessType"))

    if business_type == "file":
        file_item = data.get("fileDataItem") if isinstance(data.get("fileDataItem"), Mapping) else {}
        original_name = (
            context.get("originalFileName")
            or context.get("originalName")
            or data.get("originalFileName")
            or data.get("originalName")
        )
        file_name = (
            context.get("fileName")
            or data.get("fileName")
            or file_item.get("fileName")
            or context.get("businessKey")
        )
        parts = []
        if original_name:
            parts.append(_safe_filename_component(original_name, fallback="file"))
        if file_name:
            parts.append(_safe_filename_component(file_name, fallback="file"))
        base = "-".join(parts) or "file"
    elif business_type == "report":
        report_id = context.get("reportId") or data.get("reportId") or context.get("businessKey")
        base = f"report-{_safe_filename_component(report_id, fallback='unknown')}"
    elif business_type == "weaponry":
        architecture_id = context.get("architectureId") or data.get("architectureId") or context.get("businessKey")
        base = f"weaponry-{_safe_filename_component(architecture_id, fallback='unknown')}"
    else:
        business_key = context.get("businessKey") or data.get("fileName") or data.get("reportId") or data.get("architectureId")
        base = _safe_filename_component(
            f"{business_type or 'callback'}-{business_key}" if business_key else business_type or "callback",
        )

    current = timestamp or datetime.now()
    suffix = current.strftime("%Y%m%dT%H%M%S%f")
    return f"{_limit_utf8_bytes(base, 180)}-{suffix}"


def _unique_json_path(history_dir: Path, stem: str) -> Path:
    candidate = history_dir / f"{stem}.json"
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = history_dir / f"{stem}-{index}.json"
        if not candidate.exists():
            return candidate
        index += 1


def save_callback_history_payload(
    payload: dict,
    *,
    callback_context: Mapping[str, Any] | None = None,
    history_dir: Path | None = None,
    timestamp: datetime | None = None,
) -> Path:
    target_dir = history_dir or CALLBACK_HISTORY_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = build_callback_history_stem(payload, callback_context, timestamp=timestamp)
    dump_path = _unique_json_path(target_dir, stem)
    dump_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dump_path


def post_callback_payload(
    callback_url: str,
    payload: dict,
    timeout: float,
    *,
    callback_context: Mapping[str, Any] | None = None,
) -> bool:
    try:
        dump_path = save_callback_history_payload(payload, callback_context=callback_context)
        logger.debug("回调历史数据已保存至 %s", dump_path)
    except Exception as e:
        logger.warning("保存回调历史数据失败: %s", e)

    try:
        response = requests.post(callback_url, json=payload, timeout=timeout)
        return bool(response.ok)
    except requests.exceptions.RequestException as exc:
        logger.warning("回调请求失败 url=%s: %s", callback_url, exc)
        return False
