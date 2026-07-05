"""永久知识库写入的本地协调记录与状态转换。

AnythingLLM 与 DocSense SQLite 不可能共享数据库事务。本模块使用本地状态机记录一次知识
库操作已经到达的最远阶段，使任务重试能够复用已上传或已绑定的外部文档，而不是再次
产生全局文档。模块只保存供应商无关的不透明引用，不执行任何 HTTP 请求。
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from app.ports import (
    KnowledgeIndexConflictError,
    KnowledgeIndexError,
    KnowledgeIndexRecoveryRequiredError,
    KnowledgeOperationContext,
)


logger = logging.getLogger(__name__)


STATUS_PENDING = "pending"
STATUS_UPLOADING = "uploading"
STATUS_DOCUMENT_READY = "document_ready"
STATUS_EXTERNAL_SUCCEEDED = "external_succeeded"
STATUS_COMMITTED = "committed"
STATUS_COMPENSATED = "compensated"
STATUS_COMPENSATION_FAILED = "compensation_failed"
STATUS_DETACHING = "detaching"
STATUS_EXTERNAL_DETACHED = "external_detached"
STATUS_REPLACEMENT_CLEANUP_PENDING = "replacement_cleanup_pending"
STATUS_SUPERSEDED = "superseded"

_KNOWN_STATUSES = frozenset(
    {
        STATUS_PENDING,
        STATUS_UPLOADING,
        STATUS_DOCUMENT_READY,
        STATUS_EXTERNAL_SUCCEEDED,
        STATUS_COMMITTED,
        STATUS_COMPENSATED,
        STATUS_COMPENSATION_FAILED,
        STATUS_DETACHING,
        STATUS_EXTERNAL_DETACHED,
        STATUS_REPLACEMENT_CLEANUP_PENDING,
        STATUS_SUPERSEDED,
    }
)


def _utc_now_iso() -> str:
    """返回带时区的 UTC 时间，避免跨时区部署产生不可比较记录。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class KnowledgeIndexOperationRecord:
    """一次永久知识库操作的不可变协调快照。"""

    collection_ref: str
    idempotency_key: str
    execution_id: str
    last_execution_id: str
    business_type: str
    business_key: str
    source_kind: str
    source_digest: str
    document_ref: str
    external_document_id: str
    external_location: str
    superseded_location: str
    source_marker: str
    metadata: Mapping[str, Any]
    status: str
    last_error: str
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        """冻结元数据快照并拒绝数据库中不受支持的状态。"""
        if self.status not in _KNOWN_STATUSES:
            raise ValueError(f"未知知识库协调状态: {self.status}")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class KnowledgeCollectionReservation:
    """永久集合创建协调记录，以及当前调用是否持有创建权。"""

    architecture_id: int
    collection_name: str
    workspace_slug: str
    status: str
    owner_token: str
    owns_reservation: bool
    last_error: str
    policy_version: int


class KnowledgeIndexOperationService:
    """在任务数据库中维护永久知识库操作的幂等协调状态。"""

    def __init__(self, db_path: str) -> None:
        """保存数据库路径并执行只增不删的向前兼容建表。"""
        normalized_path = str(db_path or "").strip()
        if not normalized_path:
            raise ValueError("db_path 不能为空")
        self.db_path = normalized_path
        Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """创建启用行名访问的独立连接，避免后台线程共享连接对象。"""
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        """创建协调表及业务查询索引，不改写任何既有任务数据。"""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_index_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_ref TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    last_execution_id TEXT NOT NULL,
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    source_kind TEXT NOT NULL CHECK (source_kind IN ('upload', 'prepared')),
                    source_digest TEXT NOT NULL DEFAULT '',
                    document_ref TEXT NOT NULL DEFAULT '',
                    external_document_id TEXT NOT NULL DEFAULT '',
                    external_location TEXT NOT NULL DEFAULT '',
                    superseded_location TEXT NOT NULL DEFAULT '',
                    source_marker TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN (
                        'pending', 'uploading', 'document_ready',
                        'external_succeeded', 'replacement_cleanup_pending',
                        'committed', 'superseded', 'detaching',
                        'external_detached', 'compensated', 'compensation_failed'
                    )),
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (collection_ref, idempotency_key)
                )
                """
            )
            self._ensure_operation_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_operations_business
                ON knowledge_index_operations (
                    business_type, business_key, created_at
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_index_collections (
                    architecture_id INTEGER PRIMARY KEY,
                    collection_name TEXT NOT NULL,
                    workspace_slug TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('creating', 'ready')),
                    owner_token TEXT NOT NULL DEFAULT '',
                    policy_version INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_collection_schema(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_knowledge_operations_status
                ON knowledge_index_operations (status, updated_at)
                """
            )
            duplicate_workspace = connection.execute(
                """
                SELECT 1 FROM knowledge_index_collections
                WHERE workspace_slug != ''
                GROUP BY workspace_slug HAVING COUNT(*) > 1
                LIMIT 1
                """
            ).fetchone()
            if duplicate_workspace is not None:
                raise KnowledgeIndexConflictError(
                    "集合协调表存在重复 Workspace 映射，必须先人工修复"
                )
            duplicate_document = connection.execute(
                """
                SELECT 1 FROM knowledge_index_operations
                WHERE external_location != ''
                  AND status NOT IN ('compensated', 'superseded')
                GROUP BY collection_ref, external_location HAVING COUNT(*) > 1
                LIMIT 1
                """
            ).fetchone()
            if duplicate_document is not None:
                raise KnowledgeIndexConflictError(
                    "知识库协调表存在重复活动文档位置，必须先人工修复"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_collection_workspace
                ON knowledge_index_collections (workspace_slug)
                WHERE workspace_slug != ''
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_active_document_location
                ON knowledge_index_operations (collection_ref, external_location)
                WHERE external_location != ''
                  AND status NOT IN ('compensated', 'superseded')
                """
            )

    @staticmethod
    def _ensure_operation_schema(connection: sqlite3.Connection) -> None:
        """为阶段 8 早期数据库补充可离线恢复所需的真实文档 ID。"""
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(knowledge_index_operations)"
            ).fetchall()
        }
        if "external_document_id" not in columns:
            connection.execute(
                "ALTER TABLE knowledge_index_operations "
                "ADD COLUMN external_document_id TEXT NOT NULL DEFAULT ''"
            )
        if "superseded_location" not in columns:
            connection.execute(
                "ALTER TABLE knowledge_index_operations "
                "ADD COLUMN superseded_location TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _ensure_collection_schema(connection: sqlite3.Connection) -> None:
        """为阶段 8 早期集合协调表补充可重试的 Workspace 策略版本。"""
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(knowledge_index_collections)"
            ).fetchall()
        }
        if "policy_version" not in columns:
            connection.execute(
                "ALTER TABLE knowledge_index_collections "
                "ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _serialize_metadata(metadata: Mapping[str, Any]) -> str:
        """生成可用于不可变比较的规范 JSON，并拒绝非对象或 NaN。"""
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata 必须是 Mapping")
        try:
            return json.dumps(
                dict(metadata),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata 必须是严格 JSON 对象") from exc

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """规范化协调记录必需文本，防止空键进入唯一约束。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> KnowledgeIndexOperationRecord:
        """严格解析数据库行，损坏的 metadata 不得静默回退为空对象。"""
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            raise ValueError("knowledge_index_operations.metadata_json 必须是对象")
        return KnowledgeIndexOperationRecord(
            collection_ref=row["collection_ref"],
            idempotency_key=row["idempotency_key"],
            execution_id=row["execution_id"],
            last_execution_id=row["last_execution_id"],
            business_type=row["business_type"],
            business_key=row["business_key"],
            source_kind=row["source_kind"],
            source_digest=row["source_digest"],
            document_ref=row["document_ref"],
            external_document_id=row["external_document_id"],
            external_location=row["external_location"],
            superseded_location=row["superseded_location"],
            source_marker=row["source_marker"],
            metadata=metadata,
            status=row["status"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(
        self,
        collection_ref: str,
        idempotency_key: str,
    ) -> KnowledgeIndexOperationRecord | None:
        """读取指定集合内的协调记录；不存在时返回 ``None``。"""
        normalized_collection = self._required_text(
            collection_ref,
            name="collection_ref",
        )
        normalized_key = self._required_text(
            idempotency_key,
            name="idempotency_key",
        )
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def begin(
        self,
        *,
        collection_ref: str,
        idempotency_key: str,
        operation_context: KnowledgeOperationContext,
        source_kind: str,
        source_digest: str,
        metadata: Mapping[str, Any],
        document_ref: str = "",
        external_document_id: str = "",
        external_location: str = "",
        source_marker: str = "",
    ) -> KnowledgeIndexOperationRecord:
        """创建或复用一次操作，并拒绝同键不同内容的幂等冲突。

        新执行可以复用上一执行已提交的同一逻辑操作，因此 ``execution_id`` 保存首次创建
        者，``last_execution_id`` 记录最近一次重放者。业务类型、业务键、来源摘要和元数据
        都属于不可变身份；任一变化都必须使用新的幂等键。
        """
        if not isinstance(operation_context, KnowledgeOperationContext):
            raise TypeError("operation_context 必须是 KnowledgeOperationContext")
        normalized_collection = self._required_text(
            collection_ref,
            name="collection_ref",
        )
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        normalized_source_kind = self._required_text(source_kind, name="source_kind")
        if normalized_source_kind not in {"upload", "prepared"}:
            raise ValueError("source_kind 只能是 upload 或 prepared")
        normalized_digest = str(source_digest or "").strip()
        normalized_document_ref = str(document_ref or "").strip()
        normalized_external_document_id = str(external_document_id or "").strip()
        normalized_location = str(external_location or "").strip()
        normalized_marker = str(source_marker or "").strip()
        metadata_json = self._serialize_metadata(metadata)
        now = _utc_now_iso()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
            if existing is None:
                try:
                    connection.execute(
                        """
                        INSERT INTO knowledge_index_operations (
                            collection_ref, idempotency_key, execution_id,
                            last_execution_id, business_type, business_key,
                            source_kind, source_digest, document_ref,
                            external_document_id, external_location, source_marker,
                            metadata_json, status, last_error, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                        """,
                        (
                            normalized_collection,
                            normalized_key,
                            operation_context.execution_id,
                            operation_context.execution_id,
                            operation_context.business_type,
                            operation_context.business_key,
                            normalized_source_kind,
                            normalized_digest,
                            normalized_document_ref,
                            normalized_external_document_id,
                            normalized_location,
                            normalized_marker,
                            metadata_json,
                            STATUS_PENDING,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise KnowledgeIndexConflictError(
                        "同一永久集合中的文档位置已经由其他幂等操作占用"
                    ) from exc
            else:
                immutable_matches = (
                    existing["business_type"] == operation_context.business_type
                    and existing["business_key"] == operation_context.business_key
                    and existing["source_kind"] == normalized_source_kind
                    and existing["source_digest"] == normalized_digest
                    and existing["metadata_json"] == metadata_json
                )
                if normalized_source_kind == "prepared":
                    immutable_matches = immutable_matches and (
                        existing["document_ref"] == normalized_document_ref
                        and existing["external_location"] == normalized_location
                    )
                if not immutable_matches:
                    raise KnowledgeIndexConflictError(
                        "相同知识库幂等键对应的业务身份、来源或 metadata 发生冲突"
                    )
                if existing["status"] == STATUS_COMPENSATION_FAILED:
                    raise KnowledgeIndexRecoveryRequiredError(
                        "先前知识库操作补偿失败，禁止自动重放外部写入"
                    )
                if existing["status"] == STATUS_SUPERSEDED:
                    raise KnowledgeIndexConflictError(
                        "该文档版本已经被同名新版本替换，禁止重新写入旧版本"
                    )
                if existing["status"] in {STATUS_DETACHING, STATUS_EXTERNAL_DETACHED}:
                    raise KnowledgeIndexRecoveryRequiredError(
                        "先前知识库操作正在解绑，禁止并发重新写入"
                    )

                if existing["status"] == STATUS_COMPENSATED:
                    # 已确认补偿成功的操作可以安全重新开始。upload 路径必须清除上一份
                    # 已删除文档的引用；prepared 路径继续使用调用方传入的同一不透明句柄。
                    reset_document_ref = (
                        normalized_document_ref
                        if normalized_source_kind == "prepared"
                        else ""
                    )
                    reset_location = (
                        normalized_location
                        if normalized_source_kind == "prepared"
                        else ""
                    )
                    reset_external_document_id = (
                        normalized_external_document_id
                        if normalized_source_kind == "prepared"
                        else ""
                    )
                    connection.execute(
                        """
                        UPDATE knowledge_index_operations
                        SET last_execution_id = ?, document_ref = ?,
                            external_document_id = ?, external_location = ?, source_marker = ?,
                            superseded_location = '', status = ?, last_error = '', updated_at = ?
                        WHERE collection_ref = ? AND idempotency_key = ?
                        """,
                        (
                            operation_context.execution_id,
                            reset_document_ref,
                            reset_external_document_id,
                            reset_location,
                            normalized_marker,
                            STATUS_PENDING,
                            now,
                            normalized_collection,
                            normalized_key,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE knowledge_index_operations
                        SET last_execution_id = ?, updated_at = ?
                        WHERE collection_ref = ? AND idempotency_key = ?
                        """,
                        (
                            operation_context.execution_id,
                            now,
                            normalized_collection,
                            normalized_key,
                        ),
                    )

            row = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
        if row is None:
            raise KnowledgeIndexError("知识库协调记录提交后无法读取")
        record = self._row_to_record(row)
        logger.info(
            "知识库协调记录已准备: collection_ref=%s idempotency_key=%s "
            "status=%s source_kind=%s business_type=%s business_key=%s "
            "execution_id=%s",
            record.collection_ref,
            record.idempotency_key,
            record.status,
            record.source_kind,
            record.business_type,
            record.business_key,
            operation_context.execution_id,
        )
        return record

    def transition(
        self,
        *,
        collection_ref: str,
        idempotency_key: str,
        expected_statuses: set[str],
        target_status: str,
        document_ref: str | None = None,
        external_document_id: str | None = None,
        external_location: str | None = None,
        last_error: str | None = None,
    ) -> KnowledgeIndexOperationRecord:
        """以比较并交换方式推进状态，禁止后到线程覆盖更远进度。"""
        if target_status not in _KNOWN_STATUSES:
            raise ValueError(f"未知目标状态: {target_status}")
        if not expected_statuses or not expected_statuses.issubset(_KNOWN_STATUSES):
            raise ValueError("expected_statuses 包含未知状态")
        normalized_collection = self._required_text(
            collection_ref,
            name="collection_ref",
        )
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        now = _utc_now_iso()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
            if row is None:
                raise KnowledgeIndexError("待转换的知识库协调记录不存在")
            if row["status"] == target_status:
                # 完全相同的状态重放按幂等成功处理，但不允许用空值覆盖既有外部引用。
                if document_ref is not None and row["document_ref"] != document_ref:
                    raise KnowledgeIndexConflictError("document_ref 幂等重放冲突")
                if (
                    external_document_id is not None
                    and row["external_document_id"] != external_document_id
                ):
                    raise KnowledgeIndexConflictError(
                        "external_document_id 幂等重放冲突"
                    )
                if (
                    external_location is not None
                    and row["external_location"] != external_location
                ):
                    raise KnowledgeIndexConflictError(
                        "external_location 幂等重放冲突"
                    )
                next_error = (
                    row["last_error"]
                    if last_error is None
                    else str(last_error or "").strip()
                )
                if next_error != row["last_error"]:
                    connection.execute(
                        """
                        UPDATE knowledge_index_operations
                        SET last_error = ?, updated_at = ?
                        WHERE collection_ref = ? AND idempotency_key = ?
                        """,
                        (
                            next_error,
                            now,
                            normalized_collection,
                            normalized_key,
                        ),
                    )
                    row = connection.execute(
                        """
                        SELECT * FROM knowledge_index_operations
                        WHERE collection_ref = ? AND idempotency_key = ?
                        """,
                        (normalized_collection, normalized_key),
                    ).fetchone()
                record = self._row_to_record(row)
                logger.debug(
                    "知识库协调状态幂等复用: collection_ref=%s idempotency_key=%s "
                    "status=%s",
                    normalized_collection,
                    normalized_key,
                    target_status,
                )
                return record
            if row["status"] not in expected_statuses:
                raise KnowledgeIndexConflictError(
                    f"非法知识库协调状态转换: {row['status']} -> {target_status}"
                )

            next_document_ref = (
                row["document_ref"] if document_ref is None else str(document_ref).strip()
            )
            next_external_document_id = (
                row["external_document_id"]
                if external_document_id is None
                else str(external_document_id).strip()
            )
            next_location = (
                row["external_location"]
                if external_location is None
                else str(external_location).strip()
            )
            try:
                connection.execute(
                    """
                    UPDATE knowledge_index_operations
                    SET document_ref = ?, external_document_id = ?,
                        external_location = ?, status = ?, last_error = ?, updated_at = ?
                    WHERE collection_ref = ? AND idempotency_key = ?
                    """,
                    (
                        next_document_ref,
                        next_external_document_id,
                        next_location,
                        target_status,
                        str(last_error or "").strip() if last_error is not None else "",
                        now,
                        normalized_collection,
                        normalized_key,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeIndexConflictError(
                    "目标外部文档位置已经由其他活动操作占用"
                ) from exc
            updated = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
        if updated is None:
            raise KnowledgeIndexError("状态转换后无法读取知识库协调记录")
        record = self._row_to_record(updated)
        logger.info(
            "知识库协调状态已转换: collection_ref=%s idempotency_key=%s "
            "previous_status=%s target_status=%s has_document_ref=%s "
            "has_external_location=%s",
            normalized_collection,
            normalized_key,
            row["status"],
            target_status,
            bool(record.document_ref),
            bool(record.external_location),
        )
        return record

    def record_external_error(
        self,
        *,
        collection_ref: str,
        idempotency_key: str,
        error_message: str,
    ) -> KnowledgeIndexOperationRecord:
        """在不回退当前进度的前提下记录可用于恢复诊断的安全错误。"""
        normalized_error = str(error_message or "").strip()
        if not normalized_error:
            raise ValueError("error_message 不能为空")
        record = self.get(collection_ref, idempotency_key)
        if record is None:
            raise KnowledgeIndexError("待记录错误的知识库协调记录不存在")
        return self.transition(
            collection_ref=collection_ref,
            idempotency_key=idempotency_key,
            expected_statuses={record.status},
            target_status=record.status,
            last_error=normalized_error,
        )

    def record_replacement_target(
        self,
        *,
        collection_ref: str,
        idempotency_key: str,
        superseded_location: str,
    ) -> KnowledgeIndexOperationRecord:
        """在绑定新版本前持久化待解绑的旧文档位置。

        该字段一旦写入便不可替换，确保进程重启后仍清理最初观察到的旧版本，而不会因本地
        documents 行已经切换到新版本而丢失补偿目标。
        """
        normalized_collection = self._required_text(collection_ref, name="collection_ref")
        normalized_key = self._required_text(idempotency_key, name="idempotency_key")
        normalized_location = self._required_text(
            superseded_location,
            name="superseded_location",
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
            if row is None:
                raise KnowledgeIndexError("待登记替换目标的协调记录不存在")
            if row["status"] not in {STATUS_PENDING, STATUS_DOCUMENT_READY}:
                raise KnowledgeIndexConflictError("当前协调阶段不允许登记替换目标")
            current = row["superseded_location"]
            if current and current != normalized_location:
                raise KnowledgeIndexConflictError("同一操作的旧版本位置发生冲突")
            connection.execute(
                """
                UPDATE knowledge_index_operations
                SET superseded_location = ?, updated_at = ?
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (
                    normalized_location,
                    _utc_now_iso(),
                    normalized_collection,
                    normalized_key,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE collection_ref = ? AND idempotency_key = ?
                """,
                (normalized_collection, normalized_key),
            ).fetchone()
        return self._row_to_record(updated)

    def mark_detached(
        self,
        *,
        collection_ref: str,
        external_location: str,
    ) -> int:
        """把已完成外部解绑的操作标记为已补偿，并返回更新行数。

        Collection 解绑后，旧协调记录不能继续被 ``reconcile_document`` 当作有效永久索引。
        该更新不删除历史记录；未来显式使用同一幂等键重新保存时可以从 compensated 安全
        重启，同时仍能审计此前曾经完成过解绑。
        """
        normalized_collection = self._required_text(
            collection_ref,
            name="collection_ref",
        )
        normalized_location = self._required_text(
            external_location,
            name="external_location",
        )
        now = _utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_index_operations
                SET status = ?, last_error = '', updated_at = ?
                WHERE collection_ref = ? AND external_location = ?
                  AND status = ?
                """,
                (
                    STATUS_COMPENSATED,
                    now,
                    normalized_collection,
                    normalized_location,
                    STATUS_EXTERNAL_DETACHED,
                ),
            )
            updated_count = int(cursor.rowcount)
        logger.info(
            "知识库协调记录已标记解绑: collection_ref=%s location=%s "
            "updated_count=%d",
            normalized_collection,
            normalized_location,
            updated_count,
        )
        return updated_count

    def mark_superseded(
        self,
        *,
        collection_ref: str,
        external_location: str,
    ) -> int:
        """把已经从集合移除的旧版本操作标记为 superseded 终态。"""
        normalized_collection = self._required_text(collection_ref, name="collection_ref")
        normalized_location = self._required_text(external_location, name="external_location")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_index_operations
                SET status = ?, last_error = '', updated_at = ?
                WHERE collection_ref = ? AND external_location = ?
                  AND status IN (?, ?)
                """,
                (
                    STATUS_SUPERSEDED,
                    _utc_now_iso(),
                    normalized_collection,
                    normalized_location,
                    STATUS_COMMITTED,
                    STATUS_EXTERNAL_SUCCEEDED,
                ),
            )
            return int(cursor.rowcount)

    def begin_detach(self, *, collection_ref: str, external_location: str) -> str:
        """持久化解绑意图，并返回下一步应继续执行的解绑阶段。

        解绑请求本身具有幂等语义，因此进程在 ``detaching`` 阶段退出后可以安全重放远程
        删除绑定。只有进入 ``external_detached`` 才允许跳过远程调用并继续本地提交。
        """
        normalized_collection = self._required_text(collection_ref, name="collection_ref")
        normalized_location = self._required_text(external_location, name="external_location")
        now = _utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT status FROM knowledge_index_operations
                WHERE collection_ref = ? AND external_location = ?
                  AND status != ?
                """,
                (normalized_collection, normalized_location, STATUS_COMPENSATED),
            ).fetchall()
            statuses = {row["status"] for row in rows}
            if STATUS_COMPENSATION_FAILED in statuses:
                raise KnowledgeIndexRecoveryRequiredError(
                    "文档存在未解决的补偿失败，禁止自动解绑"
                )
            if statuses.intersection(
                {
                    STATUS_PENDING,
                    STATUS_UPLOADING,
                    STATUS_DOCUMENT_READY,
                    STATUS_REPLACEMENT_CLEANUP_PENDING,
                }
            ):
                raise KnowledgeIndexRecoveryRequiredError(
                    "文档写入尚未到达永久提交点，禁止并发解绑"
                )
            if STATUS_EXTERNAL_DETACHED in statuses:
                return STATUS_EXTERNAL_DETACHED
            connection.execute(
                """
                UPDATE knowledge_index_operations
                SET status = ?, last_error = '', updated_at = ?
                WHERE collection_ref = ? AND external_location = ?
                  AND status IN (?, ?, ?)
                """,
                (
                    STATUS_DETACHING,
                    now,
                    normalized_collection,
                    normalized_location,
                    STATUS_COMMITTED,
                    STATUS_EXTERNAL_SUCCEEDED,
                    STATUS_DETACHING,
                ),
            )
        return STATUS_DETACHING

    def mark_external_detached(
        self,
        *,
        collection_ref: str,
        external_location: str,
    ) -> int:
        """确认外部解绑已完成，使后续重试只提交本地删除。"""
        normalized_collection = self._required_text(collection_ref, name="collection_ref")
        normalized_location = self._required_text(external_location, name="external_location")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_index_operations
                SET status = ?, last_error = '', updated_at = ?
                WHERE collection_ref = ? AND external_location = ?
                  AND status = ?
                """,
                (
                    STATUS_EXTERNAL_DETACHED,
                    _utc_now_iso(),
                    normalized_collection,
                    normalized_location,
                    STATUS_DETACHING,
                ),
            )
            return int(cursor.rowcount)

    def reserve_collection(
        self,
        *,
        architecture_id: int,
        collection_name: str,
    ) -> KnowledgeCollectionReservation:
        """跨进程原子预留永久 Workspace 创建权。

        ``creating`` 记录不会自动超时接管，因为无法证明原创建者是否已经在 AnythingLLM
        创建 Workspace。贸然接管会制造重复永久集合，因此陈旧预留必须由恢复工具核查。
        """
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or architecture_id < 1
        ):
            raise ValueError("architecture_id 必须是正整数")
        normalized_name = self._required_text(collection_name, name="collection_name")
        owner_token = secrets.token_hex(16)
        now = _utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM knowledge_index_collections WHERE architecture_id = ?",
                (architecture_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO knowledge_index_collections (
                        architecture_id, collection_name, workspace_slug, status,
                        owner_token, last_error, created_at, updated_at
                    ) VALUES (?, ?, '', 'creating', ?, '', ?, ?)
                    """,
                    (architecture_id, normalized_name, owner_token, now, now),
                )
                return KnowledgeCollectionReservation(
                    architecture_id=architecture_id,
                    collection_name=normalized_name,
                    workspace_slug="",
                    status="creating",
                    owner_token=owner_token,
                    owns_reservation=True,
                    last_error="",
                    policy_version=0,
                )
            if row["collection_name"].casefold() != normalized_name.casefold():
                raise KnowledgeIndexConflictError(
                    "同一 architecture_id 对应的永久集合名称发生冲突"
                )
            if row["status"] == "creating":
                raise KnowledgeIndexRecoveryRequiredError(
                    "永久集合创建结果尚未确定，禁止并发重复创建 Workspace"
                )
            if row["status"] != "ready" or not row["workspace_slug"]:
                raise KnowledgeIndexRecoveryRequiredError("永久集合协调记录需要人工恢复")
            return KnowledgeCollectionReservation(
                architecture_id=architecture_id,
                collection_name=row["collection_name"],
                workspace_slug=row["workspace_slug"],
                status=row["status"],
                owner_token="",
                owns_reservation=False,
                last_error=row["last_error"],
                policy_version=int(row["policy_version"]),
            )

    def register_existing_collection(
        self,
        *,
        architecture_id: int,
        collection_name: str,
        workspace_slug: str,
    ) -> int:
        """登记已有本地权威映射，并返回已经成功应用的策略版本。"""
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or architecture_id < 1
        ):
            raise ValueError("architecture_id 必须是正整数")
        normalized_name = self._required_text(collection_name, name="collection_name")
        normalized_slug = self._required_text(workspace_slug, name="workspace_slug")
        now = _utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM knowledge_index_collections WHERE architecture_id = ?",
                (architecture_id,),
            ).fetchone()
            if row is not None and row["workspace_slug"] not in {"", normalized_slug}:
                raise KnowledgeIndexConflictError("永久集合协调映射与本地权威映射冲突")
            current_policy_version = int(row["policy_version"]) if row else 0
            try:
                connection.execute(
                    """
                    INSERT INTO knowledge_index_collections (
                        architecture_id, collection_name, workspace_slug, status,
                        owner_token, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, 'ready', '', '', ?, ?)
                    ON CONFLICT(architecture_id) DO UPDATE SET
                        collection_name = excluded.collection_name,
                        workspace_slug = excluded.workspace_slug,
                        status = 'ready', owner_token = '', last_error = '',
                        updated_at = excluded.updated_at
                    """,
                    (architecture_id, normalized_name, normalized_slug, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeIndexConflictError(
                    "AnythingLLM Workspace 已经映射到其他 architecture"
                ) from exc
        return current_policy_version

    def complete_collection_reservation(
        self,
        *,
        reservation: KnowledgeCollectionReservation,
        workspace_slug: str,
        policy_version: int,
    ) -> None:
        """由持有者提交 Workspace 创建结果，拒绝其他进程冒充完成。"""
        if not reservation.owns_reservation or not reservation.owner_token:
            raise ValueError("reservation 不持有集合创建权")
        normalized_slug = self._required_text(workspace_slug, name="workspace_slug")
        if isinstance(policy_version, bool) or not isinstance(policy_version, int) or policy_version < 1:
            raise ValueError("policy_version 必须是正整数")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    UPDATE knowledge_index_collections
                    SET workspace_slug = ?, status = 'ready', owner_token = '',
                        policy_version = ?, last_error = '', updated_at = ?
                    WHERE architecture_id = ? AND status = 'creating' AND owner_token = ?
                    """,
                    (
                        normalized_slug,
                        policy_version,
                        _utc_now_iso(),
                        reservation.architecture_id,
                        reservation.owner_token,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise KnowledgeIndexConflictError(
                    "AnythingLLM Workspace 已经映射到其他 architecture"
                ) from exc
            if cursor.rowcount != 1:
                raise KnowledgeIndexConflictError("永久集合创建预留已经失效")

    def mark_collection_policy_applied(
        self,
        *,
        architecture_id: int,
        workspace_slug: str,
        policy_version: int,
    ) -> None:
        """在远程 Workspace 更新成功后提交已应用策略版本。"""
        normalized_slug = self._required_text(workspace_slug, name="workspace_slug")
        if (
            isinstance(policy_version, bool)
            or not isinstance(policy_version, int)
            or policy_version < 1
        ):
            raise ValueError("policy_version 必须是正整数")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE knowledge_index_collections
                SET policy_version = ?, updated_at = ?
                WHERE architecture_id = ? AND workspace_slug = ? AND status = 'ready'
                """,
                (
                    policy_version,
                    _utc_now_iso(),
                    architecture_id,
                    normalized_slug,
                ),
            )
            if cursor.rowcount != 1:
                raise KnowledgeIndexConflictError("永久集合策略版本提交目标不存在")

    def list_recovery_required(self) -> list[KnowledgeIndexOperationRecord]:
        """列出必须人工核查的上传、补偿和解绑操作，供巡检脚本使用。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM knowledge_index_operations
                WHERE status IN (?, ?, ?, ?, ?)
                ORDER BY updated_at ASC
                """,
                (
                    STATUS_UPLOADING,
                    STATUS_COMPENSATION_FAILED,
                    STATUS_DETACHING,
                    STATUS_EXTERNAL_DETACHED,
                    STATUS_REPLACEMENT_CLEANUP_PENDING,
                ),
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_collection_recovery_required(self) -> list[KnowledgeCollectionReservation]:
        """列出结果尚不确定的 Workspace 创建预留，禁止自动接管。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM knowledge_index_collections
                WHERE status != 'ready'
                ORDER BY updated_at ASC
                """
            ).fetchall()
        return [
            KnowledgeCollectionReservation(
                architecture_id=row["architecture_id"],
                collection_name=row["collection_name"],
                workspace_slug=row["workspace_slug"],
                status=row["status"],
                owner_token="",
                owns_reservation=False,
                last_error=row["last_error"],
                policy_version=int(row["policy_version"]),
            )
            for row in rows
        ]

    def resolve_uncertain_upload(
        self,
        *,
        collection_ref: str,
        idempotency_key: str,
        document_ref: str,
        external_document_id: str,
        external_location: str,
    ) -> KnowledgeIndexOperationRecord:
        """登记人工核实的上传实体，使状态机从 uploading 安全继续。

        该方法不会查询 AnythingLLM，也不会自行选择候选文档。调用者必须先通过运维流程
        核对来源标记、文件摘要和创建时间，随后显式提交三个不透明引用。
        """
        return self.transition(
            collection_ref=collection_ref,
            idempotency_key=idempotency_key,
            expected_statuses={STATUS_UPLOADING},
            target_status=STATUS_DOCUMENT_READY,
            document_ref=self._required_text(document_ref, name="document_ref"),
            external_document_id=self._required_text(
                external_document_id,
                name="external_document_id",
            ),
            external_location=self._required_text(
                external_location,
                name="external_location",
            ),
            last_error="",
        )

    def confirm_compensated(
        self,
        *,
        collection_ref: str,
        idempotency_key: str,
    ) -> KnowledgeIndexOperationRecord:
        """在人工确认外部实体已清理后解除 compensation_failed 阻断。"""
        return self.transition(
            collection_ref=collection_ref,
            idempotency_key=idempotency_key,
            expected_statuses={STATUS_COMPENSATION_FAILED},
            target_status=STATUS_COMPENSATED,
            last_error="",
        )
