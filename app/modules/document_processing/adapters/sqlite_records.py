"""阶段 1 的文档处理 SQLite Processing Record Adapter。

本 Adapter 可与既有任务事实共用 ``llm_tasks.sqlite3``，但只拥有
``document_processing_*`` 表。它不导入 ``TaskService``，也不对遗留任务表建立外键。
每次调用创建独立连接，写入仅使用短 ``BEGIN IMMEDIATE`` 事务；事务内严禁文件、
subprocess、网络或模型调用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentRepresentation,
    LineageEvent,
    ProcessingRecordConflictError,
    ProcessingRecordError,
)
from app.modules.document_processing.ports import (
    ProcessingAcquireDecision,
    ProcessingAcquireResult,
    ProcessingRecordSnapshot,
    ProcessingRecordState,
)
from app.modules.tasks.domain import TaskId


logger = logging.getLogger(__name__)
_SQLITE_TIMEOUT_SECONDS = 30.0


class SQLiteProcessingRecordAdapter:
    """持久化处理步骤、Artifact 元数据和谱系事实。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        connection_factory: Callable[[float], sqlite3.Connection] | None = None,
    ) -> None:
        self._db_path = str(Path(db_path))
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection_factory = connection_factory
        self._initialize_schema()

    @property
    def db_path(self) -> str:
        """仅供组合根和离线诊断确认物理数据库边界。"""

        return self._db_path

    def register_artifact(self, artifact: ArtifactRef) -> None:
        """幂等登记 Store 已发布内容；冲突时保留文件并失败关闭。"""

        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef")
        try:
            with self._immediate_connection() as connection:
                self._register_artifact_row(
                    connection,
                    artifact,
                    created_at=time.time(),
                )
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "artifact_catalog_register_failed",
                "无法登记 Artifact 所有权事实",
                outcome_unknown=True,
            ) from exc

    def acquire(
        self,
        request: DocumentProcessingRequest,
    ) -> ProcessingAcquireResult:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        now = time.time()
        claim_token = uuid4().hex
        try:
            with self._immediate_connection() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM document_processing_steps
                    WHERE step_key = ?
                    """,
                    (request.step_key,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO document_processing_steps (
                            step_key,
                            task_id,
                            step_id,
                            source_artifact_id,
                            source_sha256,
                            profile_id,
                            profile_json,
                            state,
                            claim_token,
                            artifact_id,
                            error_code,
                            created_at,
                            updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, '', ?, ?)
                        """,
                        (
                            request.step_key,
                            request.task_id.value,
                            request.step_id,
                            request.source_artifact.artifact_id,
                            request.source_artifact.metadata.sha256,
                            request.profile.profile_id,
                            json.dumps(
                                request.profile.to_dict(),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            claim_token,
                            now,
                            now,
                        ),
                    )
                    snapshot = ProcessingRecordSnapshot(
                        step_key=request.step_key,
                        state=ProcessingRecordState.RUNNING,
                        claim_token=claim_token,
                    )
                    logger.debug(
                        "文档处理步骤已领取: task_id=%s step_key=%s",
                        request.task_id,
                        request.step_key[:12],
                    )
                    return ProcessingAcquireResult(
                        ProcessingAcquireDecision.ACQUIRED,
                        snapshot,
                    )

                self._require_same_request(row, request)
                snapshot = self._snapshot_from_row(connection, row)
                return ProcessingAcquireResult(
                    ProcessingAcquireDecision(snapshot.state.value),
                    snapshot,
                )
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_acquire_failed",
                "无法领取文档处理步骤",
                outcome_unknown=True,
            ) from exc

    def complete(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        artifact: ArtifactRef,
        lineage: LineageEvent,
    ) -> None:
        self._require_completion(request, artifact, lineage)
        claim_token = self._required_text(claim_token, name="claim_token")
        now = time.time()
        try:
            with self._immediate_connection() as connection:
                row = self._expected_running_row(
                    connection,
                    request,
                    claim_token,
                )
                self._require_same_request(row, request)
                self._register_artifact_row(
                    connection,
                    artifact,
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO document_processing_artifacts (
                        artifact_id,
                        task_id,
                        step_key,
                        kind,
                        representation,
                        media_type,
                        size_bytes,
                        sha256,
                        ordinal,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.task_id.value,
                        artifact.step_key,
                        artifact.kind.value,
                        artifact.representation.value,
                        artifact.metadata.media_type,
                        artifact.metadata.size_bytes,
                        artifact.metadata.sha256,
                        artifact.ordinal,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO document_processing_lineage (
                        event_id,
                        task_id,
                        step_key,
                        parent_artifact_id,
                        child_artifact_id,
                        operation,
                        profile_id,
                        processor_fingerprint,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lineage.event_id,
                        lineage.task_id.value,
                        lineage.step_key,
                        lineage.parent_artifact_id,
                        lineage.child_artifact_id,
                        lineage.operation,
                        lineage.profile_id,
                        lineage.processor_fingerprint,
                        now,
                    ),
                )
                cursor = connection.execute(
                    """
                    UPDATE document_processing_steps
                    SET state = 'succeeded',
                        artifact_id = ?,
                        error_code = '',
                        updated_at = ?
                    WHERE step_key = ?
                      AND state = 'running'
                      AND claim_token = ?
                    """,
                    (
                        artifact.artifact_id,
                        now,
                        request.step_key,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProcessingRecordConflictError()
        except ProcessingRecordError:
            raise
        except sqlite3.IntegrityError as exc:
            raise ProcessingRecordConflictError(
                "Artifact 或 Lineage 持久化身份冲突"
            ) from exc
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_complete_failed",
                "无法提交文档处理成功事实",
                outcome_unknown=True,
            ) from exc

    def fail(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        error_code: str,
    ) -> None:
        self._transition_running(
            request,
            claim_token=claim_token,
            state=ProcessingRecordState.FAILED,
            error_code=error_code,
        )

    def mark_outcome_unknown(
        self,
        request: DocumentProcessingRequest,
        *,
        error_code: str,
        claim_token: str | None = None,
    ) -> None:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        error_code = self._required_text(error_code, name="error_code")
        now = time.time()
        try:
            with self._immediate_connection() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM document_processing_steps
                    WHERE step_key = ?
                    """,
                    (request.step_key,),
                ).fetchone()
                if row is None:
                    raise ProcessingRecordConflictError("处理步骤不存在")
                self._require_same_request(row, request)
                if claim_token is None:
                    # 无 claim 的唯一合法场景是复用成功记录时发现 Artifact 缺失/损坏。
                    expected_state = ProcessingRecordState.SUCCEEDED.value
                    parameters = (
                        error_code,
                        now,
                        request.step_key,
                        expected_state,
                    )
                    predicate = "step_key = ? AND state = ?"
                else:
                    claim = self._required_text(claim_token, name="claim_token")
                    parameters = (
                        error_code,
                        now,
                        request.step_key,
                        ProcessingRecordState.RUNNING.value,
                        claim,
                    )
                    predicate = (
                        "step_key = ? AND state = ? AND claim_token = ?"
                    )
                cursor = connection.execute(
                    f"""
                    UPDATE document_processing_steps
                    SET state = 'outcome_unknown',
                        error_code = ?,
                        updated_at = ?
                    WHERE {predicate}
                    """,
                    parameters,
                )
                if cursor.rowcount != 1:
                    raise ProcessingRecordConflictError()
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_unknown_failed",
                "无法提交文档处理未知结果",
                outcome_unknown=True,
            ) from exc

    def resolve_failed(
        self,
        request: DocumentProcessingRequest,
        *,
        error_code: str,
    ) -> None:
        """提交已经对账确认的失败事实，不执行任何自动重试。"""

        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        error_code = self._required_text(error_code, name="error_code")
        now = time.time()
        try:
            with self._immediate_connection() as connection:
                row = connection.execute(
                    "SELECT * FROM document_processing_steps WHERE step_key = ?",
                    (request.step_key,),
                ).fetchone()
                if row is None:
                    raise ProcessingRecordConflictError("处理步骤不存在")
                self._require_same_request(row, request)
                if row["state"] == ProcessingRecordState.FAILED.value:
                    if row["error_code"] != error_code:
                        raise ProcessingRecordConflictError(
                            "处理步骤已被确认为不同失败事实"
                        )
                    return
                cursor = connection.execute(
                    """
                    UPDATE document_processing_steps
                    SET state = 'failed',
                        error_code = ?,
                        updated_at = ?
                    WHERE step_key = ?
                      AND state IN ('running', 'outcome_unknown')
                    """,
                    (error_code, now, request.step_key),
                )
                if cursor.rowcount != 1:
                    raise ProcessingRecordConflictError(
                        "仅允许对 running/outcome_unknown 执行失败对账"
                    )
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_resolve_failed_error",
                "无法提交文档处理对账失败事实",
                outcome_unknown=True,
            ) from exc
        logger.info(
            "文档处理步骤已对账为失败: task_id=%s step_key=%s error_code=%s",
            request.task_id,
            request.step_key[:12],
            error_code,
        )

    def recover_completed(
        self,
        request: DocumentProcessingRequest,
        *,
        artifact: ArtifactRef,
        lineage: LineageEvent,
    ) -> None:
        """以已验证 Artifact 原子修复成功事实，供显式对账流程调用。"""

        self._require_completion(request, artifact, lineage)
        now = time.time()
        try:
            with self._immediate_connection() as connection:
                row = connection.execute(
                    "SELECT * FROM document_processing_steps WHERE step_key = ?",
                    (request.step_key,),
                ).fetchone()
                if row is None:
                    raise ProcessingRecordConflictError("处理步骤不存在")
                self._require_same_request(row, request)
                if row["state"] not in {
                    ProcessingRecordState.RUNNING.value,
                    ProcessingRecordState.OUTCOME_UNKNOWN.value,
                }:
                    raise ProcessingRecordConflictError(
                        "仅允许修复 running/outcome_unknown 成功事实"
                    )

                self._register_artifact_row(
                    connection,
                    artifact,
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO document_processing_artifacts (
                        artifact_id, task_id, step_key, kind, representation,
                        media_type, size_bytes, sha256, ordinal, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.artifact_id,
                        artifact.task_id.value,
                        artifact.step_key,
                        artifact.kind.value,
                        artifact.representation.value,
                        artifact.metadata.media_type,
                        artifact.metadata.size_bytes,
                        artifact.metadata.sha256,
                        artifact.ordinal,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO document_processing_lineage (
                        event_id, task_id, step_key, parent_artifact_id,
                        child_artifact_id, operation, profile_id,
                        processor_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lineage.event_id,
                        lineage.task_id.value,
                        lineage.step_key,
                        lineage.parent_artifact_id,
                        lineage.child_artifact_id,
                        lineage.operation,
                        lineage.profile_id,
                        lineage.processor_fingerprint,
                        now,
                    ),
                )
                stored_artifact_row = connection.execute(
                    """
                    SELECT * FROM document_processing_artifacts
                    WHERE artifact_id = ?
                    """,
                    (artifact.artifact_id,),
                ).fetchone()
                stored_lineage_row = connection.execute(
                    """
                    SELECT * FROM document_processing_lineage
                    WHERE step_key = ?
                    """,
                    (request.step_key,),
                ).fetchone()
                if (
                    stored_artifact_row is None
                    or self._artifact_from_row(stored_artifact_row) != artifact
                    or stored_lineage_row is None
                    or self._lineage_from_row(stored_lineage_row) != lineage
                ):
                    raise ProcessingRecordConflictError(
                        "既有 Artifact 或 Lineage 与恢复事实不一致"
                    )
                cursor = connection.execute(
                    """
                    UPDATE document_processing_steps
                    SET state = 'succeeded',
                        artifact_id = ?,
                        error_code = '',
                        updated_at = ?
                    WHERE step_key = ?
                      AND state IN ('running', 'outcome_unknown')
                    """,
                    (artifact.artifact_id, now, request.step_key),
                )
                if cursor.rowcount != 1:
                    raise ProcessingRecordConflictError()
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_recover_complete_failed",
                "无法修复文档处理成功事实",
                outcome_unknown=True,
            ) from exc
        logger.info(
            "文档处理步骤成功事实已恢复: task_id=%s step_key=%s artifact_id=%s",
            request.task_id,
            request.step_key[:12],
            artifact.artifact_id[:12],
        )

    def quarantine_stale_running(
        self,
        *,
        stale_before_epoch: float,
        limit: int,
    ) -> tuple[str, ...]:
        """显式隔离陈旧 running；只改为 unknown，禁止直接重新领取。"""

        if isinstance(stale_before_epoch, bool) or not isinstance(
            stale_before_epoch, (int, float)
        ):
            raise TypeError("stale_before_epoch 必须是时间戳")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        bounded_limit = min(limit, 1000)
        now = time.time()
        try:
            with self._immediate_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT step_key
                    FROM document_processing_steps
                    WHERE state = 'running' AND updated_at <= ?
                    ORDER BY updated_at, step_key
                    LIMIT ?
                    """,
                    (float(stale_before_epoch), bounded_limit),
                ).fetchall()
                keys = tuple(str(row["step_key"]) for row in rows)
                for step_key in keys:
                    cursor = connection.execute(
                        """
                        UPDATE document_processing_steps
                        SET state = 'outcome_unknown',
                            error_code = 'processing_stale_running_requires_reconciliation',
                            updated_at = ?
                        WHERE step_key = ?
                          AND state = 'running'
                          AND updated_at <= ?
                        """,
                        (now, step_key, float(stale_before_epoch)),
                    )
                    if cursor.rowcount != 1:
                        raise ProcessingRecordConflictError(
                            "隔离陈旧 running 时发生并发状态变化"
                        )
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_quarantine_failed",
                "无法隔离陈旧文档处理步骤",
                outcome_unknown=True,
            ) from exc
        if keys:
            logger.warning(
                "陈旧文档处理步骤已隔离为 outcome_unknown: count=%d",
                len(keys),
            )
        return keys

    def get(self, step_key: str) -> ProcessingRecordSnapshot | None:
        step_key = self._required_text(step_key, name="step_key").lower()
        try:
            with self._connection() as connection:
                row = connection.execute(
                    """
                    SELECT *
                    FROM document_processing_steps
                    WHERE step_key = ?
                    """,
                    (step_key,),
                ).fetchone()
                if row is None:
                    return None
                return self._snapshot_from_row(connection, row)
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_read_failed",
                "无法读取文档处理步骤",
            ) from exc

    def _transition_running(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        state: ProcessingRecordState,
        error_code: str,
    ) -> None:
        if state is not ProcessingRecordState.FAILED:
            raise ValueError("_transition_running 仅允许 failed")
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        claim_token = self._required_text(claim_token, name="claim_token")
        error_code = self._required_text(error_code, name="error_code")
        now = time.time()
        try:
            with self._immediate_connection() as connection:
                row = self._expected_running_row(
                    connection,
                    request,
                    claim_token,
                )
                self._require_same_request(row, request)
                cursor = connection.execute(
                    """
                    UPDATE document_processing_steps
                    SET state = ?,
                        error_code = ?,
                        updated_at = ?
                    WHERE step_key = ?
                      AND state = 'running'
                      AND claim_token = ?
                    """,
                    (
                        state.value,
                        error_code,
                        now,
                        request.step_key,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProcessingRecordConflictError()
        except ProcessingRecordError:
            raise
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_fail_failed",
                "无法提交文档处理失败事实",
                outcome_unknown=True,
            ) from exc

    def _snapshot_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ProcessingRecordSnapshot:
        try:
            state = ProcessingRecordState(row["state"])
        except ValueError as exc:
            raise ProcessingRecordError(
                "processing_record_state_invalid",
                "Processing Record 状态不合法",
            ) from exc
        artifact = None
        lineage = None
        if state is ProcessingRecordState.SUCCEEDED:
            artifact_row = connection.execute(
                """
                SELECT *
                FROM document_processing_artifacts
                WHERE artifact_id = ?
                """,
                (row["artifact_id"],),
            ).fetchone()
            lineage_row = connection.execute(
                """
                SELECT *
                FROM document_processing_lineage
                WHERE step_key = ?
                """,
                (row["step_key"],),
            ).fetchone()
            if artifact_row is None or lineage_row is None:
                raise ProcessingRecordError(
                    "processing_record_incomplete_success",
                    "成功记录缺少 Artifact 或 Lineage",
                    outcome_unknown=True,
                )
            artifact = self._artifact_from_row(artifact_row)
            lineage = self._lineage_from_row(lineage_row)
        return ProcessingRecordSnapshot(
            step_key=row["step_key"],
            state=state,
            claim_token=row["claim_token"],
            artifact=artifact,
            lineage=lineage,
            error_code=row["error_code"] or "",
        )

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> ArtifactRef:
        return ArtifactRef(
            task_id=TaskId(row["task_id"]),
            artifact_id=row["artifact_id"],
            step_key=row["step_key"],
            kind=ArtifactKind(row["kind"]),
            representation=DocumentRepresentation(row["representation"]),
            metadata=ArtifactMetadata(
                media_type=row["media_type"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
            ),
            ordinal=row["ordinal"],
        )

    @staticmethod
    def _lineage_from_row(row: sqlite3.Row) -> LineageEvent:
        return LineageEvent(
            event_id=row["event_id"],
            task_id=TaskId(row["task_id"]),
            step_key=row["step_key"],
            parent_artifact_id=row["parent_artifact_id"],
            child_artifact_id=row["child_artifact_id"],
            operation=row["operation"],
            profile_id=row["profile_id"],
            processor_fingerprint=row["processor_fingerprint"],
        )

    @staticmethod
    def _require_completion(
        request: DocumentProcessingRequest,
        artifact: ArtifactRef,
        lineage: LineageEvent,
    ) -> None:
        if not isinstance(request, DocumentProcessingRequest):
            raise TypeError("request 必须是 DocumentProcessingRequest")
        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef")
        if not isinstance(lineage, LineageEvent):
            raise TypeError("lineage 必须是 LineageEvent")
        if (
            artifact.task_id != request.task_id
            or artifact.step_key != request.step_key
            or lineage.task_id != request.task_id
            or lineage.step_key != request.step_key
            or lineage.child_artifact_id != artifact.artifact_id
            or lineage.parent_artifact_id
            != request.source_artifact.artifact_id
        ):
            raise ProcessingRecordConflictError(
                "完成事实与处理请求不属于同一步骤"
            )

    @staticmethod
    def _require_same_request(
        row: sqlite3.Row,
        request: DocumentProcessingRequest,
    ) -> None:
        expected_profile = json.dumps(
            request.profile.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        actual = (
            row["task_id"],
            row["step_id"],
            row["source_artifact_id"],
            row["source_sha256"],
            row["profile_id"],
            row["profile_json"],
        )
        expected = (
            request.task_id.value,
            request.step_id,
            request.source_artifact.artifact_id,
            request.source_artifact.metadata.sha256,
            request.profile.profile_id,
            expected_profile,
        )
        if actual != expected:
            raise ProcessingRecordConflictError(
                "相同步骤键对应的冻结请求事实不一致"
            )

    @staticmethod
    def _expected_running_row(
        connection: sqlite3.Connection,
        request: DocumentProcessingRequest,
        claim_token: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT *
            FROM document_processing_steps
            WHERE step_key = ?
              AND state = 'running'
              AND claim_token = ?
            """,
            (request.step_key, claim_token),
        ).fetchone()
        if row is None:
            raise ProcessingRecordConflictError()
        return row

    def _connect(self, timeout_seconds: float) -> sqlite3.Connection:
        if self._connection_factory is None:
            connection = sqlite3.connect(
                self._db_path,
                timeout=max(0.0, timeout_seconds),
                isolation_level=None,
            )
        else:
            connection = self._connection_factory(timeout_seconds)
            connection.isolation_level = None
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(
            f"PRAGMA busy_timeout = {int(max(0.0, timeout_seconds) * 1000)}"
        )
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(_SQLITE_TIMEOUT_SECONDS)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _immediate_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect(_SQLITE_TIMEOUT_SECONDS)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        try:
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS document_processing_steps (
                        step_key TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        step_id TEXT NOT NULL,
                        source_artifact_id TEXT NOT NULL,
                        source_sha256 TEXT NOT NULL,
                        profile_id TEXT NOT NULL,
                        profile_json TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'running',
                                'succeeded',
                                'failed',
                                'outcome_unknown'
                            )
                        ),
                        claim_token TEXT NOT NULL,
                        artifact_id TEXT,
                        error_code TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS document_processing_artifact_catalog (
                        artifact_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        step_key TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        representation TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        sha256 TEXT NOT NULL,
                        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS document_processing_artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        step_key TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        representation TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                        sha256 TEXT NOT NULL,
                        ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                        created_at REAL NOT NULL,
                        FOREIGN KEY (step_key)
                            REFERENCES document_processing_steps(step_key)
                    );

                    CREATE TABLE IF NOT EXISTS document_processing_lineage (
                        event_id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        step_key TEXT NOT NULL UNIQUE,
                        parent_artifact_id TEXT NOT NULL,
                        child_artifact_id TEXT NOT NULL UNIQUE,
                        operation TEXT NOT NULL,
                        profile_id TEXT NOT NULL,
                        processor_fingerprint TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        FOREIGN KEY (step_key)
                            REFERENCES document_processing_steps(step_key),
                        FOREIGN KEY (child_artifact_id)
                            REFERENCES document_processing_artifacts(artifact_id)
                    );

                    CREATE INDEX IF NOT EXISTS
                        idx_document_processing_steps_task
                    ON document_processing_steps(task_id, updated_at);

                    CREATE INDEX IF NOT EXISTS
                        idx_document_processing_steps_state
                    ON document_processing_steps(state, updated_at);

                    CREATE INDEX IF NOT EXISTS
                        idx_document_processing_artifact_catalog_task_step
                    ON document_processing_artifact_catalog(task_id, step_key, ordinal);
                    """
                )
        except sqlite3.Error as exc:
            raise ProcessingRecordError(
                "processing_record_schema_failed",
                "无法初始化文档处理记录表",
            ) from exc

    def _register_artifact_row(
        self,
        connection: sqlite3.Connection,
        artifact: ArtifactRef,
        *,
        created_at: float,
    ) -> None:
        """在当前短事务内登记并复核 Artifact；支持同一步骤多个 ordinal。"""

        connection.execute(
            """
            INSERT OR IGNORE INTO document_processing_artifact_catalog (
                artifact_id, task_id, step_key, kind, representation,
                media_type, size_bytes, sha256, ordinal, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.artifact_id,
                artifact.task_id.value,
                artifact.step_key,
                artifact.kind.value,
                artifact.representation.value,
                artifact.metadata.media_type,
                artifact.metadata.size_bytes,
                artifact.metadata.sha256,
                artifact.ordinal,
                created_at,
            ),
        )
        row = connection.execute(
            """
            SELECT task_id, step_key, kind, representation, media_type,
                   size_bytes, sha256, ordinal
            FROM document_processing_artifact_catalog
            WHERE artifact_id = ?
            """,
            (artifact.artifact_id,),
        ).fetchone()
        expected = (
            artifact.task_id.value,
            artifact.step_key,
            artifact.kind.value,
            artifact.representation.value,
            artifact.metadata.media_type,
            artifact.metadata.size_bytes,
            artifact.metadata.sha256,
            artifact.ordinal,
        )
        actual = None if row is None else tuple(row)
        if actual != expected:
            raise ProcessingRecordConflictError(
                "Artifact Catalog 既有事实与当前引用不一致"
            )

    @staticmethod
    def _required_text(value: object, *, name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是 str")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized


__all__ = ["SQLiteProcessingRecordAdapter"]
