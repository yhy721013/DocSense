from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence
from uuid import uuid4

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
from app.services.llm_service.knowledge_index_operation_service import (
    KnowledgeIndexOperationService,
)
from app.services.llm_service.rag_resource_lease_service import (
    RagResourceLeaseService,
)
from app.services.core.progress import normalize_progress
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


class TaskAlreadyProcessingError(RuntimeError):
    """同一文件已有未结束执行时拒绝创建新任务。"""

    def __init__(self, business_key: str, status: str):
        self.business_key = business_key
        self.status = status
        super().__init__(f"任务正在处理中: {business_key}")


class TaskAdmissionBusyError(RuntimeError):
    """任务库在受理时持续繁忙，调用方可安全稍后重试。"""


class TaskExecutionConflictError(RuntimeError):
    """任务写入携带的执行身份已不是当前业务键对应的执行。"""

    def __init__(
        self,
        business_type: str,
        business_key: str,
        execution_id: str,
    ):
        self.business_type = business_type
        self.business_key = business_key
        self.execution_id = execution_id
        super().__init__(
            "任务执行身份已失效: "
            f"business_type={business_type}, business_key={business_key}, "
            f"execution_id={execution_id}"
        )


class TaskStateConflictError(RuntimeError):
    """当前执行已处于不允许本次操作的业务状态。"""

    def __init__(
        self,
        business_type: str,
        business_key: str,
        execution_id: str,
        status: str,
    ):
        self.business_type = business_type
        self.business_key = business_key
        self.execution_id = execution_id
        self.status = status
        super().__init__(
            "任务状态不允许当前操作: "
            f"business_type={business_type}, business_key={business_key}, "
            f"execution_id={execution_id}, status={status}"
        )


ARCHITECTURE_RECALL_FAILURE_STAGES = frozenset(
    {
        "architecture_index",
        "architecture_recall",
        "architecture_prompt_budget",
        "architecture_contract",
        "analysis_extraction",
    }
)
"""领域召回与两阶段分类链路允许持久化的稳定失败阶段。"""

MAX_ARCHITECTURE_RECALL_EXECUTION_ID_CHARS = 128
MAX_ARCHITECTURE_RECALL_BASE_CANDIDATES = 64
MAX_ARCHITECTURE_RECALL_FINAL_CANDIDATES = 128
MAX_ARCHITECTURE_RECALL_CHANNELS = 16
MAX_ARCHITECTURE_RECALL_CHANNEL_CANDIDATES = 512
MAX_ARCHITECTURE_RECALL_RRF_SCORES = 4096
MAX_ARCHITECTURE_RECALL_PROTECTED_CANDIDATES = 128
MAX_ARCHITECTURE_RECALL_PROTECTED_REASONS_PER_CANDIDATE = 8
MAX_ARCHITECTURE_RECALL_REASON_CHARS = 512
MAX_ARCHITECTURE_RECALL_PATH_CHARS = 2048
MAX_ARCHITECTURE_RECALL_NODE_TYPE_CHARS = 32
MAX_ARCHITECTURE_RECALL_REMARK_CHARS = 512
MAX_ARCHITECTURE_RECALL_JSON_CHARS = 512_000
MAX_ARCHITECTURE_RECALL_PROMPT_CHARS = 2_000_000
MAX_ARCHITECTURE_RECALL_ELAPSED_MS = 86_400_000
MAX_ARCHITECTURE_RECALL_ERROR_CHARS = 4096

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_SQLITE_INTEGER = (1 << 63) - 1


class ArchitectureRecallAuditError(RuntimeError):
    """领域召回决策无法安全持久化时抛出的稳定应用异常。"""

    stage = "architecture_recall_audit"


@dataclass(frozen=True)
class ArchitectureRecallAuditWriteResult:
    """领域召回审计写入的幂等结果。"""

    execution_id: str
    created: bool
    reused: bool
    finalized: bool

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("execution_id不能为空")
        if self.created == self.reused:
            raise ValueError("created与reused必须且只能有一个为True")


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
        # 永久知识库协调记录与任务、交互审计共用同一 SQLite 文件，但由独立服务维护，
        # 避免继续扩大 LLMTaskService 的职责。该服务只在构造期建表，不创建长期连接。
        self.knowledge_index_operations = KnowledgeIndexOperationService(db_path)
        # 资源租约必须先于阶段 9 的外部 Session 创建。即使最终交互审计失败，独立租约仍
        # 提供 Context、Conversation 和全局文档的巡检入口，避免只依赖进程内 Trace。
        self.rag_resource_leases = RagResourceLeaseService(db_path)

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS weaponry_task_document_snapshots (
                    business_key TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 1),
                    file_name TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    ingested_file_name TEXT NOT NULL DEFAULT '',
                    source_architecture_id INTEGER NOT NULL
                        CHECK (source_architecture_id >= 1),
                    doc_path TEXT NOT NULL,
                    anything_doc_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (business_key, sequence_no),
                    UNIQUE (business_key, file_name),
                    UNIQUE (business_key, doc_path)
                )
                """
            )
            self._ensure_column(
                conn,
                table="weaponry_task_document_snapshots",
                column="ingested_file_name",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_weaponry_task_document_snapshots_execution
                ON weaponry_task_document_snapshots (execution_id, sequence_no)
                """
            )
            # 召回决策按 execution_id 独立留存，故意不关联 llm_tasks 外键。同一业务键
            # 重跑会替换 llm_tasks 当前 execution_id；若在此处使用 ON DELETE CASCADE，
            # 历史分类证据会随任务重跑丢失，无法用于 E2E 取证和线上复盘。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_architecture_recall_decisions (
                    execution_id TEXT PRIMARY KEY,
                    tree_fingerprint TEXT NOT NULL,
                    query_digest TEXT NOT NULL,
                    decision_digest TEXT NOT NULL,
                    base_top64_json TEXT NOT NULL,
                    final_candidates_json TEXT NOT NULL,
                    channel_rankings_json TEXT NOT NULL,
                    rrf_scores_json TEXT NOT NULL,
                    protected_reasons_json TEXT NOT NULL,
                    prompt_chars INTEGER NOT NULL CHECK (prompt_chars >= 0),
                    recall_elapsed_ms INTEGER NOT NULL CHECK (recall_elapsed_ms >= 0),
                    returned_architecture_id INTEGER,
                    returned_rank INTEGER,
                    total_elapsed_ms INTEGER,
                    failure_stage TEXT,
                    error_message TEXT NOT NULL DEFAULT '',
                    finalization_digest TEXT,
                    finalized_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (
                        (returned_architecture_id IS NULL AND returned_rank IS NULL)
                        OR
                        (returned_architecture_id >= 1 AND returned_rank >= 1)
                    ),
                    CHECK (total_elapsed_ms IS NULL OR total_elapsed_ms >= 0)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_architecture_recall_created_at
                ON llm_architecture_recall_decisions (created_at)
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

    @staticmethod
    def _normalize_recall_execution_id(execution_id: Any) -> str:
        normalized = str(execution_id or "").strip()
        if not normalized:
            raise ValueError("execution_id不能为空")
        if len(normalized) > MAX_ARCHITECTURE_RECALL_EXECUTION_ID_CHARS:
            raise ValueError("execution_id超出召回审计长度上限")
        return normalized

    @staticmethod
    def _normalize_recall_digest(
        value: Any,
        *,
        field_name: str,
        allow_empty: bool = False,
    ) -> str:
        normalized = str(value or "").strip().lower()
        if allow_empty and not normalized:
            return ""
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError(f"{field_name}必须是64位SHA-256十六进制摘要")
        return normalized

    @staticmethod
    def _normalize_recall_positive_id(value: Any, *, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field_name}必须是正整数")
        if isinstance(value, int):
            normalized = value
        elif isinstance(value, str) and value.isascii() and value.isdigit():
            normalized = int(value)
        else:
            raise ValueError(f"{field_name}必须是正整数")
        if normalized < 1 or normalized > _MAX_SQLITE_INTEGER:
            raise ValueError(f"{field_name}超出SQLite正整数范围")
        return normalized

    @staticmethod
    def _normalize_recall_non_negative_int(
        value: Any,
        *,
        field_name: str,
        upper_bound: int,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field_name}必须是非负整数")
        if value < 0 or value > upper_bound:
            raise ValueError(f"{field_name}超出允许范围")
        return value

    @classmethod
    def _normalize_recall_ranked_ids(
        cls,
        values: Sequence[Any],
        *,
        field_name: str,
        max_items: int,
    ) -> list[int]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise TypeError(f"{field_name}必须是ID序列")
        if len(values) > max_items:
            raise ValueError(f"{field_name}数量超出上限{max_items}")
        normalized: list[int] = []
        seen: set[int] = set()
        for raw_id in values:
            node_id = cls._normalize_recall_positive_id(
                raw_id,
                field_name=f"{field_name}节点ID",
            )
            if node_id in seen:
                raise ValueError(f"{field_name}存在重复节点ID")
            seen.add(node_id)
            normalized.append(node_id)
        return normalized

    @classmethod
    def _normalize_recall_final_candidates(
        cls,
        candidates: Sequence[Mapping[str, Any]],
    ) -> list[Dict[str, Any]]:
        if isinstance(candidates, (str, bytes)) or not isinstance(
            candidates,
            Sequence,
        ):
            raise TypeError("final_candidates必须是Mapping序列")
        if len(candidates) > MAX_ARCHITECTURE_RECALL_FINAL_CANDIDATES:
            raise ValueError(
                "final_candidates数量超出上限"
                f"{MAX_ARCHITECTURE_RECALL_FINAL_CANDIDATES}"
            )

        allowed_fields = {"id", "pathName", "nodeType", "remark"}
        normalized: list[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping):
                raise TypeError("final_candidates只能包含Mapping")
            unknown_fields = set(candidate) - allowed_fields
            if unknown_fields:
                raise ValueError(
                    "final_candidates包含非模型投影字段: "
                    + ",".join(sorted(str(item) for item in unknown_fields))
                )
            node_id = cls._normalize_recall_positive_id(
                candidate.get("id"),
                field_name=f"final_candidates[{index}].id",
            )
            if node_id in seen_ids:
                raise ValueError("final_candidates存在重复节点ID")
            seen_ids.add(node_id)

            path_name = str(candidate.get("pathName") or "").strip()
            if not path_name:
                raise ValueError(f"final_candidates[{index}].pathName不能为空")
            if len(path_name) > MAX_ARCHITECTURE_RECALL_PATH_CHARS:
                raise ValueError(f"final_candidates[{index}].pathName超出长度上限")
            node_type = str(candidate.get("nodeType") or "").strip()
            if not node_type:
                raise ValueError(f"final_candidates[{index}].nodeType不能为空")
            if len(node_type) > MAX_ARCHITECTURE_RECALL_NODE_TYPE_CHARS:
                raise ValueError(f"final_candidates[{index}].nodeType超出长度上限")
            if node_type not in {"leaf", "parent"}:
                raise ValueError(
                    f"final_candidates[{index}].nodeType只能是leaf或parent"
                )

            item: Dict[str, Any] = {
                "id": node_id,
                "pathName": path_name,
                "nodeType": node_type,
            }
            if "remark" in candidate and candidate.get("remark") is not None:
                remark = str(candidate.get("remark") or "").strip()
                if len(remark) > MAX_ARCHITECTURE_RECALL_REMARK_CHARS:
                    raise ValueError(f"final_candidates[{index}].remark超出长度上限")
                if remark:
                    item["remark"] = remark
            normalized.append(item)
        return normalized

    @classmethod
    def _normalize_recall_channel_rankings(
        cls,
        channel_rankings: Mapping[str, Sequence[Any]],
    ) -> Dict[str, list[int]]:
        if not isinstance(channel_rankings, Mapping):
            raise TypeError("channel_rankings必须是Mapping")
        if len(channel_rankings) > MAX_ARCHITECTURE_RECALL_CHANNELS:
            raise ValueError(
                "channel_rankings通道数量超出上限"
                f"{MAX_ARCHITECTURE_RECALL_CHANNELS}"
            )
        normalized: Dict[str, list[int]] = {}
        for raw_name, ranked_ids in channel_rankings.items():
            channel_name = str(raw_name or "").strip()
            if not channel_name:
                raise ValueError("channel_rankings通道名不能为空")
            if len(channel_name) > 64:
                raise ValueError("channel_rankings通道名超出长度上限")
            if channel_name in normalized:
                raise ValueError("channel_rankings规范化后存在重复通道名")
            normalized[channel_name] = cls._normalize_recall_ranked_ids(
                ranked_ids,
                field_name=f"channel_rankings[{channel_name}]",
                max_items=MAX_ARCHITECTURE_RECALL_CHANNEL_CANDIDATES,
            )
        return normalized

    @classmethod
    def _normalize_recall_rrf_scores(
        cls,
        rrf_scores: Mapping[Any, Any],
    ) -> Dict[str, float]:
        if not isinstance(rrf_scores, Mapping):
            raise TypeError("rrf_scores必须是Mapping")
        if len(rrf_scores) > MAX_ARCHITECTURE_RECALL_RRF_SCORES:
            raise ValueError("rrf_scores数量超出上限")
        normalized: Dict[str, float] = {}
        for raw_id, raw_score in rrf_scores.items():
            node_id = cls._normalize_recall_positive_id(
                raw_id,
                field_name="rrf_scores节点ID",
            )
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise ValueError("rrf_scores分数必须是有限非负数")
            score = float(raw_score)
            if not math.isfinite(score) or score < 0.0 or score > 1000.0:
                raise ValueError("rrf_scores分数必须是有限非负数")
            if str(node_id) in normalized:
                raise ValueError("rrf_scores规范化后存在重复节点ID")
            normalized[str(node_id)] = score
        return normalized

    @classmethod
    def _normalize_recall_protected_reasons(
        cls,
        protected_reasons: Mapping[Any, Sequence[Any]],
        *,
        final_candidate_ids: set[int],
    ) -> Dict[str, list[str]]:
        if not isinstance(protected_reasons, Mapping):
            raise TypeError("protected_reasons必须是Mapping")
        if len(protected_reasons) > MAX_ARCHITECTURE_RECALL_PROTECTED_CANDIDATES:
            raise ValueError("protected_reasons节点数量超出上限")

        normalized: Dict[str, list[str]] = {}
        for raw_id, raw_reasons in protected_reasons.items():
            node_id = cls._normalize_recall_positive_id(
                raw_id,
                field_name="protected_reasons节点ID",
            )
            if node_id not in final_candidate_ids:
                raise ValueError("protected_reasons只能引用最终模型候选")
            if str(node_id) in normalized:
                raise ValueError("protected_reasons规范化后存在重复节点ID")
            if isinstance(raw_reasons, (str, bytes)) or not isinstance(
                raw_reasons,
                Sequence,
            ):
                raise TypeError("protected_reasons中的原因必须是字符串序列")
            if len(raw_reasons) > MAX_ARCHITECTURE_RECALL_PROTECTED_REASONS_PER_CANDIDATE:
                raise ValueError("protected_reasons单节点原因数量超出上限")
            reasons: list[str] = []
            seen: set[str] = set()
            for raw_reason in raw_reasons:
                reason = str(raw_reason or "").strip()
                if not reason:
                    raise ValueError("protected_reasons原因不能为空")
                if len(reason) > MAX_ARCHITECTURE_RECALL_REASON_CHARS:
                    raise ValueError("protected_reasons原因超出长度上限")
                if reason in seen:
                    raise ValueError("protected_reasons存在重复原因")
                seen.add(reason)
                reasons.append(reason)
            normalized[str(node_id)] = reasons
        return normalized

    def _serialize_recall_json(self, value: Any, *, field_name: str) -> str:
        serialized = self._serialize(value)
        if len(serialized) > MAX_ARCHITECTURE_RECALL_JSON_CHARS:
            raise ValueError(f"{field_name}序列化后超出召回审计长度上限")
        return serialized

    @staticmethod
    def _recall_payload_digest(payload: Mapping[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _run_recall_audit_write(
        self,
        *,
        operation: str,
        writer: Any,
    ) -> Any:
        try:
            return self._audit_executor.run(operation=operation, writer=writer)
        except InteractionAuditError as exc:
            message = str(exc).replace("交互审计", "领域召回审计")
            raise ArchitectureRecallAuditError(message) from exc

    def _row_to_task(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "execution_id": row["execution_id"],
            "request_payload": self._deserialize(row["request_payload"]),
            "status": row["status"],
            "progress": normalize_progress(row["progress"]),
            "message": row["message"],
            "result_payload": self._deserialize(row["result_payload"]),
            "callback_status": row["callback_status"],
            "callback_attempts": row["callback_attempts"],
            "last_callback_error": row["last_callback_error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _normalize_weaponry_selection_snapshot(
        selected_documents: Sequence[Mapping[str, Any]] | None,
    ) -> tuple[Dict[str, Any], ...]:
        """校验并冻结 weaponry 显式选文的内部持久化快照。

        该快照与外部请求参数隔离：它仅保存受理时已经唯一解析出的本地文件身份、来源
        分类、AnythingLLM 文档位置和实际上传文件名。任务重跑会在同一事务中替换旧
        快照，避免新旧执行共享一份可变选文范围。
        """
        if selected_documents is None:
            return ()
        if isinstance(selected_documents, (str, bytes)) or not isinstance(
            selected_documents,
            Sequence,
        ):
            raise TypeError("selected_documents必须是Mapping序列")

        normalized: list[Dict[str, Any]] = []
        seen_file_names: set[str] = set()
        seen_doc_paths: set[str] = set()
        for index, item in enumerate(selected_documents):
            if not isinstance(item, Mapping):
                raise TypeError("selected_documents只能包含Mapping")
            file_name = str(item.get("file_name") or "").strip()
            if not file_name:
                raise ValueError("weaponry任务文档快照缺少file_name")
            # 任务快照必须保留请求 originalFileName 的原值。只以 strip 判空，避免任务
            # 异步执行时把业务展示名改写为标准化名称。
            requested_original_name = str(item.get("original_name") or "")
            original_name = (
                requested_original_name
                if requested_original_name.strip()
                else file_name
            )
            ingested_file_name = (
                str(item.get("ingested_file_name") or "")
                .replace("\\", "/")
                .rsplit("/", 1)[-1]
                .strip()
            )
            if not ingested_file_name or ingested_file_name in {".", ".."}:
                raise ValueError("weaponry任务文档快照的ingested_file_name无效")
            raw_architecture_id = item.get("source_architecture_id")
            if isinstance(raw_architecture_id, bool):
                raise ValueError("weaponry任务文档快照的source_architecture_id无效")
            try:
                source_architecture_id = int(raw_architecture_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "weaponry任务文档快照的source_architecture_id无效"
                ) from exc
            if source_architecture_id < 1:
                raise ValueError("weaponry任务文档快照的source_architecture_id无效")
            doc_path = str(item.get("doc_path") or "").strip()
            if not doc_path:
                raise ValueError("weaponry任务文档快照缺少doc_path")
            if file_name in seen_file_names:
                raise ValueError("weaponry任务文档快照存在重复file_name")
            if doc_path in seen_doc_paths:
                raise ValueError("weaponry任务文档快照存在重复doc_path")
            seen_file_names.add(file_name)
            seen_doc_paths.add(doc_path)
            normalized.append(
                {
                    "file_name": file_name,
                    "original_name": original_name,
                    "ingested_file_name": ingested_file_name,
                    "source_architecture_id": source_architecture_id,
                    "doc_path": doc_path,
                    "anything_doc_id": str(item.get("anything_doc_id") or "").strip(),
                }
            )
        return tuple(normalized)

    @staticmethod
    def _replace_weaponry_selection_snapshot(
        conn: sqlite3.Connection,
        *,
        business_key: str,
        execution_id: str,
        selected_documents: Sequence[Mapping[str, Any]],
    ) -> None:
        """在任务写事务内替换同一类别上一轮执行的显式选文快照。"""
        conn.execute(
            "DELETE FROM weaponry_task_document_snapshots WHERE business_key = ?",
            (business_key,),
        )
        for sequence_no, document in enumerate(selected_documents, start=1):
            conn.execute(
                """
                INSERT INTO weaponry_task_document_snapshots (
                    business_key, execution_id, sequence_no, file_name, original_name,
                    ingested_file_name, source_architecture_id, doc_path, anything_doc_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    business_key,
                    execution_id,
                    sequence_no,
                    document["file_name"],
                    document["original_name"],
                    document["ingested_file_name"],
                    document["source_architecture_id"],
                    document["doc_path"],
                    document["anything_doc_id"],
                ),
            )

    def _upsert_task(
        self,
        business_type: str,
        business_key: str,
        request_payload: Dict[str, Any],
        status: str,
        *,
        weaponry_selection_snapshot: Sequence[Mapping[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """创建一次新执行，并在同一事务内返回本次写入的任务快照。

        即使业务键已存在，主动提交仍代表一次新执行，因此必须更新 ``execution_id`` 并
        重置结果和回调状态。读取必须发生在写事务提交前；若提交后重新查询，并发重跑可能
        已经覆盖同一业务键，调用方会错误拿到另一执行的身份。
        """
        if business_type == "weaponry":
            normalized_weaponry_snapshot = self._normalize_weaponry_selection_snapshot(
                weaponry_selection_snapshot,
            )
        elif weaponry_selection_snapshot is not None:
            raise ValueError("仅weaponry任务允许保存选中文档快照")
        else:
            normalized_weaponry_snapshot = ()

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
            if business_type == "weaponry":
                self._replace_weaponry_selection_snapshot(
                    conn,
                    business_key=business_key,
                    execution_id=execution_id,
                    selected_documents=normalized_weaponry_snapshot,
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
            "任务已创建或更新: business_type=%s business_key=%s execution_id=%s status=%s",
            business_type,
            business_key,
            execution_id,
            status,
        )
        if business_type == "weaponry":
            # 空快照表示 filePathList 缺省或为空，执行器会保持“当前类别全部文件”的
            # 既有语义；非空快照才是跨分类显式选文的可恢复任务输入。
            logger.info(
                "weaponry任务文档范围快照已更新: architecture_id=%s "
                "execution_id=%s explicit_file_count=%d",
                business_key,
                execution_id,
                len(normalized_weaponry_snapshot),
            )
        return task

    def create_file_tasks_if_available(
        self,
        file_tasks: Sequence[tuple[str, Dict[str, Any], str]],
    ) -> list[Dict[str, Any]]:
        """在一个写事务内原子受理一批文件任务。

        任一业务键仍处于 ``0``/``1`` 时整批回滚，避免路由层“先查后写”的竞态让两个
        请求同时获得 202。返回值是在同一事务内读取的各执行快照，调用方必须把其中的
        ``execution_id`` 传给后台执行器。
        """
        if not file_tasks:
            raise ValueError("file_tasks不能为空")

        normalized_tasks: list[tuple[str, str, str, str, str]] = []
        seen_file_names: set[str] = set()
        for file_name, request_payload, status in file_tasks:
            normalized_name = str(file_name or "").strip()
            if not normalized_name:
                raise ValueError("file_name不能为空")
            if normalized_name in seen_file_names:
                raise ValueError(f"file_tasks包含重复file_name: {normalized_name}")
            if not isinstance(request_payload, dict):
                raise TypeError("request_payload必须是对象")
            normalized_status = str(status)
            if normalized_status not in {"0", "1"}:
                raise ValueError("新文件任务status只能是0或1")
            seen_file_names.add(normalized_name)
            now = _utc_now_iso()
            normalized_tasks.append(
                (
                    normalized_name,
                    self._serialize(request_payload),
                    normalized_status,
                    uuid4().hex,
                    now,
                )
            )

        snapshots: list[Dict[str, Any]] = []
        try:
            with self._connection() as conn:
                # 在读 active 状态前先取得写保留锁，避免两个连接都通过检查后再竞争写入。
                # JSON 序列化和 UUID 生成已经在锁外完成，锁内只保留有界短 SQL。
                conn.execute("BEGIN IMMEDIATE")
                for file_name, _, _, _, _ in normalized_tasks:
                    active = conn.execute(
                        """
                        SELECT status
                        FROM llm_tasks
                        WHERE business_type = 'file' AND business_key = ?
                          AND status IN ('0', '1')
                        """,
                        (file_name,),
                    ).fetchone()
                    if active is not None:
                        raise TaskAlreadyProcessingError(
                            file_name,
                            str(active["status"]),
                        )

                for (
                    file_name,
                    serialized_payload,
                    status,
                    execution_id,
                    now,
                ) in normalized_tasks:
                    cursor = conn.execute(
                        """
                        INSERT INTO llm_tasks (
                            business_type, business_key, execution_id, request_payload,
                            status, progress, message,
                            result_payload, callback_status, callback_attempts,
                            last_callback_error, created_at, updated_at
                        )
                        VALUES ('file', ?, ?, ?, ?, 0.0, '', NULL, 'pending', 0, '', ?, ?)
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
                        WHERE llm_tasks.status NOT IN ('0', '1')
                        """,
                        (
                            file_name,
                            execution_id,
                            serialized_payload,
                            status,
                            now,
                            now,
                        ),
                    )
                    if cursor.rowcount != 1:
                        active = conn.execute(
                            """
                            SELECT status
                            FROM llm_tasks
                            WHERE business_type = 'file' AND business_key = ?
                            """,
                            (file_name,),
                        ).fetchone()
                        raise TaskAlreadyProcessingError(
                            file_name,
                            str(active["status"]) if active is not None else "",
                        )
                    row = conn.execute(
                        """
                        SELECT business_type, business_key, execution_id, request_payload,
                               status, progress, message, result_payload, callback_status,
                               callback_attempts, last_callback_error, created_at, updated_at
                        FROM llm_tasks
                        WHERE business_type = 'file' AND business_key = ?
                        """,
                        (file_name,),
                    ).fetchone()
                    if row is None or row["execution_id"] != execution_id:
                        raise RuntimeError("文件任务写入完成后未能读取本次执行快照")
                    snapshots.append(self._row_to_task(row))
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise TaskAdmissionBusyError("任务库繁忙，请稍后重试") from exc
            raise

        for task in snapshots:
            logger.info(
                "文件任务已原子受理: business_key=%s execution_id=%s status=%s",
                task["business_key"],
                task["execution_id"],
                task["status"],
            )
        return snapshots

    def create_file_task(
        self,
        file_name: str,
        request_payload: Dict[str, Any],
        status: str = "1",
    ) -> Dict[str, Any]:
        return self.create_file_tasks_if_available(
            ((file_name, request_payload, status),)
        )[0]

    def create_report_task(self, report_id: int, request_payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._upsert_task("report", str(report_id), request_payload, status="0")

    def create_weaponry_task(
        self,
        architecture_id: int,
        request_payload: Dict[str, Any],
        *,
        selected_documents: Sequence[Mapping[str, Any]] = (),
    ) -> Dict[str, Any]:
        """创建 weaponry 任务，并原子保存非空 filePathList 的内部解析快照。"""
        return self._upsert_task(
            "weaponry",
            str(architecture_id),
            request_payload,
            status="1",
            weaponry_selection_snapshot=selected_documents,
        )

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

    def require_current_execution(
        self,
        business_type: str,
        business_key: str,
        execution_id: str,
        *,
        allowed_statuses: Sequence[str] | None = None,
    ) -> Dict[str, Any]:
        """确认业务键仍绑定指定执行，并可选限制当前状态。

        本方法用于外部副作用前的执行身份门禁；它本身不替代后续数据库写入的 CAS。
        文件任务在 ``0``/``1`` 状态期间不能被原子受理入口替换，因此该门禁与受理规则
        共同保证远端 Session、永久知识库和回调不会由已失效 worker 发起。
        """
        expected_execution_id = str(execution_id or "").strip()
        if not expected_execution_id:
            raise ValueError("execution_id不能为空")
        task = self.get_task(business_type, business_key)
        if task is None or task["execution_id"] != expected_execution_id:
            raise TaskExecutionConflictError(
                business_type,
                business_key,
                expected_execution_id,
            )
        if allowed_statuses is not None:
            normalized_statuses = {str(status) for status in allowed_statuses}
            if task["status"] not in normalized_statuses:
                raise TaskStateConflictError(
                    business_type,
                    business_key,
                    expected_execution_id,
                    task["status"],
                )
        return task

    def get_tasks(self, business_type: str, business_keys: list[str]) -> list[Dict[str, Any]]:
        tasks: list[Dict[str, Any]] = []
        for business_key in business_keys:
            task = self.get_task(business_type, business_key)
            if task is not None:
                tasks.append(task)
        return tasks

    def upsert_architecture_recall_decision(
        self,
        *,
        execution_id: str,
        tree_fingerprint: str,
        query_digest: str,
        base_top64: Sequence[Any],
        final_candidates: Sequence[Mapping[str, Any]],
        channel_rankings: Mapping[str, Sequence[Any]],
        rrf_scores: Mapping[Any, Any],
        protected_reasons: Mapping[Any, Sequence[Any]],
        prompt_chars: int,
        recall_elapsed_ms: int,
    ) -> ArchitectureRecallAuditWriteResult:
        """在创建远端 RAG Session 前幂等保存完整召回决策。

        同一 ``execution_id`` 重放完全相同的决策会返回 ``reused=True``；任何字段变化
        都视为幂等冲突，禁止静默覆盖首次召回证据。该表不保存正文，调用方只能传递模型
        投影、排名、分数、摘要和计数。

        ``tree_fingerprint`` 仅在领域树索引尚未构建时允许为空，使
        ``architecture_index`` 失败仍可先落审计再终结；成功召回必须传 64 位摘要。
        """
        normalized_execution_id = self._normalize_recall_execution_id(execution_id)
        normalized_tree_fingerprint = self._normalize_recall_digest(
            tree_fingerprint,
            field_name="tree_fingerprint",
            allow_empty=True,
        )
        normalized_query_digest = self._normalize_recall_digest(
            query_digest,
            field_name="query_digest",
        )
        normalized_base = self._normalize_recall_ranked_ids(
            base_top64,
            field_name="base_top64",
            max_items=MAX_ARCHITECTURE_RECALL_BASE_CANDIDATES,
        )
        normalized_final = self._normalize_recall_final_candidates(final_candidates)
        final_ids = {item["id"] for item in normalized_final}
        if not set(normalized_base).issubset(final_ids):
            raise ValueError("base_top64必须全部包含在最终模型候选中")
        normalized_channels = self._normalize_recall_channel_rankings(channel_rankings)
        normalized_rrf = self._normalize_recall_rrf_scores(rrf_scores)
        normalized_protected = self._normalize_recall_protected_reasons(
            protected_reasons,
            final_candidate_ids=final_ids,
        )
        normalized_prompt_chars = self._normalize_recall_non_negative_int(
            prompt_chars,
            field_name="prompt_chars",
            upper_bound=MAX_ARCHITECTURE_RECALL_PROMPT_CHARS,
        )
        normalized_recall_elapsed = self._normalize_recall_non_negative_int(
            recall_elapsed_ms,
            field_name="recall_elapsed_ms",
            upper_bound=MAX_ARCHITECTURE_RECALL_ELAPSED_MS,
        )

        serialized_base = self._serialize_recall_json(
            normalized_base,
            field_name="base_top64",
        )
        serialized_final = self._serialize_recall_json(
            normalized_final,
            field_name="final_candidates",
        )
        serialized_channels = self._serialize_recall_json(
            normalized_channels,
            field_name="channel_rankings",
        )
        serialized_rrf = self._serialize_recall_json(
            normalized_rrf,
            field_name="rrf_scores",
        )
        serialized_protected = self._serialize_recall_json(
            normalized_protected,
            field_name="protected_reasons",
        )
        decision_payload = {
            "tree_fingerprint": normalized_tree_fingerprint,
            "query_digest": normalized_query_digest,
            "base_top64": normalized_base,
            "final_candidates": normalized_final,
            "channel_rankings": normalized_channels,
            "rrf_scores": normalized_rrf,
            "protected_reasons": normalized_protected,
            "prompt_chars": normalized_prompt_chars,
            "recall_elapsed_ms": normalized_recall_elapsed,
        }
        decision_digest = self._recall_payload_digest(decision_payload)
        now = _utc_now_iso()

        def _write(conn: sqlite3.Connection) -> tuple[bool, bool]:
            task = conn.execute(
                """
                SELECT business_type
                FROM llm_tasks
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
            if task is None:
                raise ArchitectureRecallAuditError(
                    "领域召回审计失败：对应execution不存在或已被新执行替换"
                )
            if task["business_type"] != "file":
                raise ArchitectureRecallAuditError(
                    "领域召回审计失败：仅file任务允许写入召回决策"
                )

            existing = conn.execute(
                """
                SELECT decision_digest, finalized_at
                FROM llm_architecture_recall_decisions
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
            if existing is not None:
                if existing["decision_digest"] != decision_digest:
                    raise ArchitectureRecallAuditError(
                        "领域召回审计失败：同一execution的初始决策发生幂等冲突"
                    )
                return False, existing["finalized_at"] is not None

            conn.execute(
                """
                INSERT INTO llm_architecture_recall_decisions (
                    execution_id, tree_fingerprint, query_digest, decision_digest,
                    base_top64_json, final_candidates_json, channel_rankings_json,
                    rrf_scores_json, protected_reasons_json, prompt_chars,
                    recall_elapsed_ms, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_execution_id,
                    normalized_tree_fingerprint,
                    normalized_query_digest,
                    decision_digest,
                    serialized_base,
                    serialized_final,
                    serialized_channels,
                    serialized_rrf,
                    serialized_protected,
                    normalized_prompt_chars,
                    normalized_recall_elapsed,
                    now,
                    now,
                ),
            )
            return True, False

        created, finalized = self._run_recall_audit_write(
            operation="upsert_architecture_recall_decision",
            writer=_write,
        )
        logger.info(
            "领域召回初始决策已提交: execution_id=%s created=%s reused=%s "
            "base_count=%s candidate_count=%s prompt_chars=%s",
            normalized_execution_id,
            created,
            not created,
            len(normalized_base),
            len(normalized_final),
            normalized_prompt_chars,
        )
        return ArchitectureRecallAuditWriteResult(
            execution_id=normalized_execution_id,
            created=created,
            reused=not created,
            finalized=finalized,
        )

    def finalize_architecture_recall_decision(
        self,
        *,
        execution_id: str,
        returned_architecture_id: int | None,
        returned_rank: int | None,
        total_elapsed_ms: int,
        failure_stage: str | None = None,
        error_message: str = "",
    ) -> ArchitectureRecallAuditWriteResult:
        """幂等终结召回决策，只补写结果字段，不覆盖任何初始召回证据。"""
        normalized_execution_id = self._normalize_recall_execution_id(execution_id)
        normalized_total_elapsed = self._normalize_recall_non_negative_int(
            total_elapsed_ms,
            field_name="total_elapsed_ms",
            upper_bound=MAX_ARCHITECTURE_RECALL_ELAPSED_MS,
        )
        normalized_failure_stage = str(failure_stage or "").strip() or None
        if (
            normalized_failure_stage is not None
            and normalized_failure_stage not in ARCHITECTURE_RECALL_FAILURE_STAGES
        ):
            raise ValueError("failure_stage不是允许的领域分类稳定失败阶段")
        normalized_error = str(error_message or "").strip()
        if len(normalized_error) > MAX_ARCHITECTURE_RECALL_ERROR_CHARS:
            raise ValueError("error_message超出召回审计长度上限")
        if normalized_failure_stage is None and normalized_error:
            raise ValueError("成功召回终结不得携带error_message")
        if normalized_failure_stage is not None and not normalized_error:
            raise ValueError("失败召回终结必须携带error_message")

        if returned_architecture_id is None and returned_rank is None:
            normalized_returned_id = None
            normalized_returned_rank = None
        elif returned_architecture_id is None or returned_rank is None:
            raise ValueError("returned_architecture_id与returned_rank必须同时为空或同时提供")
        else:
            normalized_returned_id = self._normalize_recall_positive_id(
                returned_architecture_id,
                field_name="returned_architecture_id",
            )
            normalized_returned_rank = self._normalize_recall_positive_id(
                returned_rank,
                field_name="returned_rank",
            )
            if normalized_returned_rank > MAX_ARCHITECTURE_RECALL_FINAL_CANDIDATES:
                raise ValueError("returned_rank超出最终候选数量上限")
        if normalized_returned_id is None and normalized_failure_stage is None:
            raise ValueError("终结召回决策必须包含返回ID或失败阶段")

        finalization_payload = {
            "returned_architecture_id": normalized_returned_id,
            "returned_rank": normalized_returned_rank,
            "total_elapsed_ms": normalized_total_elapsed,
            "failure_stage": normalized_failure_stage,
            "error_message": normalized_error,
        }
        finalization_digest = self._recall_payload_digest(finalization_payload)
        now = _utc_now_iso()

        def _write(conn: sqlite3.Connection) -> bool:
            task = conn.execute(
                """
                SELECT business_type
                FROM llm_tasks
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
            if task is None:
                raise ArchitectureRecallAuditError(
                    "领域召回审计失败：对应execution不存在或已被新执行替换"
                )
            if task["business_type"] != "file":
                raise ArchitectureRecallAuditError(
                    "领域召回审计失败：仅file任务允许终结召回决策"
                )
            existing = conn.execute(
                """
                SELECT final_candidates_json, recall_elapsed_ms,
                       finalization_digest, finalized_at
                FROM llm_architecture_recall_decisions
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
            if existing is None:
                raise ArchitectureRecallAuditError(
                    "领域召回审计失败：缺少初始召回决策"
                )
            if normalized_total_elapsed < int(existing["recall_elapsed_ms"]):
                raise ValueError("total_elapsed_ms不得小于recall_elapsed_ms")

            stored_candidates = json.loads(existing["final_candidates_json"])
            if normalized_returned_id is not None:
                candidate_ids = [int(item["id"]) for item in stored_candidates]
                if normalized_returned_id not in candidate_ids:
                    raise ValueError("returned_architecture_id不在最终模型候选中")
                expected_rank = candidate_ids.index(normalized_returned_id) + 1
                if normalized_returned_rank != expected_rank:
                    raise ValueError("returned_rank与最终模型候选顺序不一致")

            if existing["finalized_at"] is not None:
                if existing["finalization_digest"] != finalization_digest:
                    raise ArchitectureRecallAuditError(
                        "领域召回审计失败：同一execution的终结结果发生幂等冲突"
                    )
                return False

            conn.execute(
                """
                UPDATE llm_architecture_recall_decisions
                SET returned_architecture_id = ?, returned_rank = ?,
                    total_elapsed_ms = ?, failure_stage = ?, error_message = ?,
                    finalization_digest = ?, finalized_at = ?, updated_at = ?
                WHERE execution_id = ? AND finalized_at IS NULL
                """,
                (
                    normalized_returned_id,
                    normalized_returned_rank,
                    normalized_total_elapsed,
                    normalized_failure_stage,
                    normalized_error,
                    finalization_digest,
                    now,
                    now,
                    normalized_execution_id,
                ),
            )
            return True

        created = self._run_recall_audit_write(
            operation="finalize_architecture_recall_decision",
            writer=_write,
        )
        logger.info(
            "领域召回决策已终结: execution_id=%s created=%s reused=%s "
            "returned_architecture_id=%s returned_rank=%s failure_stage=%s",
            normalized_execution_id,
            created,
            not created,
            normalized_returned_id,
            normalized_returned_rank,
            normalized_failure_stage or "",
        )
        return ArchitectureRecallAuditWriteResult(
            execution_id=normalized_execution_id,
            created=created,
            reused=not created,
            finalized=True,
        )

    def get_architecture_recall_decision(
        self,
        execution_id: str,
    ) -> Optional[Dict[str, Any]]:
        """读取一条召回决策，供测试、E2E 和离线复盘取证。"""
        normalized_execution_id = self._normalize_recall_execution_id(execution_id)
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT execution_id, tree_fingerprint, query_digest,
                       base_top64_json, final_candidates_json,
                       channel_rankings_json, rrf_scores_json,
                       protected_reasons_json, prompt_chars,
                       recall_elapsed_ms, returned_architecture_id,
                       returned_rank, total_elapsed_ms, failure_stage,
                       error_message, finalized_at, created_at, updated_at
                FROM llm_architecture_recall_decisions
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
        if row is None:
            return None
        rrf_scores = {
            int(node_id): float(score)
            for node_id, score in json.loads(row["rrf_scores_json"]).items()
        }
        protected_reasons = {
            int(node_id): list(reasons)
            for node_id, reasons in json.loads(row["protected_reasons_json"]).items()
        }
        return {
            "execution_id": row["execution_id"],
            "tree_fingerprint": row["tree_fingerprint"],
            "query_digest": row["query_digest"],
            "base_top64": json.loads(row["base_top64_json"]),
            "final_candidates": json.loads(row["final_candidates_json"]),
            "channel_rankings": json.loads(row["channel_rankings_json"]),
            "rrf_scores": rrf_scores,
            "protected_reasons": protected_reasons,
            "prompt_chars": row["prompt_chars"],
            "recall_elapsed_ms": row["recall_elapsed_ms"],
            "returned_architecture_id": row["returned_architecture_id"],
            "returned_rank": row["returned_rank"],
            "total_elapsed_ms": row["total_elapsed_ms"],
            "failure_stage": row["failure_stage"],
            "error_message": row["error_message"],
            "finalized": row["finalized_at"] is not None,
            "finalized_at": row["finalized_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_weaponry_task_document_snapshots(
        self,
        *,
        architecture_id: int,
        execution_id: str,
    ) -> list[Dict[str, Any]]:
        """读取指定执行身份的 weaponry 选中文档快照。

        任务键会被同一 ``architectureId`` 的后续请求覆盖，因此查询必须同时限制
        ``execution_id``。不匹配时返回空列表，由执行器按“任务快照丢失”失败收敛，
        而不是误使用后一次请求的文档范围。
        """
        business_key = str(architecture_id)
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            raise ValueError("execution_id不能为空")
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT sequence_no, file_name, original_name, ingested_file_name,
                       source_architecture_id, doc_path, anything_doc_id
                FROM weaponry_task_document_snapshots
                WHERE business_key = ? AND execution_id = ?
                ORDER BY sequence_no ASC
                """,
                (business_key, normalized_execution_id),
            ).fetchall()
        snapshots = [
            {
                "file_name": row["file_name"],
                "original_name": row["original_name"],
                "ingested_file_name": row["ingested_file_name"],
                "source_architecture_id": row["source_architecture_id"],
                "doc_path": row["doc_path"],
                "anything_doc_id": row["anything_doc_id"],
            }
            for row in rows
        ]
        logger.info(
            "已读取weaponry任务选中文档快照: architecture_id=%s execution_id=%s file_count=%d",
            architecture_id,
            normalized_execution_id,
            len(snapshots),
        )
        return snapshots

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

    @staticmethod
    def _initial_cleanup_state(trace: RagExecutionTrace) -> tuple[str, str]:
        """根据 Session 打开阶段的回滚证据确定初始 cleanup 状态。

        正常打开的 Session 保持 ``pending``，等待初始审计提交后由业务层调用 close 并
        追加关闭事件。若 Context 根本未创建，或 Conversation 创建失败时 Gateway 已经
        完成回滚，则上层拿不到可再次关闭的 Session；此时必须在同一原子审计事务直接
        保存 cleanup 终态，避免崩溃窗口留下虚假的待清理记录。
        """
        rollback_events = tuple(
            event
            for event in trace.lifecycle_events
            if event.operation == "context_rollback"
        )
        if rollback_events:
            rollback = rollback_events[-1]
            if rollback.success:
                return "deleted", ""
            return "failed", rollback.error_message or "隔离上下文回滚失败"
        context_create_events = tuple(
            event
            for event in trace.lifecycle_events
            if event.operation == "context_create"
        )
        if context_create_events and not any(event.success for event in context_create_events):
            return "deleted", ""
        return "pending", ""

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
        normalized_prompt = normalize_rag_prompt(prompt)
        if len(normalized_prompt) > MAX_AUDIT_PROMPT_CHARS:
            raise InteractionAuditError("交互审计失败：Prompt 超出持久化安全上限")
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
            if len(str(model_attempt.raw_response or "")) > MAX_AUDIT_RESPONSE_CHARS:
                raise InteractionAuditError("交互审计失败：模型原始响应超出安全上限")
            if len(serialized_attempt_sources) > MAX_AUDIT_SOURCES_JSON_CHARS:
                raise InteractionAuditError("交互审计失败：来源证据超出安全上限")
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
        if len(serialized_trace) > MAX_AUDIT_TRACE_JSON_CHARS:
            raise InteractionAuditError("交互审计失败：完整执行轨迹超出安全上限")
        trace_digest = hashlib.sha256(serialized_trace.encode("utf-8")).hexdigest()
        now = _utc_now_iso()
        initial_cleanup_status, initial_cleanup_error = self._initial_cleanup_state(trace)

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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    initial_cleanup_status,
                    initial_cleanup_error,
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
            "LLM 交互原子审计已提交: interaction_id=%s business_type=%s "
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
            "LLM 交互记录已持久化: interaction_id=%s business_type=%s "
            "business_key=%s status=%s",
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
        execution_id: str | None = None,
    ) -> None:
        self.mark_business_result(
            business_type,
            business_key,
            result_payload=result_payload,
            status=status,
            execution_id=execution_id,
        )

    def mark_business_result(
        self,
        business_type: str,
        business_key: str,
        result_payload: Dict[str, Any],
        *,
        status: str,
        message: str = "",
        execution_id: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        expected_execution_id = str(execution_id or "").strip()
        execution_clause = " AND execution_id = ?" if expected_execution_id else ""
        active_clause = " AND status IN ('0', '1')" if expected_execution_id else ""
        params: list[Any] = [
            status,
            1.0,
            message,
            self._serialize(result_payload),
            now,
            business_type,
            business_key,
        ]
        if expected_execution_id:
            params.append(expected_execution_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET status = ?, progress = ?, message = ?, result_payload = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                {execution_clause}
                {active_clause}
                """,
                tuple(params),
            )
            if expected_execution_id and cursor.rowcount != 1:
                task = conn.execute(
                    """
                    SELECT execution_id, status
                    FROM llm_tasks
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (business_type, business_key),
                ).fetchone()
                if task is None or task["execution_id"] != expected_execution_id:
                    raise TaskExecutionConflictError(
                        business_type,
                        business_key,
                        expected_execution_id,
                    )
                raise TaskStateConflictError(
                    business_type,
                    business_key,
                    expected_execution_id,
                    task["status"],
                )
        logger.info(
            "任务业务结果已标记: business_type=%s business_key=%s "
            "execution_id=%s status=%s",
            business_type,
            business_key,
            expected_execution_id or "-",
            status,
        )

    def update_task_progress(
        self,
        business_type: str,
        business_key: str,
        *,
        progress: float,
        message: str,
        status: Optional[str] = None,
        execution_id: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        progress = normalize_progress(progress)
        expected_execution_id = str(execution_id or "").strip()
        execution_clause = " AND execution_id = ?" if expected_execution_id else ""
        active_clause = " AND status IN ('0', '1')" if expected_execution_id else ""
        status_sql = "status = ?, " if status is not None else ""
        params: list[Any] = []
        if status is not None:
            params.append(status)
        params.extend([progress, message, now, business_type, business_key])
        if expected_execution_id:
            params.append(expected_execution_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET {status_sql}progress = ?, message = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                {execution_clause}
                {active_clause}
                """,
                tuple(params),
            )
            if expected_execution_id and cursor.rowcount != 1:
                task = conn.execute(
                    """
                    SELECT execution_id, status
                    FROM llm_tasks
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (business_type, business_key),
                ).fetchone()
                if task is None or task["execution_id"] != expected_execution_id:
                    raise TaskExecutionConflictError(
                        business_type,
                        business_key,
                        expected_execution_id,
                    )
                raise TaskStateConflictError(
                    business_type,
                    business_key,
                    expected_execution_id,
                    task["status"],
                )

    def _mark_callback_result(
        self,
        business_type: str,
        business_key: str,
        *,
        callback_status: str,
        error: str,
        execution_id: str | None = None,
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
        expected_execution_id = str(execution_id or "").strip()
        execution_clause = " AND execution_id = ?" if expected_execution_id else ""
        update_params: list[Any] = [
            callback_status,
            normalized_error,
            now,
            business_type,
            business_key,
            *sorted(completed_statuses),
        ]
        if expected_execution_id:
            update_params.append(expected_execution_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_status = ?, callback_attempts = callback_attempts + 1,
                    last_callback_error = ?, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND callback_status IN ('pending', 'failed')
                  AND status IN ({status_placeholders})
                  {execution_clause}
                """,
                tuple(update_params),
            )
            if cursor.rowcount != 1:
                task = conn.execute(
                    """
                    SELECT execution_id, status, callback_status FROM llm_tasks
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (business_type, business_key),
                ).fetchone()
                if task is None:
                    raise ValueError("待更新回调结果的任务不存在")
                if (
                    expected_execution_id
                    and task["execution_id"] != expected_execution_id
                ):
                    raise TaskExecutionConflictError(
                        business_type,
                        business_key,
                        expected_execution_id,
                    )
                if task["status"] not in completed_statuses:
                    raise ValueError("任务尚未完成，不能提交回调结果")
                raise ValueError(
                    "非法回调状态转换: "
                    f"{task['callback_status']} -> {callback_status}"
                )

    def mark_callback_failed(
        self,
        business_type: str,
        business_key: str,
        error: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        """记录一次实际失败的回调，禁止覆盖成功或无需回调终态。"""
        self._mark_callback_result(
            business_type,
            business_key,
            callback_status="failed",
            error=error,
            execution_id=execution_id,
        )
        logger.warning(
            "外部回调失败已记录: business_type=%s business_key=%s error_chars=%d",
            business_type,
            business_key,
            len(str(error or "")),
        )

    def mark_callback_success(
        self,
        business_type: str,
        business_key: str,
        *,
        execution_id: str | None = None,
    ) -> None:
        """记录一次实际成功的回调，成功后状态不可再次改写。"""
        self._mark_callback_result(
            business_type,
            business_key,
            callback_status="success",
            error="",
            execution_id=execution_id,
        )
        logger.info(
            "外部回调成功已记录: business_type=%s business_key=%s",
            business_type,
            business_key,
        )

    def mark_callback_skipped(
        self,
        business_type: str,
        business_key: str,
        *,
        execution_id: str | None = None,
    ) -> bool:
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
        expected_execution_id = str(execution_id or "").strip()
        execution_clause = " AND execution_id = ?" if expected_execution_id else ""
        transition_succeeded = False
        current_status = ""
        update_params: list[Any] = [
            now,
            business_type,
            business_key,
            *sorted(completed_statuses),
        ]
        if expected_execution_id:
            update_params.append(expected_execution_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_status = 'skipped', last_callback_error = '', updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND callback_status IN ('pending', 'skipped')
                  AND status IN ({status_placeholders})
                  {execution_clause}
                """,
                tuple(update_params),
            )
            transition_succeeded = cursor.rowcount == 1
            if not transition_succeeded:
                task = conn.execute(
                    """
                    SELECT execution_id, status, callback_status
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
                if (
                    expected_execution_id
                    and task["execution_id"] != expected_execution_id
                ):
                    raise TaskExecutionConflictError(
                        business_type,
                        business_key,
                        expected_execution_id,
                    )
                if task["status"] not in completed_statuses:
                    raise ValueError("任务尚未完成，不能标记 callback_status=skipped")
                current_status = task["callback_status"]
                if current_status not in {"success", "failed"}:
                    raise ValueError(f"未知 callback_status: {current_status}")

        if not transition_succeeded:
            logger.warning(
                "忽略无需回调标记，保留已发生的回调结果: business_type=%s business_key=%s "
                "callback_status=%s",
                business_type,
                business_key,
                current_status,
            )
            return False
        logger.info(
            "任务无需回调，状态已幂等标记为 skipped: business_type=%s business_key=%s",
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
