"""MinerU 外部操作身份的 SQLite 持久化适配器。"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.modules.document_processing.domain import DocumentProcessingError


logger = logging.getLogger(__name__)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExternalOperationSnapshot:
    """供应商提交对账所需的最小持久化事实。"""

    operation_key: str
    provider: str
    state: str
    provider_operation_id: str


class SQLiteMinerUOperationObserver:
    """与任务库共用物理 SQLite 文件、但只拥有模块内表。

    每次写入使用短 ``BEGIN IMMEDIATE`` 事务，网络提交不在数据库事务中执行。
    多实例部署前仍需迁移到支持行级锁的数据库或可靠任务队列。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def record_submission_intent(
        self,
        *,
        operation_key: str,
        provider: str,
    ) -> None:
        key = self._validate_key(operation_key)
        normalized_provider = self._required(provider, "provider")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT provider
                FROM document_processing_external_operations
                WHERE operation_key = ?
                """,
                (key,),
            ).fetchone()
            if row is not None and row["provider"] != normalized_provider:
                connection.rollback()
                raise DocumentProcessingError(
                    "external_operation_provider_conflict",
                    "外部操作已绑定不同供应商",
                    outcome_unknown=True,
                )
            connection.execute(
                """
                INSERT INTO document_processing_external_operations (
                    operation_key,
                    provider,
                    state,
                    provider_operation_id,
                    created_at_utc,
                    updated_at_utc
                ) VALUES (?, ?, 'submission_intent', '', ?, ?)
                ON CONFLICT(operation_key) DO UPDATE SET
                    updated_at_utc = excluded.updated_at_utc
                """,
                (key, normalized_provider, now, now),
            )
            connection.commit()
        logger.info(
            "已记录外部文档处理提交意图: operation_key=%s provider=%s",
            key[:12],
            normalized_provider,
        )

    def record_provider_identity(
        self,
        *,
        operation_key: str,
        provider_operation_id: str,
    ) -> None:
        key = self._validate_key(operation_key)
        provider_id = self._required(
            provider_operation_id,
            "provider_operation_id",
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT provider_operation_id
                FROM document_processing_external_operations
                WHERE operation_key = ?
                """,
                (key,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise DocumentProcessingError(
                    "external_operation_intent_missing",
                    "外部操作供应商身份缺少先行提交意图",
                    outcome_unknown=True,
                )
            existing = row["provider_operation_id"]
            if existing and existing != provider_id:
                connection.rollback()
                raise DocumentProcessingError(
                    "external_operation_identity_conflict",
                    "外部操作已绑定不同供应商身份",
                    outcome_unknown=True,
                )
            connection.execute(
                """
                UPDATE document_processing_external_operations
                SET state = 'provider_identified',
                    provider_operation_id = ?,
                    updated_at_utc = ?
                WHERE operation_key = ?
                """,
                (provider_id, now, key),
            )
            connection.commit()
        logger.info(
            "已记录外部文档处理供应商身份: operation_key=%s "
            "provider_operation_id_present=true",
            key[:12],
        )

    def get(self, operation_key: str) -> ExternalOperationSnapshot | None:
        key = self._validate_key(operation_key)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT operation_key, provider, state, provider_operation_id
                FROM document_processing_external_operations
                WHERE operation_key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return None
        return ExternalOperationSnapshot(
            operation_key=row["operation_key"],
            provider=row["provider"],
            state=row["state"],
            provider_operation_id=row["provider_operation_id"],
        )

    def record_terminal(
        self,
        *,
        operation_key: str,
        state: str,
    ) -> None:
        """记录供应商明确终态；未知结果不允许伪造为终态。"""

        key = self._validate_key(operation_key)
        normalized_state = self._required(state, "state").lower()
        if normalized_state not in {"succeeded", "failed"}:
            raise ValueError("state 仅允许 succeeded/failed")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT state, provider_operation_id
                FROM document_processing_external_operations
                WHERE operation_key = ?
                """,
                (key,),
            ).fetchone()
            if row is None or not row["provider_operation_id"]:
                connection.rollback()
                raise DocumentProcessingError(
                    "external_operation_identity_missing",
                    "供应商终态缺少可对账的任务身份",
                    outcome_unknown=True,
                )
            existing_state = str(row["state"])
            if existing_state in {"succeeded", "failed"}:
                if existing_state != normalized_state:
                    connection.rollback()
                    raise DocumentProcessingError(
                        "external_operation_terminal_conflict",
                        "供应商终态与既有事实冲突",
                        outcome_unknown=True,
                    )
                connection.commit()
                return
            connection.execute(
                """
                UPDATE document_processing_external_operations
                SET state = ?, updated_at_utc = ?
                WHERE operation_key = ?
                """,
                (normalized_state, now, key),
            )
            connection.commit()
        logger.info(
            "已记录外部文档处理供应商终态: operation_key=%s state=%s",
            key[:12],
            normalized_state,
        )

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS document_processing_external_operations (
                    operation_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    state TEXT NOT NULL,
                    provider_operation_id TEXT NOT NULL DEFAULT '',
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _validate_key(value: str) -> str:
        normalized = str(value).strip().lower()
        if _SHA256.fullmatch(normalized) is None:
            raise ValueError("operation_key 必须是 64 位小写 SHA-256")
        return normalized

    @staticmethod
    def _required(value: object, name: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized


__all__ = [
    "ExternalOperationSnapshot",
    "SQLiteMinerUOperationObserver",
]
