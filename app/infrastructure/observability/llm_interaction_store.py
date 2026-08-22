"""Report/Analysis 共享的 LLM 交互审计唯一物理 Writer。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import logging
import sqlite3
from typing import Any

from app.ports.rag import (
    RagExecutionTrace,
    RagLifecycleEvent,
    RagSource,
    normalize_rag_prompt,
)
from app.services.llm_service.interaction_audit_service import (
    AUDIT_SCHEMA_VERSION,
    AUDIT_STATUS_SUCCEEDED,
    InteractionAuditError,
    InteractionAuditResult,
    MAX_AUDIT_PROMPT_CHARS,
    MAX_AUDIT_RESPONSE_CHARS,
    MAX_AUDIT_SOURCES_JSON_CHARS,
    MAX_AUDIT_TRACE_JSON_CHARS,
    SQLiteAuditExecutor,
)


logger = logging.getLogger(__name__)
TaskIdentityValidator = Callable[[str, str, str], bool]


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _source_payload(source: RagSource) -> dict[str, Any]:
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


def _initial_cleanup_state(trace: RagExecutionTrace) -> tuple[str, str]:
    unknown = tuple(
        event
        for event in trace.lifecycle_events
        if (event.failure_stage or "").endswith("_outcome_unknown")
    )
    if unknown:
        latest = unknown[-1]
        return "failed", latest.error_message or "外部写操作结果未知"
    rollbacks = tuple(
        event for event in trace.lifecycle_events if event.operation == "context_rollback"
    )
    if rollbacks:
        latest = rollbacks[-1]
        return ("deleted", "") if latest.success else (
            "failed",
            latest.error_message or "隔离上下文回滚失败",
        )
    creates = tuple(
        event for event in trace.lifecycle_events if event.operation == "context_create"
    )
    if creates and not any(event.success for event in creates):
        return "deleted", ""
    return "pending", ""


class LLMInteractionStore:
    """保存共享模型交互证据，不拥有 Task、Callback 或业务资源状态机。

    ``task_identity_validator`` 用于 v2 Task Control：校验精确 execution 的不可变业务身份，
    但不把跨 SQLite 检查伪装成原子事务。真正的执行权由 v2 Step Intent/Checkpoint 前后
    条件写保证。未传入时继续在旧审计库内核对 ``llm_tasks``，服务 Analysis 迁移前兼容链。
    """

    def __init__(
        self,
        connection_factory: Callable[[float], sqlite3.Connection],
        *,
        task_identity_validator: TaskIdentityValidator | None = None,
    ) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory 必须可调用")
        if task_identity_validator is not None and not callable(task_identity_validator):
            raise TypeError("task_identity_validator 必须可调用或为 None")
        self._connection_factory = connection_factory
        self._task_identity_validator = task_identity_validator
        self._executor = SQLiteAuditExecutor(connection_factory)

    def _validate_task_identity(
        self,
        connection: sqlite3.Connection,
        business_type: str,
        business_key: str,
        execution_id: str,
    ) -> None:
        if self._task_identity_validator is not None:
            if not self._task_identity_validator(business_type, business_key, execution_id):
                raise InteractionAuditError("交互审计失败：任务执行身份不匹配")
            return
        task = connection.execute(
            """
            SELECT execution_id FROM llm_tasks
            WHERE business_type = ? AND business_key = ?
            """,
            (business_type, business_key),
        ).fetchone()
        if task is None:
            raise InteractionAuditError("交互审计失败：对应任务不存在")
        if task["execution_id"] != execution_id:
            raise InteractionAuditError("交互审计失败：任务执行身份已发生变化")

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
        document_upload_facts: Mapping[str, Any] | None = None,
    ) -> InteractionAuditResult:
        """在一个有限重试的 ``BEGIN IMMEDIATE`` 中提交主记录和全部明细。"""

        if not isinstance(trace, RagExecutionTrace):
            raise TypeError("trace 必须是 RagExecutionTrace")
        normalized_type = _required_text(business_type, name="business_type")
        normalized_key = _required_text(business_key, name="business_key")
        normalized_execution = _required_text(execution_id, name="execution_id")
        audit_key = _required_text(
            audit_idempotency_key or f"audit:{normalized_execution}",
            name="audit_idempotency_key",
        )
        if status not in {"succeeded", "failed"}:
            raise ValueError("交互审计 status 只能是 succeeded 或 failed")
        normalized_error = str(error_message or trace.error_message or "").strip()
        if status == "succeeded" and (trace.failure_stage or normalized_error):
            raise ValueError("成功审计不得携带失败阶段或错误信息")
        if status == "failed" and not normalized_error:
            raise ValueError("失败审计必须包含 error_message")
        if document_upload_facts is not None and not isinstance(document_upload_facts, Mapping):
            raise TypeError("document_upload_facts 必须是 Mapping 或 None")
        upload_json = _serialize(dict(document_upload_facts)) if document_upload_facts is not None else "{}"

        final_attempt = trace.attempts[-1] if trace.attempts else None
        normalized_prompt = normalize_rag_prompt(prompt)
        if len(normalized_prompt) > MAX_AUDIT_PROMPT_CHARS:
            raise InteractionAuditError("交互审计失败：Prompt 超出持久化安全上限")
        if final_attempt and not final_attempt.prompt_digest:
            raise ValueError("新审计中的 RagAttempt 必须包含 prompt_digest")
        if final_attempt:
            digest = hashlib.sha256(normalized_prompt.encode("utf-8")).hexdigest()
            if digest != final_attempt.prompt_digest:
                raise ValueError("主审计 prompt 必须与最后一次 RagAttempt 对应")
        main_response = final_attempt.raw_response if final_attempt else None
        main_sources = [_source_payload(item) for item in final_attempt.sources] if final_attempt else []
        main_sources_json = _serialize(main_sources)
        attempt_rows: list[tuple[Any, ...]] = []
        attempt_digest: list[dict[str, Any]] = []
        for sequence_no, attempt in enumerate(trace.attempts, start=1):
            sources = [_source_payload(item) for item in attempt.sources]
            sources_json = _serialize(sources)
            if len(str(attempt.raw_response or "")) > MAX_AUDIT_RESPONSE_CHARS:
                raise InteractionAuditError("交互审计失败：模型原始响应超出安全上限")
            if len(sources_json) > MAX_AUDIT_SOURCES_JSON_CHARS:
                raise InteractionAuditError("交互审计失败：来源证据超出安全上限")
            attempt_rows.append(
                (
                    sequence_no,
                    attempt.operation,
                    attempt.attempt,
                    attempt.prompt_kind,
                    attempt.prompt_digest,
                    attempt.query_mode,
                    attempt.raw_response,
                    sources_json,
                    attempt.source_count,
                    attempt.verified_source_count,
                    attempt.missing_marker_count,
                    attempt.mismatched_marker_count,
                    attempt.source_marker_status,
                    attempt.call_id,
                    attempt.failure_stage,
                    attempt.error_message,
                )
            )
            attempt_digest.append(
                {
                    "sequence_no": sequence_no,
                    "operation": attempt.operation,
                    "attempt_no": attempt.attempt,
                    "prompt_kind": attempt.prompt_kind,
                    "prompt_digest": attempt.prompt_digest,
                    "query_mode": attempt.query_mode,
                    "raw_response": attempt.raw_response,
                    "sources": sources,
                    "source_count": attempt.source_count,
                    "verified_source_count": attempt.verified_source_count,
                    "missing_marker_count": attempt.missing_marker_count,
                    "mismatched_marker_count": attempt.mismatched_marker_count,
                    "source_marker_status": attempt.source_marker_status,
                    "call_id": attempt.call_id,
                    "failure_stage": attempt.failure_stage,
                    "error_message": attempt.error_message,
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
        trace_payload: dict[str, Any] = {
            "business_type": normalized_type,
            "business_key": normalized_key,
            "execution_id": normalized_execution,
            "trace_id": trace.trace_id,
            "context_name": trace.context_name,
            "context_ref": trace.context_ref,
            "conversation_ref": trace.conversation_ref,
            "prompt": normalized_prompt,
            "status": status,
            "error_message": normalized_error if status == "failed" else "",
            "attempts": attempt_digest,
            "lifecycle_events": lifecycle_rows,
        }
        if document_upload_facts is not None:
            trace_payload["document_upload"] = json.loads(upload_json)
        trace_json = json.dumps(
            trace_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(trace_json) > MAX_AUDIT_TRACE_JSON_CHARS:
            raise InteractionAuditError("交互审计失败：完整执行轨迹超出安全上限")
        trace_digest = hashlib.sha256(trace_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        cleanup_status, cleanup_error = _initial_cleanup_state(trace)

        def _write(connection: sqlite3.Connection) -> tuple[int, bool]:
            self._validate_task_identity(
                connection, normalized_type, normalized_key, normalized_execution
            )
            existing = connection.execute(
                """
                SELECT id, business_type, business_key, execution_id,
                       audit_schema_version, trace_digest
                FROM llm_interactions WHERE audit_idempotency_key = ?
                """,
                (audit_key,),
            ).fetchone()
            if existing is not None:
                matches = (
                    existing["business_type"] == normalized_type
                    and existing["business_key"] == normalized_key
                    and existing["execution_id"] == normalized_execution
                    and existing["audit_schema_version"] == AUDIT_SCHEMA_VERSION
                    and existing["trace_digest"] == trace_digest
                )
                if not matches:
                    raise InteractionAuditError("交互审计失败：幂等键对应的已提交内容发生冲突")
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO llm_interactions (
                    business_type, business_key, execution_id,
                    audit_schema_version, audit_idempotency_key, trace_digest,
                    trace_id, document_upload_json,
                    workspace_name, workspace_slug, thread_slug,
                    prompt, response, sources_json, status, error_message,
                    workspace_cleanup_status, workspace_cleanup_error,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_type,
                    normalized_key,
                    normalized_execution,
                    AUDIT_SCHEMA_VERSION,
                    audit_key,
                    trace_digest,
                    trace.trace_id,
                    upload_json,
                    trace.context_name,
                    trace.context_ref or "",
                    trace.conversation_ref or "",
                    normalized_prompt,
                    main_response,
                    main_sources_json,
                    status,
                    normalized_error if status == "failed" else "",
                    cleanup_status,
                    cleanup_error,
                    now,
                    now,
                ),
            )
            interaction_id = int(cursor.lastrowid)
            for row in attempt_rows:
                connection.execute(
                    """
                    INSERT INTO llm_interaction_attempts (
                        interaction_id, sequence_no, operation, attempt_no,
                        prompt_kind, prompt_digest, query_mode, raw_response,
                        sources_json, source_count, verified_source_count,
                        missing_marker_count, mismatched_marker_count,
                        source_marker_status, call_id, failure_stage, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (interaction_id, *row),
                )
            for row in lifecycle_rows:
                connection.execute(
                    """
                    INSERT INTO llm_interaction_lifecycle_events (
                        interaction_id, sequence_no, operation, attempt_no,
                        success, external_ref, failure_stage, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (interaction_id, *row),
                )
            return interaction_id, True

        interaction_id, created = self._executor.run(
            operation="create_interaction_with_trace",
            writer=_write,
        )
        result = InteractionAuditResult(
            interaction_id=interaction_id,
            created=created,
            reused=not created,
        )
        logger.info(
            "LLM 交互审计已提交: interaction_id=%s business_type=%s task_id=%s "
            "attempts=%d lifecycle_events=%d created=%s reused=%s",
            interaction_id,
            normalized_type,
            normalized_execution,
            len(trace.attempts),
            len(trace.lifecycle_events),
            result.created,
            result.reused,
        )
        return result

    @staticmethod
    def _event_matches(event: RagLifecycleEvent, row: sqlite3.Row) -> bool:
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
        expected_execution_id: str | None = None,
        expected_audit_idempotency_key: str | None = None,
    ) -> int:
        """幂等追加连续关闭事件，并在同一事务收敛 cleanup 状态。"""

        if isinstance(interaction_id, bool) or not isinstance(interaction_id, int) or interaction_id < 1:
            raise ValueError("interaction_id 必须是正整数")
        normalized_status = str(cleanup_status or "").strip()
        if normalized_status not in {"deleted", "failed"}:
            raise ValueError("cleanup_status 只能是 deleted 或 failed")
        normalized_error = str(cleanup_error or "").strip()
        expected_execution = (
            _required_text(expected_execution_id, name="expected_execution_id")
            if expected_execution_id is not None
            else None
        )
        expected_key = (
            _required_text(expected_audit_idempotency_key, name="expected_audit_idempotency_key")
            if expected_audit_idempotency_key is not None
            else None
        )
        if normalized_status == "failed" and not normalized_error:
            raise ValueError("清理失败必须包含 cleanup_error")
        if normalized_status != "failed" and normalized_error:
            raise ValueError("清理成功状态不得包含 cleanup_error")
        normalized_events = tuple(events)
        if not normalized_events or any(not isinstance(item, RagLifecycleEvent) for item in normalized_events):
            raise ValueError("关闭阶段必须提交 RagLifecycleEvent")
        sequences = tuple(item.sequence_no for item in normalized_events)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("待追加生命周期事件必须按 sequence_no 严格递增且不重复")
        cleanup_events = tuple(
            item
            for item in normalized_events
            if item.operation in {"conversation_delete", "context_delete", "global_document_delete"}
        )
        if not cleanup_events:
            raise ValueError("关闭阶段追加必须包含文档或上下文删除事件")
        if any(not item.success for item in cleanup_events) != (normalized_status == "failed"):
            raise ValueError("cleanup_status 必须与删除事件的成功状态一致")

        def _write(connection: sqlite3.Connection) -> int:
            interaction = connection.execute(
                """
                SELECT execution_id, audit_idempotency_key,
                       workspace_cleanup_status, workspace_cleanup_error
                FROM llm_interactions WHERE id = ?
                """,
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise ValueError(f"LLM交互记录不存在: interaction_id={interaction_id}")
            if expected_execution is not None and interaction["execution_id"] != expected_execution:
                raise InteractionAuditError("交互审计失败：清理凭据 execution_id 不匹配")
            if expected_key is not None and interaction["audit_idempotency_key"] != expected_key:
                raise InteractionAuditError("交互审计失败：清理凭据幂等键不匹配")
            rows = connection.execute(
                """
                SELECT sequence_no, operation, attempt_no, success, external_ref,
                       failure_stage, error_message
                FROM llm_interaction_lifecycle_events
                WHERE interaction_id = ? ORDER BY sequence_no ASC
                """,
                (interaction_id,),
            ).fetchall()
            by_sequence = {row["sequence_no"]: row for row in rows}
            current_max = rows[-1]["sequence_no"] if rows else 0
            inserted = 0
            for event in normalized_events:
                existing = by_sequence.get(event.sequence_no)
                if existing is not None:
                    if not self._event_matches(event, existing):
                        raise ValueError(
                            f"生命周期事件序号冲突: interaction_id={interaction_id}, sequence_no={event.sequence_no}"
                        )
                    continue
                if event.sequence_no != current_max + 1:
                    raise ValueError(
                        f"生命周期事件存在序号缺口: expected={current_max + 1}, actual={event.sequence_no}"
                    )
                connection.execute(
                    """
                    INSERT INTO llm_interaction_lifecycle_events (
                        interaction_id, sequence_no, operation, attempt_no,
                        success, external_ref, failure_stage, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                current_max = event.sequence_no
                inserted += 1
            current_status = interaction["workspace_cleanup_status"]
            current_error = interaction["workspace_cleanup_error"]
            if current_status == "pending":
                if inserted == 0:
                    raise ValueError("首次提交清理结果必须同时新增关闭阶段生命周期事件")
                connection.execute(
                    """
                    UPDATE llm_interactions
                    SET workspace_cleanup_status = ?, workspace_cleanup_error = ?
                    WHERE id = ?
                    """,
                    (normalized_status, normalized_error, interaction_id),
                )
            elif current_status == "failed":
                if inserted:
                    connection.execute(
                        """
                        UPDATE llm_interactions
                        SET workspace_cleanup_status = ?, workspace_cleanup_error = ?
                        WHERE id = ? AND workspace_cleanup_status = 'failed'
                        """,
                        (normalized_status, normalized_error, interaction_id),
                    )
                elif current_status != normalized_status or current_error != normalized_error:
                    raise ValueError("重复清理审计与已提交结果不一致")
            elif current_status == "deleted":
                if inserted or normalized_status != "deleted" or current_error != normalized_error:
                    raise ValueError("deleted 清理终态不得被追加或改写")
            else:
                raise RuntimeError(f"LLM交互存在未知清理状态: {current_status}")
            return inserted

        inserted = self._executor.run(operation="append_lifecycle_events", writer=_write)
        logger.info(
            "LLM 交互生命周期事件已追加: interaction_id=%s submitted=%d inserted=%d status=%s",
            interaction_id,
            len(normalized_events),
            inserted,
            normalized_status,
        )
        return inserted

    def get_llm_interaction_by_execution(
        self,
        business_type: str,
        business_key: str,
        execution_id: str,
        audit_idempotency_key: str,
    ) -> dict[str, Any] | None:
        """按完整业务与 execution 身份查回交互，不允许仅凭全局 ID 串读。"""

        normalized_type = _required_text(business_type, name="business_type")
        normalized_key = _required_text(business_key, name="business_key")
        normalized_execution = _required_text(execution_id, name="execution_id")
        normalized_audit_key = _required_text(
            audit_idempotency_key,
            name="audit_idempotency_key",
        )
        connection = self._connection_factory(5.0)
        try:
            row = connection.execute(
                """
                SELECT id, execution_id, audit_idempotency_key
                FROM llm_interactions
                WHERE business_type = ? AND business_key = ?
                  AND execution_id = ? AND audit_idempotency_key = ?
                """,
                (
                    normalized_type,
                    normalized_key,
                    normalized_execution,
                    normalized_audit_key,
                ),
            ).fetchone()
        finally:
            connection.close()
        return dict(row) if row is not None else None


__all__ = [
    "AUDIT_STATUS_SUCCEEDED",
    "InteractionAuditError",
    "InteractionAuditResult",
    "LLMInteractionStore",
    "TaskIdentityValidator",
]
