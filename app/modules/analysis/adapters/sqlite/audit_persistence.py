"""Analysis v2 召回组件事实与共享交互审计的组合持久化适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
from typing import Any, Protocol, runtime_checkable

from app.infrastructure.observability.llm_interaction_store import LLMInteractionStore
from app.modules.analysis.adapters.legacy_audit import LegacyAnalysisAuditAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.adapters.sqlite.transaction import SQLiteTransactionManager
from app.modules.tasks.ports import TaskReadPort


logger = logging.getLogger(__name__)


@runtime_checkable
class _InteractionAuditStore(Protocol):
    """共享审计 Writer 的最小能力，便于离线 Fake 验证跨库边界。"""

    def create_llm_interaction_with_trace(self, **kwargs): ...
    def get_llm_interaction_by_execution(self, *args): ...
    def append_llm_interaction_lifecycle_events(self, *args, **kwargs): ...


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AnalysisRecallAuditWriteResult:
    execution_id: str
    created: bool
    reused: bool
    finalized: bool


class SQLiteAnalysisAuditPersistence:
    """满足 ``LegacyAnalysisAuditAdapter`` 的窄持久化协议。

    召回与派生交互引用写入 ``analysis_control``；Prompt、响应和来源正文继续只由共享
    ``LLMInteractionStore`` 写入根审计表。两者都不依赖 ``LLMTaskService``。
    """

    def __init__(
        self,
        transaction_manager: SQLiteTransactionManager,
        interaction_store: _InteractionAuditStore,
    ) -> None:
        if not isinstance(transaction_manager, SQLiteTransactionManager):
            raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
        if not isinstance(interaction_store, _InteractionAuditStore):
            raise TypeError("interaction_store 必须实现共享交互审计协议")
        self._transactions = transaction_manager
        self._interactions = interaction_store

    @staticmethod
    def _require_file_execution(connection, execution_id: str):
        row = connection.execute(
            """
            SELECT execution.business_key
            FROM llm_task_executions AS execution
            JOIN llm_tasks AS latest
              ON latest.business_type = execution.business_type
             AND latest.business_key = execution.business_key
             AND latest.execution_id = execution.execution_id
            WHERE execution.execution_id = ? AND execution.business_type = 'file'
            """,
            (execution_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Analysis 审计对应 execution 不存在或已不是 latest")
        return str(row["business_key"])

    def upsert_architecture_recall_decision(self, **kwargs) -> AnalysisRecallAuditWriteResult:
        execution_id = str(kwargs.pop("execution_id", "")).strip()
        if not execution_id or set(kwargs) != {
            "tree_fingerprint",
            "query_digest",
            "base_top64",
            "final_candidates",
            "channel_rankings",
            "rrf_scores",
            "protected_reasons",
            "prompt_chars",
            "recall_elapsed_ms",
        }:
            raise ValueError("Analysis 召回预留参数集合无效")
        tree_fingerprint = str(kwargs["tree_fingerprint"] or "").strip().lower()
        query_digest = str(kwargs["query_digest"] or "").strip().lower()
        for name, value, allow_empty in (
            ("tree_fingerprint", tree_fingerprint, True),
            ("query_digest", query_digest, False),
        ):
            if (allow_empty and not value):
                continue
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{name} 必须是 SHA-256")
        payload = dict(kwargs)
        payload["tree_fingerprint"] = tree_fingerprint
        payload["query_digest"] = query_digest
        decision_digest = _digest(payload)
        serialized = _canonical(payload)
        timestamp = _now()
        with self._transactions.begin(read_only=False) as transaction:
            connection = transaction.connection
            business_key = self._require_file_execution(connection, execution_id)
            existing = connection.execute(
                "SELECT decision_digest, outcome FROM analysis_recall_decisions "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if existing is not None:
                if existing["decision_digest"] != decision_digest:
                    raise ValueError("同一 execution 的 Analysis 召回决策发生幂等冲突")
                finalized = existing["outcome"] is not None
                transaction.commit()
                return AnalysisRecallAuditWriteResult(execution_id, False, True, finalized)
            connection.execute(
                """
                INSERT INTO analysis_recall_decisions (
                    execution_id, business_key, idempotency_key,
                    tree_fingerprint, query_digest, decision_digest,
                    reserve_payload_json, finalize_payload_json, outcome,
                    error_code, version, created_at, finalized_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, '', 1, ?, NULL)
                """,
                (
                    execution_id,
                    business_key,
                    f"analysis-recall:{execution_id}",
                    tree_fingerprint,
                    query_digest,
                    decision_digest,
                    serialized,
                    timestamp,
                ),
            )
            transaction.commit()
        return AnalysisRecallAuditWriteResult(execution_id, True, False, False)

    def finalize_architecture_recall_decision(self, **kwargs) -> AnalysisRecallAuditWriteResult:
        execution_id = str(kwargs.pop("execution_id", "")).strip()
        expected = {
            "returned_architecture_id",
            "returned_rank",
            "total_elapsed_ms",
            "failure_stage",
            "error_message",
        }
        if not execution_id or set(kwargs) != expected:
            raise ValueError("Analysis 召回终结参数集合无效")
        failure_stage = str(kwargs["failure_stage"] or "").strip()
        error_message = str(kwargs["error_message"] or "").strip()
        outcome = "failed" if failure_stage else "succeeded"
        if (outcome == "failed") != bool(error_message):
            raise ValueError("Analysis 召回失败阶段与错误信息必须同时存在")
        payload = dict(kwargs)
        payload["failure_stage"] = failure_stage or None
        payload["error_message"] = error_message
        serialized = _canonical(payload)
        timestamp = _now()
        with self._transactions.begin(read_only=False) as transaction:
            connection = transaction.connection
            self._require_file_execution(connection, execution_id)
            row = connection.execute(
                "SELECT finalize_payload_json, outcome FROM analysis_recall_decisions "
                "WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Analysis 召回终结缺少预留事实")
            if row["outcome"] is not None:
                if row["outcome"] != outcome or row["finalize_payload_json"] != serialized:
                    raise ValueError("Analysis 召回终态发生幂等冲突")
                transaction.commit()
                return AnalysisRecallAuditWriteResult(execution_id, False, True, True)
            cursor = connection.execute(
                """
                UPDATE analysis_recall_decisions
                SET finalize_payload_json = ?, outcome = ?, error_code = ?,
                    version = version + 1, finalized_at = ?
                WHERE execution_id = ? AND outcome IS NULL
                """,
                (serialized, outcome, error_message[:256], timestamp, execution_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Analysis 召回终结条件写未命中")
            transaction.commit()
        return AnalysisRecallAuditWriteResult(execution_id, False, False, True)

    def create_llm_interaction_with_trace(self, **kwargs):
        result = self._interactions.create_llm_interaction_with_trace(**kwargs)
        execution_id = str(kwargs["execution_id"])
        business_key = str(kwargs["business_key"])
        idempotency_key = str(kwargs["audit_idempotency_key"])
        outcome = str(kwargs["status"])
        audit_id = str(result.interaction_id)
        evidence_digest = _digest(
            {
                "audit_id": audit_id,
                "execution_id": execution_id,
                "idempotency_key": idempotency_key,
                "outcome": outcome,
            }
        )
        with self._transactions.begin(read_only=False) as transaction:
            connection = transaction.connection
            if self._require_file_execution(connection, execution_id) != business_key:
                raise ValueError("Analysis 交互审计业务身份不一致")
            existing = connection.execute(
                "SELECT audit_id, outcome, evidence_digest "
                "FROM analysis_interaction_audit_refs WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO analysis_interaction_audit_refs (
                        execution_id, business_key, idempotency_key, audit_id,
                        outcome, evidence_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        execution_id,
                        business_key,
                        idempotency_key,
                        audit_id,
                        outcome,
                        evidence_digest,
                        _now(),
                    ),
                )
            elif (
                str(existing["audit_id"]) != audit_id
                or str(existing["outcome"]) != outcome
                or str(existing["evidence_digest"]) != evidence_digest
            ):
                raise ValueError("Analysis 交互审计引用发生幂等冲突")
            transaction.commit()
        logger.info(
            "Analysis v2 交互审计引用已提交: task_id=%s audit_id=%s",
            execution_id,
            audit_id,
        )
        return result

    def get_llm_interaction_by_execution(self, *args):
        return self._interactions.get_llm_interaction_by_execution(*args)

    def append_llm_interaction_lifecycle_events(self, *args, **kwargs):
        return self._interactions.append_llm_interaction_lifecycle_events(
            *args,
            **kwargs,
        )


def build_analysis_v2_audit_adapter(
    audit_database_path: str | Path,
    *,
    transaction_manager: SQLiteTransactionManager,
    task_reader: TaskReadPort,
) -> LegacyAnalysisAuditAdapter:
    """构造 Analysis v2 审计边界，不再经由 ``LLMTaskService``。

    共享交互正文仍保存在既有审计数据库，Analysis Recall 与交互引用保存在 Task
    Control 组件库。两库之间没有伪原子事务；执行期由持久化 Step intent/checkpoint
    隔离跨库提交结果未知，重复调用依靠两侧稳定幂等键补齐引用。
    """

    if not isinstance(transaction_manager, SQLiteTransactionManager):
        raise TypeError("transaction_manager 必须是 SQLiteTransactionManager")
    if not isinstance(task_reader, TaskReadPort):
        raise TypeError("task_reader 必须实现 TaskReadPort")
    resolved = Path(audit_database_path).resolve()

    def connection_factory(timeout_seconds: float) -> sqlite3.Connection:
        connection = sqlite3.connect(resolved, timeout=max(0.0, timeout_seconds))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute(
            f"PRAGMA busy_timeout = {int(max(0.0, timeout_seconds) * 1000)}"
        )
        return connection

    def validate_identity(
        business_type: str,
        business_key: str,
        execution_id: str,
    ) -> bool:
        if business_type != "file":
            return False
        try:
            snapshot = task_reader.get_by_id(TaskId(execution_id))
        except (TypeError, ValueError):
            return False
        return (
            snapshot is not None
            and snapshot.business_ref == TaskBusinessRef("file", business_key)
        )

    interaction_store = LLMInteractionStore(
        connection_factory,
        task_identity_validator=validate_identity,
    )
    return LegacyAnalysisAuditAdapter(
        SQLiteAnalysisAuditPersistence(transaction_manager, interaction_store)
    )


__all__ = [
    "AnalysisRecallAuditWriteResult",
    "SQLiteAnalysisAuditPersistence",
    "build_analysis_v2_audit_adapter",
]
