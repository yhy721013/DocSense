"""文档 RAG 外部资源的持久化租约与审计失败恢复入口。

交互审计是业务成功的硬前置，但审计数据库写入失败时仍必须保留 AnythingLLM 现场。仅把
Context、Conversation 和全局文档引用保存在内存 Trace 中无法抵抗进程退出，因此本服务在
创建外部资源前登记执行租约，并在每个可定位资源出现后更新不透明引用。阶段 9 编排只需
使用这些原子方法，不接触表结构。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """返回可排序且带时区的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RagResourceLease:
    """一次文件任务持有的外部 RAG 资源租约快照。"""

    execution_id: str
    business_type: str
    business_key: str
    context_ref: str
    conversation_ref: str
    document_ref: str
    external_location: str
    status: str
    interaction_id: int | None
    last_error: str
    created_at: str
    updated_at: str


class RagResourceLeaseService:
    """在任务数据库中维护跨进程可巡检的 RAG 资源所有权租约。"""

    _OPEN_STATUSES = frozenset({"planned", "active", "audit_failed", "audited"})

    def __init__(self, db_path: str) -> None:
        """保存数据库路径并执行只增不删的租约表初始化。"""
        normalized_path = str(db_path or "").strip()
        if not normalized_path:
            raise ValueError("db_path 不能为空")
        self.db_path = normalized_path
        Path(normalized_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rag_resource_leases (
                    execution_id TEXT PRIMARY KEY,
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    context_ref TEXT NOT NULL DEFAULT '',
                    conversation_ref TEXT NOT NULL DEFAULT '',
                    document_ref TEXT NOT NULL DEFAULT '',
                    external_location TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (
                        status IN ('planned', 'active', 'audit_failed', 'audited', 'closed')
                    ),
                    interaction_id INTEGER,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rag_resource_leases_status
                ON rag_resource_leases (status, updated_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        """为当前原子操作创建独立 SQLite 连接。"""
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _required_text(value: str, *, name: str) -> str:
        """规范化租约身份中的必需文本。"""
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized

    @staticmethod
    def _row(row: sqlite3.Row) -> RagResourceLease:
        """把数据库行转换为不可变租约快照。"""
        return RagResourceLease(
            execution_id=row["execution_id"],
            business_type=row["business_type"],
            business_key=row["business_key"],
            context_ref=row["context_ref"],
            conversation_ref=row["conversation_ref"],
            document_ref=row["document_ref"],
            external_location=row["external_location"],
            status=row["status"],
            interaction_id=row["interaction_id"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def begin(self, *, execution_id: str, business_type: str, business_key: str) -> None:
        """在创建 AnythingLLM 资源前幂等登记计划租约。"""
        identity = (
            self._required_text(execution_id, name="execution_id"),
            self._required_text(business_type, name="business_type"),
            self._required_text(business_key, name="business_key"),
        )
        now = _utc_now_iso()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM rag_resource_leases WHERE execution_id = ?",
                (identity[0],),
            ).fetchone()
            if existing is not None:
                if (existing["business_type"], existing["business_key"]) != identity[1:]:
                    raise ValueError("execution_id 对应的 RAG 租约业务身份冲突")
                if existing["status"] == "closed":
                    raise ValueError("已关闭的 execution_id 不得重新创建 RAG 资源")
                return
            connection.execute(
                """
                INSERT INTO rag_resource_leases (
                    execution_id, business_type, business_key, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'planned', ?, ?)
                """,
                (*identity, now, now),
            )

    def record_resources(
        self,
        *,
        execution_id: str,
        context_ref: str = "",
        conversation_ref: str = "",
        document_ref: str = "",
        external_location: str = "",
    ) -> None:
        """保存当前已经取得的外部引用，空参数不会覆盖先前进度。"""
        normalized_execution = self._required_text(execution_id, name="execution_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM rag_resource_leases WHERE execution_id = ?",
                (normalized_execution,),
            ).fetchone()
            if row is None:
                raise ValueError("RAG 资源租约不存在")
            if row["status"] not in {"planned", "active"}:
                raise ValueError("已进入审计终态的租约不得重新登记资源")
            connection.execute(
                """
                UPDATE rag_resource_leases
                SET context_ref = ?, conversation_ref = ?, document_ref = ?,
                    external_location = ?, status = 'active', updated_at = ?
                WHERE execution_id = ?
                """,
                (
                    str(context_ref or "").strip() or row["context_ref"],
                    str(conversation_ref or "").strip() or row["conversation_ref"],
                    str(document_ref or "").strip() or row["document_ref"],
                    str(external_location or "").strip() or row["external_location"],
                    _utc_now_iso(),
                    normalized_execution,
                ),
            )

    def mark_audit_result(
        self,
        *,
        execution_id: str,
        interaction_id: int | None,
        error_message: str = "",
    ) -> None:
        """记录审计成功凭据或失败原因，失败租约保持可恢复开放状态。"""
        normalized_execution = self._required_text(execution_id, name="execution_id")
        succeeded = (
            isinstance(interaction_id, int)
            and not isinstance(interaction_id, bool)
            and interaction_id > 0
        )
        normalized_error = str(error_message or "").strip()
        if not succeeded and not normalized_error:
            raise ValueError("审计失败租约必须包含 error_message")
        if succeeded and normalized_error:
            raise ValueError("审计成功租约不得包含 error_message")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM rag_resource_leases WHERE execution_id = ?",
                (normalized_execution,),
            ).fetchone()
            if existing is None or existing["status"] == "closed":
                raise ValueError("RAG 资源租约不存在或已经关闭")
            if existing["status"] == "audited":
                if succeeded and existing["interaction_id"] == interaction_id:
                    return
                raise ValueError("已提交的审计成功租约不得被覆盖")
            cursor = connection.execute(
                """
                UPDATE rag_resource_leases
                SET status = ?, interaction_id = ?, last_error = ?, updated_at = ?
                WHERE execution_id = ? AND status != 'closed'
                """,
                (
                    "audited" if succeeded else "audit_failed",
                    interaction_id if succeeded else None,
                    normalized_error,
                    _utc_now_iso(),
                    normalized_execution,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("RAG 资源租约不存在或已经关闭")
        logger.log(
            logging.INFO if succeeded else logging.ERROR,
            "RAG 资源租约审计状态已更新: execution_id=%s status=%s "
            "interaction_id=%s error_chars=%d",
            normalized_execution,
            "audited" if succeeded else "audit_failed",
            interaction_id if succeeded else None,
            len("" if succeeded else normalized_error),
        )

    def mark_closed(
        self,
        *,
        execution_id: str,
        error_message: str = "",
        manual_recovery: bool = False,
    ) -> None:
        """在审计成功后终结租约；审计失败只能由显式人工恢复关闭。"""
        if not isinstance(manual_recovery, bool):
            raise TypeError("manual_recovery 必须是 bool")
        normalized_execution = self._required_text(execution_id, name="execution_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM rag_resource_leases WHERE execution_id = ?",
                (normalized_execution,),
            ).fetchone()
            if row is None:
                raise ValueError("RAG 资源租约不存在")
            if row["status"] != "audited" and not manual_recovery:
                raise ValueError("审计未成功的资源租约只能通过人工恢复关闭")
            cursor = connection.execute(
                """
                UPDATE rag_resource_leases
                SET status = 'closed', last_error = ?, updated_at = ?
                WHERE execution_id = ?
                """,
                (str(error_message or "").strip(), _utc_now_iso(), normalized_execution),
            )
            if cursor.rowcount != 1:
                raise ValueError("RAG 资源租约关闭失败")

    def record_cleanup_failure(
        self,
        *,
        execution_id: str,
        error_message: str,
    ) -> None:
        """记录审计成功后的关闭失败，并保持租约处于可巡检开放状态。

        ``session.close`` 失败意味着外部 Context 或全局文档可能仍然存在，不能把租约推进
        到 ``closed``。本方法只允许更新 ``audited`` 租约的错误信息，使 ``list_open`` 继续
        返回该记录，供补偿任务或人工恢复处理。
        """
        normalized_execution = self._required_text(execution_id, name="execution_id")
        normalized_error = self._required_text(error_message, name="error_message")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE rag_resource_leases
                SET last_error = ?, updated_at = ?
                WHERE execution_id = ? AND status = 'audited'
                """,
                (normalized_error, _utc_now_iso(), normalized_execution),
            )
            if cursor.rowcount != 1:
                raise ValueError("只有审计成功且尚未关闭的租约可以记录清理失败")
        logger.warning(
            "RAG 资源租约已记录清理失败，等待后续恢复: "
            "execution_id=%s status=audited error_chars=%d",
            normalized_execution,
            len(normalized_error),
        )

    def list_open(self) -> list[RagResourceLease]:
        """按更新时间列出仍需审计、清理或人工恢复的资源租约。"""
        placeholders = ",".join("?" for _ in self._OPEN_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM rag_resource_leases WHERE status IN ({placeholders}) "
                "ORDER BY updated_at ASC",
                tuple(sorted(self._OPEN_STATUSES)),
            ).fetchall()
        return [self._row(row) for row in rows]
