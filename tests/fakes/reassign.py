"""分类节点变更 Repository/Knowledge Port 的严格离线 Fake。

这些替身不访问 SQLite、文件系统或网络。Repository Fake 通过短事务快照模拟原子本地事实；
Knowledge Fake 必须由测试显式声明每一次外部调用，并会拒绝事务仍开启时的网络调用、错误顺序
以及未经批准的重复副作用，防止测试用默认 truthy 结果掩盖真实 Saga 风险。
"""

from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from app.modules.reassign.domain import (
    ReassignmentContractError,
    ReassignmentBindingState,
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentOperation,
    ReassignmentOperationStatus,
    ReassignmentRawValue,
    ReassignmentStep,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidenceKind,
    operation_holds_document_protection,
    record_step_write_intent,
    transition_operation_status,
    transition_step_state,
)
from app.modules.reassign.domain.rules import build_step_idempotency_key
from app.modules.reassign.ports import (
    ReassignmentAuditEvent,
    ReassignmentBestEffortPinCompletion,
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentMutationResult,
    ReassignmentEventType,
    ReassignmentExpiredLeaseTakeoverRequest,
    ReassignmentKnowledgeOutcome,
    ReassignmentKnowledgePort,
    ReassignmentKnowledgePortFactory,
    ReassignmentLease,
    ReassignmentLeaseUpdateResult,
    ReassignmentLocalCommitState,
    ReassignmentLocalCommitRequest,
    ReassignmentNoSideEffectFailureRequest,
    ReassignmentMembershipProbeRequest,
    ReassignmentMembershipProbeResult,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentRecoveryCursor,
    ReassignmentRecoveryFinalizationRequest,
    ReassignmentRecoveryObservation,
    ReassignmentRecoveryObservationRecord,
    ReassignmentRepositoryPort,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentReservationResult,
    ReassignmentStepCompletion,
    ReassignmentStepRecord,
    ReassignmentUnitOfWork,
    ReassignmentWorkspacePreparationClaim,
    ReassignmentWorkspacePreparationClaimOutcome,
    ReassignmentWorkspacePreparationClaimRequest,
    ReassignmentWorkspacePreparationClaimResult,
    ReassignmentWorkspaceMappingRequest,
    ReassignmentWorkspacePreparationFactRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspacePreparationRequest,
    ReassignmentWorkspacePreparationResult,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceReferenceProbeRequest,
    ReassignmentWriteOutcome,
)


_SQLITE_INTEGER_MIN = -(2**63)
_SQLITE_INTEGER_MAX = 2**63 - 1


def _required_text(value: object, *, name: str) -> str:
    """测试替身同样拒绝隐式字符串转换，避免替身比生产宽松。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _text_digest(value: str) -> str:
    """与 SQLite Adapter 一致地保存可关联但不可逆的操作者摘要。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _now_text(clock: Callable[[], datetime]) -> str:
    """使用可控时钟生成 UTC 文本，避免测试依赖本机时区。"""

    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock 必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock 必须返回带时区 datetime")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _is_expired(expires_at: str, *, now: str) -> bool:
    """Fake 与 SQLite Adapter 使用同一 UTC 规范化后的 lease 过期判定。"""

    return _normalize_utc_text(expires_at, name="expires_at") <= _normalize_utc_text(
        now,
        name="now",
    )


def _normalize_utc_text(value: object, *, name: str) -> str:
    """按生产 Adapter 的规则规范化 lease 时间，禁止 Fake 用字符串字典序误判过期。"""

    text = _required_text(value, name=name)
    iso_value = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ReassignmentContractError(
            f"{name} 必须是带时区的 ISO-8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReassignmentContractError(f"{name} 必须包含时区")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _sqlite_storage_architecture_key(
    raw: ReassignmentRawValue,
    *,
    name: str,
) -> int:
    """模拟当前 SQLite ``INTEGER`` 亲和性中本阶段需要的原始分类值绑定。

    公开契约明确要求不提前收紧 ``newArchitectureId``：例如 ``"11"`` 与 ``false``
    仍可能进入本地提交。真实 SQLite 会把它们分别按数值 ``11`` 与 ``0`` 参与
    ``INTEGER`` 列比较；Fake 必须模拟这一最小兼容语义。非整数小数、非数字文本、
    数组和对象不能形成可恢复的本地权威 ID，因此与 SQLite Adapter 一致明确失败。
    """

    if not isinstance(raw, ReassignmentRawValue):
        raise TypeError(f"{name} 必须是 ReassignmentRawValue")
    value = raw.to_python()
    if type(value) is bool:
        storage_value = int(value)
    elif type(value) is int:
        storage_value = value
    elif isinstance(value, float) and value.is_integer():
        storage_value = int(value)
    elif isinstance(value, str):
        try:
            storage_value = int(value, 10)
        except ValueError as exc:
            raise ReassignmentContractError(
                f"{name} 不能投影为 SQLite INTEGER"
            ) from exc
    else:
        raise ReassignmentContractError(
            f"{name} 不能投影为 SQLite INTEGER"
        )
    if not _SQLITE_INTEGER_MIN <= storage_value <= _SQLITE_INTEGER_MAX:
        raise ReassignmentContractError(
            f"{name} 超出 SQLite INTEGER 范围"
        )
    return storage_value


def _document_target_architecture_id(raw: ReassignmentRawValue) -> int:
    """把可由当前 ``documents`` 领域快照表达的目标值转换为存储后的整数。"""

    value = _sqlite_storage_architecture_key(
        raw,
        name="target_architecture_raw",
    )
    return value


@dataclass
class _FakeReassignmentState:
    """跨多个 Fake UoW 共享的内存事实；所有字段只保存不可变 DTO。"""

    documents: dict[tuple[str, int], ReassignmentDocumentSnapshot]
    workspaces: dict[int, str]
    workspace_preparation_claims: dict[
        int,
        "_FakeWorkspacePreparationClaimState",
    ]
    operations: dict[str, ReassignmentOperationRecord]
    steps: dict[tuple[str, ReassignmentStepName], ReassignmentStepRecord]
    events: dict[str, tuple[ReassignmentAuditEvent, ...]]
    recovery_observations: dict[
        str,
        tuple[ReassignmentRecoveryObservationRecord, ...],
    ]
    fencing_by_document: dict[int, int]
    lock: threading.RLock


@dataclass(frozen=True)
class _FakeWorkspacePreparationClaimState:
    """保留目标 claim 的最新 fencing，即使释放后也不能发生 ABA 重用。"""

    claim: ReassignmentWorkspacePreparationClaim
    active: bool


class FakeReassignmentRepository(ReassignmentRepositoryPort):
    """线程安全的内存 Repository Fake，不会创建 SQLite 文件。"""

    def __init__(
        self,
        *,
        documents: tuple[ReassignmentDocumentSnapshot, ...] = (),
        workspace_mappings: tuple[tuple[object, str], ...] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        document_map: dict[tuple[str, int], ReassignmentDocumentSnapshot] = {}
        for document in documents:
            if not isinstance(document, ReassignmentDocumentSnapshot):
                raise TypeError("documents 只能包含 ReassignmentDocumentSnapshot")
            key = (document.file_name, document.source_architecture_id)
            if key in document_map:
                raise ValueError("Fake documents 不能包含重复 file_name + architecture")
            document_map[key] = document
        workspace_map: dict[int, str] = {}
        for raw_value, slug in workspace_mappings:
            raw = ReassignmentRawValue.from_external_value(raw_value)
            if raw.value is None:
                raise ValueError("workspace mapping architecture 不能为空")
            normalized_slug = _required_text(slug, name="workspace_slug")
            key = _sqlite_storage_architecture_key(
                raw,
                name="workspace_mapping_architecture",
            )
            if key in workspace_map and workspace_map[key] != normalized_slug:
                raise ValueError("Fake workspace mapping 不能覆盖不同 slug")
            if (
                any(
                    existing.casefold() == normalized_slug.casefold()
                    for existing in workspace_map.values()
                )
                and key not in workspace_map
            ):
                raise ValueError("Fake workspace slug 不能被多个分类复用")
            workspace_map[key] = normalized_slug
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._transaction_context = threading.local()
        self._state = _FakeReassignmentState(
            documents=document_map,
            workspaces=workspace_map,
            workspace_preparation_claims={},
            operations={},
            steps={},
            events={},
            recovery_observations={},
            fencing_by_document={},
            lock=threading.RLock(),
        )

    @property
    def transaction_active(self) -> bool:
        """判断当前线程是否持有 UoW，避免其他并发请求被误判为事务内调用。"""

        return bool(getattr(self._transaction_context, "active", False))

    def unit_of_work(
        self,
        *,
        read_only: bool = False,
    ) -> "FakeReassignmentUnitOfWork":
        if not isinstance(read_only, bool):
            raise TypeError("read_only 必须是 bool")
        return FakeReassignmentUnitOfWork(
            self._state,
            self._clock,
            transaction_context=self._transaction_context,
            read_only=read_only,
        )


class PostCommitFailureReassignmentRepository(ReassignmentRepositoryPort):
    """故障注入包装器：目标方法已提交后，让 Unit of Work 退出阶段抛出一次异常。"""

    def __init__(
        self,
        inner: ReassignmentRepositoryPort,
        *,
        target_method: str,
    ) -> None:
        if not isinstance(inner, ReassignmentRepositoryPort):
            raise TypeError("inner 必须实现 ReassignmentRepositoryPort")
        self._inner = inner
        self._target_method = _required_text(target_method, name="target_method")
        self._fault_consumed = False

    def unit_of_work(self, *, read_only: bool = False) -> ReassignmentUnitOfWork:
        return _PostCommitFailureUnitOfWork(
            self._inner.unit_of_work(read_only=read_only),
            self,
        )


class _PostCommitFailureUnitOfWork:
    """先委托真实 Fake 提交，再模拟提交确认或连接关闭阶段失败。"""

    def __init__(
        self,
        inner: ReassignmentUnitOfWork,
        owner: PostCommitFailureReassignmentRepository,
    ) -> None:
        self._inner = inner
        self._owner = owner
        self._raise_after_exit = False

    def __enter__(self) -> "_PostCommitFailureUnitOfWork":
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        suppressed = self._inner.__exit__(exc_type, exc_value, traceback)
        if (
            exc_type is None
            and self._raise_after_exit
            and not self._owner._fault_consumed
        ):
            self._owner._fault_consumed = True
            raise RuntimeError("模拟事务已提交但确认阶段异常")
        return suppressed

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if name != self._owner._target_method or not callable(attribute):
            return attribute

        def invoke(*args, **kwargs):
            result = attribute(*args, **kwargs)
            self._raise_after_exit = True
            return result

        return invoke


class FakeReassignmentUnitOfWork(ReassignmentUnitOfWork):
    """通过共享状态快照表达提交/回滚，不模拟或替代真实 SQL。"""

    def __init__(
        self,
        state: _FakeReassignmentState,
        clock: Callable[[], datetime],
        *,
        transaction_context: threading.local,
        read_only: bool = False,
    ) -> None:
        self._state = state
        self._clock = clock
        self._transaction_context = transaction_context
        self._read_only = read_only
        self._active = False
        self._before: tuple[
            dict[tuple[str, int], ReassignmentDocumentSnapshot],
            dict[int, str],
            dict[int, _FakeWorkspacePreparationClaimState],
            dict[str, ReassignmentOperationRecord],
            dict[tuple[str, ReassignmentStepName], ReassignmentStepRecord],
            dict[str, tuple[ReassignmentAuditEvent, ...]],
            dict[str, tuple[ReassignmentRecoveryObservationRecord, ...]],
            dict[int, int],
        ] | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def read_only(self) -> bool:
        return self._read_only

    def __enter__(self) -> "FakeReassignmentUnitOfWork":
        if self._active:
            raise RuntimeError("Fake UoW 不能重复进入")
        if bool(getattr(self._transaction_context, "active", False)):
            raise ReassignmentContractError("同一调用上下文禁止嵌套 Fake UoW")
        self._state.lock.acquire()
        self._before = (
            dict(self._state.documents),
            dict(self._state.workspaces),
            dict(self._state.workspace_preparation_claims),
            dict(self._state.operations),
            dict(self._state.steps),
            dict(self._state.events),
            dict(self._state.recovery_observations),
            dict(self._state.fencing_by_document),
        )
        self._transaction_context.active = True
        self._active = True
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool:
        if not self._active:
            return False
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        return False

    def commit(self) -> None:
        self._require_active()
        self._close()

    def rollback(self) -> None:
        self._require_active()
        if self._before is None:
            raise RuntimeError("Fake UoW 缺少事务快照")
        (
            self._state.documents,
            self._state.workspaces,
            self._state.workspace_preparation_claims,
            self._state.operations,
            self._state.steps,
            self._state.events,
            self._state.recovery_observations,
            self._state.fencing_by_document,
        ) = self._before
        self._close()

    def _close(self) -> None:
        self._before = None
        self._active = False
        self._transaction_context.active = False
        self._state.lock.release()

    def _require_active(self) -> None:
        if not self._active:
            raise ReassignmentContractError("Fake UnitOfWork 未处于活动事务")

    def _require_writable(self) -> None:
        self._require_active()
        if self._read_only:
            raise ReassignmentContractError("只读 Fake UnitOfWork 禁止执行写操作")

    def _now(self) -> str:
        return _now_text(self._clock)

    def _append_event(
        self,
        *,
        operation_id: str,
        event_type: ReassignmentEventType,
        step_name: ReassignmentStepName | None = None,
        operation_status: ReassignmentOperationStatus | None = None,
        detail_code: str | None = None,
        reference_digest: str | None = None,
        fencing_token: int | None = None,
        attempt_count: int | None = None,
        probe_outcome: ReassignmentMutationOutcome | None = None,
        actor_digest: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        existing = self._state.events.get(operation_id, ())
        event = ReassignmentAuditEvent(
            operation_id=operation_id,
            sequence_no=len(existing) + 1,
            event_type=event_type,
            occurred_at=self._now(),
            step_name=step_name,
            operation_status=operation_status,
            detail_code=detail_code,
            reference_digest=reference_digest,
            fencing_token=fencing_token,
            attempt_count=attempt_count,
            probe_outcome=probe_outcome,
            actor_digest=actor_digest,
            reason_code=reason_code,
        )
        self._state.events[operation_id] = (*existing, event)

    def get_document_snapshot(
        self,
        *,
        file_name: str,
        source_architecture_id: int,
    ) -> ReassignmentDocumentSnapshot | None:
        self._require_active()
        if not isinstance(source_architecture_id, int) or isinstance(
            source_architecture_id,
            bool,
        ):
            raise TypeError("source_architecture_id 必须是 int")
        return self._state.documents.get(
            (_required_text(file_name, name="file_name"), source_architecture_id)
        )

    def reserve(
        self,
        request: ReassignmentReservationRequest,
    ) -> ReassignmentReservationResult:
        self._require_writable()
        if not isinstance(request, ReassignmentReservationRequest):
            raise TypeError("request 必须是 ReassignmentReservationRequest")
        snapshot = self.get_document_snapshot(
            file_name=request.command.file_name,
            source_architecture_id=request.command.old_architecture_id_query_value,
        )
        if snapshot is None:
            return ReassignmentReservationResult(
                ReassignmentReservationOutcome.DOCUMENT_NOT_FOUND
            )
        if request.operation_id in self._state.operations:
            # 生产 SQLite 以 operation_id 主键拒绝重用。Fake 也必须 fail fast，
            # 不能静默覆盖历史审计或把两个请求误拼成同一次 Saga。
            raise ReassignmentContractError("Fake operation_id 不能重复使用")
        if any(
            operation.operation.document.document_row_id == snapshot.document_row_id
            and operation_holds_document_protection(operation.operation.status)
            for operation in self._state.operations.values()
        ):
            return ReassignmentReservationResult(
                ReassignmentReservationOutcome.ACTIVE_OPERATION_EXISTS
            )
        now = self._now()
        normalized_expiry = _normalize_utc_text(
            request.lease_expires_at,
            name="lease_expires_at",
        )
        if _is_expired(normalized_expiry, now=now):
            raise ReassignmentContractError("初始 lease 已过期")
        fencing = self._state.fencing_by_document.get(snapshot.document_row_id, 0) + 1
        self._state.fencing_by_document[snapshot.document_row_id] = fencing
        source_raw = request.command.old_architecture_id_raw
        # 旧 workspace 必须按 Web Adapter 已完成 ``int(...)`` 转换后的查询值读取；
        # 原始 ``\"11\"`` 只用于审计，不能令 Fake 与真实 workspaces 表查找结果不同。
        source_workspace = self._state.workspaces.get(
            snapshot.source_architecture_id
        )
        operation = ReassignmentOperation(
            operation_id=request.operation_id,
            document=snapshot,
            source_architecture_id=snapshot.source_architecture_id,
            source_architecture_raw=source_raw,
            target_architecture_raw=request.command.new_architecture_id_raw,
            status=ReassignmentOperationStatus.RESERVED,
            current_step=ReassignmentStepName.RESERVE_DOCUMENT,
            lease_owner=request.lease_owner,
            lease_token=request.lease_token,
            lease_expires_at=normalized_expiry,
            fencing_token=fencing,
        )
        record = ReassignmentOperationRecord(
            operation=operation,
            source_workspace_slug=source_workspace,
            target_workspace_slug=None,
            target_workspace_ownership=None,
            error_code=None,
            error_summary=None,
            recovery_required_fencing_token=None,
            created_at=now,
            updated_at=now,
        )
        self._state.operations[operation.operation_id] = record
        for step_name in ReassignmentStepName:
            if step_name is ReassignmentStepName.RESERVE_DOCUMENT:
                step = ReassignmentStep(
                    operation_id=operation.operation_id,
                    step_name=step_name,
                    idempotency_key=build_step_idempotency_key(operation, step_name),
                    state=ReassignmentStepState.SUCCEEDED,
                    write_intent_recorded=True,
                )
            else:
                step = ReassignmentStep(
                    operation_id=operation.operation_id,
                    step_name=step_name,
                    idempotency_key=build_step_idempotency_key(operation, step_name),
                )
            self._state.steps[(operation.operation_id, step_name)] = ReassignmentStepRecord(
                step=step,
                attempt_count=0,
                last_attempt_fencing_token=None,
                mutation_started_at=None,
                probe_outcome=None,
                created_at=now,
                updated_at=now,
            )
        self._append_event(
            operation_id=operation.operation_id,
            event_type=ReassignmentEventType.OPERATION_RESERVED,
            step_name=ReassignmentStepName.RESERVE_DOCUMENT,
            operation_status=operation.status,
            fencing_token=operation.fencing_token,
        )
        return ReassignmentReservationResult(
            ReassignmentReservationOutcome.ACQUIRED,
            record,
        )

    def get_operation(
        self,
        operation_id: str,
    ) -> ReassignmentOperationRecord | None:
        self._require_active()
        return self._state.operations.get(_required_text(operation_id, name="operation_id"))

    def get_step(
        self,
        *,
        operation_id: str,
        step_name: ReassignmentStepName,
    ) -> ReassignmentStepRecord | None:
        self._require_active()
        if not isinstance(step_name, ReassignmentStepName):
            raise TypeError("step_name 必须是 ReassignmentStepName")
        return self._state.steps.get(
            (_required_text(operation_id, name="operation_id"), step_name)
        )

    def list_steps(self, operation_id: str) -> tuple[ReassignmentStepRecord, ...]:
        self._require_active()
        normalized = _required_text(operation_id, name="operation_id")
        return tuple(
            record
            for (_, _), record in self._state.steps.items()
            if record.step.operation_id == normalized
        )

    def list_events(self, operation_id: str) -> tuple[ReassignmentAuditEvent, ...]:
        self._require_active()
        return self._state.events.get(
            _required_text(operation_id, name="operation_id"),
            (),
        )

    def list_recoverable_operations(
        self,
        *,
        limit: int,
        cursor: ReassignmentRecoveryCursor | None = None,
    ) -> tuple[ReassignmentOperationRecord, ...]:
        """按与 SQLite 相同的稳定顺序返回 lease 已过期的保护中 Operation。"""

        self._require_active()
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是 int")
        if limit < 1 or limit > 500:
            raise ValueError("limit 必须在 1 到 500 之间")
        if cursor is not None and not isinstance(cursor, ReassignmentRecoveryCursor):
            raise TypeError("cursor 必须是 ReassignmentRecoveryCursor 或 None")
        now = self._now()
        cursor_key = (
            None
            if cursor is None
            else (
                _normalize_utc_text(
                    cursor.lease_expires_at,
                    name="cursor.lease_expires_at",
                ),
                cursor.operation_id,
            )
        )
        candidates = sorted(
            (
                record
                for record in self._state.operations.values()
                if operation_holds_document_protection(record.operation.status)
                and _is_expired(record.lease.expires_at, now=now)
                and (
                    cursor_key is None
                    or (
                        record.lease.expires_at,
                        record.operation.operation_id,
                    )
                    > cursor_key
                )
            ),
            key=lambda record: (
                record.lease.expires_at,
                record.operation.operation_id,
            ),
        )
        return tuple(candidates[:limit])

    def probe_local_commit_state(
        self,
        operation_id: str,
    ) -> ReassignmentLocalCommitState:
        """用 Fake 当前权威行模拟 SQLite 的恢复 CAS 探测。"""

        self._require_active()
        record = self._state.operations.get(
            _required_text(operation_id, name="operation_id")
        )
        if record is None:
            raise ReassignmentContractError("Fake Operation 不存在，无法探测本地提交状态")
        document = record.operation.document
        candidates = tuple(
            candidate
            for candidate in self._state.documents.values()
            if candidate.document_row_id == document.document_row_id
        )
        if len(candidates) != 1:
            return ReassignmentLocalCommitState.CONFLICT
        current = candidates[0]
        if (
            current.file_name != document.file_name
            or current.anything_doc_id != document.anything_doc_id
            or current.doc_path != document.doc_path
        ):
            return ReassignmentLocalCommitState.CONFLICT
        if current.source_architecture_id == document.source_architecture_id:
            return ReassignmentLocalCommitState.SOURCE_UNCHANGED
        if current.source_architecture_id == _document_target_architecture_id(
            record.operation.target_architecture_raw
        ):
            return ReassignmentLocalCommitState.TARGET_COMMITTED
        return ReassignmentLocalCommitState.CONFLICT

    def get_workspace_slug(self, architecture_raw: ReassignmentRawValue) -> str | None:
        self._require_active()
        if not isinstance(architecture_raw, ReassignmentRawValue):
            raise TypeError("architecture_raw 必须是 ReassignmentRawValue")
        return self._state.workspaces.get(
            _sqlite_storage_architecture_key(
                architecture_raw,
                name="architecture_raw",
            )
        )

    def acquire_workspace_preparation_claim(
        self,
        request: ReassignmentWorkspacePreparationClaimRequest,
    ) -> ReassignmentWorkspacePreparationClaimResult | ReassignmentWriteOutcome:
        """以与 SQLite 一致的目标键、过期接管和 fencing 语义模拟准备权。"""

        self._require_writable()
        if not isinstance(request, ReassignmentWorkspacePreparationClaimRequest):
            raise TypeError(
                "request 必须是 ReassignmentWorkspacePreparationClaimRequest"
            )
        owned = self._owned_operation(request.operation_lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if (
            owned.operation.target_architecture_raw.canonical_json()
            != request.target_architecture_raw.canonical_json()
        ):
            raise ReassignmentContractError("Fake 准备权目标分类与 Operation 不一致")
        now = self._now()
        expiry = _normalize_utc_text(
            request.claim_expires_at,
            name="claim_expires_at",
        )
        if _is_expired(expiry, now=now):
            raise ReassignmentContractError("Fake 目标 workspace 准备权已过期")
        target_key = _sqlite_storage_architecture_key(
            request.target_architecture_raw,
            name="target_architecture_raw",
        )
        workspace_slug = self._state.workspaces.get(target_key)
        if workspace_slug is not None:
            return ReassignmentWorkspacePreparationClaimResult(
                ReassignmentWorkspacePreparationClaimOutcome.MAPPING_EXISTS,
                workspace_slug=workspace_slug,
            )
        persisted = self._state.workspace_preparation_claims.get(target_key)
        fencing_token = 1
        if persisted is not None:
            current = persisted.claim
            same_owner = (
                persisted.active
                and current.operation_id == request.operation_lease.operation_id
                and current.owner == request.operation_lease.owner
                and current.token == request.claim_token
            )
            if same_owner and not _is_expired(current.expires_at, now=now):
                return ReassignmentWorkspacePreparationClaimResult(
                    ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
                    claim=current,
                )
            if persisted.active and not _is_expired(current.expires_at, now=now):
                self._append_event(
                    operation_id=request.operation_lease.operation_id,
                    event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_BLOCKED,
                    operation_status=owned.operation.status,
                    detail_code="active_target_preparation_claim",
                    fencing_token=request.operation_lease.fencing_token,
                )
                return ReassignmentWorkspacePreparationClaimResult(
                    ReassignmentWorkspacePreparationClaimOutcome.ACTIVE_CLAIM_EXISTS
                )
            fencing_token = current.fencing_token + 1
        claim = ReassignmentWorkspacePreparationClaim(
            target_architecture_raw=request.target_architecture_raw,
            operation_id=request.operation_lease.operation_id,
            owner=request.operation_lease.owner,
            token=request.claim_token,
            fencing_token=fencing_token,
            expires_at=expiry,
        )
        self._state.workspace_preparation_claims[target_key] = (
            _FakeWorkspacePreparationClaimState(claim=claim, active=True)
        )
        self._append_event(
            operation_id=request.operation_lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_ACQUIRED,
            operation_status=owned.operation.status,
            detail_code=(
                "claim_acquired"
                if persisted is None
                else "claim_taken_over_or_reacquired"
            ),
            fencing_token=claim.fencing_token,
            actor_digest=_text_digest(request.operation_lease.owner),
        )
        return ReassignmentWorkspacePreparationClaimResult(
            ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED,
            claim=claim,
        )

    def release_workspace_preparation_claim(
        self,
        claim: ReassignmentWorkspacePreparationClaim,
    ) -> ReassignmentWriteOutcome:
        """释放当前 owner 的 claim，同时保留 fencing 历史。"""

        self._require_writable()
        if not isinstance(claim, ReassignmentWorkspacePreparationClaim):
            raise TypeError("claim 必须是 ReassignmentWorkspacePreparationClaim")
        target_key = _sqlite_storage_architecture_key(
            claim.target_architecture_raw,
            name="claim.target_architecture_raw",
        )
        persisted = self._state.workspace_preparation_claims.get(target_key)
        if persisted is None or not persisted.active or persisted.claim != claim:
            return ReassignmentWriteOutcome.STALE_LEASE
        if _is_expired(claim.expires_at, now=self._now()):
            return ReassignmentWriteOutcome.STALE_LEASE
        self._state.workspace_preparation_claims[target_key] = replace(
            persisted,
            active=False,
        )
        self._append_event(
            operation_id=claim.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_RELEASED,
            detail_code="claim_released",
            fencing_token=claim.fencing_token,
            actor_digest=_text_digest(claim.owner),
        )
        return ReassignmentWriteOutcome.APPLIED

    def _owned_operation(
        self,
        lease: ReassignmentLease,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        if not isinstance(lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        record = self._state.operations.get(lease.operation_id)
        if record is None:
            return ReassignmentWriteOutcome.OPERATION_NOT_FOUND
        if not operation_holds_document_protection(record.operation.status):
            return ReassignmentWriteOutcome.CONFLICT
        current = record.lease
        if current != lease or _is_expired(current.expires_at, now=self._now()):
            return ReassignmentWriteOutcome.STALE_LEASE
        return record

    @staticmethod
    def _recovery_fencing_is_authorized(
        record: ReassignmentOperationRecord,
        *,
        lease: ReassignmentLease,
        recovery_authorized: bool,
    ) -> bool:
        """与 SQLite Adapter 一致地拒绝旧 fencing 解封恢复隔离。"""

        if record.operation.status is not ReassignmentOperationStatus.RECOVERY_REQUIRED:
            return True
        return (
            recovery_authorized
            and record.recovery_required_fencing_token is not None
            and lease.fencing_token > record.recovery_required_fencing_token
        )

    def renew_lease(
        self,
        *,
        lease: ReassignmentLease,
        lease_expires_at: str,
    ) -> ReassignmentLeaseUpdateResult:
        self._require_writable()
        owned = self._owned_operation(lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return ReassignmentLeaseUpdateResult(owned)
        now = self._now()
        normalized_expiry = _normalize_utc_text(
            lease_expires_at,
            name="lease_expires_at",
        )
        if _is_expired(normalized_expiry, now=now):
            raise ReassignmentContractError("续租 lease 已过期")
        renewed = ReassignmentLease(
            operation_id=lease.operation_id,
            owner=lease.owner,
            token=lease.token,
            fencing_token=lease.fencing_token,
            expires_at=normalized_expiry,
        )
        operation = replace(
            owned.operation,
            lease_expires_at=renewed.expires_at,
        )
        self._state.operations[lease.operation_id] = replace(
            owned,
            operation=operation,
            updated_at=now,
        )
        renewed_claim: ReassignmentWorkspacePreparationClaim | None = None
        for target_key, persisted in tuple(
            self._state.workspace_preparation_claims.items()
        ):
            current_claim = persisted.claim
            if (
                persisted.active
                and current_claim.operation_id == lease.operation_id
                and current_claim.owner == lease.owner
                and not _is_expired(current_claim.expires_at, now=now)
            ):
                renewed_claim = replace(
                    current_claim,
                    expires_at=renewed.expires_at,
                )
                self._state.workspace_preparation_claims[target_key] = replace(
                    persisted,
                    claim=renewed_claim,
                )
                break
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.LEASE_RENEWED,
            operation_status=operation.status,
            fencing_token=lease.fencing_token,
            actor_digest=_text_digest(lease.owner),
        )
        return ReassignmentLeaseUpdateResult(
            ReassignmentWriteOutcome.APPLIED,
            renewed,
            renewed_claim,
        )

    def take_over_expired_lease(
        self,
        request: ReassignmentExpiredLeaseTakeoverRequest,
    ) -> ReassignmentLeaseUpdateResult:
        self._require_writable()
        if not isinstance(request, ReassignmentExpiredLeaseTakeoverRequest):
            raise TypeError("request 必须是 ReassignmentExpiredLeaseTakeoverRequest")
        record = self._state.operations.get(request.operation_id)
        if record is None:
            return ReassignmentLeaseUpdateResult(
                ReassignmentWriteOutcome.OPERATION_NOT_FOUND
            )
        if not operation_holds_document_protection(record.operation.status):
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.CONFLICT)
        if record.lease.fencing_token != request.expected_fencing_token:
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.STALE_LEASE)
        now = self._now()
        if not _is_expired(record.lease.expires_at, now=now):
            return ReassignmentLeaseUpdateResult(ReassignmentWriteOutcome.NOT_EXPIRED)
        normalized_expiry = _normalize_utc_text(
            request.lease_expires_at,
            name="lease_expires_at",
        )
        if _is_expired(normalized_expiry, now=now):
            raise ReassignmentContractError("接管后的 lease 已过期")
        lease = ReassignmentLease(
            operation_id=request.operation_id,
            owner=request.lease_owner,
            token=request.lease_token,
            fencing_token=request.expected_fencing_token + 1,
            expires_at=normalized_expiry,
        )
        operation = replace(
            record.operation,
            lease_owner=lease.owner,
            lease_token=lease.token,
            lease_expires_at=lease.expires_at,
            fencing_token=lease.fencing_token,
        )
        self._state.operations[request.operation_id] = replace(
            record,
            operation=operation,
            updated_at=now,
        )
        recovered_claim: ReassignmentWorkspacePreparationClaim | None = None
        for target_key, persisted in tuple(
            self._state.workspace_preparation_claims.items()
        ):
            current_claim = persisted.claim
            if (
                not persisted.active
                or current_claim.operation_id != request.operation_id
            ):
                continue
            if _is_expired(current_claim.expires_at, now=now):
                recovered_claim = ReassignmentWorkspacePreparationClaim(
                    target_architecture_raw=current_claim.target_architecture_raw,
                    operation_id=request.operation_id,
                    owner=request.lease_owner,
                    token=request.workspace_claim_token or current_claim.token,
                    fencing_token=current_claim.fencing_token + 1,
                    expires_at=lease.expires_at,
                )
                self._state.workspace_preparation_claims[target_key] = replace(
                    persisted,
                    claim=recovered_claim,
                )
                self._append_event(
                    operation_id=request.operation_id,
                    event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_TAKEN_OVER,
                    operation_status=operation.status,
                    detail_code="workspace_claim_taken_over",
                    fencing_token=recovered_claim.fencing_token,
                    actor_digest=_text_digest(request.actor or request.lease_owner),
                    reason_code=request.reason_code,
                )
            else:
                self._append_event(
                    operation_id=request.operation_id,
                    event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_BLOCKED,
                    operation_status=operation.status,
                    detail_code="workspace_claim_not_expired_during_takeover",
                    fencing_token=lease.fencing_token,
                    actor_digest=_text_digest(request.actor or request.lease_owner),
                    reason_code=request.reason_code,
                )
            break
        self._append_event(
            operation_id=request.operation_id,
            event_type=ReassignmentEventType.LEASE_TAKEN_OVER,
            operation_status=operation.status,
            fencing_token=lease.fencing_token,
            actor_digest=_text_digest(request.actor or request.lease_owner),
            reason_code=request.reason_code,
        )
        return ReassignmentLeaseUpdateResult(
            ReassignmentWriteOutcome.APPLIED,
            lease,
            recovered_claim,
        )

    def record_recovery_observation(
        self,
        observation: ReassignmentRecoveryObservation,
    ) -> ReassignmentRecoveryObservationRecord | ReassignmentWriteOutcome:
        """严格模拟恢复探测的追加事实；网络调用仍必须发生在 Fake UoW 外。"""

        self._require_writable()
        if not isinstance(observation, ReassignmentRecoveryObservation):
            raise TypeError("observation 必须是 ReassignmentRecoveryObservation")
        owned = self._owned_operation(observation.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=observation.lease,
            recovery_authorized=True,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        expected_remote = owned.operation.document.requires_remote_membership_change
        if observation.remote_membership_required != expected_remote:
            raise ReassignmentContractError("Fake 恢复观测远端标记不匹配")
        if not expected_remote and (
            observation.source_binding_state
            is not ReassignmentBindingState.NOT_APPLICABLE
            or observation.target_binding_state
            is not ReassignmentBindingState.NOT_APPLICABLE
        ):
            raise ReassignmentContractError("Fake 本地-only 恢复观测包含远端状态")
        observation_id = (
            sum(len(values) for values in self._state.recovery_observations.values())
            + 1
        )
        record = ReassignmentRecoveryObservationRecord(
            observation_id=observation_id,
            observation=observation,
            observed_at=self._now(),
        )
        existing = self._state.recovery_observations.get(
            observation.lease.operation_id,
            (),
        )
        self._state.recovery_observations[observation.lease.operation_id] = (
            *existing,
            record,
        )
        self._append_event(
            operation_id=observation.lease.operation_id,
            event_type=ReassignmentEventType.RECOVERY_OBSERVATION_RECORDED,
            step_name=owned.operation.current_step,
            operation_status=owned.operation.status,
            detail_code=(
                f"local={observation.local_commit_state.value};"
                f"source={observation.source_binding_state.value};"
                f"target={observation.target_binding_state.value}"
            ),
            fencing_token=observation.lease.fencing_token,
            actor_digest=_text_digest(observation.actor),
            reason_code=observation.reason_code,
        )
        return record

    def _validate_recovery_terminal_observation(
        self,
        *,
        owned: ReassignmentOperationRecord,
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> None:
        """与 SQLite Adapter 一致地验证最新观测与当前本地行没有发生漂移。"""

        observation = request.observation.observation
        if observation.remote_membership_required != (
            owned.operation.document.requires_remote_membership_change
        ):
            raise ReassignmentContractError("Fake 恢复终态远端标记不匹配")
        if (
            self.probe_local_commit_state(request.lease.operation_id)
            is not observation.local_commit_state
        ):
            raise ReassignmentContractError("Fake 恢复终态前本地文档状态已变化")
        latest = self._state.recovery_observations.get(
            request.lease.operation_id,
            (),
        )
        if not latest or latest[-1] != request.observation:
            raise ReassignmentContractError("Fake 恢复终态未引用最新观测")

    @staticmethod
    def _validate_recovery_terminal_invariant(
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> None:
        """复用生产 Adapter 的终态不变量，防止 Fake 放宽补偿安全界限。"""

        observation = request.observation.observation
        if request.next_status is ReassignmentOperationStatus.SUCCEEDED:
            if observation.local_commit_state is not ReassignmentLocalCommitState.TARGET_COMMITTED:
                raise ReassignmentContractError("Fake 恢复成功要求本地分类已提交到目标")
            if observation.remote_membership_required:
                if (
                    observation.target_binding_state
                    is not ReassignmentBindingState.CONFIRMED_PRESENT
                    or observation.source_binding_state
                    not in {
                        ReassignmentBindingState.CONFIRMED_ABSENT,
                        ReassignmentBindingState.NOT_APPLICABLE,
                    }
                ):
                    raise ReassignmentContractError("Fake 恢复成功远端状态不一致")
            elif (
                observation.source_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
                or observation.target_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
            ):
                raise ReassignmentContractError("Fake 本地-only 成功状态不一致")
            return
        if request.next_status is ReassignmentOperationStatus.COMPENSATED:
            if observation.local_commit_state is not ReassignmentLocalCommitState.SOURCE_UNCHANGED:
                raise ReassignmentContractError("Fake 补偿终态要求本地分类仍指向来源")
            if (
                not observation.remote_membership_required
                or observation.target_binding_state
                is not ReassignmentBindingState.CONFIRMED_ABSENT
                or observation.source_binding_state
                not in {
                    ReassignmentBindingState.CONFIRMED_PRESENT,
                    ReassignmentBindingState.NOT_APPLICABLE,
                }
            ):
                raise ReassignmentContractError("Fake 补偿终态远端状态不一致")
            return
        if request.next_status is ReassignmentOperationStatus.FAILED:
            if observation.local_commit_state is not ReassignmentLocalCommitState.SOURCE_UNCHANGED:
                raise ReassignmentContractError("Fake 失败终态要求本地分类未提交")
            if observation.remote_membership_required and (
                observation.target_binding_state
                is not ReassignmentBindingState.CONFIRMED_ABSENT
                or observation.source_binding_state
                not in {
                    ReassignmentBindingState.CONFIRMED_PRESENT,
                    ReassignmentBindingState.NOT_APPLICABLE,
                }
            ):
                raise ReassignmentContractError("Fake 无副作用失败远端状态不一致")
            if not observation.remote_membership_required and (
                observation.source_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
                or observation.target_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
            ):
                raise ReassignmentContractError("Fake 本地-only 失败状态不一致")
            return
        raise ReassignmentContractError("Fake 不支持的恢复终态")

    def finalize_recovery_operation(
        self,
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """严格模拟恢复终态和接管 claim 的原子释放。"""

        self._require_writable()
        if not isinstance(request, ReassignmentRecoveryFinalizationRequest):
            raise TypeError("request 必须是 ReassignmentRecoveryFinalizationRequest")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=request.lease,
            recovery_authorized=True,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        self._validate_recovery_terminal_observation(owned=owned, request=request)
        self._validate_recovery_terminal_invariant(request)
        if request.next_status is ReassignmentOperationStatus.SUCCEEDED:
            # 严格 Fake 复用生产 Adapter 的成功门禁，避免测试把缺少 workspace
            # 事实或前向检查点的异常现场错误地收敛成成功。
            self._validate_forward_success_prerequisites(owned)
        if request.next_status is ReassignmentOperationStatus.FAILED:
            self._validate_no_side_effect_failure_prerequisites(owned)

        observation = request.observation.observation
        if request.preparation_claim is not None:
            claim = request.preparation_claim
            target_key = _sqlite_storage_architecture_key(
                claim.target_architecture_raw,
                name="preparation_claim.target_architecture_raw",
            )
            persisted = self._state.workspace_preparation_claims.get(target_key)
            if (
                persisted is None
                or not persisted.active
                or persisted.claim != claim
                or claim.operation_id != request.lease.operation_id
                or claim.owner != request.lease.owner
            ):
                return ReassignmentWriteOutcome.STALE_LEASE
            self._state.workspace_preparation_claims[target_key] = replace(
                persisted,
                active=False,
            )
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_PREPARATION_CLAIM_RELEASED,
                detail_code="recovery_claim_released",
                fencing_token=claim.fencing_token,
                actor_digest=_text_digest(observation.actor),
                reason_code=observation.reason_code,
            )

        operation = replace(
            transition_operation_status(
                owned.operation,
                request.next_status,
                recovery_authorized=True,
                terminal_evidence=request.terminal_evidence,
            ),
            current_step=request.current_step,
        )
        now = self._now()
        updated = replace(
            owned,
            operation=operation,
            error_code=request.error_code,
            error_summary=request.error_summary,
            updated_at=now,
            finished_at=now,
        )
        self._state.operations[request.lease.operation_id] = updated
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.RECOVERY_OPERATION_FINALIZED,
            step_name=operation.current_step,
            operation_status=operation.status,
            detail_code=operation.status.value,
            fencing_token=request.lease.fencing_token,
            actor_digest=_text_digest(observation.actor),
            reason_code=observation.reason_code,
        )
        return updated

    def begin_step_mutation(
        self,
        *,
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
        recovery_authorized: bool = False,
    ) -> ReassignmentStepRecord | ReassignmentWriteOutcome:
        self._require_writable()
        owned = self._owned_operation(lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=lease,
            recovery_authorized=recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        record = self.get_step(operation_id=lease.operation_id, step_name=step_name)
        if record is None:
            raise ReassignmentContractError("Fake Operation 缺少固定 Step")
        if record.step.state is ReassignmentStepState.PENDING:
            step = transition_step_state(
                record_step_write_intent(record.step),
                ReassignmentStepState.MUTATION_STARTED,
            )
        elif (
            record.step.state is ReassignmentStepState.KNOWN_FAILED
            and recovery_authorized
        ):
            if (
                record.last_attempt_fencing_token is None
                or lease.fencing_token <= record.last_attempt_fencing_token
            ):
                return ReassignmentWriteOutcome.CONFLICT
            step = replace(
                transition_step_state(
                    record.step,
                    ReassignmentStepState.MUTATION_STARTED,
                    recovery_authorized=True,
                ),
                external_reference=None,
                error_code=None,
                error_summary=None,
            )
        else:
            raise ReassignmentContractError("Fake Step 外部写顺序不合法")
        now = self._now()
        updated = ReassignmentStepRecord(
            step=step,
            attempt_count=record.attempt_count + 1,
            last_attempt_fencing_token=lease.fencing_token,
            mutation_started_at=now,
            probe_outcome=None,
            created_at=record.created_at,
            updated_at=now,
        )
        self._state.steps[(lease.operation_id, step_name)] = updated
        self._state.operations[lease.operation_id] = replace(
            owned,
            operation=replace(owned.operation, current_step=step_name),
            updated_at=now,
        )
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.STEP_MUTATION_STARTED,
            step_name=step_name,
            operation_status=owned.operation.status,
            fencing_token=lease.fencing_token,
            attempt_count=updated.attempt_count,
        )
        return updated

    def complete_step(
        self,
        completion: ReassignmentStepCompletion,
    ) -> ReassignmentStepRecord | ReassignmentWriteOutcome:
        self._require_writable()
        if not isinstance(completion, ReassignmentStepCompletion):
            raise TypeError("completion 必须是 ReassignmentStepCompletion")
        owned = self._owned_operation(completion.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=completion.lease,
            recovery_authorized=completion.recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        record = self.get_step(
            operation_id=completion.lease.operation_id,
            step_name=completion.step_name,
        )
        if record is None:
            raise ReassignmentContractError("Fake Operation 缺少固定 Step")
        step = replace(
            transition_step_state(
                record.step,
                completion.next_state,
                recovery_authorized=completion.recovery_authorized,
            ),
            external_reference=completion.external_reference,
            error_code=completion.error_code,
            error_summary=completion.error_summary,
        )
        now = self._now()
        updated = ReassignmentStepRecord(
            step=step,
            attempt_count=record.attempt_count,
            last_attempt_fencing_token=record.last_attempt_fencing_token,
            mutation_started_at=record.mutation_started_at,
            probe_outcome=completion.probe_outcome,
            created_at=record.created_at,
            updated_at=now,
        )
        self._state.steps[(completion.lease.operation_id, completion.step_name)] = updated
        self._state.operations[completion.lease.operation_id] = replace(
            owned,
            operation=replace(
                owned.operation,
                current_step=completion.step_name,
            ),
            updated_at=now,
        )
        self._append_event(
            operation_id=completion.lease.operation_id,
            event_type=ReassignmentEventType.STEP_COMPLETED,
            step_name=completion.step_name,
            operation_status=owned.operation.status,
            detail_code=step.state.value,
            fencing_token=completion.lease.fencing_token,
            attempt_count=record.attempt_count,
            probe_outcome=completion.probe_outcome,
        )
        return updated

    def transition_operation(
        self,
        transition: ReassignmentOperationTransition,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        self._require_writable()
        if not isinstance(transition, ReassignmentOperationTransition):
            raise TypeError("transition 必须是 ReassignmentOperationTransition")
        if transition.next_status in {
            ReassignmentOperationStatus.SUCCEEDED,
            ReassignmentOperationStatus.FAILED,
            ReassignmentOperationStatus.COMPENSATED,
        }:
            raise ReassignmentContractError(
                "Fake 终态必须使用具备持久事实校验的专用提交方法"
            )
        owned = self._owned_operation(transition.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=transition.lease,
            recovery_authorized=transition.recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        operation = replace(
            transition_operation_status(
                owned.operation,
                transition.next_status,
                recovery_authorized=transition.recovery_authorized,
                terminal_evidence=transition.terminal_evidence,
            ),
            current_step=transition.current_step,
        )
        now = self._now()
        updated = replace(
            owned,
            operation=operation,
            error_code=transition.error_code,
            error_summary=transition.error_summary,
            recovery_required_fencing_token=(
                transition.lease.fencing_token
                if operation.status is ReassignmentOperationStatus.RECOVERY_REQUIRED
                else owned.recovery_required_fencing_token
            ),
            updated_at=now,
            finished_at=(
                now
                if operation.status
                in {
                    ReassignmentOperationStatus.SUCCEEDED,
                    ReassignmentOperationStatus.FAILED,
                    ReassignmentOperationStatus.COMPENSATED,
                }
                else None
            ),
        )
        self._state.operations[operation.operation_id] = updated
        self._append_event(
            operation_id=operation.operation_id,
            event_type=ReassignmentEventType.OPERATION_TRANSITIONED,
            step_name=operation.current_step,
            operation_status=operation.status,
            fencing_token=transition.lease.fencing_token,
            detail_code=operation.status.value,
        )
        return updated

    def record_workspace_mapping(
        self,
        request: ReassignmentWorkspaceMappingRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        self._require_writable()
        if not isinstance(request, ReassignmentWorkspaceMappingRequest):
            raise TypeError("request 必须是 ReassignmentWorkspaceMappingRequest")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if (
            owned.operation.target_architecture_raw.canonical_json()
            != request.target_architecture_raw.canonical_json()
        ):
            raise ReassignmentContractError("Fake workspace mapping 目标分类不一致")
        step_record = self.get_step(
            operation_id=request.lease.operation_id,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        )
        if step_record is None:
            raise ReassignmentContractError("Fake Operation 缺少 prepare Step")
        step = transition_step_state(
            step_record.step,
            ReassignmentStepState.SUCCEEDED,
        )
        target_key = _sqlite_storage_architecture_key(
            request.target_architecture_raw,
            name="target_architecture_raw",
        )
        claim = request.preparation_claim
        if claim is not None:
            if (
                claim.operation_id != request.lease.operation_id
                or claim.owner != request.lease.owner
                or claim.target_architecture_raw.canonical_json()
                != request.target_architecture_raw.canonical_json()
            ):
                raise ReassignmentContractError("Fake workspace mapping 准备权不匹配")
            persisted_claim = self._state.workspace_preparation_claims.get(target_key)
            if (
                persisted_claim is None
                or not persisted_claim.active
                or persisted_claim.claim != claim
                or _is_expired(claim.expires_at, now=self._now())
            ):
                return ReassignmentWriteOutcome.STALE_LEASE
        existing = self._state.workspaces.get(target_key)
        if (
            existing is not None
            and existing.casefold() != request.workspace_slug.casefold()
        ):
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_mapping_conflict",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if existing is None and claim is None:
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_preparation_claim_required",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if (
            existing is not None
            and request.ownership
            is ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION
        ):
            # 与 SQLite Adapter 一致：已有映射不能被当前 Operation 标为“由我创建”，
            # 否则后续补偿会错误地把共享 workspace 当成可删除资源。
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_creation_fact_conflict",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            return ReassignmentWriteOutcome.CONFLICT
        if existing is None and any(
            slug.casefold() == request.workspace_slug.casefold()
            for slug in self._state.workspaces.values()
        ):
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
                step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                operation_status=owned.operation.status,
                detail_code="workspace_slug_conflict",
                fencing_token=request.lease.fencing_token,
                attempt_count=step_record.attempt_count,
            )
            return ReassignmentWriteOutcome.CONFLICT
        persisted_ownership = (
            ReassignmentWorkspaceOwnership.PREEXISTING
            if existing is not None
            else request.ownership
        )
        preparation_outcome = {
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION: (
                ReassignmentMutationOutcome.CONFIRMED_EFFECT
            ),
            ReassignmentWorkspaceOwnership.PREEXISTING: (
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            ),
            ReassignmentWorkspaceOwnership.UNKNOWN: None,
        }[persisted_ownership]
        now = self._now()
        self._state.workspaces[target_key] = request.workspace_slug
        self._state.steps[
            (request.lease.operation_id, ReassignmentStepName.PREPARE_TARGET_WORKSPACE)
        ] = replace(
            step_record,
            step=step,
            probe_outcome=preparation_outcome,
            updated_at=now,
        )
        updated = replace(
            owned,
            target_workspace_slug=request.workspace_slug,
            target_workspace_ownership=persisted_ownership,
            operation=replace(
                owned.operation,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            ),
            updated_at=now,
        )
        self._state.operations[request.lease.operation_id] = updated
        if claim is not None:
            # Fake 与 SQLite 一致：mapping、prepare Step 和 claim release 作为同一提交
            # 快照变更出现，回滚时会一起恢复，避免测试掩盖真实跨实例竞态。
            persisted_claim = self._state.workspace_preparation_claims.get(target_key)
            if (
                persisted_claim is None
                or not persisted_claim.active
                or persisted_claim.claim != claim
            ):
                raise ReassignmentContractError("Fake mapping 提交时准备权已失效")
            self._state.workspace_preparation_claims[target_key] = replace(
                persisted_claim,
                active=False,
            )
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_MAPPING_RECORDED,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            operation_status=owned.operation.status,
            detail_code="workspace_mapping_recorded",
            fencing_token=request.lease.fencing_token,
            attempt_count=step_record.attempt_count,
            probe_outcome=preparation_outcome,
        )
        return updated

    def record_workspace_preparation_fact(
        self,
        request: ReassignmentWorkspacePreparationFactRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """保存已确认远端资源现场，但不写 mapping、不完成 Step、不释放 claim。"""

        self._require_writable()
        if not isinstance(request, ReassignmentWorkspacePreparationFactRequest):
            raise TypeError(
                "request 必须是 ReassignmentWorkspacePreparationFactRequest"
            )
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if not self._recovery_fencing_is_authorized(
            owned,
            lease=request.lease,
            recovery_authorized=request.recovery_authorized,
        ):
            return ReassignmentWriteOutcome.CONFLICT
        if owned.target_workspace_slug is not None and (
            owned.target_workspace_slug.casefold() != request.workspace_slug.casefold()
            or owned.target_workspace_ownership is not request.ownership
        ):
            return ReassignmentWriteOutcome.CONFLICT
        step_key = (
            request.lease.operation_id,
            ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        )
        step_record = self._state.steps.get(step_key)
        if step_record is None:
            raise ReassignmentContractError("Fake Operation 缺少 prepare Step")
        allowed_recovery_unknown = (
            request.recovery_authorized
            and step_record.step.state is ReassignmentStepState.OUTCOME_UNKNOWN
        )
        if (
            step_record.step.state is not ReassignmentStepState.MUTATION_STARTED
            and not allowed_recovery_unknown
        ) or not step_record.step.write_intent_recorded:
            raise ReassignmentContractError(
                "Fake 只有已记录 prepare 写意图时才能保存远端准备事实"
            )
        now = self._now()
        self._state.steps[step_key] = replace(
            step_record,
            step=replace(
                step_record.step,
                external_reference=request.workspace_slug,
                error_code=request.error_code,
            ),
            updated_at=now,
        )
        updated = replace(
            owned,
            target_workspace_slug=request.workspace_slug,
            target_workspace_ownership=request.ownership,
            operation=replace(
                owned.operation,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            ),
            error_code=request.error_code,
            updated_at=now,
        )
        self._state.operations[request.lease.operation_id] = updated
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.WORKSPACE_PREPARATION_FACT_RECORDED,
            step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            operation_status=owned.operation.status,
            detail_code=request.error_code,
            reference_digest=_text_digest(request.workspace_slug),
            fencing_token=request.lease.fencing_token,
            attempt_count=step_record.attempt_count,
            probe_outcome={
                ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION: (
                    ReassignmentMutationOutcome.CONFIRMED_EFFECT
                ),
                ReassignmentWorkspaceOwnership.PREEXISTING: (
                    ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
                ),
                ReassignmentWorkspaceOwnership.UNKNOWN: (
                    ReassignmentMutationOutcome.OUTCOME_UNKNOWN
                ),
            }[request.ownership],
        )
        return updated

    def begin_best_effort_pin(
        self,
        *,
        lease: ReassignmentLease,
    ) -> ReassignmentWriteOutcome:
        self._require_writable()
        owned = self._owned_operation(lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        attach = self._state.steps.get(
            (lease.operation_id, ReassignmentStepName.ATTACH_TARGET_DOCUMENT)
        )
        if attach is None or attach.step.state is not ReassignmentStepState.SUCCEEDED:
            raise ReassignmentContractError("Fake 目标挂载尚未确认，禁止 Pin")
        self._append_event(
            operation_id=lease.operation_id,
            event_type=ReassignmentEventType.BEST_EFFORT_PIN_ATTEMPTED,
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            operation_status=owned.operation.status,
            detail_code="pin_attempted",
            fencing_token=lease.fencing_token,
        )
        return ReassignmentWriteOutcome.APPLIED

    def complete_best_effort_pin(
        self,
        completion: ReassignmentBestEffortPinCompletion,
    ) -> ReassignmentWriteOutcome:
        self._require_writable()
        if not isinstance(completion, ReassignmentBestEffortPinCompletion):
            raise TypeError("completion 必须是 ReassignmentBestEffortPinCompletion")
        owned = self._owned_operation(completion.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        events = self._state.events.get(completion.lease.operation_id, ())
        attempted = sum(
            event.event_type is ReassignmentEventType.BEST_EFFORT_PIN_ATTEMPTED
            for event in events
        )
        completed = sum(
            event.event_type is ReassignmentEventType.BEST_EFFORT_PIN_COMPLETED
            for event in events
        )
        if attempted <= completed:
            raise ReassignmentContractError("Fake Pin 完成事实缺少对应尝试意图")
        self._append_event(
            operation_id=completion.lease.operation_id,
            event_type=ReassignmentEventType.BEST_EFFORT_PIN_COMPLETED,
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            operation_status=owned.operation.status,
            detail_code=completion.error_code or "pin_completed",
            fencing_token=completion.lease.fencing_token,
            probe_outcome=completion.mutation_outcome,
        )
        return ReassignmentWriteOutcome.APPLIED

    def _validate_forward_success_prerequisites(
        self,
        record: ReassignmentOperationRecord,
    ) -> None:
        """与 SQLite 一致地阻止关键远端步骤尚未确认时提交成功。"""

        remote_step_names = (
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )
        steps = {
            step_name: self.get_step(
                operation_id=record.operation.operation_id,
                step_name=step_name,
            )
            for step_name in remote_step_names
        }
        if any(item is None for item in steps.values()):
            raise ReassignmentContractError("Fake Operation 缺少必要远端 Step")
        if not record.operation.document.requires_remote_membership_change:
            if any(
                item.step.state is not ReassignmentStepState.PENDING
                for item in steps.values()
                if item is not None
            ):
                raise ReassignmentContractError(
                    "Fake 本地-only文档存在不应发生的远端步骤事实"
                )
            return
        prepare = steps[ReassignmentStepName.PREPARE_TARGET_WORKSPACE]
        attach = steps[ReassignmentStepName.ATTACH_TARGET_DOCUMENT]
        detach = steps[ReassignmentStepName.DETACH_SOURCE_DOCUMENT]
        if (
            record.target_workspace_slug is None
            or record.target_workspace_ownership is None
            or prepare is None
            or prepare.step.state is not ReassignmentStepState.SUCCEEDED
            or attach is None
            or attach.step.state is not ReassignmentStepState.SUCCEEDED
        ):
            raise ReassignmentContractError(
                "Fake 目标 workspace 与成员关系尚未确认"
            )
        if (
            record.source_workspace_slug is not None
            and (
                detach is None
                or detach.step.state is not ReassignmentStepState.SUCCEEDED
            )
        ):
            raise ReassignmentContractError("Fake 来源成员关系尚未确认移除")

    def _validate_no_side_effect_failure_prerequisites(
        self,
        record: ReassignmentOperationRecord,
    ) -> None:
        """与 SQLite 一致地禁止把已发生或未知副作用伪装成普通失败。"""

        if record.target_workspace_ownership in {
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            ReassignmentWorkspaceOwnership.UNKNOWN,
        }:
            raise ReassignmentContractError(
                "Fake 目标 workspace 存在待恢复创建事实"
            )
        for step_name in ReassignmentStepName:
            step_record = self.get_step(
                operation_id=record.operation.operation_id,
                step_name=step_name,
            )
            if step_record is None:
                raise ReassignmentContractError("Fake Operation 缺少固定 Step")
            state = step_record.step.state
            outcome = step_record.probe_outcome
            if state in {
                ReassignmentStepState.MUTATION_STARTED,
                ReassignmentStepState.OUTCOME_UNKNOWN,
            }:
                raise ReassignmentContractError("Fake 存在执行中或未知 Step")
            if outcome in {
                ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            }:
                raise ReassignmentContractError("Fake 存在已确认或未知副作用")
            if (
                state is ReassignmentStepState.KNOWN_FAILED
                and outcome is not ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            ):
                raise ReassignmentContractError("Fake 已知失败缺少无副作用事实")

    def finalize_no_side_effect_failure(
        self,
        request: ReassignmentNoSideEffectFailureRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """以严格的步骤事实核验模拟可安全释放文档保护的失败终态。"""

        self._require_writable()
        if not isinstance(request, ReassignmentNoSideEffectFailureRequest):
            raise TypeError("request 必须是 ReassignmentNoSideEffectFailureRequest")
        if (
            request.terminal_evidence.kind
            is not ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
        ):
            raise ReassignmentContractError("Fake 无副作用失败终态证据不匹配")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        self._validate_no_side_effect_failure_prerequisites(owned)
        operation = replace(
            transition_operation_status(
                owned.operation,
                ReassignmentOperationStatus.FAILED,
                terminal_evidence=request.terminal_evidence,
            ),
            current_step=request.current_step,
        )
        now = self._now()
        updated = replace(
            owned,
            operation=operation,
            error_code=request.error_code,
            error_summary=request.error_summary,
            updated_at=now,
            finished_at=now,
        )
        self._state.operations[request.lease.operation_id] = updated
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.NO_SIDE_EFFECT_FAILURE_FINALIZED,
            step_name=operation.current_step,
            operation_status=operation.status,
            detail_code=request.error_code,
            fencing_token=request.lease.fencing_token,
        )
        return updated

    def commit_local_architecture(
        self,
        request: ReassignmentLocalCommitRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        self._require_writable()
        if not isinstance(request, ReassignmentLocalCommitRequest):
            raise TypeError("request 必须是 ReassignmentLocalCommitRequest")
        owned = self._owned_operation(request.lease)
        if isinstance(owned, ReassignmentWriteOutcome):
            return owned
        if owned.operation.document != request.expected_document:
            raise ReassignmentContractError("Fake 本地 CAS 文档快照不一致")
        if (
            owned.operation.target_architecture_raw.canonical_json()
            != request.target_architecture_raw.canonical_json()
        ):
            raise ReassignmentContractError("Fake 本地 CAS 目标分类不一致")
        self._validate_forward_success_prerequisites(owned)
        target = _document_target_architecture_id(request.target_architecture_raw)
        step_record = self.get_step(
            operation_id=request.lease.operation_id,
            step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        if step_record is None:
            raise ReassignmentContractError("Fake Operation 缺少 commit Step")
        step = transition_step_state(
            step_record.step,
            ReassignmentStepState.SUCCEEDED,
        )
        known_failure_step = transition_step_state(
            step_record.step,
            ReassignmentStepState.KNOWN_FAILED,
        )
        old_key = (
            request.expected_document.file_name,
            request.expected_document.source_architecture_id,
        )
        if self._state.documents.get(old_key) != request.expected_document:
            self._state.steps[
                (
                    request.lease.operation_id,
                    ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                )
            ] = replace(
                step_record,
                step=known_failure_step,
                probe_outcome=ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                updated_at=self._now(),
            )
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.LOCAL_ARCHITECTURE_CONFLICT,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                operation_status=owned.operation.status,
                detail_code="local_cas_not_one_row",
                fencing_token=request.lease.fencing_token,
            )
            return ReassignmentWriteOutcome.CONFLICT
        target_key = (request.expected_document.file_name, target)
        if target_key in self._state.documents and target_key != old_key:
            self._state.steps[
                (
                    request.lease.operation_id,
                    ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                )
            ] = replace(
                step_record,
                step=known_failure_step,
                probe_outcome=ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                updated_at=self._now(),
            )
            self._append_event(
                operation_id=request.lease.operation_id,
                event_type=ReassignmentEventType.LOCAL_ARCHITECTURE_CONFLICT,
                step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                operation_status=owned.operation.status,
                detail_code="local_unique_conflict",
                fencing_token=request.lease.fencing_token,
            )
            return ReassignmentWriteOutcome.CONFLICT
        operation = replace(
            transition_operation_status(
                owned.operation,
                ReassignmentOperationStatus.SUCCEEDED,
                terminal_evidence=request.terminal_evidence,
            ),
            current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        )
        now = self._now()
        replacement = ReassignmentDocumentSnapshot(
            document_row_id=request.expected_document.document_row_id,
            file_name=request.expected_document.file_name,
            source_architecture_id=target,
            anything_doc_id=request.expected_document.anything_doc_id,
            doc_path=request.expected_document.doc_path,
            original_file_name=request.expected_document.original_file_name,
        )
        self._state.documents.pop(old_key)
        self._state.documents[target_key] = replacement
        self._state.steps[
            (request.lease.operation_id, ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        ] = replace(step_record, step=step, updated_at=now)
        updated = replace(
            owned,
            operation=operation,
            updated_at=now,
            finished_at=now,
        )
        self._state.operations[request.lease.operation_id] = updated
        self._append_event(
            operation_id=request.lease.operation_id,
            event_type=ReassignmentEventType.LOCAL_ARCHITECTURE_COMMITTED,
            step_name=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            operation_status=operation.status,
            detail_code="local_cas_committed",
            fencing_token=request.lease.fencing_token,
        )
        return updated


@dataclass(frozen=True)
class _ExpectedKnowledgeCall:
    """一条必须严格消费的远端调用期望。"""

    method: str
    request: object | None
    result: object | None
    raised_error: BaseException | None
    allow_duplicate: bool = False


class FakeReassignmentKnowledgePort(ReassignmentKnowledgePort):
    """声明式外部调用 Fake；没有期望的调用永远是测试失败。"""

    def __init__(
        self,
        *,
        transaction_active: Callable[[], bool] | None = None,
    ) -> None:
        if transaction_active is not None and not callable(transaction_active):
            raise TypeError("transaction_active 必须是可调用对象或 None")
        self._transaction_active = transaction_active or (lambda: False)
        self._expected: list[_ExpectedKnowledgeCall] = []
        self._calls: list[tuple[str, object]] = []
        # 这里只保存脱敏的测试请求身份，不记录任何模拟供应商正文。结果未知或异常同样
        # 可能已经产生外部副作用，必须阻止测试在没有显式授权时盲目重放。
        self._applied_mutations: set[tuple[str, ...]] = set()

    @property
    def calls(self) -> tuple[tuple[str, object], ...]:
        """返回已经消费的调用顺序，供应用层测试断言。"""

        return tuple(self._calls)

    def expect_prepare_target_workspace(
        self,
        result: ReassignmentWorkspacePreparationResult,
        *,
        request: ReassignmentWorkspacePreparationRequest | None = None,
        raised_error: BaseException | None = None,
        allow_duplicate: bool = False,
    ) -> None:
        self._expect(
            "prepare_target_workspace",
            request,
            result,
            raised_error,
            allow_duplicate=allow_duplicate,
        )

    def expect_probe_target_workspace(
        self,
        result: ReassignmentWorkspaceProbeResult | None = None,
        *,
        request: ReassignmentWorkspacePreparationRequest | None = None,
        raised_error: BaseException | None = None,
    ) -> None:
        """声明一次纯只读 workspace 查回，不登记为外部副作用。"""

        self._expect(
            "probe_target_workspace",
            request,
            result,
            raised_error,
        )

    def expect_probe_workspace_reference(
        self,
        result: ReassignmentWorkspaceProbeResult | None = None,
        *,
        request: ReassignmentWorkspaceReferenceProbeRequest | None = None,
        raised_error: BaseException | None = None,
    ) -> None:
        """声明一次按既有 mapping slug 执行的纯只读查回。"""

        self._expect(
            "probe_workspace_reference",
            request,
            result,
            raised_error,
        )

    def expect_probe_document_membership(
        self,
        result: ReassignmentMembershipProbeResult,
        *,
        request: ReassignmentMembershipProbeRequest | None = None,
        raised_error: BaseException | None = None,
    ) -> None:
        self._expect(
            "probe_document_membership",
            request,
            result,
            raised_error,
        )

    def expect_detach_document(
        self,
        result: ReassignmentDocumentMutationResult,
        *,
        request: ReassignmentDocumentMutationRequest | None = None,
        raised_error: BaseException | None = None,
        allow_duplicate: bool = False,
    ) -> None:
        self._expect(
            "detach_document",
            request,
            result,
            raised_error,
            allow_duplicate=allow_duplicate,
        )

    def expect_attach_document(
        self,
        result: ReassignmentDocumentMutationResult,
        *,
        request: ReassignmentDocumentMutationRequest | None = None,
        raised_error: BaseException | None = None,
        allow_duplicate: bool = False,
    ) -> None:
        self._expect(
            "attach_document",
            request,
            result,
            raised_error,
            allow_duplicate=allow_duplicate,
        )

    def expect_pin_document_best_effort(
        self,
        result: ReassignmentDocumentMutationResult,
        *,
        request: ReassignmentDocumentMutationRequest | None = None,
        raised_error: BaseException | None = None,
        allow_duplicate: bool = False,
    ) -> None:
        self._expect(
            "pin_document_best_effort",
            request,
            result,
            raised_error,
            allow_duplicate=allow_duplicate,
        )

    def _expect(
        self,
        method: str,
        request: object | None,
        result: object | None,
        raised_error: BaseException | None,
        *,
        allow_duplicate: bool = False,
    ) -> None:
        if raised_error is not None and not isinstance(raised_error, BaseException):
            raise TypeError("raised_error 必须是 BaseException 或 None")
        if not isinstance(allow_duplicate, bool):
            raise TypeError("allow_duplicate 必须是 bool")
        self._expected.append(
            _ExpectedKnowledgeCall(
                method=method,
                request=request,
                result=result,
                raised_error=raised_error,
                allow_duplicate=allow_duplicate,
            )
        )

    def assert_expectations_consumed(self) -> None:
        """确保测试没有遗漏本应发生的外部动作。"""

        if self._expected:
            methods = ", ".join(expected.method for expected in self._expected)
            raise AssertionError(f"存在未消费的 Fake Knowledge 期望: {methods}")

    @staticmethod
    def _mutation_identity(
        method: str,
        request: object,
    ) -> tuple[str, ...]:
        """为可能产生外部副作用的请求生成稳定身份，覆盖 workspace 创建与文档写。"""

        if isinstance(request, ReassignmentDocumentMutationRequest):
            return (
                method,
                request.workspace.slug,
                request.document.doc_path,
                request.idempotency_key,
                request.step_name.value,
            )
        if isinstance(request, ReassignmentWorkspacePreparationRequest):
            return (
                method,
                request.operation_id,
                request.target_architecture_raw.canonical_json(),
                request.desired_workspace_name,
                request.idempotency_key,
            )
        raise AssertionError("可能产生副作用的 Fake Knowledge 请求类型不正确")

    @staticmethod
    def _may_have_external_effect(expected: _ExpectedKnowledgeCall) -> bool:
        """未知结果、异常和明确成功均视为可能已写入，只有明确失败允许后续受控重试。"""

        if expected.raised_error is not None:
            return True
        result = expected.result
        if isinstance(result, ReassignmentDocumentMutationResult):
            return result.outcome is not ReassignmentKnowledgeOutcome.KNOWN_FAILURE
        if isinstance(result, ReassignmentWorkspacePreparationResult):
            return result.outcome is not ReassignmentKnowledgeOutcome.KNOWN_FAILURE
        # 结果类型本身错误时同样不能放宽重放保护；随后调用处会报出更具体的类型错误。
        return True

    def _consume(
        self,
        method: str,
        request: object,
        *,
        expected_type: type,
        mutation: bool = False,
    ) -> object:
        if not isinstance(request, expected_type):
            raise TypeError(f"{method} 请求类型不正确")
        if self._transaction_active():
            raise AssertionError("Fake Knowledge 禁止在 Reassignment UnitOfWork 活动时调用")
        if not self._expected:
            raise AssertionError(f"Fake Knowledge 收到未声明调用: {method}")
        expected = self._expected.pop(0)
        if expected.method != method:
            raise AssertionError(
                "Fake Knowledge 调用顺序错误: "
                f"expected={expected.method} actual={method}"
            )
        if expected.request is not None and expected.request != request:
            raise AssertionError(f"Fake Knowledge 请求不匹配: {method}")
        if mutation:
            identity = self._mutation_identity(method, request)
            if identity in self._applied_mutations and not expected.allow_duplicate:
                raise AssertionError("Fake Knowledge 拒绝未授权的重复外部副作用")
            if self._may_have_external_effect(expected):
                self._applied_mutations.add(identity)
        self._calls.append((method, request))
        if expected.raised_error is not None:
            raise expected.raised_error
        return expected.result

    def prepare_target_workspace(
        self,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> ReassignmentWorkspacePreparationResult:
        result = self._consume(
            "prepare_target_workspace",
            request,
            expected_type=ReassignmentWorkspacePreparationRequest,
            mutation=True,
        )
        if not isinstance(result, ReassignmentWorkspacePreparationResult):
            raise AssertionError("Fake prepare 结果类型不正确")
        return result

    def probe_target_workspace(
        self,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> ReassignmentWorkspaceProbeResult:
        result = self._consume(
            "probe_target_workspace",
            request,
            expected_type=ReassignmentWorkspacePreparationRequest,
        )
        if not isinstance(result, ReassignmentWorkspaceProbeResult):
            raise AssertionError("Fake workspace probe 结果类型不正确")
        return result

    def probe_workspace_reference(
        self,
        request: ReassignmentWorkspaceReferenceProbeRequest,
    ) -> ReassignmentWorkspaceProbeResult:
        result = self._consume(
            "probe_workspace_reference",
            request,
            expected_type=ReassignmentWorkspaceReferenceProbeRequest,
        )
        if not isinstance(result, ReassignmentWorkspaceProbeResult):
            raise AssertionError("Fake workspace reference probe 结果类型不正确")
        return result

    def probe_document_membership(
        self,
        request: ReassignmentMembershipProbeRequest,
    ) -> ReassignmentMembershipProbeResult:
        result = self._consume(
            "probe_document_membership",
            request,
            expected_type=ReassignmentMembershipProbeRequest,
        )
        if not isinstance(result, ReassignmentMembershipProbeResult):
            raise AssertionError("Fake probe 结果类型不正确")
        return result

    def detach_document(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        if request.step_name not in {
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
        }:
            raise AssertionError("detach_document 使用了错误的 Saga Step")
        result = self._consume(
            "detach_document",
            request,
            expected_type=ReassignmentDocumentMutationRequest,
            mutation=True,
        )
        if not isinstance(result, ReassignmentDocumentMutationResult):
            raise AssertionError("Fake detach 结果类型不正确")
        return result

    def attach_document(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        if request.step_name not in {
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
        }:
            raise AssertionError("attach_document 使用了错误的 Saga Step")
        result = self._consume(
            "attach_document",
            request,
            expected_type=ReassignmentDocumentMutationRequest,
            mutation=True,
        )
        if not isinstance(result, ReassignmentDocumentMutationResult):
            raise AssertionError("Fake attach 结果类型不正确")
        return result

    def pin_document_best_effort(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        if request.step_name is not ReassignmentStepName.ATTACH_TARGET_DOCUMENT:
            raise AssertionError("pin_document_best_effort 只能跟随目标加入 Step")
        result = self._consume(
            "pin_document_best_effort",
            request,
            expected_type=ReassignmentDocumentMutationRequest,
            mutation=True,
        )
        if not isinstance(result, ReassignmentDocumentMutationResult):
            raise AssertionError("Fake pin 结果类型不正确")
        return result


class FakeReassignmentKnowledgePortFactory(ReassignmentKnowledgePortFactory):
    """为每个 Operation 创建独立严格 Fake，并保留已创建实例供测试断言。

    默认构造全新 Fake；需要共享事务活动探针或预置期望时，测试可注入 ``builder``。
    Factory 本身不复用端口，从测试层阻止请求级 deadline/调用期望跨 Operation 串扰。
    """

    def __init__(
        self,
        builder: Callable[[], FakeReassignmentKnowledgePort] | None = None,
    ) -> None:
        if builder is not None and not callable(builder):
            raise TypeError("builder 必须是可调用对象或 None")
        self._builder = builder or FakeReassignmentKnowledgePort
        self._ports: list[FakeReassignmentKnowledgePort] = []

    @property
    def ports(self) -> tuple[FakeReassignmentKnowledgePort, ...]:
        """返回已创建端口的只读快照。"""

        return tuple(self._ports)

    def create(self, *, elapsed_seconds: float = 0.0) -> FakeReassignmentKnowledgePort:
        """创建并记录一个不共享状态的严格 Knowledge Port。"""

        if (
            isinstance(elapsed_seconds, bool)
            or not isinstance(elapsed_seconds, (int, float))
            or not math.isfinite(float(elapsed_seconds))
            or float(elapsed_seconds) < 0.0
        ):
            raise ValueError("elapsed_seconds 必须是有限非负秒数")

        port = self._builder()
        if not isinstance(port, FakeReassignmentKnowledgePort):
            raise TypeError("builder 必须返回 FakeReassignmentKnowledgePort")
        if any(existing is port for existing in self._ports):
            raise AssertionError("Fake Knowledge Factory 禁止跨 Operation 复用端口")
        self._ports.append(port)
        return port


__all__ = [
    "FakeReassignmentKnowledgePort",
    "FakeReassignmentKnowledgePortFactory",
    "FakeReassignmentRepository",
    "FakeReassignmentUnitOfWork",
    "PostCommitFailureReassignmentRepository",
]
