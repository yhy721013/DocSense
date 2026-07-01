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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    workspace_name TEXT NOT NULL DEFAULT '',
                    workspace_slug TEXT NOT NULL DEFAULT '',
                    thread_slug TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    response TEXT,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    workspace_cleanup_status TEXT NOT NULL DEFAULT 'pending',
                    workspace_cleanup_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_llm_interactions_business
                ON llm_interactions (business_type, business_key, created_at)
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

    def create_llm_interaction(
        self,
        *,
        business_type: str,
        business_key: str,
        workspace_name: str,
        workspace_slug: str,
        thread_slug: str,
        prompt: str,
        response: Optional[str],
        sources: list[Dict[str, Any]],
        status: str,
        error_message: str = "",
    ) -> int:
        """持久化一次模型交互，返回自增记录 ID。"""
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO llm_interactions (
                    business_type, business_key, workspace_name, workspace_slug,
                    thread_slug, prompt, response, sources_json, status,
                    error_message, workspace_cleanup_status,
                    workspace_cleanup_error, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)
                """,
                (
                    business_type,
                    business_key,
                    workspace_name,
                    workspace_slug,
                    thread_slug,
                    prompt,
                    response,
                    self._serialize(sources),
                    status,
                    error_message,
                    now,
                    now,
                ),
            )
            interaction_id = int(cursor.lastrowid)
        logger.info(
            "LLM交互已持久化: id=%s, type=%s, key=%s, status=%s",
            interaction_id,
            business_type,
            business_key,
            status,
        )
        return interaction_id

    def update_llm_interaction_cleanup(
        self,
        interaction_id: int,
        *,
        status: str,
        error_message: str = "",
    ) -> None:
        """记录临时 Workspace 的清理结果。"""
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE llm_interactions
                SET workspace_cleanup_status = ?, workspace_cleanup_error = ?
                WHERE id = ?
                """,
                (status, error_message, interaction_id),
            )

    def get_llm_interactions(
        self,
        business_type: str,
        business_key: str,
    ) -> list[Dict[str, Any]]:
        """按创建顺序返回指定业务任务的全部模型交互。"""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, business_type, business_key, workspace_name,
                       workspace_slug, thread_slug, prompt, response,
                       sources_json, status, error_message,
                       workspace_cleanup_status, workspace_cleanup_error,
                       created_at, completed_at
                FROM llm_interactions
                WHERE business_type = ? AND business_key = ?
                ORDER BY id ASC
                """,
                (business_type, business_key),
            ).fetchall()

        interactions: list[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["sources"] = self._deserialize(item.pop("sources_json")) or []
            interactions.append(item)
        return interactions

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

    def _callback_context_for_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        business_type = task["business_type"]
        business_key = task["business_key"]
        request_payload = task.get("request_payload") or {}
        params = request_payload.get("params")
        if isinstance(params, list) and params and isinstance(params[0], dict):
            first_param = params[0]
        elif isinstance(params, dict):
            first_param = params
        else:
            first_param = {}

        context: Dict[str, Any] = {
            "businessType": business_type,
            "businessKey": business_key,
        }
        if business_type == "file":
            context["fileName"] = first_param.get("fileName") or business_key
            context["originalFileName"] = (
                first_param.get("originalFileName")
                or first_param.get("originalName")
                or business_key
            )
        elif business_type == "report":
            context["reportId"] = first_param.get("reportId") or business_key
        elif business_type == "weaponry":
            context["architectureId"] = first_param.get("architectureId") or business_key
        return context

    def replay_callback_if_needed(self, business_type: str, business_key: str, *, callback_url: str, timeout: float) -> bool:
        if not callback_url or not self.should_replay_callback(business_type, business_key):
            return False

        task = self.get_task(business_type, business_key)
        if not task:
            return False

        payload = task["result_payload"] or {}
        callback_ok = post_callback_payload(
            callback_url,
            payload,
            timeout=timeout,
            callback_context=self._callback_context_for_task(task),
        )
        if callback_ok:
            self.mark_callback_success(business_type, business_key)
            return True

        self.mark_callback_failed(business_type, business_key, "callback replay failed")
        return False
