from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from app.services.utils.callback_client import post_callback_payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

logger = logging.getLogger(__name__)


class LLMTaskService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_tasks (
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    result_payload TEXT,
                    callback_status TEXT NOT NULL DEFAULT 'pending',
                    callback_attempts INTEGER NOT NULL DEFAULT 0,
                    last_callback_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (business_type, business_key)
                )
                """
            )

    def _serialize(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    def _deserialize(self, value: Optional[str]) -> Any:
        if not value:
            return None
        return json.loads(value)

    def _row_to_task(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "request_payload": self._deserialize(row["request_payload"]),
            "status": row["status"],
            "progress": row["progress"],
            "message": row["message"],
            "result_payload": self._deserialize(row["result_payload"]),
            "callback_status": row["callback_status"],
            "callback_attempts": row["callback_attempts"],
            "last_callback_error": row["last_callback_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _upsert_task(self, business_type: str, business_key: str, request_payload: Dict[str, Any], status: str) -> Dict[str, Any]:
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_tasks (
                    business_type, business_key, request_payload, status, progress, message,
                    result_payload, callback_status, callback_attempts, last_callback_error,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_type, business_key) DO UPDATE SET
                    request_payload = excluded.request_payload,
                    status = excluded.status,
                    progress = excluded.progress,
                    message = excluded.message,
                    result_payload = excluded.result_payload,
                    callback_status = excluded.callback_status,
                    callback_attempts = excluded.callback_attempts,
                    last_callback_error = excluded.last_callback_error,
                    updated_at = excluded.updated_at
                """,
                (
                    business_type,
                    business_key,
                    self._serialize(request_payload),
                    status,
                    0.0,
                    "",
                    None,
                    "pending",
                    0,
                    "",
                    now,
                    now,
                ),
            )
        task = self.get_task(business_type, business_key)
        assert task is not None
        logger.info("创建/更新任务: type=%s, key=%s, status=%s", business_type, business_key, status)
        return task

    def create_file_task(self, file_name: str, request_payload: Dict[str, Any], status: str = "1") -> Dict[str, Any]:
        return self._upsert_task("file", file_name, request_payload, status=status)

    def create_report_task(self, report_id: int, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._upsert_task("report", str(report_id), request_payload, status="0")

    def create_weaponry_task(self, architecture_id: int, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._upsert_task("weaponry", str(architecture_id), request_payload, status="1")

    def get_task(self, business_type: str, business_key: str) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT business_type, business_key, request_payload, status, progress, message,
                       result_payload, callback_status, callback_attempts, last_callback_error,
                       created_at, updated_at
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (business_type, business_key),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def get_tasks(self, business_type: str, business_keys: list[str]) -> list[Dict[str, Any]]:
        tasks: list[Dict[str, Any]] = []
        for business_key in business_keys:
            task = self.get_task(business_type, business_key)
            if task is not None:
                tasks.append(task)
        return tasks

    def mark_business_completed(
        self,
        business_type: str,
        business_key: str,
        result_payload: Dict[str, Any],
        *,
        status: str,
    ) -> None:
        self.mark_business_result(
            business_type,
            business_key,
            result_payload=result_payload,
            status=status,
        )

    def mark_business_result(
        self,
        business_type: str,
        business_key: str,
        result_payload: Dict[str, Any],
        *,
        status: str,
        message: str = "",
    ) -> None:
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE llm_tasks
                SET status = ?, progress = ?, message = ?, result_payload = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                """,
                (status, 1.0, message, self._serialize(result_payload), now, business_type, business_key),
            )
        logger.info("任务结果已标记: type=%s, key=%s, status=%s", business_type, business_key, status)

    def update_task_progress(
        self,
        business_type: str,
        business_key: str,
        *,
        progress: float,
        message: str,
        status: Optional[str] = None,
    ) -> None:
        now = _utc_now_iso()
        status_sql = "status = ?, " if status is not None else ""
        params: list[Any] = []
        if status is not None:
            params.append(status)
        params.extend([progress, message, now, business_type, business_key])
        with self._connection() as conn:
            conn.execute(
                f"""
                UPDATE llm_tasks
                SET {status_sql}progress = ?, message = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                """,
                tuple(params),
            )

    def mark_callback_failed(self, business_type: str, business_key: str, error: str) -> None:
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE llm_tasks
                SET callback_status = ?, callback_attempts = callback_attempts + 1,
                    last_callback_error = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                """,
                ("failed", error, now, business_type, business_key),
            )
        logger.warning("回调失败: type=%s, key=%s, error=%s", business_type, business_key, error)

    def mark_callback_success(self, business_type: str, business_key: str) -> None:
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE llm_tasks
                SET callback_status = ?, callback_attempts = callback_attempts + 1,
                    last_callback_error = '', updated_at = ?
                WHERE business_type = ? AND business_key = ?
                """,
                ("success", now, business_type, business_key),
            )
        logger.info("回调成功: type=%s, key=%s", business_type, business_key)

    def should_replay_callback(self, business_type: str, business_key: str) -> bool:
        task = self.get_task(business_type, business_key)
        if not task:
            return False
        completed_statuses = {"file": {"2", "3"}, "report": {"1", "2"}, "weaponry": {"2", "3"}}
        return task["status"] in completed_statuses.get(business_type, set()) and task["callback_status"] != "success"

    def replay_callback_if_needed(self, business_type: str, business_key: str, *, callback_url: str, timeout: float) -> bool:
        if not callback_url or not self.should_replay_callback(business_type, business_key):
            return False

        task = self.get_task(business_type, business_key)
        if not task:
            return False

        payload = task["result_payload"] or {}
        callback_ok = post_callback_payload(callback_url, payload, timeout=timeout)
        if callback_ok:
            self.mark_callback_success(business_type, business_key)
            return True

        self.mark_callback_failed(business_type, business_key, "callback replay failed")
        return False
