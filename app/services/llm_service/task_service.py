from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import time
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

logger = logging.getLogger(__name__)


_COMPLETED_TASK_STATUSES = {
    "file": frozenset({"2", "3"}),
    "report": frozenset({"1", "2"}),
    "weaponry": frozenset({"2", "3"}),
}
"""允许进入回调终态的现有业务完成状态。"""

_ACTIVE_FILE_TASK_STATUSES = frozenset({"0", "1"})
_FILE_CALLBACK_HANDOFF_STATUS = "pending"
_MIN_CALLBACK_DELIVERY_LEASE_SECONDS = 60.0
_MAX_CALLBACK_DELIVERY_LEASE_SECONDS = 7 * 24 * 60 * 60.0


def _callback_delivery_lease_seconds(timeout: Any) -> float:
    """根据 HTTP timeout 生成有余量且有硬上限的回调租约。"""
    try:
        normalized_timeout = float(timeout)
    except (TypeError, ValueError):
        normalized_timeout = 0.0
    if not math.isfinite(normalized_timeout) or normalized_timeout < 0:
        normalized_timeout = 0.0
    return min(
        _MAX_CALLBACK_DELIVERY_LEASE_SECONDS,
        max(
            _MIN_CALLBACK_DELIVERY_LEASE_SECONDS,
            normalized_timeout * 4 + 60.0,
        ),
    )


def file_task_admission_block_reason(
    task: Mapping[str, Any] | None,
    *,
    callback_delivery_in_flight: bool = False,
) -> str | None:
    """返回同名文件任务暂不可重跑的稳定原因。

    文件业务结果与外部回调状态分两次提交。终态任务仍为 ``pending`` 时，旧 worker
    可能尚未发起回调、正在执行 HTTP，或刚完成 HTTP 尚未提交回调结果；此时覆盖单行
    任务记录会让旧执行丢失回调或无法记录已送达结果。因此该短暂交接窗口与处理中状态
    一样必须阻止新执行受理。``failed`` 已代表至少完成过一次真实尝试，平时仍保留显式
    重跑能力；但其补发租约有效期间也必须阻断，避免旧补发写坏新执行。
    """
    if not task:
        return None
    status = str(task.get("status") or "")
    if status in _ACTIVE_FILE_TASK_STATUSES:
        return "processing"
    callback_status = str(task.get("callback_status") or "")
    if callback_status == _FILE_CALLBACK_HANDOFF_STATUS:
        return "callback_pending"
    if callback_delivery_in_flight:
        return "callback_pending"
    return None


class TaskAlreadyProcessingError(RuntimeError):
    """同一文件已有活动执行或尚未完成首次回调交接。"""

    def __init__(
        self,
        business_key: str,
        status: str,
        callback_status: str = "",
        *,
        callback_delivery_in_flight: bool = False,
    ):
        self.business_key = business_key
        self.status = status
        self.callback_status = callback_status
        self.reason = file_task_admission_block_reason(
            {
                "status": status,
                "callback_status": callback_status,
            },
            callback_delivery_in_flight=callback_delivery_in_flight,
        )
        message = (
            "上一次任务回调尚未结束"
            if self.reason == "callback_pending"
            else "任务正在处理中"
        )
        super().__init__(f"{message}: {business_key}")


class TaskAdmissionBusyError(RuntimeError):
    """任务库在受理时持续繁忙，调用方可安全稍后重试。"""


@dataclass(frozen=True)
class AnalysisBatchTaskAdmission:
    """一条待写入 Analysis 批次的内部 execution 事实。

    该值对象只描述 SQLite 事务所需的最小字段，不导入 Analysis Domain 或 Web DTO，避免
    兼容 ``LLMTaskService`` 反向依赖业务分层。调用方仍必须在事务外完成 Codec 往返校验；
    本服务会在事务内再次核对 task、批次、序号和公开投影身份，防止错误的 Adapter 绕过
    原子边界。
    """

    execution_id: str
    business_key: str
    input_schema_version: int
    input_payload: Mapping[str, Any]
    projection_request_payload: Mapping[str, Any]
    initial_public_status: str
    trace_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_id",
            _required_internal_text(self.execution_id, name="execution_id"),
        )
        object.__setattr__(
            self,
            "business_key",
            _required_internal_text(self.business_key, name="business_key"),
        )
        if (
            isinstance(self.input_schema_version, bool)
            or not isinstance(self.input_schema_version, int)
            or self.input_schema_version <= 0
        ):
            raise ValueError("input_schema_version必须是正整数")
        if not isinstance(self.input_payload, Mapping):
            raise TypeError("input_payload必须是Mapping")
        if not isinstance(self.projection_request_payload, Mapping):
            raise TypeError("projection_request_payload必须是Mapping")
        object.__setattr__(
            self,
            "initial_public_status",
            _required_internal_text(
                self.initial_public_status,
                name="initial_public_status",
            ),
        )
        object.__setattr__(
            self,
            "trace_id",
            _required_internal_text(self.trace_id, name="trace_id"),
        )


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


_TASK_EXECUTION_STATES = frozenset(
    {"accepted", "running", "succeeded", "failed", "stale"}
)
_CALLBACK_GUARD_STATES = frozenset({"idle", "sending", "outcome_unknown"})
_CALLBACK_DELIVERY_OUTCOMES = frozenset(
    {
        "success",
        "rejected",
        "definitely_not_sent",
        "delivery_outcome_unknown",
        "skipped",
        "stale",
    }
)
_CALLBACK_DELIVERY_TRIGGERS = frozenset(
    {"initial_delivery", "explicit_check_task_recovery"}
)
_EXPLICIT_UNKNOWN_RETRY_BUSINESS_TYPES = frozenset(
    {"file", "report", "weaponry"}
)
_CALLBACK_ATTEMPT_EVENT_TYPES = frozenset(
    {
        "authorized",
        "completed",
        "lease_expired_unknown",
        "guard_inconsistent_unknown",
    }
)
_REPORT_RESOURCE_STATES = frozenset(
    {"tracking", "cleanup_pending", "audit_pending", "cleaned", "quarantined"}
)
# Analysis 资源记录与 Report 复用同一组状态，但表和业务身份严格隔离。这里保留
# 独立常量，避免未来任一模块扩展状态时意外放宽另一模块的 SQLite 写入边界。
_ANALYSIS_RESOURCE_STATES = frozenset(
    {"tracking", "cleanup_pending", "audit_pending", "cleaned", "quarantined"}
)
_ANALYSIS_RESOURCE_TRANSITIONS = {
    "tracking": frozenset(
        {"tracking", "cleanup_pending", "audit_pending", "quarantined"}
    ),
    "cleanup_pending": frozenset(
        {"cleanup_pending", "audit_pending", "cleaned", "quarantined"}
    ),
    "audit_pending": frozenset({"audit_pending", "cleaned", "quarantined"}),
    # cleaned/quarantined 是不可逆终态。终态证据只能追加到独立审计记录，不能通过
    # 同状态更新悄悄递增版本或改写已有资源现场。
    "cleaned": frozenset(),
    "quarantined": frozenset(),
}
_TASK_COMMAND_SQLITE_TIMEOUT_SECONDS = 30.0
# 1F-4 只为新文件分析 execution 追加批次元数据。这里保留局部常量而不导入
# ``app.modules.analysis.domain``，避免兼容 SQLite Service 反向依赖业务领域层。
_ANALYSIS_BATCH_BUSINESS_TYPE = "file"
_MAX_ANALYSIS_BATCH_ITEMS = 32
_ANALYSIS_BATCH_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _required_internal_text(value: object, *, name: str) -> str:
    """严格校验任务控制面内部文本，禁止隐式字符串化掩盖调用错误。"""

    if not isinstance(value, str):
        raise TypeError(f"{name}必须是str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name}不能为空")
    return normalized


def _strict_execution_progress(value: object) -> float:
    """校验 execution 事实进度；与旧投影的宽松清洗逻辑明确隔离。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("progress必须是数字")
    normalized = float(value)
    if (
        normalized != normalized
        or normalized in (float("inf"), float("-inf"))
        or normalized < 0.0
        or normalized > 1.0
    ):
        raise ValueError("progress必须是0到1之间的有限数字")
    return normalized


def _aware_datetime(value: object, *, name: str) -> datetime:
    """解析内部 ISO 时间并统一为 UTC；拒绝会造成租约比较歧义的无时区时间。"""

    normalized = _required_internal_text(value, name=name)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name}必须是ISO时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name}必须包含时区")
    return parsed.astimezone(timezone.utc)


def _callback_guard_deadline_expired(
    deadline_at: object,
    *,
    observed_at: datetime,
) -> bool:
    """保守判断 Guard 截止时间；缺失或损坏时间一律视为已经过期。

    ``sending`` 无法证明 HTTP 是否已经到达甲方，因此过期后只能冻结为
    ``outcome_unknown``，绝不能自动重抢。集中该判断可避免受理、获取、观察和后台扫描
    对损坏时间给出不同结论。
    """

    if not deadline_at:
        return True
    try:
        deadline = _aware_datetime(deadline_at, name="deadline_at")
    except (TypeError, ValueError):
        return True
    return deadline <= observed_at

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
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(
            f"PRAGMA busy_timeout = {int(max(0.0, timeout_seconds) * 1000)}"
        )
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

    @contextmanager
    def _immediate_connection(self) -> Iterator[sqlite3.Connection]:
        """托管任务控制面的短 ``BEGIN IMMEDIATE`` 写事务。

        原子受理、领取和 expected-execution 条件写都必须先取得 SQLite 写保留锁，
        从而把“检查当前 owner”和“写 execution + 最新投影”放在同一个串行化窗口内。
        每次调用仍创建独立连接，绝不在线程之间共享 ``sqlite3.Connection``；较长的
        busy timeout 只用于吸收并发短事务排队，事务中禁止网络、文件或模型调用。
        """

        conn = self._connect(timeout_seconds=_TASK_COMMAND_SQLITE_TIMEOUT_SECONDS)
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            # SQLite 仅作为阶段 1C～2 的单机过渡存储。WAL 允许 Progress/查询读连接在短写
            # 事务排队时继续工作；MySQL 阶段仍需通过 Repository/UoW 和行锁重新实现，
            # 不能把这里的串行写性能当作最终 50 并发验收依据。
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
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
                    callback_claim_id TEXT NOT NULL DEFAULT '',
                    callback_claim_expires_at REAL NOT NULL DEFAULT 0,
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
                    trace_id TEXT NOT NULL DEFAULT '',
                    document_upload_json TEXT NOT NULL DEFAULT '{}',
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
            self._ensure_column(
                conn,
                table="llm_tasks",
                column="callback_claim_id",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn,
                table="llm_tasks",
                column="callback_claim_expires_at",
                definition="REAL NOT NULL DEFAULT 0",
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
            self._ensure_column(
                conn,
                table="llm_interactions",
                column="trace_id",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn,
                table="llm_interactions",
                column="document_upload_json",
                definition="TEXT NOT NULL DEFAULT '{}'",
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
                    call_id TEXT NOT NULL DEFAULT '',
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
                ("call_id", "TEXT NOT NULL DEFAULT ''"),
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
            # 阶段 1C-3 以追加 execution 保存可靠受理事实。旧 llm_tasks 继续作为
            # file/weaponry/check-task 与 Progress 的最新公开投影，不能被删除或改名。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_task_executions (
                    execution_id TEXT PRIMARY KEY,
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    input_schema_version INTEGER NOT NULL
                        CHECK (input_schema_version >= 1),
                    input_payload TEXT NOT NULL,
                    batch_id TEXT,
                    batch_sequence INTEGER,
                    dispatch_sequence INTEGER,
                    dispatch_failure_count INTEGER NOT NULL DEFAULT 0
                        CHECK (dispatch_failure_count >= 0),
                    next_dispatch_at TEXT,
                    last_dispatch_error TEXT NOT NULL DEFAULT '',
                    execution_state TEXT NOT NULL
                        CHECK (execution_state IN (
                            'accepted', 'running', 'succeeded', 'failed', 'stale'
                        )),
                    public_status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0
                        CHECK (progress >= 0 AND progress <= 1),
                    message TEXT NOT NULL DEFAULT '',
                    result_payload TEXT,
                    callback_status TEXT NOT NULL DEFAULT 'pending',
                    callback_outcome TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # 旧开发库只做增量补列；历史测试数据无需业务迁移，但仍要分配稳定序号，
            # 使后续扫描不再依赖可能回拨的应用时钟或并发事务提交先后。
            self._ensure_column(
                conn,
                table="llm_task_executions",
                column="batch_id",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="llm_task_executions",
                column="batch_sequence",
                definition="INTEGER",
            )
            self._ensure_column(
                conn,
                table="llm_task_executions",
                column="dispatch_sequence",
                definition="INTEGER",
            )
            self._ensure_column(
                conn,
                table="llm_task_executions",
                column="dispatch_failure_count",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table="llm_task_executions",
                column="next_dispatch_at",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="llm_task_executions",
                column="last_dispatch_error",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """
                UPDATE llm_task_executions
                SET dispatch_sequence = rowid
                WHERE dispatch_sequence IS NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_task_executions_dispatch_sequence
                ON llm_task_executions (dispatch_sequence)
                """
            )
            # 新 Analysis execution 使用共享 batch_id + 请求内连续 sequence 表示顺序。
            # 历史 report/weaponry/旧 file 行保持 NULL，不受该部分索引约束；SQLite 的
            # CHECK/跨行连续性由 1F-4 Adapter 在同一短事务内双重校验，不能为此重建表。
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_llm_task_executions_file_batch_sequence
                ON llm_task_executions (business_type, batch_id, batch_sequence)
                WHERE business_type = 'file' AND batch_id IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_llm_task_executions_scan
                ON llm_task_executions (
                    business_type, execution_state, created_at, execution_id
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_llm_task_executions_ready_scan
                ON llm_task_executions (
                    business_type, execution_state, next_dispatch_at,
                    dispatch_sequence
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_llm_task_executions_business
                ON llm_task_executions (
                    business_type, business_key, created_at, execution_id
                )
                """
            )
            # Callback Guard 在 1C-5 已形成获取、完成、unknown 冻结和人工解除闭环。
            # owner 不设置外键，以兼容切换前仍可能来自旧 llm_tasks 的回调执行身份；
            # 阶段 3 的 MySQL Repository 再以正式迁移 Schema 表达跨表约束。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS callback_delivery_guards (
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    owner_execution_id TEXT,
                    state TEXT NOT NULL DEFAULT 'idle'
                        CHECK (state IN ('idle', 'sending', 'outcome_unknown')),
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_version INTEGER NOT NULL DEFAULT 0
                        CHECK (lease_version >= 0),
                    lease_started_at TEXT,
                    deadline_at TEXT,
                    last_outcome TEXT NOT NULL DEFAULT '',
                    error_stage TEXT NOT NULL DEFAULT '',
                    released_at TEXT,
                    released_by TEXT NOT NULL DEFAULT '',
                    release_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (business_type, business_key)
                )
                """
            )
            # 已存在的开发数据库通过只增不删迁移补齐 fencing 字段；旧 idle/unknown 行
            # 使用空 token 与 version=0，首次成功 acquire 会原子递增到 1。
            self._ensure_column(
                conn,
                table="callback_delivery_guards",
                column="lease_token",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                conn,
                table="callback_delivery_guards",
                column="lease_version",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_callback_delivery_guards_recovery
                ON callback_delivery_guards (state, deadline_at)
                """
            )
            # 人工解除是高风险运维动作，不能只保存在会被下一次 acquire 清空的 Guard
            # 当前快照中。每个 fencing 版本至多写一条追加式记录，和 Guard 状态转换位于
            # 同一个短事务，确保“已解除”与“谁因何解除”不会发生分裂。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS callback_guard_release_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    owner_execution_id TEXT NOT NULL,
                    lease_version INTEGER NOT NULL CHECK (lease_version >= 0),
                    released_at TEXT NOT NULL,
                    released_by TEXT NOT NULL,
                    release_reason TEXT NOT NULL,
                    worker_stopped_confirmed INTEGER NOT NULL DEFAULT 0
                        CHECK (worker_stopped_confirmed IN (0, 1)),
                    UNIQUE (business_type, business_key, lease_version)
                )
                """
            )
            self._ensure_column(
                conn,
                table="callback_guard_release_audits",
                column="worker_stopped_confirmed",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_callback_guard_release_audits_key
                ON callback_guard_release_audits (
                    business_type, business_key, released_at, id
                )
                """
            )
            # 每次 Callback 发送权及其最终收敛结果使用独立的追加式事件账本。该表不与
            # 人工解除审计复用：前者证明某一 attempt 为什么获得发送权、最终如何收敛，
            # 后者证明运维人员为何解除业务键冻结。两类证据拥有不同生命周期和唯一键。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS callback_delivery_attempt_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    owner_execution_id TEXT NOT NULL,
                    callback_attempt INTEGER NOT NULL
                        CHECK (callback_attempt >= 0),
                    lease_version INTEGER NOT NULL CHECK (lease_version >= 0),
                    trigger TEXT NOT NULL
                        CHECK (trigger IN (
                            'initial_delivery',
                            'explicit_check_task_recovery'
                        )),
                    event_type TEXT NOT NULL
                        CHECK (event_type IN (
                            'authorized',
                            'completed',
                            'lease_expired_unknown',
                            'guard_inconsistent_unknown'
                        )),
                    delivery_outcome TEXT NOT NULL DEFAULT '',
                    request_trace_id TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    UNIQUE (
                        business_type, business_key, owner_execution_id,
                        lease_version, event_type
                    )
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_callback_attempt_events_key
                ON callback_delivery_attempt_events (
                    business_type, business_key, occurred_at, id
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_callback_attempt_events_trace
                ON callback_delivery_attempt_events (request_trace_id, id)
                WHERE request_trace_id <> ''
                """
            )
            # 每个 report execution 独占一份资源恢复记录。record_payload 保存供应商无关
            # Artifact/RAG/Audit 引用和清理阶段；state/version 单独成列，便于恢复扫描和 CAS。
            # 当前历史数据仅用于测试，不回填旧任务；新执行从 Application 创建 scope 时登记。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_resource_records (
                    execution_id TEXT PRIMARY KEY,
                    business_type TEXT NOT NULL CHECK (business_type = 'report'),
                    business_key TEXT NOT NULL,
                    artifact_namespace TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN (
                            'tracking', 'cleanup_pending', 'audit_pending',
                            'cleaned', 'quarantined'
                        )),
                    record_payload TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
                    recovery_deferral_count INTEGER NOT NULL DEFAULT 0
                        CHECK (recovery_deferral_count >= 0),
                    next_recovery_at TEXT,
                    last_recovery_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (execution_id)
                        REFERENCES llm_task_executions(execution_id) ON DELETE CASCADE
                )
                """
            )
            self._ensure_column(
                conn,
                table="report_resource_records",
                column="recovery_deferral_count",
                definition="INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                conn,
                table="report_resource_records",
                column="next_recovery_at",
                definition="TEXT",
            )
            self._ensure_column(
                conn,
                table="report_resource_records",
                column="last_recovery_reason",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_resource_records_recovery
                ON report_resource_records (state, updated_at, execution_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_resource_records_ready_recovery
                ON report_resource_records (
                    state, next_recovery_at, updated_at, execution_id
                )
                """
            )
            # 1F-6 为新 Analysis execution 追加独立的资源恢复事实。它不回填、更不
            # 修改旧 ``rag_resource_leases``：旧租约缺少可证明的所有权/清理结果，只能
            # 在切换前做只读诊断，不能被新链路猜测性迁移或自动删除。
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analysis_resource_records (
                    execution_id TEXT PRIMARY KEY,
                    business_type TEXT NOT NULL CHECK (business_type = 'file'),
                    business_key TEXT NOT NULL,
                    state TEXT NOT NULL
                        CHECK (state IN (
                            'tracking', 'cleanup_pending', 'audit_pending',
                            'cleaned', 'quarantined'
                        )),
                    record_payload TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
                    recovery_deferral_count INTEGER NOT NULL DEFAULT 0
                        CHECK (recovery_deferral_count >= 0),
                    next_recovery_at TEXT,
                    last_recovery_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (execution_id)
                        REFERENCES llm_task_executions(execution_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_analysis_resource_records_ready_recovery
                ON analysis_resource_records (
                    state, next_recovery_at, updated_at, execution_id
                )
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

    def _row_to_task_execution(self, row: sqlite3.Row) -> Dict[str, Any]:
        """把追加式 execution 行转换为不携带 SQLite 对象的普通快照。"""

        return {
            "execution_id": row["execution_id"],
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "input_schema_version": row["input_schema_version"],
            "input_payload": self._deserialize(row["input_payload"]),
            "batch_id": row["batch_id"],
            "batch_sequence": row["batch_sequence"],
            "dispatch_sequence": row["dispatch_sequence"],
            "execution_state": row["execution_state"],
            "public_status": row["public_status"],
            "progress": _strict_execution_progress(row["progress"]),
            "message": row["message"],
            "result_payload": self._deserialize(row["result_payload"]),
            "callback_status": row["callback_status"],
            "callback_outcome": row["callback_outcome"],
            "trace_id": row["trace_id"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _select_task_execution_row(
        conn: sqlite3.Connection,
        execution_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT execution_id, business_type, business_key,
                   input_schema_version, input_payload, batch_id, batch_sequence,
                   dispatch_sequence, execution_state,
                   public_status, progress, message, result_payload,
                   callback_status, callback_outcome, trace_id,
                   created_at, started_at, completed_at, updated_at
            FROM llm_task_executions
            WHERE execution_id = ?
            """,
            (execution_id,),
        ).fetchone()

    @staticmethod
    def _mark_execution_stale_if_superseded(
        conn: sqlite3.Connection,
        *,
        execution: sqlite3.Row | None,
        latest: sqlite3.Row | None,
        execution_id: str,
        business_type: str,
        business_key: str,
    ) -> bool:
        """在 latest owner 已变化时让旧的活动 execution 原子收敛为 stale。

        条件写返回 ``False`` 只是告诉 Application 停止后续副作用；如果不同时保存 stale
        事实，已经运行的旧 Worker 会永久占据 running 统计。身份不匹配或 execution 已经
        终态时不做任何修改，避免调用方参数错误污染其他任务。
        """

        if (
            execution is None
            or execution["business_type"] != business_type
            or execution["business_key"] != business_key
            or execution["execution_state"] not in {"accepted", "running"}
            or (latest is not None and latest["execution_id"] == execution_id)
        ):
            return False
        now = _utc_now_iso()
        cursor = conn.execute(
            """
            UPDATE llm_task_executions
            SET execution_state = 'stale',
                completed_at = COALESCE(completed_at, ?),
                updated_at = ?
            WHERE execution_id = ?
              AND business_type = ?
              AND business_key = ?
              AND execution_state IN ('accepted', 'running')
            """,
            (now, now, execution_id, business_type, business_key),
        )
        return cursor.rowcount == 1

    def create_task_execution_if_allowed(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        input_schema_version: int,
        input_payload: Mapping[str, Any],
        projection_request_payload: Mapping[str, Any],
        initial_public_status: str,
        active_public_statuses: Sequence[str],
        trace_id: str,
        accepted_at: str,
    ) -> Dict[str, Any]:
        """原子创建一次 execution，并把它设为 ``llm_tasks`` 最新 owner。

        Guard 检查、活动状态检查、execution 插入和最新投影 upsert 全部位于同一个
        ``BEGIN IMMEDIATE`` 事务。返回值只表达内部分类；HTTP 202/409 仍由后续 Web
        Presenter 决定。本方法绝不执行 Progress、线程唤醒、回调或任何外部 I/O。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        if (
            isinstance(input_schema_version, bool)
            or not isinstance(input_schema_version, int)
            or input_schema_version <= 0
        ):
            raise ValueError("input_schema_version必须是正整数")
        if not isinstance(input_payload, Mapping):
            raise TypeError("input_payload必须是Mapping")
        if not isinstance(projection_request_payload, Mapping):
            raise TypeError("projection_request_payload必须是Mapping")
        normalized_public_status = _required_internal_text(
            initial_public_status,
            name="initial_public_status",
        )
        if isinstance(active_public_statuses, (str, bytes)):
            raise TypeError("active_public_statuses必须是文本序列")
        normalized_active_statuses = tuple(
            _required_internal_text(item, name="active_public_status")
            for item in active_public_statuses
        )
        if not normalized_active_statuses:
            raise ValueError("active_public_statuses不能为空")
        normalized_trace_id = _required_internal_text(trace_id, name="trace_id")
        accepted_time = _aware_datetime(accepted_at, name="accepted_at")
        normalized_accepted_at = accepted_time.isoformat()

        # JSON 编码先于写事务执行，编码失败不能占用 SQLite 写锁，也不能留下半条事实。
        serialized_input = self._serialize(dict(input_payload))
        serialized_projection_request = self._serialize(
            dict(projection_request_payload)
        )
        accepted_execution: Dict[str, Any] | None = None
        outcome = ""
        with self._immediate_connection() as conn:
            guard = conn.execute(
                """
                SELECT owner_execution_id, state, deadline_at
                FROM callback_delivery_guards
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            guard_state = guard["state"] if guard is not None else "idle"
            if guard_state not in _CALLBACK_GUARD_STATES:
                raise RuntimeError("callback Guard 存在未知状态")
            if guard_state == "sending":
                if _callback_guard_deadline_expired(
                    guard["deadline_at"],
                    observed_at=accepted_time,
                ):
                    owner_execution_id = str(
                        guard["owner_execution_id"] or ""
                    ).strip()
                    if not owner_execution_id:
                        raise RuntimeError(
                            "sending callback Guard 缺少 owner_execution_id"
                        )
                    transitioned = self._transition_callback_guard_to_unknown(
                        conn,
                        business_type=normalized_business_type,
                        business_key=normalized_business_key,
                        owner_execution_id=owner_execution_id,
                        now=normalized_accepted_at,
                        reason="callback lease expired before new task submission",
                    )
                    if not transitioned:
                        raise RuntimeError(
                            "受理时过期 callback Guard 未能冻结为 outcome_unknown"
                        )
                    outcome = "callback_outcome_unknown"
                else:
                    outcome = "callback_sending"
            elif guard_state == "outcome_unknown":
                outcome = "callback_outcome_unknown"
            else:
                current = conn.execute(
                    """
                    SELECT execution_id, status
                    FROM llm_tasks
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (normalized_business_type, normalized_business_key),
                ).fetchone()
                if (
                    current is not None
                    and current["status"] in normalized_active_statuses
                ):
                    outcome = "active_conflict"
                else:
                    # ``BEGIN IMMEDIATE`` 已串行化所有任务控制面写入，因此该序号与本地
                    # 受理事务顺序一致，不受调用方时钟回拨或事务外 accepted_at 生成顺序
                    # 影响。阶段 3 的 MySQL Repository 将改用数据库原生序列。
                    dispatch_sequence = int(
                        conn.execute(
                            """
                            SELECT COALESCE(MAX(dispatch_sequence), 0) + 1
                            FROM llm_task_executions
                            """
                        ).fetchone()[0]
                    )
                    conn.execute(
                        """
                        INSERT INTO llm_task_executions (
                            execution_id, business_type, business_key,
                            input_schema_version, input_payload,
                            dispatch_sequence,
                            execution_state, public_status, progress, message,
                            result_payload, callback_status, callback_outcome,
                            trace_id, created_at, started_at, completed_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, 'accepted', ?, 0, '', NULL,
                                'pending', '', ?, ?, NULL, NULL, ?)
                        """,
                        (
                            normalized_execution_id,
                            normalized_business_type,
                            normalized_business_key,
                            input_schema_version,
                            serialized_input,
                            dispatch_sequence,
                            normalized_public_status,
                            normalized_trace_id,
                            normalized_accepted_at,
                            normalized_accepted_at,
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO llm_tasks (
                            business_type, business_key, execution_id,
                            request_payload, status, progress, message,
                            result_payload, callback_status, callback_attempts,
                            last_callback_error, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, 0, '', NULL, 'pending', 0, '', ?, ?)
                        ON CONFLICT(business_type, business_key) DO UPDATE SET
                            execution_id = excluded.execution_id,
                            request_payload = excluded.request_payload,
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
                            normalized_business_type,
                            normalized_business_key,
                            normalized_execution_id,
                            serialized_projection_request,
                            normalized_public_status,
                            normalized_accepted_at,
                            normalized_accepted_at,
                        ),
                    )
                    row = self._select_task_execution_row(
                        conn,
                        normalized_execution_id,
                    )
                    if row is None:
                        raise RuntimeError("原子受理后未读取到 execution")
                    accepted_execution = self._row_to_task_execution(row)
                    outcome = "accepted"

        logger.info(
            "任务原子受理完成: business_type=%s business_key=%s outcome=%s "
            "execution_id=%s",
            normalized_business_type,
            normalized_business_key,
            outcome,
            normalized_execution_id if outcome == "accepted" else "-",
        )
        result: Dict[str, Any] = {"outcome": outcome}
        if accepted_execution is not None:
            result["execution"] = accepted_execution
        return result

    def create_analysis_batch_if_allowed(
        self,
        *,
        batch_id: str,
        admissions: Sequence[AnalysisBatchTaskAdmission],
        accepted_at: str,
    ) -> Dict[str, Any]:
        """在一个短 ``BEGIN IMMEDIATE`` 事务中原子受理一批文件分析 execution。

        这是 1F-4 的唯一 Analysis 批量写入口。所有 JSON/Codec 校验都由调用方在锁外
        完成；本方法只在锁内复核活动投影、Callback Guard、批次身份、连续全局调度序号，
        并同时写入 ``llm_task_executions`` 与 ``llm_tasks``。任何插入、投影或读回校验
        失败都会回滚整批，绝不留下前半批 execution。

        返回的 ``outcome`` 是内部分类，不能直接当作 HTTP 状态码。成功后由 Application
        负责一次有界唤醒；该事务不发布 Progress、不创建线程，也不进行文件、HTTP 或模型 I/O。
        """

        normalized_batch_id = _required_internal_text(batch_id, name="batch_id")
        if not _ANALYSIS_BATCH_ID_PATTERN.fullmatch(normalized_batch_id):
            raise ValueError("batch_id必须是32位小写十六进制字符串")
        normalized_admissions = tuple(admissions)
        if not normalized_admissions:
            raise ValueError("admissions不能为空")
        if len(normalized_admissions) > _MAX_ANALYSIS_BATCH_ITEMS:
            raise ValueError(
                f"admissions数量不能超过{_MAX_ANALYSIS_BATCH_ITEMS}"
            )
        if any(
            not isinstance(item, AnalysisBatchTaskAdmission)
            for item in normalized_admissions
        ):
            raise TypeError("admissions只能包含AnalysisBatchTaskAdmission")
        execution_ids = tuple(item.execution_id for item in normalized_admissions)
        business_keys = tuple(item.business_key for item in normalized_admissions)
        if len(set(execution_ids)) != len(execution_ids):
            raise ValueError("admissions.execution_id不得重复")
        if len(set(business_keys)) != len(business_keys):
            raise ValueError("admissions.business_key不得重复")

        accepted_time = _aware_datetime(accepted_at, name="accepted_at")
        normalized_accepted_at = accepted_time.isoformat()
        accepted_epoch = accepted_time.timestamp()

        # 序列化及 Analysis 输入的身份复核必须在写锁外完成。这样因 Codec 或请求投影
        # 损坏失败时不会占用 SQLite 控制面锁，也不会形成“已受理但无法执行”的半事实。
        prepared: list[tuple[AnalysisBatchTaskAdmission, str, str, int]] = []
        for index, admission in enumerate(normalized_admissions, start=1):
            expected_public_status = "1" if index == 1 else "0"
            if admission.initial_public_status != expected_public_status:
                raise ValueError(
                    "Analysis批次首项必须使用公开处理中状态1，后续项必须使用等待状态0"
                )
            input_payload = dict(admission.input_payload)
            if (
                input_payload.get("task_id") != admission.execution_id
                or input_payload.get("batch_id") != normalized_batch_id
                or input_payload.get("batch_sequence") != index
                or input_payload.get("file_name") != admission.business_key
                or input_payload.get("accepted_at") != normalized_accepted_at
                or input_payload.get("trace_id") != admission.trace_id
            ):
                raise ValueError("Analysis任务输入与批次受理身份不一致")
            projection_payload = dict(admission.projection_request_payload)
            projection_params = projection_payload.get("params")
            if (
                projection_payload.get("businessType") != _ANALYSIS_BATCH_BUSINESS_TYPE
                or not isinstance(projection_params, list)
                or len(projection_params) != 1
                or not isinstance(projection_params[0], Mapping)
                or _required_internal_text(
                    projection_params[0].get("fileName"),
                    name="projection.fileName",
                )
                != admission.business_key
            ):
                raise ValueError("Analysis任务公开投影与业务键不一致")
            prepared.append(
                (
                    admission,
                    self._serialize(input_payload),
                    self._serialize(projection_payload),
                    index,
                )
            )

        outcome = ""
        accepted_executions: tuple[Dict[str, Any], ...] = ()
        first_dispatch_sequence: int | None = None
        try:
            with self._immediate_connection() as conn:
                # 必须在同一个写事务内逐项检查，而不是依赖路由层的 get_task 预查。请求
                # 顺序即冲突优先级；这样未来切路由后仍保持既有首个冲突项的公开错误语义。
                for admission, _, _, _ in prepared:
                    guard = conn.execute(
                        """
                        SELECT owner_execution_id, state, deadline_at
                        FROM callback_delivery_guards
                        WHERE business_type = ? AND business_key = ?
                        """,
                        (_ANALYSIS_BATCH_BUSINESS_TYPE, admission.business_key),
                    ).fetchone()
                    guard_state = guard["state"] if guard is not None else "idle"
                    if guard_state not in _CALLBACK_GUARD_STATES:
                        raise RuntimeError("callback Guard 存在未知状态")
                    if guard_state == "sending":
                        if _callback_guard_deadline_expired(
                            guard["deadline_at"],
                            observed_at=accepted_time,
                        ):
                            owner_execution_id = str(
                                guard["owner_execution_id"] or ""
                            ).strip()
                            if not owner_execution_id:
                                raise RuntimeError(
                                    "sending callback Guard 缺少 owner_execution_id"
                                )
                            transitioned = self._transition_callback_guard_to_unknown(
                                conn,
                                business_type=_ANALYSIS_BATCH_BUSINESS_TYPE,
                                business_key=admission.business_key,
                                owner_execution_id=owner_execution_id,
                                now=normalized_accepted_at,
                                reason=(
                                    "callback lease expired before analysis batch submission"
                                ),
                            )
                            if not transitioned:
                                raise RuntimeError(
                                    "Analysis受理时过期callback Guard未能冻结为outcome_unknown"
                                )
                            outcome = "callback_outcome_unknown"
                        else:
                            outcome = "callback_sending"
                    elif guard_state == "outcome_unknown":
                        outcome = "callback_outcome_unknown"
                    if outcome:
                        break

                    current = conn.execute(
                        """
                        SELECT execution_id, status, callback_status,
                               callback_claim_id, callback_claim_expires_at
                        FROM llm_tasks
                        WHERE business_type = ? AND business_key = ?
                        """,
                        (_ANALYSIS_BATCH_BUSINESS_TYPE, admission.business_key),
                    ).fetchone()
                    if current is None:
                        continue
                    callback_claim_in_flight = (
                        bool(current["callback_claim_id"])
                        and float(current["callback_claim_expires_at"] or 0)
                        > accepted_epoch
                    )
                    block_reason = file_task_admission_block_reason(
                        {
                            "status": current["status"],
                            "callback_status": current["callback_status"],
                        },
                        callback_delivery_in_flight=callback_claim_in_flight,
                    )
                    if block_reason == "processing":
                        outcome = "active_conflict"
                        break
                    if block_reason == "callback_pending":
                        outcome = "callback_pending"
                        break

                if not outcome:
                    # ``BEGIN IMMEDIATE`` 已经串行化不同请求的控制面写入。一次查询后
                    # 为整批预留连续区间，避免批内任务被其他请求交叉插入并破坏顺序。
                    first_dispatch_sequence = int(
                        conn.execute(
                            """
                            SELECT COALESCE(MAX(dispatch_sequence), 0) + 1
                            FROM llm_task_executions
                            """
                        ).fetchone()[0]
                    )
                    persisted_rows: list[Dict[str, Any]] = []
                    for admission, serialized_input, serialized_projection, sequence in prepared:
                        dispatch_sequence = first_dispatch_sequence + sequence - 1
                        conn.execute(
                            """
                            INSERT INTO llm_task_executions (
                                execution_id, business_type, business_key,
                                input_schema_version, input_payload,
                                batch_id, batch_sequence, dispatch_sequence,
                                execution_state, public_status, progress, message,
                                result_payload, callback_status, callback_outcome,
                                trace_id, created_at, started_at, completed_at, updated_at
                            )
                            VALUES (?, 'file', ?, ?, ?, ?, ?, ?, 'accepted', ?, 0,
                                    '', NULL, 'pending', '', ?, ?, NULL, NULL, ?)
                            """,
                            (
                                admission.execution_id,
                                admission.business_key,
                                admission.input_schema_version,
                                serialized_input,
                                normalized_batch_id,
                                sequence,
                                dispatch_sequence,
                                admission.initial_public_status,
                                admission.trace_id,
                                normalized_accepted_at,
                                normalized_accepted_at,
                            ),
                        )
                        cursor = conn.execute(
                            """
                            INSERT INTO llm_tasks (
                                business_type, business_key, execution_id,
                                request_payload, status, progress, message,
                                result_payload, callback_status, callback_attempts,
                                last_callback_error, callback_claim_id,
                                callback_claim_expires_at, created_at, updated_at
                            )
                            VALUES ('file', ?, ?, ?, ?, 0, '', NULL, 'pending', 0,
                                    '', '', 0, ?, ?)
                            ON CONFLICT(business_type, business_key) DO UPDATE SET
                                execution_id = excluded.execution_id,
                                request_payload = excluded.request_payload,
                                status = excluded.status,
                                progress = excluded.progress,
                                message = excluded.message,
                                result_payload = excluded.result_payload,
                                callback_status = excluded.callback_status,
                                callback_attempts = excluded.callback_attempts,
                                last_callback_error = excluded.last_callback_error,
                                callback_claim_id = excluded.callback_claim_id,
                                callback_claim_expires_at =
                                    excluded.callback_claim_expires_at,
                                created_at = excluded.created_at,
                                updated_at = excluded.updated_at
                            -- 事务内预查只能说明此刻未被阻断；条件写再收紧为旧文件
                            -- 受理的完成态集合，防止未来新增状态或异常写入被静默覆盖。
                            -- 同时清理已过期的旧回调租约，避免它残留在新 execution 上。
                            WHERE llm_tasks.status IN ('2', '3')
                              AND llm_tasks.callback_status
                                  IN ('success', 'failed', 'skipped')
                              AND (
                                  llm_tasks.callback_claim_id = ''
                                  OR llm_tasks.callback_claim_expires_at <= ?
                              )
                            """,
                            (
                                admission.business_key,
                                admission.execution_id,
                                serialized_projection,
                                admission.initial_public_status,
                                normalized_accepted_at,
                                normalized_accepted_at,
                                accepted_epoch,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("Analysis批次最新任务投影条件写未命中")
                        row = self._select_task_execution_row(
                            conn,
                            admission.execution_id,
                        )
                        if row is None:
                            raise RuntimeError("Analysis批次受理后未读取到execution")
                        persisted_rows.append(self._row_to_task_execution(row))

                    if len(persisted_rows) != len(prepared):
                        raise RuntimeError("Analysis批次execution插入数量不一致")
                    for (
                        (admission, _, _, sequence),
                        execution,
                    ) in zip(prepared, persisted_rows):
                        expected_dispatch_sequence = (
                            first_dispatch_sequence + sequence - 1
                        )
                        if (
                            execution["execution_id"] != admission.execution_id
                            or execution["business_type"]
                            != _ANALYSIS_BATCH_BUSINESS_TYPE
                            or execution["business_key"] != admission.business_key
                            or execution["batch_id"] != normalized_batch_id
                            or execution["batch_sequence"] != sequence
                            or execution["dispatch_sequence"]
                            != expected_dispatch_sequence
                            or execution["execution_state"] != "accepted"
                        ):
                            raise RuntimeError("Analysis批次execution读回身份或顺序不一致")
                        projection = conn.execute(
                            """
                            SELECT execution_id
                            FROM llm_tasks
                            WHERE business_type = 'file' AND business_key = ?
                            """,
                            (admission.business_key,),
                        ).fetchone()
                        if (
                            projection is None
                            or projection["execution_id"] != admission.execution_id
                        ):
                            raise RuntimeError("Analysis批次最新任务投影owner不一致")
                    accepted_executions = tuple(persisted_rows)
                    outcome = "accepted"
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                logger.warning(
                    "文件分析批次原子受理遇到SQLite繁忙: batch_id=%s item_count=%d",
                    normalized_batch_id,
                    len(prepared),
                )
                raise TaskAdmissionBusyError("任务库繁忙，请稍后重试") from exc
            raise

        if outcome == "accepted":
            logger.info(
                "文件分析批次已原子受理: batch_id=%s item_count=%d "
                "first_dispatch_sequence=%d",
                normalized_batch_id,
                len(accepted_executions),
                first_dispatch_sequence,
            )
        else:
            # 活动任务/Guard 冲突是调用方可预期的 409 分支，不应在高并发重试时淹没
            # 默认告警输出；仍以 INFO 保留 batch、数量和稳定 outcome 供诊断检索。
            logger.info(
                "文件分析批次未受理: batch_id=%s item_count=%d outcome=%s",
                normalized_batch_id,
                len(prepared),
                outcome,
            )
        result: Dict[str, Any] = {"outcome": outcome}
        if accepted_executions:
            result["executions"] = accepted_executions
        return result

    def validate_callback_delivery_guard(
        self,
        *,
        expected_execution_id: str,
        business_type: str,
        business_key: str,
        lease_token: str,
        fencing_token: int,
        validated_at: str,
    ) -> Dict[str, Any]:
        """在真正发起 HTTP 前原子复核一次 callback 发送权。

        ``acquire`` 是首次授权点，但 Worker 可能在取得租约后长时间暂停。该方法只执行
        latest/终态/Guard 条件读取和必要的过期冻结，不持有事务执行任何网络或文件 I/O。
        返回 ``valid=False`` 时调用方必须跳过 HTTP；过期租约会保守冻结为
        ``outcome_unknown``，等待受控人工核查。
        """

        execution_id = _required_internal_text(
            expected_execution_id,
            name="expected_execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_token = _required_internal_text(
            lease_token,
            name="lease_token",
        )
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ValueError("fencing_token必须是正整数")
        validated_time = _aware_datetime(validated_at, name="validated_at")
        normalized_validated_at = validated_time.isoformat()

        outcome = "invalid"
        valid = False
        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(conn, execution_id)
            latest = conn.execute(
                """
                SELECT execution_id, status
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if (
                execution is None
                or execution["business_type"] != normalized_business_type
                or execution["business_key"] != normalized_business_key
                or latest is None
                or latest["execution_id"] != execution_id
            ):
                outcome = "stale"
            elif (
                execution["execution_state"] not in {"succeeded", "failed"}
                or latest["status"]
                not in _COMPLETED_TASK_STATUSES.get(
                    normalized_business_type,
                    frozenset(),
                )
            ):
                outcome = "not_terminal"
            else:
                guard = conn.execute(
                    """
                    SELECT owner_execution_id, state, lease_token,
                           lease_version, deadline_at
                    FROM callback_delivery_guards
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (normalized_business_type, normalized_business_key),
                ).fetchone()
                if guard is None:
                    outcome = "missing"
                elif guard["state"] not in _CALLBACK_GUARD_STATES:
                    raise RuntimeError("callback Guard 存在未知状态")
                elif (
                    guard["state"] != "sending"
                    or str(guard["owner_execution_id"] or "").strip() != execution_id
                    or str(guard["lease_token"] or "").strip() != normalized_token
                    or int(guard["lease_version"]) != fencing_token
                ):
                    outcome = "lease_lost"
                else:
                    deadline_text = str(guard["deadline_at"] or "").strip()
                    expired = not deadline_text
                    if deadline_text:
                        try:
                            expired = (
                                _aware_datetime(deadline_text, name="deadline_at")
                                <= validated_time
                            )
                        except (TypeError, ValueError):
                            expired = True
                    if expired:
                        transitioned = self._transition_callback_guard_to_unknown(
                            conn,
                            business_type=normalized_business_type,
                            business_key=normalized_business_key,
                            owner_execution_id=execution_id,
                            now=normalized_validated_at,
                            reason="callback lease expired before HTTP delivery",
                        )
                        if not transitioned:
                            raise RuntimeError(
                                "发送前过期 callback Guard 未能冻结为 outcome_unknown"
                            )
                        outcome = "expired"
                    else:
                        valid = True
                        outcome = "valid"

        logger.log(
            logging.DEBUG if valid else logging.WARNING,
            "callback Guard 发送前复核完成: business_type=%s business_key=%s "
            "execution_id=%s outcome=%s fencing_token=%s",
            normalized_business_type,
            normalized_business_key,
            execution_id,
            outcome,
            fencing_token,
        )
        return {"valid": valid, "outcome": outcome}

    def get_task_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """按不可变 execution ID 读取追加事实，不回退到最新业务键。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        with self._connection() as conn:
            row = self._select_task_execution_row(conn, normalized_execution_id)
        return self._row_to_task_execution(row) if row is not None else None

    def get_analysis_task_execution_control_record(
        self,
        execution_id: str,
    ) -> Optional[Dict[str, Any]]:
        """读取 Analysis 控制面身份，不加载和反序列化可能很大的输入快照。

        expected 写、latest 检查和领取前冷却只需要 execution、业务键与批次身份。把这些
        高频操作与 ``input_payload`` 解码分开，既降低大领域树任务的 CPU/内存放大，也让
        后续毒快照收敛可以在不信任坏 payload 的前提下完成。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT execution_id, business_type, business_key,
                       input_schema_version, batch_id, batch_sequence,
                       dispatch_sequence, execution_state
                FROM llm_task_executions
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_analysis_callback_recovery_record(
        self,
        business_key: str,
    ) -> Optional[Dict[str, Any]]:
        """读取文件回调同步恢复所需的最小权威投影。

        恢复回调不需要原始请求或 execution 输入快照。这里在一个只读连接中联结 latest
        投影与 execution 身份，只反序列化最终回调 payload，避免大请求放大读取成本，
        也防止无关的损坏输入阻断一个仍可安全补发的终态结果。
        """

        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT task.execution_id, task.status, task.result_payload,
                       task.callback_status, task.callback_attempts, task.updated_at,
                       execution.business_type AS execution_business_type,
                       execution.business_key AS execution_business_key,
                       execution.batch_id, execution.batch_sequence,
                       execution.execution_state
                FROM llm_tasks AS task
                LEFT JOIN llm_task_executions AS execution
                  ON execution.execution_id = task.execution_id
                WHERE task.business_type = 'file' AND task.business_key = ?
                """,
                (normalized_business_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "execution_id": row["execution_id"],
            "status": row["status"],
            "result_payload": self._deserialize(row["result_payload"]),
            "callback_status": row["callback_status"],
            "callback_attempts": int(row["callback_attempts"]),
            "updated_at": row["updated_at"],
            "execution_business_type": row["execution_business_type"],
            "execution_business_key": row["execution_business_key"],
            "batch_id": row["batch_id"],
            "batch_sequence": row["batch_sequence"],
            "execution_state": row["execution_state"],
        }

    def claim_task_execution(self, execution_id: str) -> Dict[str, Any]:
        """条件领取一次 accepted execution，并返回可判定的幂等分类。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        outcome = ""
        execution: Dict[str, Any] | None = None
        with self._immediate_connection() as conn:
            row = self._select_task_execution_row(conn, normalized_execution_id)
            if row is None:
                outcome = "missing"
            else:
                state = row["execution_state"]
                if state not in _TASK_EXECUTION_STATES:
                    raise RuntimeError("execution 存在未知状态")
                if state == "stale":
                    outcome = "stale"
                elif state in {"succeeded", "failed"}:
                    outcome = "terminal"
                else:
                    latest = conn.execute(
                        """
                        SELECT execution_id
                        FROM llm_tasks
                        WHERE business_type = ? AND business_key = ?
                        """,
                        (row["business_type"], row["business_key"]),
                    ).fetchone()
                    if (
                        latest is None
                        or latest["execution_id"] != normalized_execution_id
                    ):
                        now = _utc_now_iso()
                        conn.execute(
                            """
                            UPDATE llm_task_executions
                            SET execution_state = 'stale',
                                completed_at = COALESCE(completed_at, ?),
                                updated_at = ?
                            WHERE execution_id = ?
                              AND execution_state IN ('accepted', 'running')
                            """,
                            (now, now, normalized_execution_id),
                        )
                        row = self._select_task_execution_row(
                            conn,
                            normalized_execution_id,
                        )
                        outcome = "stale"
                    elif state == "running":
                        outcome = "already_running"
                    else:
                        now = _utc_now_iso()
                        cursor = conn.execute(
                            """
                            UPDATE llm_task_executions
                            SET execution_state = 'running',
                                started_at = COALESCE(started_at, ?),
                                next_dispatch_at = NULL,
                                last_dispatch_error = '',
                                updated_at = ?
                            WHERE execution_id = ? AND execution_state = 'accepted'
                            """,
                            (now, now, normalized_execution_id),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("execution 领取条件写未命中")
                        row = self._select_task_execution_row(
                            conn,
                            normalized_execution_id,
                        )
                        outcome = "claimed"
                if row is None:
                    raise RuntimeError("领取分类后 execution 意外缺失")
                execution = self._row_to_task_execution(row)

        logger.info(
            "任务 execution 领取完成: execution_id=%s outcome=%s",
            normalized_execution_id,
            outcome,
        )
        result: Dict[str, Any] = {"outcome": outcome}
        if execution is not None:
            result["execution"] = execution
        return result

    def update_task_execution_progress_if_current(
        self,
        *,
        expected_execution_id: str,
        business_type: str,
        business_key: str,
        progress: float,
        message: str,
        execution_state: str,
        public_status: str,
    ) -> bool:
        """原子更新 execution 事实与最新投影；旧 owner 明确返回 ``False``。"""

        execution_id = _required_internal_text(
            expected_execution_id,
            name="expected_execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_progress = _strict_execution_progress(progress)
        if not isinstance(message, str):
            raise TypeError("message必须是str")
        normalized_state = _required_internal_text(
            execution_state,
            name="execution_state",
        )
        if normalized_state != "running":
            raise ValueError("进度 execution_state 必须是 running")
        normalized_public_status = _required_internal_text(
            public_status,
            name="public_status",
        )
        updated = False
        marked_stale = False
        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(conn, execution_id)
            latest = conn.execute(
                """
                SELECT execution_id
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if (
                execution is not None
                and execution["business_type"] == normalized_business_type
                and execution["business_key"] == normalized_business_key
                and execution["execution_state"] == "running"
                and latest is not None
                and latest["execution_id"] == execution_id
            ):
                now = _utc_now_iso()
                execution_cursor = conn.execute(
                    """
                    UPDATE llm_task_executions
                    SET execution_state = ?, public_status = ?, progress = ?,
                        message = ?, updated_at = ?
                    WHERE execution_id = ? AND execution_state = 'running'
                    """,
                    (
                        normalized_state,
                        normalized_public_status,
                        normalized_progress,
                        message,
                        now,
                        execution_id,
                    ),
                )
                projection_cursor = conn.execute(
                    """
                    UPDATE llm_tasks
                    SET status = ?, progress = ?, message = ?, updated_at = ?
                    WHERE business_type = ? AND business_key = ?
                      AND execution_id = ?
                    """,
                    (
                        normalized_public_status,
                        normalized_progress,
                        message,
                        now,
                        normalized_business_type,
                        normalized_business_key,
                        execution_id,
                    ),
                )
                if execution_cursor.rowcount != 1 or projection_cursor.rowcount != 1:
                    raise RuntimeError("进度事实与最新投影未能原子同步")
                updated = True
            if not updated:
                marked_stale = self._mark_execution_stale_if_superseded(
                    conn,
                    execution=execution,
                    latest=latest,
                    execution_id=execution_id,
                    business_type=normalized_business_type,
                    business_key=normalized_business_key,
                )

        if not updated:
            logger.info(
                "任务进度条件写发现旧或终态 execution: execution_id=%s "
                "business_type=%s business_key=%s marked_stale=%s",
                execution_id,
                normalized_business_type,
                normalized_business_key,
                marked_stale,
            )
        return updated

    def finish_task_execution_if_current(
        self,
        *,
        expected_execution_id: str,
        business_type: str,
        business_key: str,
        execution_state: str,
        public_status: str,
        message: str,
        execution_result_payload: Mapping[str, Any],
        projection_result_payload: Mapping[str, Any],
    ) -> bool:
        """原子提交终态 execution 与最新投影，禁止旧执行覆盖新 owner。"""

        execution_id = _required_internal_text(
            expected_execution_id,
            name="expected_execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_state = _required_internal_text(
            execution_state,
            name="execution_state",
        )
        if normalized_state not in {"succeeded", "failed"}:
            raise ValueError("终态 execution_state 只能是 succeeded 或 failed")
        normalized_public_status = _required_internal_text(
            public_status,
            name="public_status",
        )
        if not isinstance(message, str):
            raise TypeError("message必须是str")
        if not isinstance(execution_result_payload, Mapping):
            raise TypeError("execution_result_payload必须是Mapping")
        if not isinstance(projection_result_payload, Mapping):
            raise TypeError("projection_result_payload必须是Mapping")
        # 两份 JSON 都在进入写事务前完成编码。任何不可序列化值都不能占用 SQLite 写锁，
        # 更不能造成 execution 与公开投影只提交一侧。
        serialized_execution_result = self._serialize(
            dict(execution_result_payload)
        )
        serialized_projection_result = self._serialize(
            dict(projection_result_payload)
        )
        finished = False
        marked_stale = False
        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(conn, execution_id)
            latest = conn.execute(
                """
                SELECT execution_id
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if (
                execution is not None
                and execution["business_type"] == normalized_business_type
                and execution["business_key"] == normalized_business_key
                and execution["execution_state"] == "running"
                and latest is not None
                and latest["execution_id"] == execution_id
            ):
                now = _utc_now_iso()
                execution_cursor = conn.execute(
                    """
                    UPDATE llm_task_executions
                    SET execution_state = ?, public_status = ?, progress = 1,
                        message = ?, result_payload = ?, completed_at = ?, updated_at = ?
                    WHERE execution_id = ? AND execution_state = 'running'
                    """,
                    (
                        normalized_state,
                        normalized_public_status,
                        message,
                        serialized_execution_result,
                        now,
                        now,
                        execution_id,
                    ),
                )
                projection_cursor = conn.execute(
                    """
                    UPDATE llm_tasks
                    SET status = ?, progress = 1, message = ?,
                        result_payload = ?, updated_at = ?
                    WHERE business_type = ? AND business_key = ?
                      AND execution_id = ?
                    """,
                    (
                        normalized_public_status,
                        message,
                        serialized_projection_result,
                        now,
                        normalized_business_type,
                        normalized_business_key,
                        execution_id,
                    ),
                )
                if execution_cursor.rowcount != 1 or projection_cursor.rowcount != 1:
                    raise RuntimeError("终态事实与最新投影未能原子同步")
                finished = True
            if not finished:
                marked_stale = self._mark_execution_stale_if_superseded(
                    conn,
                    execution=execution,
                    latest=latest,
                    execution_id=execution_id,
                    business_type=normalized_business_type,
                    business_key=normalized_business_key,
                )

        if not finished:
            logger.info(
                "任务终态条件写发现旧或终态 execution: execution_id=%s "
                "business_type=%s business_key=%s marked_stale=%s",
                execution_id,
                normalized_business_type,
                normalized_business_key,
                marked_stale,
            )
        return finished

    def is_task_execution_latest(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
    ) -> bool:
        """判断 execution 是否仍拥有对应 ``llm_tasks`` 最新投影。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ? AND execution_id = ?
                """,
                (
                    normalized_business_type,
                    normalized_business_key,
                    normalized_execution_id,
                ),
            ).fetchone()
        return row is not None

    @staticmethod
    def _append_callback_delivery_attempt_event(
        conn: sqlite3.Connection,
        *,
        business_type: str,
        business_key: str,
        owner_execution_id: str,
        callback_attempt: int,
        lease_version: int,
        trigger: str,
        event_type: str,
        delivery_outcome: str,
        request_trace_id: str,
        occurred_at: str,
    ) -> None:
        """在调用方事务内追加一条 Callback attempt 事件。

        本方法故意不捕获 SQLite 错误。授权、完成或冻结只要无法留下对应审计，就必须
        回滚同一事务中的 Guard、execution 与最新投影，避免出现“已经允许发送但没有
        可追溯授权原因”的分裂事实。
        """

        if trigger not in _CALLBACK_DELIVERY_TRIGGERS:
            raise ValueError("未知 callback delivery trigger")
        if event_type not in _CALLBACK_ATTEMPT_EVENT_TYPES:
            raise ValueError("未知 callback attempt event_type")
        conn.execute(
            """
            INSERT INTO callback_delivery_attempt_events (
                business_type, business_key, owner_execution_id,
                callback_attempt, lease_version, trigger, event_type,
                delivery_outcome, request_trace_id, occurred_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                business_type,
                business_key,
                owner_execution_id,
                callback_attempt,
                lease_version,
                trigger,
                event_type,
                delivery_outcome,
                request_trace_id,
                occurred_at,
            ),
        )

    @classmethod
    def _append_callback_delivery_attempt_followup_event(
        cls,
        conn: sqlite3.Connection,
        *,
        business_type: str,
        business_key: str,
        owner_execution_id: str,
        lease_version: int,
        event_type: str,
        delivery_outcome: str,
        occurred_at: str,
    ) -> bool:
        """沿用授权事件的 attempt、trigger 与 trace 追加收敛事件。

        升级前已经处于 sending 的历史 Guard 没有授权事件，允许继续按旧逻辑收敛，
        因此返回 ``False`` 而不是伪造 trigger。升级后取得的租约一定先在同一事务写入
        authorized；其审计插入异常会直接回滚调用方事务。
        """

        authorization = conn.execute(
            """
            SELECT callback_attempt, trigger, request_trace_id
            FROM callback_delivery_attempt_events
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND lease_version = ?
              AND event_type = 'authorized'
            """,
            (
                business_type,
                business_key,
                owner_execution_id,
                lease_version,
            ),
        ).fetchone()
        if authorization is None:
            return False
        cls._append_callback_delivery_attempt_event(
            conn,
            business_type=business_type,
            business_key=business_key,
            owner_execution_id=owner_execution_id,
            callback_attempt=int(authorization["callback_attempt"]),
            lease_version=lease_version,
            trigger=str(authorization["trigger"]),
            event_type=event_type,
            delivery_outcome=delivery_outcome,
            request_trace_id=str(authorization["request_trace_id"]),
            occurred_at=occurred_at,
        )
        return True

    @classmethod
    def _transition_callback_guard_to_unknown(
        cls,
        conn: sqlite3.Connection,
        *,
        business_type: str,
        business_key: str,
        owner_execution_id: str,
        now: str,
        reason: str,
    ) -> bool:
        """把过期 sending 租约冻结为 outcome_unknown，并同步当前任务投影。"""

        cursor = conn.execute(
            """
            UPDATE callback_delivery_guards
            SET state = 'outcome_unknown', lease_token = '',
                deadline_at = NULL, last_outcome = 'delivery_outcome_unknown',
                error_stage = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ? AND state = 'sending'
            """,
            (
                reason,
                now,
                business_type,
                business_key,
                owner_execution_id,
            ),
        )
        if cursor.rowcount != 1:
            return False
        guard = conn.execute(
            """
            SELECT lease_version
            FROM callback_delivery_guards
            WHERE business_type = ? AND business_key = ?
              AND owner_execution_id = ?
            """,
            (business_type, business_key, owner_execution_id),
        ).fetchone()
        if guard is None:
            raise RuntimeError("callback Guard 冻结后无法读取 fencing 版本")
        cls._append_callback_delivery_attempt_followup_event(
            conn,
            business_type=business_type,
            business_key=business_key,
            owner_execution_id=owner_execution_id,
            lease_version=int(guard["lease_version"]),
            event_type="lease_expired_unknown",
            delivery_outcome="delivery_outcome_unknown",
            occurred_at=now,
        )
        conn.execute(
            """
            UPDATE llm_task_executions
            SET callback_status = 'outcome_unknown',
                callback_outcome = 'delivery_outcome_unknown', updated_at = ?
            WHERE execution_id = ? AND business_type = ? AND business_key = ?
            """,
            (now, owner_execution_id, business_type, business_key),
        )
        # 只有旧执行仍是最新投影时才同步公开查询表。若遗留路径绕过 Guard 强行覆盖
        # owner，本 Guard 仍保持 unknown 冻结，但绝不能覆盖新任务的 callback 状态。
        conn.execute(
            """
            UPDATE llm_tasks
            SET callback_status = 'outcome_unknown',
                last_callback_error = ?, updated_at = ?
            WHERE business_type = ? AND business_key = ? AND execution_id = ?
            """,
            (
                reason,
                now,
                business_type,
                business_key,
                owner_execution_id,
            ),
        )
        return True

    def acquire_callback_delivery_guard(
        self,
        *,
        expected_execution_id: str,
        business_type: str,
        business_key: str,
        lease_token: str,
        lease_seconds: float,
        acquired_at: str,
        allow_failed_retry: bool = False,
        allow_outcome_unknown_retry: bool = False,
        expected_callback_attempts: int | None = None,
        delivery_trigger: str = "initial_delivery",
        request_trace_id: str = "",
    ) -> Dict[str, Any]:
        """原子复核 latest owner 并取得带 fencing token 的回调发送权。

        该事务是 HTTP 投递的唯一授权点。调用方在事务外执行的 ``is_latest`` 只能减少
        无效请求，不能替代这里的复核。过期 sending 租约不会被自动重抢，因为旧 HTTP
        请求是否到达接收方已经无法证明；后台路径必须冻结为 ``outcome_unknown``。
        只有业务白名单内的显式 ``/llm/check-task`` 恢复可以请求 at-least-once
        unknown 补发，并继续受 callback_attempts、latest owner、Guard owner 与
        fencing token 约束。trigger 与 trace 只进入内部追加式审计，不属于公开接口。
        """

        execution_id = _required_internal_text(
            expected_execution_id,
            name="expected_execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_token = _required_internal_text(
            lease_token,
            name="lease_token",
        )
        normalized_trigger = _required_internal_text(
            delivery_trigger,
            name="delivery_trigger",
        )
        if normalized_trigger not in _CALLBACK_DELIVERY_TRIGGERS:
            raise ValueError("未知 callback delivery_trigger")
        if not isinstance(request_trace_id, str):
            raise TypeError("request_trace_id必须是str")
        normalized_trace_id = request_trace_id.strip()
        if len(normalized_trace_id) > 128:
            raise ValueError("request_trace_id最多128个字符")
        if isinstance(lease_seconds, bool) or not isinstance(
            lease_seconds,
            (int, float),
        ):
            raise TypeError("lease_seconds必须是数字")
        normalized_lease_seconds = float(lease_seconds)
        if (
            normalized_lease_seconds != normalized_lease_seconds
            or normalized_lease_seconds in (float("inf"), float("-inf"))
            or normalized_lease_seconds <= 0.0
        ):
            raise ValueError("lease_seconds必须是正有限数字")
        if not isinstance(allow_failed_retry, bool):
            raise TypeError("allow_failed_retry必须是bool")
        if not isinstance(allow_outcome_unknown_retry, bool):
            raise TypeError("allow_outcome_unknown_retry必须是bool")
        if expected_callback_attempts is not None and (
            isinstance(expected_callback_attempts, bool)
            or not isinstance(expected_callback_attempts, int)
            or expected_callback_attempts < 0
        ):
            raise ValueError("expected_callback_attempts必须是非负整数或None")
        if expected_callback_attempts is not None and not allow_failed_retry:
            raise ValueError(
                "expected_callback_attempts只允许用于同步失败恢复"
            )
        if allow_outcome_unknown_retry and (
            normalized_business_type
            not in _EXPLICIT_UNKNOWN_RETRY_BUSINESS_TYPES
            or not allow_failed_retry
            or expected_callback_attempts is None
            or normalized_trigger != "explicit_check_task_recovery"
        ):
            raise ValueError(
                "outcome_unknown显式补发只允许白名单业务的check-task携带attempt快照"
            )
        if (
            normalized_business_type == _ANALYSIS_BATCH_BUSINESS_TYPE
            and allow_failed_retry
            and expected_callback_attempts is None
        ):
            raise ValueError(
                "Analysis同步失败恢复必须携带expected_callback_attempts"
            )
        acquired_time = _aware_datetime(acquired_at, name="acquired_at")
        normalized_acquired_at = acquired_time.isoformat()
        deadline_at = (
            acquired_time + timedelta(seconds=normalized_lease_seconds)
        ).isoformat()

        result: Dict[str, Any] = {"outcome": ""}
        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(conn, execution_id)
            latest = conn.execute(
                """
                SELECT execution_id, status, callback_status, callback_attempts
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if (
                execution is None
                or execution["business_type"] != normalized_business_type
                or execution["business_key"] != normalized_business_key
                or latest is None
                or latest["execution_id"] != execution_id
            ):
                result["outcome"] = "stale"
            elif (
                expected_callback_attempts is not None
                and int(latest["callback_attempts"]) != expected_callback_attempts
            ):
                # 首个恢复者取得租约时会在同一事务内递增 callback_attempts。等待者必须
                # 继续使用首次读取的 attempt 快照，不能把同一批并发请求滚动成下一轮
                # 明确失败重试。
                result["outcome"] = "stale"
            elif (
                execution["execution_state"] not in {"succeeded", "failed"}
                or latest["status"]
                not in _COMPLETED_TASK_STATUSES.get(
                    normalized_business_type,
                    frozenset(),
                )
            ):
                raise ValueError("只有已提交终态的 latest execution 可以取得回调权")
            elif execution["callback_status"] in {"success", "skipped"}:
                # rejected/definitely-not-sent 在阶段 1C 同样是一次已经形成明确结果的投递。
                # 普通 Worker 重放不能绕过“不自动重试”口径再次发送；未来可靠恢复必须使用
                # 独立、可审计的 recovery command，而不是伪装成首次 acquire。
                result["outcome"] = "already_completed"
            elif (
                execution["callback_status"] == "failed"
                and not allow_failed_retry
            ):
                result["outcome"] = "already_completed"
            elif (
                execution["callback_status"] == "outcome_unknown"
                and not allow_outcome_unknown_retry
            ):
                result["outcome"] = "outcome_unknown"
            # 并发调用者可能在等待 ``BEGIN IMMEDIATE`` 写锁期间，观察到前一个调用者已将
            # execution 标记为 sending。sending 不是损坏状态，后续必须继续读取 Guard，
            # 由同一租约事实返回 busy 或在超时后保守冻结为 outcome_unknown。
            elif execution["callback_status"] not in {
                "pending",
                "failed",
                "sending",
                "outcome_unknown",
            }:
                raise RuntimeError("execution 存在未知 callback_status")
            else:
                guard = conn.execute(
                    """
                    SELECT owner_execution_id, state, lease_token,
                           lease_version, deadline_at
                    FROM callback_delivery_guards
                    WHERE business_type = ? AND business_key = ?
                    """,
                    (normalized_business_type, normalized_business_key),
                ).fetchone()
                if guard is not None and guard["state"] not in _CALLBACK_GUARD_STATES:
                    raise RuntimeError("callback Guard 存在未知状态")
                explicit_unknown_retry = (
                    allow_outcome_unknown_retry
                    and execution["callback_status"] == "outcome_unknown"
                    and latest["callback_status"] == "outcome_unknown"
                    and guard is not None
                    and guard["state"] == "outcome_unknown"
                    and guard["owner_execution_id"] == execution_id
                )
                if (
                    guard is not None
                    and guard["state"] == "outcome_unknown"
                    and not explicit_unknown_retry
                ):
                    result["outcome"] = "outcome_unknown"
                elif guard is not None and guard["state"] == "sending":
                    deadline_text = guard["deadline_at"]
                    deadline_expired = _callback_guard_deadline_expired(
                        deadline_text,
                        observed_at=acquired_time,
                    )
                    if deadline_expired:
                        owner_execution_id = str(
                            guard["owner_execution_id"] or ""
                        ).strip()
                        if not owner_execution_id:
                            raise RuntimeError(
                                "sending callback Guard 缺少 owner_execution_id"
                            )
                        transitioned = self._transition_callback_guard_to_unknown(
                            conn,
                            business_type=normalized_business_type,
                            business_key=normalized_business_key,
                            owner_execution_id=owner_execution_id,
                            now=normalized_acquired_at,
                            reason="callback lease expired before completion",
                        )
                        if not transitioned:
                            raise RuntimeError(
                                "过期 callback Guard 未能原子冻结为 outcome_unknown"
                            )
                        result["outcome"] = "outcome_unknown"
                    else:
                        result["outcome"] = "busy"
                elif execution["callback_status"] == "sending":
                    # execution 已是 sending，但 Guard 却不存在或已经回到 idle，说明两个
                    # 持久化事实不一致。此时绝不能重新取得发送权，否则可能重复通知甲方。
                    # 该分支只处理异常/人工改库场景；正常并发会在上面的 sending Guard
                    # 分支返回 busy。
                    conn.execute(
                        """
                        UPDATE llm_task_executions
                        SET callback_status = 'outcome_unknown',
                            callback_outcome = 'guard_state_inconsistent',
                            updated_at = ?
                        WHERE execution_id = ?
                        """,
                        (normalized_acquired_at, execution_id),
                    )
                    conn.execute(
                        """
                        UPDATE llm_tasks
                        SET callback_status = 'outcome_unknown',
                            last_callback_error = ?, updated_at = ?
                        WHERE business_type = ? AND business_key = ?
                          AND execution_id = ?
                        """,
                        (
                            "callback guard state inconsistent",
                            normalized_acquired_at,
                            normalized_business_type,
                            normalized_business_key,
                            execution_id,
                        ),
                    )
                    if guard is None:
                        conn.execute(
                            """
                            INSERT INTO callback_delivery_guards (
                                business_type, business_key, owner_execution_id,
                                state, lease_token, lease_version,
                                lease_started_at, deadline_at, last_outcome,
                                error_stage, released_at, released_by,
                                release_reason, updated_at
                            )
                            VALUES (?, ?, ?, 'outcome_unknown', '', 0, NULL, NULL,
                                    'guard_state_inconsistent',
                                    'guard_state_inconsistent', NULL, '', '', ?)
                            """,
                            (
                                normalized_business_type,
                                normalized_business_key,
                                execution_id,
                                normalized_acquired_at,
                            ),
                        )
                        inconsistent_lease_version = 0
                    else:
                        conn.execute(
                            """
                            UPDATE callback_delivery_guards
                            SET owner_execution_id = ?, state = 'outcome_unknown',
                                lease_token = '', deadline_at = NULL,
                                last_outcome = 'guard_state_inconsistent',
                                error_stage = 'guard_state_inconsistent',
                                updated_at = ?
                            WHERE business_type = ? AND business_key = ?
                            """,
                            (
                                execution_id,
                                normalized_acquired_at,
                                normalized_business_type,
                                normalized_business_key,
                            ),
                        )
                        inconsistent_lease_version = int(guard["lease_version"])
                    # 该分支是损坏状态的保守冻结，不构成新的发送授权。记录投影当前
                    # attempt 便于审计定位，但绝不能递增 callback_attempts。
                    self._append_callback_delivery_attempt_event(
                        conn,
                        business_type=normalized_business_type,
                        business_key=normalized_business_key,
                        owner_execution_id=execution_id,
                        callback_attempt=int(latest["callback_attempts"]),
                        lease_version=inconsistent_lease_version,
                        trigger=normalized_trigger,
                        event_type="guard_inconsistent_unknown",
                        delivery_outcome="guard_state_inconsistent",
                        request_trace_id=normalized_trace_id,
                        occurred_at=normalized_acquired_at,
                    )
                    logger.critical(
                        "callback execution 与 Guard 状态不一致，已冻结为 outcome_unknown: "
                        "business_type=%s business_key=%s execution_id=%s guard_state=%s",
                        normalized_business_type,
                        normalized_business_key,
                        execution_id,
                        guard["state"] if guard is not None else "missing",
                    )
                    result["outcome"] = "outcome_unknown"
                elif (
                    execution["callback_status"] == "outcome_unknown"
                    and not explicit_unknown_retry
                ):
                    # 未知结果的 execution、latest 投影与 Guard owner 必须一致，才允许
                    # 显式 at-least-once 补发。人工改库或旧版本残留继续 fail closed。
                    logger.critical(
                        "callback outcome_unknown事实不一致，拒绝显式补发: "
                        "business_type=%s business_key=%s execution_id=%s "
                        "projection_status=%s guard_state=%s guard_owner=%s",
                        normalized_business_type,
                        normalized_business_key,
                        execution_id,
                        latest["callback_status"],
                        guard["state"] if guard is not None else "missing",
                        (
                            guard["owner_execution_id"]
                            if guard is not None
                            else "missing"
                        ),
                    )
                    result["outcome"] = "outcome_unknown"
                else:
                    previous_version = int(guard["lease_version"]) if guard else 0
                    fencing_token = previous_version + 1
                    if guard is None:
                        conn.execute(
                            """
                            INSERT INTO callback_delivery_guards (
                                business_type, business_key, owner_execution_id,
                                state, lease_token, lease_version,
                                lease_started_at, deadline_at, last_outcome,
                                error_stage, released_at, released_by,
                                release_reason, updated_at
                            )
                            VALUES (?, ?, ?, 'sending', ?, ?, ?, ?, '', '',
                                    NULL, '', '', ?)
                            """,
                            (
                                normalized_business_type,
                                normalized_business_key,
                                execution_id,
                                normalized_token,
                                fencing_token,
                                normalized_acquired_at,
                                deadline_at,
                                normalized_acquired_at,
                            ),
                        )
                    else:
                        expected_guard_state = (
                            "outcome_unknown"
                            if explicit_unknown_retry
                            else "idle"
                        )
                        cursor = conn.execute(
                            """
                            UPDATE callback_delivery_guards
                            SET owner_execution_id = ?, state = 'sending',
                                lease_token = ?, lease_version = ?,
                                lease_started_at = ?, deadline_at = ?,
                                last_outcome = '', error_stage = '',
                                released_at = NULL, released_by = '',
                                release_reason = '', updated_at = ?
                            WHERE business_type = ? AND business_key = ?
                              AND state = ? AND lease_version = ?
                              AND (? = 'idle' OR owner_execution_id = ?)
                            """,
                            (
                                execution_id,
                                normalized_token,
                                fencing_token,
                                normalized_acquired_at,
                                deadline_at,
                                normalized_acquired_at,
                                normalized_business_type,
                                normalized_business_key,
                                expected_guard_state,
                                previous_version,
                                expected_guard_state,
                                execution_id,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("callback Guard fencing 条件写未命中")
                    conn.execute(
                        """
                        UPDATE llm_task_executions
                        SET callback_status = 'sending', callback_outcome = '',
                            updated_at = ?
                        WHERE execution_id = ?
                        """,
                        (normalized_acquired_at, execution_id),
                    )
                    projection_cursor = conn.execute(
                        """
                        UPDATE llm_tasks
                        SET callback_status = 'sending',
                            callback_attempts = callback_attempts + 1,
                            last_callback_error = '', updated_at = ?
                        WHERE business_type = ? AND business_key = ?
                          AND execution_id = ?
                        """,
                        (
                            normalized_acquired_at,
                            normalized_business_type,
                            normalized_business_key,
                            execution_id,
                        ),
                    )
                    if projection_cursor.rowcount != 1:
                        raise RuntimeError("callback Guard 与最新投影未能原子同步")
                    callback_attempt = int(latest["callback_attempts"]) + 1
                    self._append_callback_delivery_attempt_event(
                        conn,
                        business_type=normalized_business_type,
                        business_key=normalized_business_key,
                        owner_execution_id=execution_id,
                        callback_attempt=callback_attempt,
                        lease_version=fencing_token,
                        trigger=normalized_trigger,
                        event_type="authorized",
                        delivery_outcome="",
                        request_trace_id=normalized_trace_id,
                        occurred_at=normalized_acquired_at,
                    )
                    result.update(
                        {
                            "outcome": "acquired",
                            "lease_token": normalized_token,
                            "fencing_token": fencing_token,
                            "deadline_at": deadline_at,
                        }
                    )
                    if explicit_unknown_retry:
                        logger.warning(
                            "check-task 已显式授权 outcome_unknown 至少一次补发，"
                            "接收方可能收到重复业务回调: business_key=%s "
                            "business_type=%s execution_id=%s "
                            "expected_callback_attempts=%s fencing_token=%s trace_id=%s",
                            normalized_business_key,
                            normalized_business_type,
                            execution_id,
                            expected_callback_attempts,
                            fencing_token,
                            normalized_trace_id or "-",
                        )

        logger.info(
            "callback Guard 获取完成: business_type=%s business_key=%s "
            "execution_id=%s outcome=%s fencing_token=%s trigger=%s trace_id=%s",
            normalized_business_type,
            normalized_business_key,
            execution_id,
            result["outcome"],
            result.get("fencing_token", "-"),
            normalized_trigger,
            normalized_trace_id or "-",
        )
        return result

    def complete_callback_delivery_guard(
        self,
        *,
        expected_execution_id: str,
        business_type: str,
        business_key: str,
        lease_token: str,
        fencing_token: int,
        delivery_outcome: str,
        detail: str,
        completed_at: str,
    ) -> bool:
        """使用 token 与 fencing token 完成一次回调，拒绝迟到 Worker 覆盖新租约。"""

        execution_id = _required_internal_text(
            expected_execution_id,
            name="expected_execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_token = _required_internal_text(lease_token, name="lease_token")
        if (
            isinstance(fencing_token, bool)
            or not isinstance(fencing_token, int)
            or fencing_token <= 0
        ):
            raise ValueError("fencing_token必须是正整数")
        normalized_outcome = _required_internal_text(
            delivery_outcome,
            name="delivery_outcome",
        )
        if normalized_outcome not in _CALLBACK_DELIVERY_OUTCOMES:
            raise ValueError("未知 callback delivery_outcome")
        if not isinstance(detail, str):
            raise TypeError("detail必须是str")
        normalized_completed_at = _aware_datetime(
            completed_at,
            name="completed_at",
        ).isoformat()

        if normalized_outcome == "delivery_outcome_unknown":
            guard_state = "outcome_unknown"
            callback_status = "outcome_unknown"
            projection_status = "outcome_unknown"
        elif normalized_outcome == "success":
            guard_state = "idle"
            callback_status = "success"
            projection_status = "success"
        elif normalized_outcome in {"skipped", "stale"}:
            guard_state = "idle"
            callback_status = "skipped"
            projection_status = "skipped"
        else:
            guard_state = "idle"
            callback_status = "failed"
            projection_status = "failed"

        completed = False
        with self._immediate_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE callback_delivery_guards
                SET state = ?, lease_token = '', deadline_at = NULL,
                    last_outcome = ?, error_stage = ?,
                    released_at = ?, released_by = ?, release_reason = ?,
                    updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND owner_execution_id = ? AND state = 'sending'
                  AND lease_token = ? AND lease_version = ?
                """,
                (
                    guard_state,
                    normalized_outcome,
                    "" if normalized_outcome == "success" else "delivery",
                    normalized_completed_at if guard_state == "idle" else None,
                    execution_id if guard_state == "idle" else "",
                    normalized_outcome if guard_state == "idle" else "",
                    normalized_completed_at,
                    normalized_business_type,
                    normalized_business_key,
                    execution_id,
                    normalized_token,
                    fencing_token,
                ),
            )
            if cursor.rowcount == 1:
                execution_cursor = conn.execute(
                    """
                    UPDATE llm_task_executions
                    SET callback_status = ?, callback_outcome = ?, updated_at = ?
                    WHERE execution_id = ? AND business_type = ? AND business_key = ?
                    """,
                    (
                        callback_status,
                        normalized_outcome,
                        normalized_completed_at,
                        execution_id,
                        normalized_business_type,
                        normalized_business_key,
                    ),
                )
                if execution_cursor.rowcount != 1:
                    raise RuntimeError("callback 完成时 execution 身份不存在")
                projection_cursor = conn.execute(
                    """
                    UPDATE llm_tasks
                    SET callback_status = ?, last_callback_error = ?, updated_at = ?
                    WHERE business_type = ? AND business_key = ?
                      AND execution_id = ?
                    """,
                    (
                        projection_status,
                        "" if normalized_outcome == "success" else detail,
                        normalized_completed_at,
                        normalized_business_type,
                        normalized_business_key,
                        execution_id,
                    ),
                )
                if projection_cursor.rowcount != 1:
                    # 三份权威事实必须在同一事务共同完成。若 latest 投影异常丢失或已
                    # 切换，抛错会回滚 Guard 与 execution 更新；外部 HTTP 可能已送达，
                    # 调用方不得在当前请求内再次发送。
                    raise RuntimeError("callback 完成时最新投影不存在或已切换")
                self._append_callback_delivery_attempt_followup_event(
                    conn,
                    business_type=normalized_business_type,
                    business_key=normalized_business_key,
                    owner_execution_id=execution_id,
                    lease_version=fencing_token,
                    event_type="completed",
                    delivery_outcome=normalized_outcome,
                    occurred_at=normalized_completed_at,
                )
                completed = True

        if not completed:
            logger.warning(
                "callback Guard 完成CAS未命中: business_type=%s business_key=%s "
                "execution_id=%s fencing_token=%s outcome=%s",
                normalized_business_type,
                normalized_business_key,
                execution_id,
                fencing_token,
                normalized_outcome,
            )
        return completed

    def observe_callback_delivery_guard(
        self,
        *,
        business_type: str,
        business_key: str,
        observed_at: str,
    ) -> Dict[str, Any]:
        """供事务外有界等待读取 Guard；过期 sending 会原子冻结为 unknown。"""

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        observed_time = _aware_datetime(observed_at, name="observed_at")
        normalized_observed_at = observed_time.isoformat()
        # 常规轮询只使用读连接，避免 50 个等待请求为了观察未过期 sending 租约而串行
        # 争抢 SQLite 写锁。只有确认租约可能过期时才进入一次短 BEGIN IMMEDIATE 复核。
        with self._connection() as conn:
            guard = conn.execute(
                """
                SELECT owner_execution_id, state, deadline_at, lease_version
                FROM callback_delivery_guards
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if guard is None:
                return {"state": "idle", "deadline_at": None}
            state = guard["state"]
            if state not in _CALLBACK_GUARD_STATES:
                raise RuntimeError("callback Guard 存在未知状态")
            deadline_text = guard["deadline_at"]
            if state != "sending":
                return {
                    "state": state,
                    "deadline_at": deadline_text,
                    "fencing_token": int(guard["lease_version"]),
                }
            expired = not deadline_text
            if deadline_text:
                try:
                    expired = (
                        _aware_datetime(deadline_text, name="deadline_at")
                        <= observed_time
                    )
                except (TypeError, ValueError):
                    expired = True
            if not expired:
                return {
                    "state": state,
                    "deadline_at": deadline_text,
                    "fencing_token": int(guard["lease_version"]),
                }

        with self._immediate_connection() as conn:
            current = conn.execute(
                """
                SELECT owner_execution_id, state, deadline_at, lease_version
                FROM callback_delivery_guards
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if current is None:
                return {"state": "idle", "deadline_at": None}
            state = current["state"]
            if state == "sending":
                current_deadline = current["deadline_at"]
                still_expired = not current_deadline
                if current_deadline:
                    try:
                        still_expired = (
                            _aware_datetime(
                                current_deadline,
                                name="deadline_at",
                            )
                            <= observed_time
                        )
                    except (TypeError, ValueError):
                        still_expired = True
                if still_expired:
                    owner_execution_id = str(
                        current["owner_execution_id"] or ""
                    ).strip()
                    if not owner_execution_id:
                        raise RuntimeError("sending callback Guard 缺少 owner_execution_id")
                    transitioned = self._transition_callback_guard_to_unknown(
                        conn,
                        business_type=normalized_business_type,
                        business_key=normalized_business_key,
                        owner_execution_id=owner_execution_id,
                        now=normalized_observed_at,
                        reason="callback lease expired while waiting",
                    )
                    if not transitioned:
                        raise RuntimeError(
                            "过期 callback Guard 未能原子冻结为 outcome_unknown"
                        )
                    state = "outcome_unknown"
                    current_deadline = None
                return {
                    "state": state,
                    "deadline_at": current_deadline,
                    "fencing_token": int(current["lease_version"]),
                }
            if state not in _CALLBACK_GUARD_STATES:
                raise RuntimeError("callback Guard 存在未知状态")
            return {
                "state": state,
                "deadline_at": current["deadline_at"],
                "fencing_token": int(current["lease_version"]),
            }

    def freeze_expired_callback_delivery_guards(
        self,
        *,
        business_type: str,
        limit: int,
        observed_at: str,
    ) -> Dict[str, int]:
        """有界扫描并冻结失联 Worker 留下的过期 ``sending`` Guard。

        扫描只负责把“仍在发送”收敛为“结果未知”，不会取得新发送权，也不会触发 HTTP。
        候选读取和最终 CAS 分离：即使正常 Worker 在两者之间完成，写事务也只会跳过已经
        不再是 ``sending`` 的记录。这样维护任务不会长时间持有 SQLite 写锁。
        """

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit必须是1~1000的整数")
        observed_time = _aware_datetime(observed_at, name="observed_at")
        normalized_observed_at = observed_time.isoformat()

        with self._connection() as conn:
            candidates = conn.execute(
                """
                SELECT business_key
                FROM callback_delivery_guards
                WHERE business_type = ? AND state = 'sending'
                  AND (
                      deadline_at IS NULL
                      OR julianday(deadline_at) IS NULL
                      OR julianday(deadline_at) <= julianday(?)
                  )
                ORDER BY julianday(deadline_at), business_key
                LIMIT ?
                """,
                (normalized_business_type, normalized_observed_at, limit),
            ).fetchall()

        frozen_count = 0
        if candidates:
            with self._immediate_connection() as conn:
                for candidate in candidates:
                    business_key = str(candidate["business_key"])
                    current = conn.execute(
                        """
                        SELECT owner_execution_id, state, deadline_at
                        FROM callback_delivery_guards
                        WHERE business_type = ? AND business_key = ?
                        """,
                        (normalized_business_type, business_key),
                    ).fetchone()
                    if current is None or current["state"] != "sending":
                        continue
                    if not _callback_guard_deadline_expired(
                        current["deadline_at"],
                        observed_at=observed_time,
                    ):
                        continue
                    owner_execution_id = str(
                        current["owner_execution_id"] or ""
                    ).strip()
                    if owner_execution_id:
                        transitioned = self._transition_callback_guard_to_unknown(
                            conn,
                            business_type=normalized_business_type,
                            business_key=business_key,
                            owner_execution_id=owner_execution_id,
                            now=normalized_observed_at,
                            reason="callback lease expired during maintenance sweep",
                        )
                    else:
                        # 损坏 Guard 仍必须阻塞新任务，不能因为缺少 owner 而永久保持
                        # sending 或被错误重抢。没有 execution 身份可同步时只冻结 Guard，
                        # 并通过 critical 日志要求人工核查数据库。
                        cursor = conn.execute(
                            """
                            UPDATE callback_delivery_guards
                            SET state = 'outcome_unknown', lease_token = '',
                                deadline_at = NULL,
                                last_outcome = 'delivery_outcome_unknown',
                                error_stage = ?, updated_at = ?
                            WHERE business_type = ? AND business_key = ?
                              AND state = 'sending'
                            """,
                            (
                                "callback lease owner missing during maintenance sweep",
                                normalized_observed_at,
                                normalized_business_type,
                                business_key,
                            ),
                        )
                        transitioned = cursor.rowcount == 1
                        logger.critical(
                            "过期 callback Guard 缺少 owner，已仅冻结业务键: "
                            "business_type=%s business_key=%s",
                            normalized_business_type,
                            business_key,
                        )
                    if transitioned:
                        frozen_count += 1

        scanned_count = len(candidates)
        logger.log(
            logging.WARNING if frozen_count else logging.DEBUG,
            "callback Guard 过期扫描完成: business_type=%s scanned=%d frozen=%d",
            normalized_business_type,
            scanned_count,
            frozen_count,
        )
        return {
            "scanned_count": scanned_count,
            "frozen_count": frozen_count,
        }

    def release_callback_delivery_guard(
        self,
        *,
        business_type: str,
        business_key: str,
        released_by: str,
        release_reason: str,
        worker_stopped_confirmed: bool,
        released_at: str,
    ) -> str:
        """人工解除 outcome-unknown 冻结，并原子保存完整审计字段。

        本方法只释放“新任务提交门禁”，不会修改旧 execution 已经保存的
        ``delivery_outcome_unknown`` 事实，也不会把旧回调重新置为 pending。这样既允许
        运维确认后重新提交，又不会把“请求可能已经送达”伪装成“从未投递”。重复执行返回
        ``already_released``，且不得覆盖首次操作者、原因和时间。调用方必须显式证明旧
        Worker/旧进程已经停止或被隔离；否则禁止解除，避免旧执行在新任务受理后恢复发送。
        """

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_released_by = _required_internal_text(
            released_by,
            name="released_by",
        )
        normalized_reason = _required_internal_text(
            release_reason,
            name="release_reason",
        )
        if len(normalized_released_by) > 128:
            raise ValueError("released_by最多128个字符")
        if len(normalized_reason) > 512:
            raise ValueError("release_reason最多512个字符")
        if not isinstance(worker_stopped_confirmed, bool):
            raise TypeError("worker_stopped_confirmed必须是bool")
        if not worker_stopped_confirmed:
            raise ValueError("人工解除前必须确认旧Worker已停止或被隔离")
        normalized_released_at = _aware_datetime(
            released_at,
            name="released_at",
        ).isoformat()

        outcome = "not_frozen"
        with self._immediate_connection() as conn:
            guard = conn.execute(
                """
                SELECT owner_execution_id, state, lease_version, last_outcome,
                       released_at, released_by, release_reason
                FROM callback_delivery_guards
                WHERE business_type = ? AND business_key = ?
                """,
                (normalized_business_type, normalized_business_key),
            ).fetchone()
            if guard is None:
                outcome = "not_frozen"
            elif guard["state"] == "outcome_unknown":
                owner_execution_id = str(guard["owner_execution_id"] or "").strip()
                if not owner_execution_id:
                    raise RuntimeError("outcome_unknown callback Guard 缺少 owner")
                conn.execute(
                    """
                    INSERT INTO callback_guard_release_audits (
                        business_type, business_key, owner_execution_id,
                        lease_version, released_at, released_by, release_reason,
                        worker_stopped_confirmed
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        normalized_business_type,
                        normalized_business_key,
                        owner_execution_id,
                        int(guard["lease_version"]),
                        normalized_released_at,
                        normalized_released_by,
                        normalized_reason,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE callback_delivery_guards
                    SET state = 'idle', lease_token = '', deadline_at = NULL,
                        released_at = ?, released_by = ?, release_reason = ?,
                        updated_at = ?
                    WHERE business_type = ? AND business_key = ?
                      AND state = 'outcome_unknown'
                    """,
                    (
                        normalized_released_at,
                        normalized_released_by,
                        normalized_reason,
                        normalized_released_at,
                        normalized_business_type,
                        normalized_business_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("callback Guard 人工解除条件写未命中")
                outcome = "released"
            elif (
                guard["state"] == "idle"
                and guard["last_outcome"] == "delivery_outcome_unknown"
                and guard["released_at"]
                and str(guard["released_by"] or "").strip()
                and str(guard["release_reason"] or "").strip()
            ):
                # 第一次解除的审计证据不可被后续重复命令覆盖。
                outcome = "already_released"
            elif guard["state"] in _CALLBACK_GUARD_STATES:
                outcome = "not_frozen"
            else:  # pragma: no cover - Schema CHECK 已防御，保留运行时损坏检测。
                raise RuntimeError("callback Guard 存在未知状态")

        logger.log(
            logging.WARNING if outcome == "released" else logging.INFO,
            "callback Guard 人工解除处理完成: business_type=%s business_key=%s "
            "outcome=%s released_by=%s",
            normalized_business_type,
            normalized_business_key,
            outcome,
            normalized_released_by,
        )
        return outcome

    def list_callback_delivery_attempt_events(
        self,
        *,
        business_type: str,
        business_key: str,
        limit: int = 100,
    ) -> tuple[Dict[str, Any], ...]:
        """按时间倒序读取内部 Callback attempt 事件，不向公开接口投影。"""

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > 1000
        ):
            raise ValueError("limit必须是1到1000之间的整数")
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, business_type, business_key, owner_execution_id,
                       callback_attempt, lease_version, trigger, event_type,
                       delivery_outcome, request_trace_id, occurred_at
                FROM callback_delivery_attempt_events
                WHERE business_type = ? AND business_key = ?
                ORDER BY occurred_at DESC, id DESC
                LIMIT ?
                """,
                (
                    normalized_business_type,
                    normalized_business_key,
                    limit,
                ),
            ).fetchall()
        return tuple(
            {
                "id": int(row["id"]),
                "business_type": row["business_type"],
                "business_key": row["business_key"],
                "owner_execution_id": row["owner_execution_id"],
                "callback_attempt": int(row["callback_attempt"]),
                "lease_version": int(row["lease_version"]),
                "trigger": row["trigger"],
                "event_type": row["event_type"],
                "delivery_outcome": row["delivery_outcome"],
                "request_trace_id": row["request_trace_id"],
                "occurred_at": row["occurred_at"],
            }
            for row in rows
        )

    def list_callback_delivery_guard_release_audits(
        self,
        *,
        business_type: str,
        business_key: str,
        limit: int = 100,
    ) -> tuple[Dict[str, Any], ...]:
        """按时间倒序读取内部人工解除审计，供测试和后续运维命令复用。"""

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit <= 0
            or limit > 1000
        ):
            raise ValueError("limit必须是1到1000之间的整数")
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT id, business_type, business_key, owner_execution_id,
                       lease_version, released_at, released_by, release_reason,
                       worker_stopped_confirmed
                FROM callback_guard_release_audits
                WHERE business_type = ? AND business_key = ?
                ORDER BY released_at DESC, id DESC
                LIMIT ?
                """,
                (
                    normalized_business_type,
                    normalized_business_key,
                    limit,
                ),
            ).fetchall()
        return tuple(
            {
                "id": int(row["id"]),
                "business_type": row["business_type"],
                "business_key": row["business_key"],
                "owner_execution_id": row["owner_execution_id"],
                "lease_version": int(row["lease_version"]),
                "released_at": row["released_at"],
                "released_by": row["released_by"],
                "release_reason": row["release_reason"],
                "worker_stopped_confirmed": bool(
                    row["worker_stopped_confirmed"]
                ),
            }
            for row in rows
        )

    @staticmethod
    def _row_to_report_resource_record(row: sqlite3.Row) -> Dict[str, Any]:
        """把资源恢复行转换成 Adapter 可映射的普通快照。"""

        try:
            payload = json.loads(row["record_payload"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("report 资源恢复记录 JSON 已损坏") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("report 资源恢复记录 payload 必须是对象")
        return {
            "execution_id": row["execution_id"],
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "artifact_namespace": row["artifact_namespace"],
            "state": row["state"],
            "record_payload": payload,
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _select_report_resource_record(
        conn: sqlite3.Connection,
        execution_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT execution_id, business_type, business_key, artifact_namespace,
                   state, record_payload, version, created_at, updated_at
            FROM report_resource_records
            WHERE execution_id = ?
            """,
            (execution_id,),
        ).fetchone()

    def create_report_resource_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        artifact_namespace: str,
        state: str,
        record_payload: Mapping[str, Any],
        created_at: str,
    ) -> Dict[str, Any]:
        """幂等登记 execution 的任务级资源命名空间。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        if normalized_business_type != "report":
            raise ValueError("report资源记录的business_type必须是report")
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_namespace = _required_internal_text(
            artifact_namespace,
            name="artifact_namespace",
        )
        normalized_state = _required_internal_text(state, name="state")
        if normalized_state not in _REPORT_RESOURCE_STATES:
            raise ValueError("report资源记录state无效")
        if not isinstance(record_payload, Mapping):
            raise TypeError("record_payload必须是Mapping")
        serialized_payload = self._serialize(dict(record_payload))
        normalized_created_at = _aware_datetime(
            created_at,
            name="created_at",
        ).isoformat()

        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(
                conn,
                normalized_execution_id,
            )
            if (
                execution is None
                or execution["business_type"] != normalized_business_type
                or execution["business_key"] != normalized_business_key
            ):
                raise ValueError("report资源记录与execution身份不一致")
            existing = self._select_report_resource_record(
                conn,
                normalized_execution_id,
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO report_resource_records (
                        execution_id, business_type, business_key,
                        artifact_namespace, state, record_payload, version,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        normalized_execution_id,
                        normalized_business_type,
                        normalized_business_key,
                        normalized_namespace,
                        normalized_state,
                        serialized_payload,
                        normalized_created_at,
                        normalized_created_at,
                    ),
                )
                existing = self._select_report_resource_record(
                    conn,
                    normalized_execution_id,
                )
            elif (
                existing["business_type"] != normalized_business_type
                or existing["business_key"] != normalized_business_key
                or existing["artifact_namespace"] != normalized_namespace
            ):
                raise ValueError("report资源记录幂等键发生身份冲突")
            if existing is None:  # pragma: no cover - INSERT 后的防御性检查。
                raise RuntimeError("report资源记录创建后不可见")
            result = self._row_to_report_resource_record(existing)

        logger.info(
            "report任务资源已登记: execution_id=%s business_key=%s state=%s version=%s",
            normalized_execution_id,
            normalized_business_key,
            result["state"],
            result["version"],
        )
        return result

    def get_report_resource_record(
        self,
        execution_id: str,
    ) -> Dict[str, Any] | None:
        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        with self._connection() as conn:
            row = self._select_report_resource_record(
                conn,
                normalized_execution_id,
            )
        return self._row_to_report_resource_record(row) if row is not None else None

    def save_report_resource_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        artifact_namespace: str,
        state: str,
        record_payload: Mapping[str, Any],
        expected_version: int,
        updated_at: str,
    ) -> Dict[str, Any] | None:
        """按 version CAS 更新资源恢复事实；冲突返回 ``None``。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_namespace = _required_internal_text(
            artifact_namespace,
            name="artifact_namespace",
        )
        normalized_state = _required_internal_text(state, name="state")
        if normalized_state not in _REPORT_RESOURCE_STATES:
            raise ValueError("report资源记录state无效")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise ValueError("expected_version必须是正整数")
        if not isinstance(record_payload, Mapping):
            raise TypeError("record_payload必须是Mapping")
        serialized_payload = self._serialize(dict(record_payload))
        normalized_updated_at = _aware_datetime(
            updated_at,
            name="updated_at",
        ).isoformat()

        with self._immediate_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE report_resource_records
                SET state = ?, record_payload = ?, version = version + 1,
                    next_recovery_at = NULL, last_recovery_reason = '',
                    updated_at = ?
                WHERE execution_id = ? AND business_type = ? AND business_key = ?
                  AND artifact_namespace = ? AND version = ?
                """,
                (
                    normalized_state,
                    serialized_payload,
                    normalized_updated_at,
                    normalized_execution_id,
                    normalized_business_type,
                    normalized_business_key,
                    normalized_namespace,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = self._select_report_resource_record(conn, normalized_execution_id)
            if row is None:  # pragma: no cover - UPDATE 后的防御性检查。
                raise RuntimeError("report资源记录更新后不可见")
            return self._row_to_report_resource_record(row)

    def prepare_report_resource_cleanup(
        self,
        execution_id: str,
        *,
        updated_at: str,
    ) -> Dict[str, Any]:
        """读取不可变 execution 终态，权威决定最终 Artifact 是否获得所有权。

        只有成功终态结果中的 ``report_artifact`` 才能进入 retained。失败或 stale execution
        的保留集合恒为空；这条规则在数据库短事务中执行，Application 无权自行声明所有权。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_updated_at = _aware_datetime(
            updated_at,
            name="updated_at",
        ).isoformat()
        with self._immediate_connection() as conn:
            row = self._select_report_resource_record(conn, normalized_execution_id)
            execution = self._select_task_execution_row(conn, normalized_execution_id)
            if row is None:
                raise ValueError("report资源记录不存在")
            if execution is None:
                raise ValueError("report execution不存在")
            current = self._row_to_report_resource_record(row)
            if current["state"] != "tracking":
                return current
            execution_state = execution["execution_state"]
            if execution_state not in {"succeeded", "failed", "stale"}:
                raise RuntimeError("report execution尚未形成可清理终态")
            payload = dict(current["record_payload"])
            tracked_artifact = payload.get("final_artifact")
            retained: list[Mapping[str, Any]] = []
            if execution_state == "succeeded":
                result_payload = self._deserialize(execution["result_payload"])
                if not isinstance(result_payload, Mapping):
                    raise RuntimeError("成功report execution缺少结果事实")
                owned_artifact = result_payload.get("report_artifact")
                if not isinstance(owned_artifact, Mapping):
                    raise RuntimeError("成功report execution缺少Artifact所有权")
                if not isinstance(tracked_artifact, Mapping) or dict(
                    tracked_artifact
                ) != dict(owned_artifact):
                    raise RuntimeError("终态Artifact与任务级资源记录不一致")
                retained = [dict(owned_artifact)]
            payload["retained"] = retained
            payload["artifact_state"] = "pending"
            payload["external_state"] = (
                "pending" if payload.get("cleanup_ref") else "not_required"
            )
            payload["last_error_stage"] = ""
            payload["last_error_message"] = ""
            cursor = conn.execute(
                """
                UPDATE report_resource_records
                SET state = 'cleanup_pending', record_payload = ?,
                    version = version + 1, next_recovery_at = NULL,
                    last_recovery_reason = '', updated_at = ?
                WHERE execution_id = ? AND state = 'tracking' AND version = ?
                """,
                (
                    self._serialize(payload),
                    normalized_updated_at,
                    normalized_execution_id,
                    current["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("report资源清理准备CAS未命中")
            prepared = self._select_report_resource_record(
                conn,
                normalized_execution_id,
            )
            if prepared is None:  # pragma: no cover
                raise RuntimeError("report资源清理准备后记录不可见")
            return self._row_to_report_resource_record(prepared)

    def defer_report_resource_recovery(
        self,
        execution_id: str,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """独立于 JSON payload 记录恢复冷却，确保损坏记录也不会永久占住首页。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_retry_at = _aware_datetime(
            retry_at,
            name="retry_at",
        ).isoformat()
        normalized_reason = _required_internal_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason长度不能超过256")
        with self._immediate_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE report_resource_records
                SET recovery_deferral_count = recovery_deferral_count + 1,
                    next_recovery_at = ?,
                    last_recovery_reason = ?
                WHERE execution_id = ?
                  AND state IN ('tracking', 'cleanup_pending', 'audit_pending')
                """,
                (
                    normalized_retry_at,
                    normalized_reason,
                    normalized_execution_id,
                ),
            )
            deferred = cursor.rowcount == 1
        logger.log(
            logging.WARNING if deferred else logging.DEBUG,
            "report资源恢复冷却记录完成: execution_id=%s deferred=%s "
            "retry_at=%s reason=%s",
            normalized_execution_id,
            deferred,
            normalized_retry_at,
            normalized_reason,
        )
        return deferred

    def list_recoverable_report_resource_ids(
        self,
        *,
        limit: int,
        ready_at: str | None = None,
    ) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit必须是正整数")
        normalized_ready_at = _aware_datetime(
            ready_at or _utc_now_iso(),
            name="ready_at",
        ).isoformat()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT resource.execution_id
                FROM report_resource_records AS resource
                JOIN llm_task_executions AS execution
                  ON execution.execution_id = resource.execution_id
                WHERE (
                    resource.state IN ('cleanup_pending', 'audit_pending')
                    OR (
                        resource.state = 'tracking'
                        AND execution.execution_state
                            IN ('succeeded', 'failed', 'stale')
                    )
                )
                  AND (
                      resource.next_recovery_at IS NULL
                      OR julianday(resource.next_recovery_at) <= julianday(?)
                  )
                ORDER BY resource.updated_at, resource.execution_id
                LIMIT ?
                """,
                (normalized_ready_at, limit),
            ).fetchall()
        return tuple(str(row["execution_id"]) for row in rows)

    @staticmethod
    def _row_to_analysis_resource_record(row: sqlite3.Row) -> Dict[str, Any]:
        """把 Analysis 资源恢复行转换成 Adapter 可验证的普通快照。

        JSON 损坏不能降级为“没有资源”。调用方必须保留现场并通过明确错误进入诊断或
        隔离流程，避免错误地对可能仍存在的外部 Context/Document 发起删除。
        """

        try:
            payload = json.loads(row["record_payload"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("analysis资源恢复记录JSON已损坏") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("analysis资源恢复记录payload必须是对象")
        return {
            "execution_id": row["execution_id"],
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "batch_id": row["batch_id"],
            "batch_sequence": row["batch_sequence"],
            "state": row["state"],
            "record_payload": payload,
            "version": int(row["version"]),
            "recovery_deferral_count": int(row["recovery_deferral_count"]),
            "next_recovery_at": row["next_recovery_at"],
            "last_recovery_reason": row["last_recovery_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _select_analysis_resource_record(
        conn: sqlite3.Connection,
        execution_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT resource.execution_id, resource.business_type, resource.business_key,
                   execution.batch_id, execution.batch_sequence, resource.state,
                   resource.record_payload, resource.version,
                   resource.recovery_deferral_count, resource.next_recovery_at,
                   resource.last_recovery_reason, resource.created_at, resource.updated_at
            FROM analysis_resource_records AS resource
            JOIN llm_task_executions AS execution
              ON execution.execution_id = resource.execution_id
            WHERE resource.execution_id = ?
            """,
            (execution_id,),
        ).fetchone()

    def create_analysis_resource_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        state: str,
        record_payload: Mapping[str, Any],
        created_at: str,
    ) -> Dict[str, Any]:
        """在首个远端 RAG 操作前幂等登记新 Analysis execution 的资源事实。

        只允许带 1F-4 批次身份的新 file execution 写入本表。这样未来切换前的旧线程
        任务不会被误认为新链路资源，更不会被恢复器错误处理。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        if normalized_business_type != _ANALYSIS_BATCH_BUSINESS_TYPE:
            raise ValueError("analysis资源记录的business_type必须是file")
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_state = _required_internal_text(state, name="state")
        if normalized_state != "tracking":
            raise ValueError("analysis资源记录创建只能进入tracking")
        if not isinstance(record_payload, Mapping):
            raise TypeError("record_payload必须是Mapping")
        serialized_payload = self._serialize(dict(record_payload))
        normalized_created_at = _aware_datetime(
            created_at,
            name="created_at",
        ).isoformat()

        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(
                conn,
                normalized_execution_id,
            )
            if (
                execution is None
                or execution["business_type"] != normalized_business_type
                or execution["business_key"] != normalized_business_key
                or not str(execution["batch_id"] or "").strip()
                or execution["batch_sequence"] is None
            ):
                raise ValueError("analysis资源记录与新execution身份不一致")
            existing = self._select_analysis_resource_record(
                conn,
                normalized_execution_id,
            )
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO analysis_resource_records (
                        execution_id, business_type, business_key, state,
                        record_payload, version, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'tracking', ?, 0, ?, ?)
                    """,
                    (
                        normalized_execution_id,
                        normalized_business_type,
                        normalized_business_key,
                        serialized_payload,
                        normalized_created_at,
                        normalized_created_at,
                    ),
                )
                existing = self._select_analysis_resource_record(
                    conn,
                    normalized_execution_id,
                )
            elif (
                existing["business_type"] != normalized_business_type
                or existing["business_key"] != normalized_business_key
            ):
                raise ValueError("analysis资源记录幂等键发生身份冲突")
            if existing is None:  # pragma: no cover - INSERT 后的防御性检查。
                raise RuntimeError("analysis资源记录创建后不可见")
            result = self._row_to_analysis_resource_record(existing)

        logger.info(
            "analysis任务资源已登记: execution_id=%s business_key=%s state=%s version=%s",
            normalized_execution_id,
            normalized_business_key,
            result["state"],
            result["version"],
        )
        return result

    def get_analysis_resource_record(
        self,
        execution_id: str,
    ) -> Dict[str, Any] | None:
        """只读获取一份 Analysis 资源事实，不执行外部恢复或清理。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        with self._connection() as conn:
            row = self._select_analysis_resource_record(
                conn,
                normalized_execution_id,
            )
        return self._row_to_analysis_resource_record(row) if row is not None else None

    def get_analysis_resource_control_record(
        self,
        execution_id: str,
    ) -> Dict[str, Any] | None:
        """读取资源恢复控制面字段，不解析可能已损坏的业务 payload。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        with self._connection() as conn:
            row = self._select_analysis_resource_record(
                conn,
                normalized_execution_id,
            )
        if row is None:
            return None
        raw_payload = row["record_payload"]
        payload_bytes = str(raw_payload).encode("utf-8", errors="replace")
        return {
            "execution_id": row["execution_id"],
            "business_type": row["business_type"],
            "business_key": row["business_key"],
            "batch_id": row["batch_id"],
            "batch_sequence": row["batch_sequence"],
            "state": row["state"],
            "version": int(row["version"]),
            "payload_bytes": len(payload_bytes),
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }

    def quarantine_analysis_resource_recovery_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        expected_state: str,
        expected_version: int,
        reason: str,
        updated_at: str,
    ) -> bool:
        """在不解析 payload 的前提下 CAS 隔离毒化或不可恢复的资源记录。

        原始 ``record_payload`` 保持字节级不变，便于后续人工取证；隔离原因写入恢复控制
        字段。该入口不执行 RAG、文件或网络补偿，也不允许复活终态记录。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_expected_state = _required_internal_text(
            expected_state,
            name="expected_state",
        )
        if normalized_business_type != _ANALYSIS_BATCH_BUSINESS_TYPE:
            raise ValueError("analysis资源隔离的business_type必须是file")
        if normalized_expected_state not in {
            "tracking",
            "cleanup_pending",
            "audit_pending",
        }:
            raise ValueError("只有非终态analysis资源记录可以被恢复器隔离")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version必须是非负整数")
        normalized_reason = _required_internal_text(reason, name="reason")[:256]
        normalized_updated_at = _aware_datetime(
            updated_at,
            name="updated_at",
        ).isoformat()

        payload_sha256 = ""
        payload_bytes = 0
        with self._immediate_connection() as conn:
            row = self._select_analysis_resource_record(
                conn,
                normalized_execution_id,
            )
            if row is None:
                return False
            raw_payload = str(row["record_payload"])
            encoded_payload = raw_payload.encode("utf-8", errors="replace")
            payload_sha256 = hashlib.sha256(encoded_payload).hexdigest()
            payload_bytes = len(encoded_payload)
            cursor = conn.execute(
                """
                UPDATE analysis_resource_records
                SET state = 'quarantined', version = version + 1,
                    recovery_deferral_count =
                        CASE WHEN recovery_deferral_count < 1
                             THEN 1 ELSE recovery_deferral_count END,
                    next_recovery_at = ?,
                    last_recovery_reason = ?, updated_at = ?
                WHERE execution_id = ? AND business_type = ? AND business_key = ?
                  AND state = ? AND version = ?
                  AND state IN ('tracking', 'cleanup_pending', 'audit_pending')
                """,
                (
                    normalized_updated_at,
                    normalized_reason,
                    normalized_updated_at,
                    normalized_execution_id,
                    normalized_business_type,
                    normalized_business_key,
                    normalized_expected_state,
                    expected_version,
                ),
            )
            quarantined = cursor.rowcount == 1

        logger.log(
            logging.CRITICAL if quarantined else logging.WARNING,
            "analysis资源恢复记录隔离完成: execution_id=%s state=%s version=%s "
            "quarantined=%s payload_bytes=%s payload_sha256=%s reason=%s",
            normalized_execution_id,
            normalized_expected_state,
            expected_version,
            quarantined,
            payload_bytes,
            payload_sha256[:16],
            normalized_reason,
        )
        return quarantined

    def advance_analysis_resource_record(
        self,
        *,
        execution_id: str,
        business_type: str,
        business_key: str,
        expected_state: str,
        expected_version: int,
        target_state: str,
        record_payload: Mapping[str, Any],
        updated_at: str,
    ) -> Dict[str, Any] | None:
        """以 ``state + version`` CAS 推进或补充 Analysis 资源事实。

        同状态更新用于“先有 Context、后有 Document”的分段引用持久化；即使状态文本
        不变，版本也必须递增，确保并发恢复者不会覆盖已经写入的外部身份。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        if normalized_business_type != _ANALYSIS_BATCH_BUSINESS_TYPE:
            raise ValueError("analysis资源记录的business_type必须是file")
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_expected_state = _required_internal_text(
            expected_state,
            name="expected_state",
        )
        normalized_target_state = _required_internal_text(
            target_state,
            name="target_state",
        )
        if (
            normalized_expected_state not in _ANALYSIS_RESOURCE_STATES
            or normalized_target_state not in _ANALYSIS_RESOURCE_STATES
        ):
            raise ValueError("analysis资源记录state无效")
        if (
            normalized_target_state
            not in _ANALYSIS_RESOURCE_TRANSITIONS[normalized_expected_state]
        ):
            raise ValueError(
                "非法analysis资源状态迁移: "
                f"{normalized_expected_state} -> {normalized_target_state}"
            )
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version必须是非负整数")
        if not isinstance(record_payload, Mapping):
            raise TypeError("record_payload必须是Mapping")
        serialized_payload = self._serialize(dict(record_payload))
        normalized_updated_at = _aware_datetime(
            updated_at,
            name="updated_at",
        ).isoformat()

        with self._immediate_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE analysis_resource_records
                SET state = ?, record_payload = ?, version = version + 1,
                    recovery_deferral_count = 0, next_recovery_at = NULL,
                    last_recovery_reason = '',
                    updated_at = ?
                WHERE execution_id = ? AND business_type = ? AND business_key = ?
                  AND state = ? AND version = ?
                """,
                (
                    normalized_target_state,
                    serialized_payload,
                    normalized_updated_at,
                    normalized_execution_id,
                    normalized_business_type,
                    normalized_business_key,
                    normalized_expected_state,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = self._select_analysis_resource_record(
                conn,
                normalized_execution_id,
            )
            if row is None:  # pragma: no cover - UPDATE 后的防御性检查。
                raise RuntimeError("analysis资源记录更新后不可见")
            return self._row_to_analysis_resource_record(row)

    def defer_analysis_resource_recovery(
        self,
        execution_id: str,
        *,
        expected_version: int,
        retry_at: str,
        reason: str,
    ) -> Dict[str, Any] | None:
        """为可恢复 Analysis 记录设置有限退避；未知/隔离记录不会被重新排队。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version必须是非负整数")
        normalized_retry_at = _aware_datetime(
            retry_at,
            name="retry_at",
        ).isoformat()
        normalized_reason = _required_internal_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason长度不能超过256")
        updated_at = _utc_now_iso()

        with self._immediate_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE analysis_resource_records
                SET recovery_deferral_count = recovery_deferral_count + 1,
                    next_recovery_at = ?, last_recovery_reason = ?,
                    version = version + 1, updated_at = ?
                WHERE execution_id = ? AND version = ?
                  AND state IN ('tracking', 'cleanup_pending', 'audit_pending')
                """,
                (
                    normalized_retry_at,
                    normalized_reason,
                    updated_at,
                    normalized_execution_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = self._select_analysis_resource_record(
                conn,
                normalized_execution_id,
            )
            if row is None:  # pragma: no cover - UPDATE 后的防御性检查。
                raise RuntimeError("analysis资源恢复延期后记录不可见")
            result = self._row_to_analysis_resource_record(row)

        logger.info(
            "analysis资源恢复已延期: execution_id=%s version=%s retry_at=%s reason=%s",
            normalized_execution_id,
            result["version"],
            normalized_retry_at,
            normalized_reason,
        )
        return result

    def list_recoverable_analysis_resource_ids(
        self,
        *,
        limit: int,
        ready_at: str | None = None,
    ) -> tuple[str, ...]:
        """有界扫描到期 Analysis 资源记录，不读取或重放 ``running`` execution。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit必须是1~1000的整数")
        normalized_ready_at = _aware_datetime(
            ready_at or _utc_now_iso(),
            name="ready_at",
        ).isoformat()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT resource.execution_id
                FROM analysis_resource_records AS resource
                JOIN llm_task_executions AS execution
                  ON execution.execution_id = resource.execution_id
                WHERE execution.business_type = 'file'
                  AND execution.batch_id IS NOT NULL
                  AND execution.batch_sequence IS NOT NULL
                  AND (
                      resource.state IN ('cleanup_pending', 'audit_pending')
                      OR (
                          resource.state = 'tracking'
                          AND execution.execution_state
                              IN ('succeeded', 'failed', 'stale')
                      )
                  )
                  AND (
                      resource.next_recovery_at IS NULL
                      OR julianday(resource.next_recovery_at) <= julianday(?)
                  )
                ORDER BY resource.updated_at, resource.execution_id
                LIMIT ?
                """,
                (normalized_ready_at, limit),
            ).fetchall()
        return tuple(str(row["execution_id"]) for row in rows)

    def defer_accepted_task_execution(
        self,
        execution_id: str,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """条件记录领取前故障；绝不把 running 或终态重新放回 accepted。"""

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_retry_at = _aware_datetime(
            retry_at,
            name="retry_at",
        ).isoformat()
        normalized_reason = _required_internal_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason长度不能超过256")
        updated_at = _utc_now_iso()
        with self._immediate_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE llm_task_executions
                SET dispatch_failure_count = dispatch_failure_count + 1,
                    next_dispatch_at = ?,
                    last_dispatch_error = ?,
                    updated_at = ?
                WHERE execution_id = ? AND execution_state = 'accepted'
                """,
                (
                    normalized_retry_at,
                    normalized_reason,
                    updated_at,
                    normalized_execution_id,
                ),
            )
            deferred = cursor.rowcount == 1
        logger.log(
            logging.WARNING if deferred else logging.DEBUG,
            "任务领取前故障冷却记录完成: execution_id=%s deferred=%s "
            "retry_at=%s reason=%s",
            normalized_execution_id,
            deferred,
            normalized_retry_at,
            normalized_reason,
        )
        return deferred

    def defer_accepted_task_execution_with_backoff(
        self,
        execution_id: str,
        *,
        retry_base_seconds: float,
        retry_max_seconds: float,
        reason: str,
        now: str | None = None,
    ) -> bool:
        """原子记录 accepted 调度失败并根据持久计数计算下一次领取时间。

        退避次数必须以数据库中的 ``dispatch_failure_count`` 为准，而不能依赖 Worker
        内存：进程重启后内存会丢失，未来替换为共享 Repository 后多个 Dispatcher 也不应
        因各自本地计数而把同一任务重新变成热循环。当前 SQLite ``BEGIN IMMEDIATE`` 把
        读取计数、计算 ``next_dispatch_at`` 与条件写锁在同一短事务内；本方法绝不触碰
        ``running`` 或终态 execution。
        """

        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        for name, value in (
            ("retry_base_seconds", retry_base_seconds),
            ("retry_max_seconds", retry_max_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name}必须是数字")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0.0:
                raise ValueError(f"{name}必须是正有限数字")
        normalized_base = float(retry_base_seconds)
        normalized_max = float(retry_max_seconds)
        if normalized_max < normalized_base:
            raise ValueError("retry_max_seconds不能小于retry_base_seconds")
        normalized_reason = _required_internal_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason长度不能超过256")
        reference_at = _aware_datetime(
            _utc_now_iso() if now is None else now,
            name="now",
        )

        failure_count: int | None = None
        retry_at: str | None = None
        with self._immediate_connection() as conn:
            row = conn.execute(
                """
                SELECT dispatch_failure_count
                FROM llm_task_executions
                WHERE execution_id = ? AND execution_state = 'accepted'
                """,
                (normalized_execution_id,),
            ).fetchone()
            if row is None:
                deferred = False
            else:
                raw_count = row["dispatch_failure_count"]
                if (
                    isinstance(raw_count, bool)
                    or not isinstance(raw_count, int)
                    or raw_count < 0
                ):
                    raise RuntimeError("dispatch_failure_count持久化数据无效")
                failure_count = raw_count + 1
                # Python 的大整数不会溢出，但无界指数没有业务价值。指数达到 60 后
                # 已经远超本配置允许的时间范围，直接使用 max 可保持固定计算成本。
                exponent = min(failure_count - 1, 60)
                delay_seconds = min(
                    normalized_max,
                    normalized_base * (2.0**exponent),
                )
                retry_at = (
                    reference_at + timedelta(seconds=delay_seconds)
                ).isoformat()
                cursor = conn.execute(
                    """
                    UPDATE llm_task_executions
                    SET dispatch_failure_count = ?,
                        next_dispatch_at = ?,
                        last_dispatch_error = ?,
                        updated_at = ?
                    WHERE execution_id = ?
                      AND execution_state = 'accepted'
                      AND dispatch_failure_count = ?
                    """,
                    (
                        failure_count,
                        retry_at,
                        normalized_reason,
                        reference_at.isoformat(),
                        normalized_execution_id,
                        raw_count,
                    ),
                )
                deferred = cursor.rowcount == 1

        logger.log(
            logging.WARNING if deferred else logging.DEBUG,
            "任务领取前指数退避记录完成: execution_id=%s deferred=%s "
            "failure_count=%s retry_at=%s reason=%s",
            normalized_execution_id,
            deferred,
            failure_count if failure_count is not None else "-",
            retry_at or "-",
            normalized_reason,
        )
        return deferred

    def fail_accepted_analysis_task_execution_if_current(
        self,
        *,
        expected_execution_id: str,
        expected_business_key: str,
        message: str,
        reason: str,
        execution_result_payload: Mapping[str, Any],
        projection_result_payload: Mapping[str, Any],
    ) -> bool:
        """把无法解码的新 Analysis accepted 快照原子收敛为公开失败。

        本方法是 Analysis 专用控制面：它不读取 ``input_payload``，但必须在同一写事务中
        证明记录属于带批次身份的新 file execution，且仍是公开最新 owner。旧兼容 file、
        running、终态或已被新任务替代的记录都不能被这里改写。
        """

        execution_id = _required_internal_text(
            expected_execution_id,
            name="expected_execution_id",
        )
        business_key = _required_internal_text(
            expected_business_key,
            name="expected_business_key",
        )
        if not isinstance(message, str):
            raise TypeError("message必须是str")
        normalized_reason = _required_internal_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason长度不能超过256")
        if not isinstance(execution_result_payload, Mapping):
            raise TypeError("execution_result_payload必须是Mapping")
        if not isinstance(projection_result_payload, Mapping):
            raise TypeError("projection_result_payload必须是Mapping")
        serialized_execution_result = self._serialize(
            dict(execution_result_payload)
        )
        serialized_projection_result = self._serialize(
            dict(projection_result_payload)
        )

        finished = False
        marked_stale = False
        with self._immediate_connection() as conn:
            execution = self._select_task_execution_row(conn, execution_id)
            latest = conn.execute(
                """
                SELECT execution_id
                FROM llm_tasks
                WHERE business_type = 'file' AND business_key = ?
                """,
                (business_key,),
            ).fetchone()
            if (
                execution is not None
                and execution["business_type"] == "file"
                and execution["business_key"] == business_key
                and execution["batch_id"] is not None
                and execution["batch_sequence"] is not None
                and execution["dispatch_sequence"] is not None
                and execution["execution_state"] == "accepted"
                and latest is not None
                and latest["execution_id"] == execution_id
            ):
                now = _utc_now_iso()
                execution_cursor = conn.execute(
                    """
                    UPDATE llm_task_executions
                    SET execution_state = 'failed', public_status = '3',
                        progress = 1, message = ?, result_payload = ?,
                        dispatch_failure_count = dispatch_failure_count + 1,
                        next_dispatch_at = NULL, last_dispatch_error = ?,
                        completed_at = ?, updated_at = ?
                    WHERE execution_id = ? AND execution_state = 'accepted'
                    """,
                    (
                        message,
                        serialized_execution_result,
                        normalized_reason,
                        now,
                        now,
                        execution_id,
                    ),
                )
                projection_cursor = conn.execute(
                    """
                    UPDATE llm_tasks
                    SET status = '3', progress = 1, message = ?,
                        result_payload = ?, updated_at = ?
                    WHERE business_type = 'file' AND business_key = ?
                      AND execution_id = ?
                    """,
                    (
                        message,
                        serialized_projection_result,
                        now,
                        business_key,
                        execution_id,
                    ),
                )
                if (
                    execution_cursor.rowcount != 1
                    or projection_cursor.rowcount != 1
                ):
                    raise RuntimeError("Analysis毒任务终态与公开投影未能原子同步")
                finished = True
            if not finished:
                marked_stale = self._mark_execution_stale_if_superseded(
                    conn,
                    execution=execution,
                    latest=latest,
                    execution_id=execution_id,
                    business_type="file",
                    business_key=business_key,
                )

        logger.log(
            logging.ERROR if finished else logging.INFO,
            "Analysis毒快照条件收敛完成: execution_id=%s business_key=%s "
            "finished=%s marked_stale=%s reason=%s",
            execution_id,
            business_key,
            finished,
            marked_stale,
            normalized_reason,
        )
        return finished

    def list_accepted_task_execution_ids(
        self,
        business_type: str,
        *,
        limit: int,
        ready_at: str | None = None,
    ) -> tuple[str, ...]:
        """按事务内序号扫描当前已到重试时间的 accepted execution。"""

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit必须是正整数")
        normalized_ready_at = _aware_datetime(
            ready_at or _utc_now_iso(),
            name="ready_at",
        ).isoformat()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT execution_id
                FROM llm_task_executions
                WHERE business_type = ? AND execution_state = 'accepted'
                  AND (
                      next_dispatch_at IS NULL
                      OR julianday(next_dispatch_at) <= julianday(?)
                  )
                ORDER BY dispatch_sequence
                LIMIT ?
                """,
                (normalized_business_type, normalized_ready_at, limit),
            ).fetchall()
        return tuple(row["execution_id"] for row in rows)

    def list_accepted_analysis_task_execution_ids(
        self,
        *,
        limit: int,
        ready_at: str | None = None,
    ) -> tuple[str, ...]:
        """按全局调度序号扫描已受理的新 Analysis 批次任务。

        旧 file 兼容链没有 ``batch_id``，不能被未来 1F Dispatcher 当作可由新
        ``RunAnalysisTask`` 处理的 execution。单独收窄查询条件可防止切换前后的两条链
        路互相误领；它也不把 ``running`` 重置为 ``accepted``。
        """

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit必须是正整数")
        normalized_ready_at = _aware_datetime(
            ready_at or _utc_now_iso(),
            name="ready_at",
        ).isoformat()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT execution_id
                FROM llm_task_executions
                WHERE business_type = 'file'
                  AND batch_id IS NOT NULL
                  AND batch_sequence IS NOT NULL
                  AND execution_state = 'accepted'
                  AND (
                      next_dispatch_at IS NULL
                      OR julianday(next_dispatch_at) <= julianday(?)
                  )
                ORDER BY dispatch_sequence
                LIMIT ?
                """,
                (normalized_ready_at, limit),
            ).fetchall()
        return tuple(str(row["execution_id"]) for row in rows)

    def inspect_analysis_task_execution_queue(
        self,
        *,
        running_sample_limit: int,
    ) -> Dict[str, Any]:
        """只读汇总带批次身份的新 Analysis execution。

        文件分析切换期间，旧 ``file`` 兼容链与新批次链共享业务类型。诊断若直接复用
        通用 ``file`` 汇总，会把旧 accepted/running 误显示为新 Dispatcher 的积压，甚至
        诱导运维人员错误接管。这里与 ``list_accepted_analysis_task_execution_ids`` 保持
        相同的 batch 身份边界，只做观测，绝不重置任何 ``running`` 记录。
        """

        if (
            isinstance(running_sample_limit, bool)
            or not isinstance(running_sample_limit, int)
            or running_sample_limit < 1
            or running_sample_limit > 1000
        ):
            raise ValueError("running_sample_limit必须是1~1000的整数")

        with self._connection() as conn:
            aggregate = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN execution_state = 'accepted' THEN 1 ELSE 0 END)
                        AS accepted_count,
                    SUM(CASE WHEN execution_state = 'running' THEN 1 ELSE 0 END)
                        AS running_count,
                    MIN(CASE WHEN execution_state = 'accepted' THEN created_at END)
                        AS oldest_accepted_at,
                    MIN(
                        CASE WHEN execution_state = 'running'
                        THEN COALESCE(started_at, created_at) END
                    ) AS oldest_running_at
                FROM llm_task_executions
                WHERE business_type = 'file'
                  AND batch_id IS NOT NULL
                  AND batch_sequence IS NOT NULL
                  AND execution_state IN ('accepted', 'running')
                """
            ).fetchone()
            running_rows = conn.execute(
                """
                SELECT execution_id
                FROM llm_task_executions
                WHERE business_type = 'file'
                  AND batch_id IS NOT NULL
                  AND batch_sequence IS NOT NULL
                  AND execution_state = 'running'
                ORDER BY COALESCE(started_at, created_at), execution_id
                LIMIT ?
                """,
                (running_sample_limit,),
            ).fetchall()

        if aggregate is None:  # pragma: no cover - SQLite aggregate 恒返回一行。
            raise RuntimeError("Analysis任务队列汇总查询未返回结果")
        result: Dict[str, Any] = {
            "business_type": "file",
            "accepted_count": int(aggregate["accepted_count"] or 0),
            "running_count": int(aggregate["running_count"] or 0),
            "oldest_accepted_at": aggregate["oldest_accepted_at"],
            "oldest_running_at": aggregate["oldest_running_at"],
            "running_execution_ids": tuple(
                str(row["execution_id"]) for row in running_rows
            ),
        }
        logger.debug(
            "Analysis任务队列只读汇总完成: accepted=%d running=%d "
            "running_sample_count=%d",
            result["accepted_count"],
            result["running_count"],
            len(result["running_execution_ids"]),
        )
        return result

    def inspect_task_execution_queue(
        self,
        business_type: str,
        *,
        running_sample_limit: int,
    ) -> Dict[str, Any]:
        """只读汇总某类 execution 队列，不修改或回收 ``running``。

        ``running`` 可能代表仍在执行的 Worker，也可能是进程崩溃后遗留的任务。阶段 2
        的租约与 Checkpoint 尚未落地前，两者无法可靠区分，因此本方法只返回数量、最老
        时间和有界 task ID 样本，严禁在诊断查询中附带状态重置。
        """

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        if (
            isinstance(running_sample_limit, bool)
            or not isinstance(running_sample_limit, int)
            or running_sample_limit < 1
            or running_sample_limit > 1000
        ):
            raise ValueError("running_sample_limit必须是1~1000的整数")

        with self._connection() as conn:
            aggregate = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN execution_state = 'accepted' THEN 1 ELSE 0 END)
                        AS accepted_count,
                    SUM(CASE WHEN execution_state = 'running' THEN 1 ELSE 0 END)
                        AS running_count,
                    MIN(CASE WHEN execution_state = 'accepted' THEN created_at END)
                        AS oldest_accepted_at,
                    MIN(
                        CASE WHEN execution_state = 'running'
                        THEN COALESCE(started_at, created_at) END
                    ) AS oldest_running_at
                FROM llm_task_executions
                WHERE business_type = ?
                  AND execution_state IN ('accepted', 'running')
                """,
                (normalized_business_type,),
            ).fetchone()
            running_rows = conn.execute(
                """
                SELECT execution_id
                FROM llm_task_executions
                WHERE business_type = ? AND execution_state = 'running'
                ORDER BY COALESCE(started_at, created_at), execution_id
                LIMIT ?
                """,
                (normalized_business_type, running_sample_limit),
            ).fetchall()

        if aggregate is None:  # pragma: no cover - SQLite aggregate 恒返回一行。
            raise RuntimeError("任务队列汇总查询未返回结果")
        result: Dict[str, Any] = {
            "business_type": normalized_business_type,
            "accepted_count": int(aggregate["accepted_count"] or 0),
            "running_count": int(aggregate["running_count"] or 0),
            "oldest_accepted_at": aggregate["oldest_accepted_at"],
            "oldest_running_at": aggregate["oldest_running_at"],
            "running_execution_ids": tuple(
                str(row["execution_id"]) for row in running_rows
            ),
        }
        logger.debug(
            "任务队列只读汇总完成: business_type=%s accepted=%d running=%d "
            "running_sample_count=%d",
            normalized_business_type,
            result["accepted_count"],
            result["running_count"],
            len(result["running_execution_ids"]),
        )
        return result

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
                    callback_claim_id, callback_claim_expires_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    callback_claim_id = excluded.callback_claim_id,
                    callback_claim_expires_at = excluded.callback_claim_expires_at,
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
                    "",
                    0.0,
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
    def get_task_by_execution_id(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """按不可变执行身份读取同一次任务。

        旧接口主要按 ``business_type + business_key`` 查询当前投影；任务模块的只读
        Port 还需要在恢复和一致性校验时锁定同一次执行，因此在兼容 Service 上补充
        只读入口。该方法不改变状态、不触发回调，也不会把 execution_id 暴露给前端。
        """

        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            raise ValueError("execution_id不能为空")
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT business_type, business_key, execution_id, request_payload,
                       status, progress, message,
                       result_payload, callback_status, callback_attempts, last_callback_error,
                       created_at, updated_at
                FROM llm_tasks
                WHERE execution_id = ?
                """,
                (normalized_execution_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

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
        outcome_unknown_events = tuple(
            event
            for event in trace.lifecycle_events
            if (event.failure_stage or "").endswith("_outcome_unknown")
        )
        if outcome_unknown_events:
            # 写请求响应丢失时，“没有拿到引用”不等于“远端没有创建”。旧逻辑会把仅有
            # context_create 失败事件的记录直接标为 deleted，造成不可见的外部孤儿。
            latest = outcome_unknown_events[-1]
            return "failed", latest.error_message or "外部写操作结果未知"

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
        document_upload_facts: Mapping[str, Any] | None = None,
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
        if document_upload_facts is not None and not isinstance(
            document_upload_facts,
            Mapping,
        ):
            raise TypeError("document_upload_facts 必须是 Mapping 或 None")
        serialized_document_upload = (
            self._serialize(dict(document_upload_facts))
            if document_upload_facts is not None
            else "{}"
        )

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
                model_attempt.call_id,
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
                    "call_id": model_attempt.call_id,
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
            "trace_id": trace.trace_id,
            "context_name": trace.context_name,
            "context_ref": trace.context_ref,
            "conversation_ref": trace.conversation_ref,
            "prompt": normalized_prompt,
            "status": status,
            "error_message": normalized_error if status == "failed" else "",
            "attempts": attempt_digest_payload,
            "lifecycle_events": lifecycle_rows,
        }
        # 旧 V1/V2 审计不传该字段时保持原 digest 形状，避免升级后破坏既有幂等重放。
        if document_upload_facts is not None:
            trace_digest_payload["document_upload"] = json.loads(
                serialized_document_upload
            )
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
                    trace_id,
                    document_upload_json,
                    workspace_name, workspace_slug, thread_slug,
                    prompt, response, sources_json, status,
                    error_message, workspace_cleanup_status,
                    workspace_cleanup_error, created_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_business_type,
                    normalized_business_key,
                    normalized_execution_id,
                    AUDIT_SCHEMA_VERSION,
                    normalized_audit_key,
                    trace_digest,
                    trace.trace_id,
                    serialized_document_upload,
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
                        call_id, failure_stage, error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        expected_execution_id: str | None = None,
        expected_audit_idempotency_key: str | None = None,
    ) -> int:
        """幂等追加关闭阶段事件，并在同一事务更新清理状态。

        新事件必须从数据库当前最大 ``sequence_no`` 的下一位连续开始。已经存在且内容完全
        一致的事件可以安全重放。``failed`` 允许用更高序号追加下一次清理尝试，并最终转为
        ``deleted``；``deleted`` 仍是不可逆终态。返回值是本次真正新增的事件数量。
        """
        if interaction_id < 1:
            raise ValueError("interaction_id 必须是正整数")
        normalized_cleanup_status = str(cleanup_status or "").strip()
        if normalized_cleanup_status not in {"deleted", "failed"}:
            raise ValueError("cleanup_status 只能是 deleted 或 failed")
        normalized_cleanup_error = str(cleanup_error or "").strip()
        normalized_expected_execution_id = (
            _required_internal_text(
                expected_execution_id,
                name="expected_execution_id",
            )
            if expected_execution_id is not None
            else None
        )
        normalized_expected_idempotency_key = (
            _required_internal_text(
                expected_audit_idempotency_key,
                name="expected_audit_idempotency_key",
            )
            if expected_audit_idempotency_key is not None
            else None
        )
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
            if event.operation
            in {
                "conversation_delete",
                "context_delete",
                "global_document_delete",
            }
        )
        if not cleanup_events:
            raise ValueError("关闭阶段追加必须包含文档或上下文删除事件")
        cleanup_has_failure = any(not event.success for event in cleanup_events)
        if cleanup_has_failure != (normalized_cleanup_status == "failed"):
            raise ValueError("cleanup_status 必须与删除事件的成功状态一致")

        def _write(conn: sqlite3.Connection) -> int:
            interaction = conn.execute(
                """
                SELECT execution_id, audit_idempotency_key,
                       workspace_cleanup_status, workspace_cleanup_error
                FROM llm_interactions
                WHERE id = ?
                """,
                (interaction_id,),
            ).fetchone()
            if interaction is None:
                raise ValueError(f"LLM交互记录不存在: interaction_id={interaction_id}")
            if (
                normalized_expected_execution_id is not None
                and interaction["execution_id"] != normalized_expected_execution_id
            ):
                raise InteractionAuditError(
                    "交互审计失败：清理凭据 execution_id 不匹配"
                )
            if (
                normalized_expected_idempotency_key is not None
                and interaction["audit_idempotency_key"]
                != normalized_expected_idempotency_key
            ):
                raise InteractionAuditError(
                    "交互审计失败：清理凭据幂等键不匹配"
                )

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
            elif current_cleanup_status == "failed":
                if inserted_count:
                    # 失败清理可以追加一次具有更高连续序号的新尝试。新结果可能仍失败，
                    # 也可能成功收敛为 deleted；历史失败事件始终保留，不做覆盖。
                    conn.execute(
                        """
                        UPDATE llm_interactions
                        SET workspace_cleanup_status = ?, workspace_cleanup_error = ?
                        WHERE id = ? AND workspace_cleanup_status = 'failed'
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
                        "重复清理审计与已提交结果不一致: "
                        f"interaction_id={interaction_id}"
                    )
            elif current_cleanup_status == "deleted":
                if inserted_count:
                    raise ValueError("deleted清理终态不得追加新的资源删除尝试")
                if (
                    normalized_cleanup_status != "deleted"
                    or current_cleanup_error != normalized_cleanup_error
                ):
                    raise ValueError("deleted清理终态不得被改写")
            else:
                raise RuntimeError(
                    "LLM交互存在未知清理状态: "
                    f"interaction_id={interaction_id}, status={current_cleanup_status}"
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
                       source_marker_status, call_id, failure_stage, error_message
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

    def get_llm_interaction_by_execution(
        self,
        business_type: str,
        business_key: str,
        execution_id: str,
        audit_idempotency_key: str,
    ) -> Dict[str, Any] | None:
        """按审计幂等键和 execution 精确读取单条交互。

        恢复任务只需要确认一个 execution 的原子审计是否已提交。不能先读取同一业务键的
        全部历史 Prompt、响应和来源再在 Python 中过滤，否则重复分析同名文件时，恢复成本
        会随历史总量无界增长。``audit_idempotency_key`` 已有唯一索引，其余条件用于再次
        校验业务归属，避免错误调用跨任务串读。
        """

        normalized_business_type = _required_internal_text(
            business_type,
            name="business_type",
        )
        normalized_business_key = _required_internal_text(
            business_key,
            name="business_key",
        )
        normalized_execution_id = _required_internal_text(
            execution_id,
            name="execution_id",
        )
        normalized_audit_key = _required_internal_text(
            audit_idempotency_key,
            name="audit_idempotency_key",
        )
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT id, business_type, business_key, workspace_name,
                       execution_id, audit_schema_version,
                       audit_idempotency_key, trace_digest, trace_id,
                       document_upload_json,
                       workspace_slug, thread_slug, prompt, response,
                       sources_json, status, error_message,
                       workspace_cleanup_status, workspace_cleanup_error,
                       created_at, completed_at
                FROM llm_interactions
                WHERE audit_idempotency_key = ?
                  AND execution_id = ?
                  AND business_type = ?
                  AND business_key = ?
                LIMIT 1
                """,
                (
                    normalized_audit_key,
                    normalized_execution_id,
                    normalized_business_type,
                    normalized_business_key,
                ),
            ).fetchone()
        if row is None:
            return None
        interaction = dict(row)
        interaction["sources"] = self._deserialize(
            interaction.pop("sources_json")
        ) or []
        interaction["document_upload"] = self._deserialize(
            interaction.pop("document_upload_json")
        ) or {}
        return interaction

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
                       audit_idempotency_key, trace_digest, trace_id,
                       document_upload_json,
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
            item["document_upload"] = self._deserialize(
                item.pop("document_upload_json")
            ) or {}
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

    def claim_callback_delivery(
        self,
        business_type: str,
        business_key: str,
        *,
        timeout: float,
        execution_id: str | None = None,
    ) -> tuple[str, Dict[str, Any]] | None:
        """原子领取一次文件终态回调发送租约并返回冻结任务快照。

        ``pending`` 首次发送和 ``failed`` 补发共用同一租约。文件任务受理会在租约有效期
        内拒绝同名重跑；回调结果写入还必须同时匹配 ``execution_id`` 与租约 ID，从而
        防止旧补发把新执行误标为成功或失败。租约超过 HTTP timeout 的保守余量后允许
        接管，避免进程崩溃把任务永久锁死。
        """
        if business_type != "file":
            raise ValueError("回调发送租约当前仅支持file任务")
        completed_statuses = _COMPLETED_TASK_STATUSES.get(business_type)
        if not completed_statuses:
            raise ValueError(f"未知 business_type: {business_type}")
        expected_execution_id = str(execution_id or "").strip()
        now_epoch = time.time()
        now = _utc_now_iso()
        claim_id = uuid4().hex
        claim_expires_at = (
            now_epoch + _callback_delivery_lease_seconds(timeout)
        )
        status_placeholders = ", ".join("?" for _ in completed_statuses)

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """
                SELECT business_type, business_key, execution_id, request_payload,
                       status, progress, message, result_payload, callback_status,
                       callback_attempts, last_callback_error, callback_claim_id,
                       callback_claim_expires_at, created_at, updated_at
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (business_type, business_key),
            ).fetchone()
            if current is None:
                return None
            current_execution_id = str(current["execution_id"] or "")
            if (
                expected_execution_id
                and current_execution_id != expected_execution_id
            ):
                raise TaskExecutionConflictError(
                    business_type,
                    business_key,
                    expected_execution_id,
                )
            if (
                current["status"] not in completed_statuses
                or current["callback_status"] not in {"pending", "failed"}
            ):
                return None
            current_claim_id = str(current["callback_claim_id"] or "")
            current_claim_expires_at = float(
                current["callback_claim_expires_at"] or 0
            )
            if (
                current_claim_id
                and current_claim_expires_at > now_epoch
            ):
                return None

            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_claim_id = ?, callback_claim_expires_at = ?,
                    updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND execution_id = ?
                  AND status IN ({status_placeholders})
                  AND callback_status IN ('pending', 'failed')
                  AND (
                      callback_claim_id = ''
                      OR callback_claim_expires_at <= ?
                  )
                """,
                (
                    claim_id,
                    claim_expires_at,
                    now,
                    business_type,
                    business_key,
                    current_execution_id,
                    *sorted(completed_statuses),
                    now_epoch,
                ),
            )
            if cursor.rowcount != 1:
                return None
            claimed = conn.execute(
                """
                SELECT business_type, business_key, execution_id, request_payload,
                       status, progress, message, result_payload, callback_status,
                       callback_attempts, last_callback_error, callback_claim_id,
                       callback_claim_expires_at, created_at, updated_at
                FROM llm_tasks
                WHERE business_type = ? AND business_key = ?
                """,
                (business_type, business_key),
            ).fetchone()
            if (
                claimed is None
                or claimed["execution_id"] != current_execution_id
                or claimed["callback_claim_id"] != claim_id
            ):
                raise RuntimeError("回调发送租约领取后无法读取一致快照")
            task = self._row_to_task(claimed)

        logger.info(
            "回调发送租约已领取: business_type=%s business_key=%s "
            "execution_id=%s lease_seconds=%.3f",
            business_type,
            business_key,
            current_execution_id,
            claim_expires_at - now_epoch,
        )
        return claim_id, task

    def _mark_callback_result(
        self,
        business_type: str,
        business_key: str,
        *,
        callback_status: str,
        error: str,
        execution_id: str | None = None,
        claim_id: str | None = None,
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
        expected_claim_id = str(claim_id or "").strip()
        claim_clause = (
            " AND callback_claim_id = ?"
            if expected_claim_id
            else " AND callback_claim_id = ''"
        )
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
        if expected_claim_id:
            update_params.append(expected_claim_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_status = ?, callback_attempts = callback_attempts + 1,
                    last_callback_error = ?, callback_claim_id = '',
                    callback_claim_expires_at = 0, updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND callback_status IN ('pending', 'failed')
                  AND status IN ({status_placeholders})
                  {execution_clause}
                  {claim_clause}
                """,
                tuple(update_params),
            )
            if cursor.rowcount != 1:
                task = conn.execute(
                    """
                    SELECT execution_id, status, callback_status,
                           callback_claim_id
                    FROM llm_tasks
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
                if (
                    expected_claim_id
                    and task["callback_claim_id"] != expected_claim_id
                ):
                    raise ValueError("回调发送租约已失效")
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
        claim_id: str | None = None,
    ) -> None:
        """记录一次实际失败的回调，禁止覆盖成功或无需回调终态。"""
        self._mark_callback_result(
            business_type,
            business_key,
            callback_status="failed",
            error=error,
            execution_id=execution_id,
            claim_id=claim_id,
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
        claim_id: str | None = None,
    ) -> None:
        """记录一次实际成功的回调，成功后状态不可再次改写。"""
        self._mark_callback_result(
            business_type,
            business_key,
            callback_status="success",
            error="",
            execution_id=execution_id,
            claim_id=claim_id,
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
        now_epoch = time.time()
        transition_succeeded = False
        current_status = ""
        callback_delivery_in_flight = False
        update_params: list[Any] = [
            now,
            business_type,
            business_key,
            *sorted(completed_statuses),
            now_epoch,
        ]
        if expected_execution_id:
            update_params.append(expected_execution_id)
        with self._connection() as conn:
            cursor = conn.execute(
                f"""
                UPDATE llm_tasks
                SET callback_status = 'skipped', last_callback_error = '',
                    callback_claim_id = '', callback_claim_expires_at = 0,
                    updated_at = ?
                WHERE business_type = ? AND business_key = ?
                  AND callback_status IN ('pending', 'skipped')
                  AND status IN ({status_placeholders})
                  AND (
                      callback_claim_id = ''
                      OR callback_claim_expires_at <= ?
                  )
                  {execution_clause}
                """,
                tuple(update_params),
            )
            transition_succeeded = cursor.rowcount == 1
            if not transition_succeeded:
                task = conn.execute(
                    """
                    SELECT execution_id, status, callback_status,
                           callback_claim_id, callback_claim_expires_at
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
                callback_delivery_in_flight = (
                    bool(task["callback_claim_id"])
                    and float(task["callback_claim_expires_at"] or 0)
                    > now_epoch
                )
                if callback_delivery_in_flight:
                    logger.warning(
                        "回调发送租约仍有效，暂不标记为 skipped: "
                        "business_type=%s business_key=%s",
                        business_type,
                        business_key,
                    )
                    return False
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
