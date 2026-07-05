from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence
from uuid import uuid4

from app.ports.rag import RagExecutionTrace, RagLifecycleEvent, RagSource
from app.services.llm_service.interaction_audit_service import (
    AUDIT_SCHEMA_VERSION,
    AUDIT_STATUS_SUCCEEDED,
    InteractionAuditError,
    InteractionAuditResult,
    SQLiteAuditExecutor,
)
from app.services.utils.callback_client import post_callback_payload


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

logger = logging.getLogger(__name__)


_COMPLETED_TASK_STATUSES = {
    "file": frozenset({"2", "3"}),
    "report": frozenset({"1", "2"}),
    "weaponry": frozenset({"2", "3"}),
}
"""允许进入回调终态的现有业务完成状态。"""

class LLMTaskService:
    """持久化异步 LLM 任务、交互审计和回调状态。

    任务状态与交互审计共享一个 SQLite 文件，但具有不同一致性要求：普通进度更新使用短
    事务；阶段 7 新增的审计入口必须在一个显式写事务内提交主记录和全部明细，且只对
    SQLite 短暂锁冲突执行有硬上限的重试。
    """

    def __init__(self, db_path: str):
        """初始化数据库路径并以向前兼容方式创建所需表和索引。"""
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._audit_executor = SQLiteAuditExecutor(
            lambda timeout: self._connect(timeout_seconds=timeout)
        )
        self._init_db()

    def _connect(self, *, timeout_seconds: float = 5.0) -> sqlite3.Connection:
        """创建启用外键约束的独立 SQLite 连接。

        每次操作使用独立连接，避免后台任务线程共享 ``sqlite3.Connection``。审计写事务
        会传入零等待超时并自行执行有限退避；普通任务查询与更新保留 SQLite 默认级别的
        短暂等待，降低无意义的瞬时失败。
        """
        conn = sqlite3.connect(self.db_path, timeout=max(0.0, timeout_seconds))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """托管普通短事务，异常时显式回滚并始终关闭连接。"""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        *,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """以只增不删方式补充 SQLite 列，兼容已有任务数据库。

        表名、列名和定义全部由本模块内常量调用点提供，不接收外部输入，因此可以安全用于
        SQLite 不支持参数化的 ``ALTER TABLE`` 标识符位置。
        """
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_tasks (
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
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
                    execution_id TEXT NOT NULL DEFAULT '',
                    audit_schema_version INTEGER NOT NULL DEFAULT 1,
                    audit_idempotency_key TEXT,
                    trace_digest TEXT NOT NULL DEFAULT '',
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
            self._ensure_column(
                conn,
                table="llm_tasks",
                column="execution_id",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """
                UPDATE llm_tasks
                SET execution_id = 'legacy-task:' || lower(hex(randomblob(16)))
                WHERE execution_id IS NULL OR execution_id = ''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_tasks_execution_id
                ON llm_tasks (execution_id)
                """
            )
            self._ensure_column(
                conn,
                table="llm_interactions",
                column="execution_id",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn,
                table="llm_interactions",
                column="audit_schema_version",
                definition="INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn,
                table="llm_interactions",
                column="audit_idempotency_key",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="llm_interactions",
                column="trace_digest",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """
                UPDATE llm_interactions
                SET execution_id = 'legacy-interaction:' || id
                WHERE execution_id IS NULL OR execution_id = ''
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_interactions_audit_key
                ON llm_interactions (audit_idempotency_key)
                WHERE audit_idempotency_key IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_interaction_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interaction_id INTEGER NOT NULL,
                    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
                    operation TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
                    prompt_kind TEXT NOT NULL,
                    prompt_digest TEXT NOT NULL DEFAULT '',
                    query_mode TEXT NOT NULL DEFAULT 'query',
                    raw_response TEXT,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    source_count INTEGER NOT NULL DEFAULT 0,
                    verified_source_count INTEGER NOT NULL DEFAULT 0,
                    missing_marker_count INTEGER NOT NULL DEFAULT 0,
                    mismatched_marker_count INTEGER NOT NULL DEFAULT 0,
                    source_marker_status TEXT NOT NULL DEFAULT 'not_returned',
                    failure_stage TEXT,
                    error_message TEXT,
                    FOREIGN KEY (interaction_id)
                        REFERENCES llm_interactions(id) ON DELETE CASCADE,
                    UNIQUE (interaction_id, sequence_no)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_llm_interaction_attempts_interaction
                ON llm_interaction_attempts (interaction_id, sequence_no)
                """
            )
            for column, definition in (
                ("prompt_digest", "TEXT NOT NULL DEFAULT ''"),
                ("query_mode", "TEXT NOT NULL DEFAULT 'query'"),
                ("source_count", "INTEGER NOT NULL DEFAULT 0"),
                ("verified_source_count", "INTEGER NOT NULL DEFAULT 0"),
                ("missing_marker_count", "INTEGER NOT NULL DEFAULT 0"),
                ("mismatched_marker_count", "INTEGER NOT NULL DEFAULT 0"),
                ("source_marker_status", "TEXT NOT NULL DEFAULT 'not_returned'"),
            ):
                self._ensure_column(
                    conn,
                    table="llm_interaction_attempts",
                    column=column,
                    definition=definition,
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_interaction_lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interaction_id INTEGER NOT NULL,
                    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
                    operation TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
                    success INTEGER NOT NULL CHECK (success IN (0, 1)),
                    external_ref TEXT,
                    failure_stage TEXT,
                    error_message TEXT,
                    FOREIGN KEY (interaction_id)
                        REFERENCES llm_interactions(id) ON DELETE CASCADE,
                    UNIQUE (interaction_id, sequence_no)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_llm_lifecycle_events_interaction
                ON llm_interaction_lifecycle_events (interaction_id, sequence_no)
                """
            )

    def _serialize(self, value: Any) -> str:
        """生成严格 JSON，拒绝 SQLite 之外无法可靠交换的 NaN/Infinity。"""
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    def _deserialize(self, value: Optional[str]) -> Any:
        if not value:
            return None
        return json.loads(value)

    def _row_to_task(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "execution_id": row["execution_id"],
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

    def _upsert_task(
        self,
        business_type: str,
        business_key: str,
        request_payload: Dict[str, Any],
        status: str,
    ) -> Dict[str, Any]:
        """创建一次新执行，并在同一事务内返回本次写入的任务快照。

        即使业务键已存在，主动提交仍代表一次新执行，因此必须更新 ``execution_id`` 并
        重置结果和回调状态。读取必须发生在写事务提交前；若提交后重新查询，并发重跑可能
        已经覆盖同一业务键，调用方会错误拿到另一执行的身份。
        """
        now = _utc_now_iso()
        execution_id = uuid4().hex
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO llm_tasks (
                    business_type, business_key, execution_id, request_payload,
                    status, progress, message,
                    result_payload, callback_status, callback_attempts, last_callback_error,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(business_type, business_key) DO UPDATE SET
                    request_payload = excluded.request_payload,
                    execution_id = excluded.execution_id,
                    status = excluded.status,
                    progress = excluded.progress,
                    message = excluded.message,
                    result_payload = excluded.result_payload,
                    callback_status = excluded.callback_status,
                    callback_attempts = excluded.callback_attempts,
                    last_callback_error = excluded.last_callback_error,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    business_type,
                    business_key,
                    execution_id,
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
            row = conn.execute(
                """
                SELECT business_type, business_key, execution_id, request_payload,
                       status, progress, message, result_payload, callback_status,
                       callback_attempts, last_callback_error, created_at, updated_at
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (business_type, business_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("任务写入完成后未能读取事务内快照")
        task = self._row_to_task(row)
        logger.info(
            "创建/更新任务: type=%s key=%s execution_id=%s status=%s",
            business_type,
            business_key,
            execution_id,
            status,
        )
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
                SELECT business_type, business_key, execution_id, request_payload,
                       status, progress, message,
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

    @staticmethod
    def _rag_source_payload(source: RagSource) -> Dict[str, Any]:
        """把供应商无关来源 DTO 转换为可稳定序列化的审计结构。

        这里显式选择字段而不是直接序列化对象内部字典，避免未来 DTO 增加内部字段后未经
        审查进入审计库。空的可选字段仍被保留，便于区分“上游没有返回”和“导出工具遗漏”。
        """
        if not isinstance(source, RagSource):
            raise TypeError("交互审计 sources 只能包含 RagSource")
        return {
            "document_ref": source.document_ref,
            "text": source.text,
            "id": source.id,
            "title": source.title,
            "url": source.url,
            "score": source.score,
        }

    def create_llm_interaction_with_trace(
        self,
        *,
        business_type: str,
        business_key: str,
        execution_id: str,
        prompt: str,
        trace: RagExecutionTrace,
        status: str,
        error_message: str = "",
        audit_idempotency_key: str | None = None,
    ) -> InteractionAuditResult:
        """原子持久化主交互、全部模型调用和初始资源生命周期事件。

        主表的响应与来源取自最后一次模型调用，用于兼容现有审计查询；完整调用序列则全部
        写入 ``llm_interaction_attempts``。准备阶段可能尚未发生模型调用，此时主表响应为空，
        但生命周期事件仍会完整保存。任何主表或明细写入失败都会回滚整个事务。

        返回:
            仅在事务提交成功后构造的 ``InteractionAuditResult``。调用方必须以其中的
            ``audit_status`` 作为后续永久入库、翻译和成功回调的硬门禁。
        """
        if not isinstance(trace, RagExecutionTrace):
            raise TypeError("trace 必须是 RagExecutionTrace")

        normalized_business_type = str(business_type or "").strip()
        normalized_business_key = str(business_key or "").strip()
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_business_type:
            raise ValueError("business_type 不能为空")
        if not normalized_business_key:
            raise ValueError("business_key 不能为空")
        if not normalized_execution_id:
            raise ValueError("execution_id 不能为空")
        normalized_audit_key = str(
            audit_idempotency_key or f"audit:{normalized_execution_id}"
        ).strip()
        if not normalized_audit_key:
            raise ValueError("audit_idempotency_key 不能为空")
        if status not in {"succeeded", "failed"}:
            raise ValueError("交互审计 status 只能是 succeeded 或 failed")

        normalized_error = str(error_message or trace.error_message or "").strip()
        if status == "succeeded" and (trace.failure_stage or normalized_error):
            raise ValueError("成功审计不得携带失败阶段或错误信息")
        if status == "failed" and not normalized_error:
            raise ValueError("失败审计必须包含 error_message")

        final_attempt = trace.attempts[-1] if trace.attempts else None
        normalized_prompt = str(prompt or "")
        if final_attempt and not final_attempt.prompt_digest:
            raise ValueError("新审计中的 RagAttempt 必须包含 prompt_digest")
        if final_attempt:
            supplied_prompt_digest = hashlib.sha256(
                normalized_prompt.encode("utf-8")
            ).hexdigest()
            if supplied_prompt_digest != final_attempt.prompt_digest:
                raise ValueError("主审计 prompt 必须与最后一次 RagAttempt 对应")
        main_response = final_attempt.raw_response if final_attempt else None
        main_sources = (
            [self._rag_source_payload(source) for source in final_attempt.sources]
            if final_attempt
            else []
        )
        serialized_main_sources = self._serialize(main_sources)
        attempt_rows: list[tuple[Any, ...]] = []
        attempt_digest_payload: list[Dict[str, Any]] = []
        for sequence_no, model_attempt in enumerate(trace.attempts, start=1):
            attempt_sources = [
                self._rag_source_payload(source)
                for source in model_attempt.sources
            ]
            serialized_attempt_sources = self._serialize(attempt_sources)
            attempt_row = (
                sequence_no,
                model_attempt.operation,
                model_attempt.attempt,
                model_attempt.prompt_kind,
                model_attempt.prompt_digest,
                model_attempt.query_mode,
                model_attempt.raw_response,
                serialized_attempt_sources,
                model_attempt.source_count,
                model_attempt.verified_source_count,
                model_attempt.missing_marker_count,
                model_attempt.mismatched_marker_count,
                model_attempt.source_marker_status,
                model_attempt.failure_stage,
                model_attempt.error_message,
            )
            attempt_rows.append(attempt_row)
            attempt_digest_payload.append(
                {
                    "sequence_no": sequence_no,
                    "operation": model_attempt.operation,
                    "attempt_no": model_attempt.attempt,
                    "prompt_kind": model_attempt.prompt_kind,
                    "prompt_digest": model_attempt.prompt_digest,
                    "query_mode": model_attempt.query_mode,
                    "raw_response": model_attempt.raw_response,
                    "sources": attempt_sources,
                    "source_count": model_attempt.source_count,
                    "verified_source_count": model_attempt.verified_source_count,
                    "missing_marker_count": model_attempt.missing_marker_count,
                    "mismatched_marker_count": model_attempt.mismatched_marker_count,
                    "source_marker_status": model_attempt.source_marker_status,
                    "failure_stage": model_attempt.failure_stage,
                    "error_message": model_attempt.error_message,
                }
            )
        lifecycle_rows = tuple(
            (
                event.sequence_no,
                event.operation,
                event.attempt,
                1 if event.success else 0,
                event.external_ref,
                event.failure_stage,
                event.error_message,
            )
            for event in trace.lifecycle_events
        )
        trace_digest_payload = {
            "business_type": normalized_business_type,
            "business_key": normalized_business_key,
            "execution_id": normalized_execution_id,
            "context_name": trace.context_name,
            "context_ref": trace.context_ref,
            "conversation_ref": trace.conversation_ref,
            "prompt": normalized_prompt,
            "status": status,
            "error_message": normalized_error if status == "failed" else "",
            "attempts": attempt_digest_payload,
            "lifecycle_events": lifecycle_rows,
        }
        serialized_trace = json.dumps(
            trace_digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        trace_digest = hashlib.sha256(serialized_trace.encode("utf-8")).hexdigest()
        now = _utc_now_iso()

        def _write(conn: sqlite3.Connection) -> tuple[int, bool]:
            task = conn.execute(
                """
                SELECT execution_id
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if task is None:
                raise InteractionAuditError("交互审计失败：对应任务不存在")
            if task["execution_id"] != normalized_execution_id:
                raise InteractionAuditError("交互审计失败：任务执行身份已发生变化")

            existing = conn.execute(
                """
                SELECT id, business_type, business_key, execution_id,
                       audit_schema_version, trace_digest
                FROM llm_interactions
                WHERE audit_idempotency_key = ?
                """,
                (normalized_audit_key,),
            ).fetchone()
            if existing is not None:
                identity_matches = (
                    existing["business_type"] == normalized_business_type
                    and existing["business_key"] == normalized_business_key
                    and existing["execution_id"] == normalized_execution_id
                    and existing["audit_schema_version"] == AUDIT_SCHEMA_VERSION
                    and existing["trace_digest"] == trace_digest
                )
                if not identity_matches:
                    raise InteractionAuditError(
                        "交互审计失败：幂等键对应的已提交内容发生冲突"
                    )
                return int(existing["id"]), False

            cursor = conn.execute(
                """
                INSERT INTO llm_interactions (
                    business_type, business_key, execution_id,
                    audit_schema_version, audit_idempotency_key, trace_digest,
                    workspace_name, workspace_slug, thread_slug,
                    prompt, response, sources_json, status,
                    error_message, workspace_cleanup_status,
                    workspace_cleanup_error, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', '', ?, ?)
                """,
                (
                    normalized_business_type,
                    normalized_business_key,
                    normalized_execution_id,
                    AUDIT_SCHEMA_VERSION,
                    normalized_audit_key,
                    trace_digest,
                    trace.context_name,
                    trace.context_ref or "",
                    trace.conversation_ref or "",
                    normalized_prompt,
                    main_response,
                    serialized_main_sources,
                    status,
                    normalized_error if status == "failed" else "",
                    now,
                    now,
                ),
            )
            interaction_id = int(cursor.lastrowid)

            for attempt_row in attempt_rows:
                conn.execute(
                    """
                    INSERT INTO llm_interaction_attempts (
                        interaction_id, sequence_no, operation, attempt_no,
                        prompt_kind, prompt_digest, query_mode,
                        raw_response, sources_json, source_count,
                        verified_source_count, missing_marker_count,
                        mismatched_marker_count, source_marker_status,
                        failure_stage, error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (interaction_id, *attempt_row),
                )

            for lifecycle_row in lifecycle_rows:
                conn.execute(
                    """
                    INSERT INTO llm_interaction_lifecycle_events (
                        interaction_id, sequence_no, operation, attempt_no,
                        success, external_ref, failure_stage, error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (interaction_id, *lifecycle_row),
                )
            return interaction_id, True

        interaction_id, created = self._audit_executor.run(
            operation="create_interaction_with_trace",
            writer=_write,
        )
        result = InteractionAuditResult(
            interaction_id=interaction_id,
            created=created,
            reused=not created,
        )
        logger.info(
            "LLM交互原子审计已提交: interaction_id=%s business_type=%s "
            "business_key=%s status=%s attempts_count=%s lifecycle_count=%s "
            "audit_status=%s created=%s reused=%s execution_id=%s",
            interaction_id,
            normalized_business_type,
            normalized_business_key,
            status,
            len(trace.attempts),
            len(trace.lifecycle_events),
            result.audit_status,
            result.created,
            result.reused,
            normalized_execution_id,
        )
        return result

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
            task = conn.execute(
                """
                SELECT execution_id FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (business_type, business_key),
            ).fetchone()
            legacy_execution_id = (
                task["execution_id"]
                if task is not None
                else f"legacy-interaction:{uuid4().hex}"
            )
            cursor = conn.execute(
                """
                INSERT INTO llm_interactions (
                    business_type, business_key, execution_id,
                    audit_schema_version, workspace_name, workspace_slug,
                    thread_slug, prompt, response, sources_json, status,
                    error_message, workspace_cleanup_status,
                    workspace_cleanup_error, created_at, completed_at
                )
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)
                """,
                (
                    business_type,
                    business_key,
                    legacy_execution_id,
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

    @staticmethod
    def _lifecycle_event_matches_row(
        event: RagLifecycleEvent,
        row: sqlite3.Row,
    ) -> bool:
        """判断待追加事件是否与数据库中的同序号事件完全一致。

        同序号、同内容表示调用方正在重放一次已经提交的追加操作，应按幂等成功处理；同
        序号但任一字段不同表示审计历史发生冲突，必须拒绝覆盖，不能使用
        ``INSERT OR REPLACE`` 篡改已提交证据。
        """
        return (
            row["operation"] == event.operation
            and row["attempt_no"] == event.attempt
            and bool(row["success"]) == bool(event.success)
            and row["external_ref"] == event.external_ref
            and row["failure_stage"] == event.failure_stage
            and row["error_message"] == event.error_message
        )

    def append_llm_interaction_lifecycle_events(
        self,
        interaction_id: int,
        events: Sequence[RagLifecycleEvent],
        *,
        cleanup_status: str,
        cleanup_error: str = "",
    ) -> int:
        """幂等追加关闭阶段事件，并在同一事务更新清理状态。

        新事件必须从数据库当前最大 ``sequence_no`` 的下一位连续开始。已经存在且内容完全
        一致的事件可以安全重放；序号缺口、同序号内容冲突或已经确定的清理结果被改写都会
        立即失败。返回值是本次真正新增的事件数量，重复调用通常返回零。
        """
        if interaction_id < 1:
            raise ValueError("interaction_id 必须是正整数")
        normalized_cleanup_status = str(cleanup_status or "").strip()
        if normalized_cleanup_status not in {"deleted", "failed"}:
            raise ValueError("cleanup_status 只能是 deleted 或 failed")
        normalized_cleanup_error = str(cleanup_error or "").strip()
        if normalized_cleanup_status == "failed" and not normalized_cleanup_error:
            raise ValueError("清理失败必须包含 cleanup_error")
        if normalized_cleanup_status != "failed" and normalized_cleanup_error:
            raise ValueError("清理成功状态不得包含 cleanup_error")

        normalized_events = tuple(events)
        if not normalized_events:
            raise ValueError("关闭阶段必须至少提交一条生命周期事件")
        if any(not isinstance(event, RagLifecycleEvent) for event in normalized_events):
            raise TypeError("events 只能包含 RagLifecycleEvent")
        incoming_sequences = tuple(event.sequence_no for event in normalized_events)
        if incoming_sequences != tuple(sorted(set(incoming_sequences))):
            raise ValueError("待追加生命周期事件必须按 sequence_no 严格递增且不重复")
        cleanup_events = tuple(
            event
            for event in normalized_events
            if event.operation in {"global_document_delete", "context_delete"}
        )
        if not cleanup_events:
            raise ValueError("关闭阶段追加必须包含文档或上下文删除事件")
        cleanup_has_failure = any(not event.success for event in cleanup_events)
        if cleanup_has_failure != (normalized_cleanup_status == "failed"):
            raise ValueError("cleanup_status 必须与删除事件的成功状态一致")

        def _write(conn: sqlite3.Connection) -> int:
            interaction = conn.execute(
                """
                SELECT workspace_cleanup_status, workspace_cleanup_error
                FROM llm_interactions
                WHERE id = ?
                """,
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise ValueError(f"LLM交互记录不存在: interaction_id={interaction_id}")

            existing_rows = conn.execute(
                """
                SELECT sequence_no, operation, attempt_no, success, external_ref,
                       failure_stage, error_message
                FROM llm_interaction_lifecycle_events
                WHERE interaction_id = ?
                ORDER BY sequence_no ASC
                """,
                (interaction_id,),
            ).fetchall()
            existing_by_sequence = {row["sequence_no"]: row for row in existing_rows}
            current_max_sequence = existing_rows[-1]["sequence_no"] if existing_rows else 0
            inserted_count = 0

            for event in normalized_events:
                existing = existing_by_sequence.get(event.sequence_no)
                if existing is not None:
                    if not self._lifecycle_event_matches_row(event, existing):
                        raise ValueError(
                            "生命周期事件序号冲突: "
                            f"interaction_id={interaction_id}, sequence_no={event.sequence_no}"
                        )
                    continue

                expected_sequence = current_max_sequence + 1
                if event.sequence_no != expected_sequence:
                    raise ValueError(
                        "生命周期事件存在序号缺口: "
                        f"expected={expected_sequence}, actual={event.sequence_no}"
                    )
                conn.execute(
                    """
                    INSERT INTO llm_interaction_lifecycle_events (
                        interaction_id, sequence_no, operation, attempt_no,
                        success, external_ref, failure_stage, error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        interaction_id,
                        event.sequence_no,
                        event.operation,
                        event.attempt,
                        1 if event.success else 0,
                        event.external_ref,
                        event.failure_stage,
                        event.error_message,
                    ),
                )
                current_max_sequence = event.sequence_no
                existing_by_sequence[event.sequence_no] = {
                    "sequence_no": event.sequence_no,
                    "operation": event.operation,
                    "attempt_no": event.attempt,
                    "success": 1 if event.success else 0,
                    "external_ref": event.external_ref,
                    "failure_stage": event.failure_stage,
                    "error_message": event.error_message,
                }
                inserted_count += 1

            current_cleanup_status = interaction["workspace_cleanup_status"]
            current_cleanup_error = interaction["workspace_cleanup_error"]
            if current_cleanup_status == "pending":
                if inserted_count == 0:
                    raise ValueError(
                        "首次提交清理结果必须同时新增关闭阶段生命周期事件"
                    )
                conn.execute(
                    """
                    UPDATE llm_interactions
                    SET workspace_cleanup_status = ?, workspace_cleanup_error = ?
                    WHERE id = ?
                    """,
                    (
                        normalized_cleanup_status,
                        normalized_cleanup_error,
                        interaction_id,
                    ),
                )
            elif (
                current_cleanup_status != normalized_cleanup_status
                or current_cleanup_error != normalized_cleanup_error
            ):
                raise ValueError(
                    "已提交的清理结果不得被覆盖: "
                    f"interaction_id={interaction_id}, current={current_cleanup_status}, "
                    f"requested={normalized_cleanup_status}"
                )
            return inserted_count

        inserted_count = self._audit_executor.run(
            operation="append_lifecycle_events",
            writer=_write,
        )
        logger.info(
            "LLM交互生命周期事件已幂等追加: interaction_id=%s submitted_count=%s "
            "inserted_count=%s cleanup_status=%s audit_status=%s",
            interaction_id,
            len(normalized_events),
            inserted_count,
            normalized_cleanup_status,
            AUDIT_STATUS_SUCCEEDED,
        )
        return inserted_count

    def get_llm_interaction_attempts(self, interaction_id: int) -> list[Dict[str, Any]]:
        """按审计顺序返回一次交互的全部模型调用明细。"""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT sequence_no, operation, attempt_no, prompt_kind,
                       prompt_digest, query_mode, raw_response, sources_json,
                       source_count, verified_source_count,
                       missing_marker_count, mismatched_marker_count,
                       source_marker_status, failure_stage, error_message
                FROM llm_interaction_attempts
                WHERE interaction_id = ?
                ORDER BY sequence_no ASC
                """,
                (interaction_id,),
            ).fetchall()
        result: list[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["sources"] = self._deserialize(item.pop("sources_json")) or []
            result.append(item)
        return result

    def get_llm_interaction_lifecycle_events(
        self,
        interaction_id: int,
    ) -> list[Dict[str, Any]]:
        """按全局发生顺序返回一次交互的全部资源生命周期事件。"""
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT sequence_no, operation, attempt_no, success, external_ref,
                       failure_stage, error_message
                FROM llm_interaction_lifecycle_events
                WHERE interaction_id = ?
                ORDER BY sequence_no ASC
                """,
                (interaction_id,),
            ).fetchall()
        events: list[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["success"] = bool(item["success"])
            events.append(item)
        return events

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
                       execution_id, audit_schema_version,
                       audit_idempotency_key, trace_digest,
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

    def _mark_callback_result(
        self,
        business_type: str,
        business_key: str,
        *,
        callback_status: str,
        error: str,
    ) -> None:
        """以比较并交换方式提交一次真实回调结果。

        只有 ``pending`` 或 ``failed`` 可以进入新的真实结果；``success`` 与 ``skipped``
        都是不可覆盖的终态。每次允许的转换都代表一次已经发生的外部调用，因此尝试次数
        精确增加一。
        """
        if callback_status not in {"success", "failed"}:
            raise ValueError("callback_status 只能是 success 或 failed")
        normalized_error = str(error or "").strip()
        if callback_status == "failed" and not normalized_error:
            raise ValueError("回调失败必须包含 error")
        if callback_status == "success" and normalized_error:
            raise ValueError("回调成功不得包含 error")
        completed_statuses = _COMPLETED_TASK_STATUSES.get(business_type)
        if not completed_statuses:
            raise ValueError(f"未知 business_type: {business_type}")
        status_placeholders = ", ".join("?" for _ in completed_statuses)
        now = _utc_now_iso()
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_status = ?, callback_attempts = callback_attempts + 1,
                    last_callback_error = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND callback_status IN ('pending', 'failed')
                  AND status IN ({status_placeholders})
                """,
                (
                    callback_status,
                    normalized_error,
                    now,
                    business_type,
                    business_key,
                    *sorted(completed_statuses),
                ),
            )
            if cursor.rowcount != 1:
                task = conn.execute(
                    """
                    SELECT status, callback_status FROM llm_tasks
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (business_type, business_key),
                ).fetchone()
                if task is None:
                    raise ValueError("待更新回调结果的任务不存在")
                if task["status"] not in completed_statuses:
                    raise ValueError("任务尚未完成，不能提交回调结果")
                raise ValueError(
                    "非法回调状态转换: "
                    f"{task['callback_status']} -> {callback_status}"
                )

    def mark_callback_failed(self, business_type: str, business_key: str, error: str) -> None:
        """记录一次实际失败的回调，禁止覆盖成功或无需回调终态。"""
        self._mark_callback_result(
            business_type,
            business_key,
            callback_status="failed",
            error=error,
        )
        logger.warning("回调失败: type=%s, key=%s, error=%s", business_type, business_key, error)

    def mark_callback_success(self, business_type: str, business_key: str) -> None:
        """记录一次实际成功的回调，成功后状态不可再次改写。"""
        self._mark_callback_result(
            business_type,
            business_key,
            callback_status="success",
            error="",
        )
        logger.info("回调成功: type=%s, key=%s", business_type, business_key)

    def mark_callback_skipped(self, business_type: str, business_key: str) -> bool:
        """把未配置回调的任务幂等标记为 ``skipped``。

        只有仍处于 ``pending`` 或已经是 ``skipped`` 的任务允许执行该转换。实际回调已经
        成功或失败时，本方法返回 ``False`` 并保留原状态，防止“未配置回调”的后置判断
        覆盖真实外部交互结果。跳过不是一次回调尝试，因此不会增加 ``callback_attempts``。
        """
        now = _utc_now_iso()
        completed_statuses = _COMPLETED_TASK_STATUSES.get(business_type)
        if not completed_statuses:
            raise ValueError(f"未知 business_type: {business_type}")
        status_placeholders = ", ".join("?" for _ in completed_statuses)
        transition_succeeded = False
        current_status = ""
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_status = 'skipped', last_callback_error = '', updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND callback_status IN ('pending', 'skipped')
                  AND status IN ({status_placeholders})
                """,
                (
                    now,
                    business_type,
                    business_key,
                    *sorted(completed_statuses),
                ),
            )
            transition_succeeded = cursor.rowcount == 1
            if not transition_succeeded:
                task = conn.execute(
                    """
                    SELECT status, callback_status
                    FROM llm_tasks
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (business_type, business_key),
                ).fetchone()
                if task is None:
                    raise ValueError(
                        "待跳过回调的任务不存在: "
                        f"business_type={business_type}, business_key={business_key}"
                    )
                if task["status"] not in completed_statuses:
                    raise ValueError("任务尚未完成，不能标记 callback_status=skipped")
                current_status = task["callback_status"]
                if current_status not in {"success", "failed"}:
                    raise ValueError(f"未知 callback_status: {current_status}")

        if not transition_succeeded:
            logger.warning(
                "忽略回调跳过标记，保留已发生的回调结果: type=%s key=%s "
                "callback_status=%s",
                business_type,
                business_key,
                current_status,
            )
            return False
        logger.info(
            "任务无需回调，状态已幂等标记为 skipped: type=%s key=%s",
            business_type,
            business_key,
        )
        return True

    def should_replay_callback(self, business_type: str, business_key: str) -> bool:
        task = self.get_task(business_type, business_key)
        if not task:
            return False
        return (
            task["status"] in _COMPLETED_TASK_STATUSES.get(business_type, frozenset())
            and task["callback_status"] in {"pending", "failed"}
        )

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

    def replay_callback_if_needed(
        self,
        business_type: str,
        business_key: str,
        *,
        callback_url: str,
        timeout: float,
    ) -> bool:
        """按当前回调配置补偿一次终态任务，并维护精确的回调状态。

        空回调地址表示当前部署没有外部接收方。对于已经完成且仍处于 ``pending`` 的历史
        任务，此时应幂等迁移到 ``skipped``，而不是悄悄返回并让任务永久表现为等待回调。
        """
        normalized_callback_url = str(callback_url or "").strip()
        if not normalized_callback_url:
            task = self.get_task(business_type, business_key)
            if (
                task
                and task["status"]
                in _COMPLETED_TASK_STATUSES.get(business_type, frozenset())
                and task["callback_status"] == "pending"
            ):
                self.mark_callback_skipped(business_type, business_key)
            return False
        if not self.should_replay_callback(business_type, business_key):
            return False

        task = self.get_task(business_type, business_key)
        if not task:
            return False

        payload = task["result_payload"] or {}
        callback_ok = post_callback_payload(
            normalized_callback_url,
            payload,
            timeout=timeout,
            callback_context=self._callback_context_for_task(task),
        )
        if callback_ok:
            self.mark_callback_success(business_type, business_key)
            return True

        self.mark_callback_failed(business_type, business_key, "callback replay failed")
        return False
