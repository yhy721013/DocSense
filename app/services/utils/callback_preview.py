from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

from app.services.core.settings import RUNTIME_DIR


CALLBACK_HISTORY_DIR = RUNTIME_DIR / "callback"
CALLBACK_RECORD_LIMIT = 50


def _record_metadata(path: Path, stat_result: Any | None = None) -> dict[str, Any]:
    current_stat = stat_result or path.stat()
    return {
        "id": path.name,
        "fileName": path.name,
        "modifiedAt": datetime.fromtimestamp(current_stat.st_mtime).isoformat(timespec="seconds"),
        "sizeBytes": current_stat.st_size,
    }


def list_callback_records(
    *,
    history_dir: Path | None = None,
    limit: int = CALLBACK_RECORD_LIMIT,
) -> list[dict[str, Any]]:
    target_dir = history_dir or CALLBACK_HISTORY_DIR
    if not target_dir.exists() or not target_dir.is_dir():
        return []

    records = []
    for path in target_dir.glob("*.json"):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        records.append((stat_result.st_mtime_ns, path.name, path, stat_result))

    records.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [_record_metadata(path, stat_result) for _, _, path, stat_result in records[:limit]]


def _response(
    *,
    ok: bool,
    message: str,
    payload: dict[str, Any] | None,
    records: list[dict[str, Any]],
    selected_record: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "message": message,
        "payload": payload,
        "records": records,
        "selectedRecord": selected_record,
    }


def _resolve_record_path(record: str | None, history_dir: Path, records: list[dict[str, Any]]) -> tuple[Path | None, dict[str, Any] | None]:
    if not record:
        if not records:
            return None, None
        selected_path = history_dir / records[0]["id"]
        return selected_path, records[0]

    record_path = Path(record)
    if record_path.name != record or record_path.suffix.lower() != ".json":
        return None, None

    selected_path = history_dir / record
    if not selected_path.is_file():
        return None, None

    try:
        selected_record = _record_metadata(selected_path)
    except OSError:
        return None, None
    return selected_path, selected_record


def load_callback_preview(
    *,
    record: str | None = None,
    history_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir = history_dir or CALLBACK_HISTORY_DIR
    records = list_callback_records(history_dir=target_dir)
    target, selected_record = _resolve_record_path(record, target_dir, records)

    if target is None:
        message = "当前还没有新版回调历史文件" if not records and not record else "指定的回调历史记录不存在"
        return {
            "ok": False,
            "message": message,
            "payload": None,
            "records": records,
            "selectedRecord": None,
        }

    try:
        raw_text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _response(
            ok=False,
            message="回调文件读取失败",
            payload=None,
            records=records,
            selected_record=selected_record,
        )

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return _response(
            ok=False,
            message="回调文件不是合法 JSON",
            payload=None,
            records=records,
            selected_record=selected_record,
        )

    if not isinstance(payload, dict):
        return _response(
            ok=False,
            message="回调文件根节点必须为对象",
            payload=None,
            records=records,
            selected_record=selected_record,
        )

    return _response(
        ok=True,
        message="读取成功",
        payload=payload,
        records=records,
        selected_record=selected_record,
    )
