"""报告任务级 Artifact 与 AnythingLLM 临时资源的可恢复收口服务。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import logging
import threading
import time
from uuid import uuid4

from app.modules.report.domain import (
    ReportCleanupError,
    ReportPortContractError,
    ReportResourceConcurrencyError,
    ReportResourceNotReadyError,
)
from app.modules.report.ports import (
    AppendReportLifecycleEvents,
    CleanupReportRag,
    ReportArtifactCleanupResult,
    ReportArtifactPort,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportCleanupPartState,
    ReportInteractionAuditPort,
    ReportRagCleanupRef,
    ReportRagLifecycleEvent,
    ReportRagPort,
    ReportResourceCleanupOutcome,
    ReportResourceCleanupResult,
    ReportResourceRecord,
    ReportResourceState,
    ReportResourceStorePort,
    ReportResourceSweepResult,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId


logger = logging.getLogger(__name__)

_DELETION_OPERATIONS = frozenset(
    {"conversation_delete", "context_delete", "global_document_delete"}
)


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("resource recovery clock 必须返回数字")
    normalized = float(value)
    if (
        normalized != normalized
        or normalized in (float("inf"), float("-inf"))
        or normalized < 0.0
    ):
        raise ValueError("resource recovery clock 必须返回非负有限数字")
    return normalized


def _new_attempt_token() -> str:
    return uuid4().hex


@dataclass
class _ArtifactLockEntry:
    """同一进程内按 TaskId 复用并自动回收的 Artifact 副作用锁。"""

    lock: threading.RLock
    user_count: int = 0


class ReportResourceRecoveryService:
    """先写恢复事实，再执行外部/文件副作用，并以幂等审计收敛。

    该服务不决定业务成功或失败。它只在 execution 已有确定终态后，向 Store 请求权威
    Artifact 所有权，再依次处理外部 RAG 资源、清理事件审计和本地未保留对象。任何失败
    都保存在任务级记录中，不反向覆盖业务终态。
    """

    def __init__(
        self,
        *,
        store: ReportResourceStorePort,
        artifacts: ReportArtifactPort,
        rag: ReportRagPort,
        audit: ReportInteractionAuditPort,
        clock: Callable[[], float] = time.time,
        external_attempt_timeout_seconds: float = 300.0,
        sweep_retry_delay_seconds: float = 30.0,
        attempt_token_factory: Callable[[], str] = _new_attempt_token,
    ) -> None:
        if not isinstance(store, ReportResourceStorePort):
            raise TypeError("store 必须实现 ReportResourceStorePort")
        if not isinstance(artifacts, ReportArtifactPort):
            raise TypeError("artifacts 必须实现 ReportArtifactPort")
        if not isinstance(rag, ReportRagPort):
            raise TypeError("rag 必须实现 ReportRagPort")
        if not isinstance(audit, ReportInteractionAuditPort):
            raise TypeError("audit 必须实现 ReportInteractionAuditPort")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        if not callable(attempt_token_factory):
            raise TypeError("attempt_token_factory 必须可调用")
        if (
            isinstance(external_attempt_timeout_seconds, bool)
            or not isinstance(external_attempt_timeout_seconds, (int, float))
        ):
            raise TypeError("external_attempt_timeout_seconds 必须是数字")
        timeout = float(external_attempt_timeout_seconds)
        if (
            timeout != timeout
            or timeout in (float("inf"), float("-inf"))
            or timeout <= 0.0
        ):
            raise ValueError("external_attempt_timeout_seconds 必须是正有限数字")
        if (
            isinstance(sweep_retry_delay_seconds, bool)
            or not isinstance(sweep_retry_delay_seconds, (int, float))
        ):
            raise TypeError("sweep_retry_delay_seconds 必须是数字")
        sweep_retry_delay = float(sweep_retry_delay_seconds)
        if (
            sweep_retry_delay != sweep_retry_delay
            or sweep_retry_delay in (float("inf"), float("-inf"))
            or sweep_retry_delay <= 0.0
        ):
            raise ValueError("sweep_retry_delay_seconds 必须是正有限数字")
        self._store = store
        self._artifacts = artifacts
        self._rag = rag
        self._audit = audit
        self._clock = clock
        self._external_attempt_timeout_seconds = timeout
        self._sweep_retry_delay_seconds = sweep_retry_delay
        self._attempt_token_factory = attempt_token_factory
        self._artifact_locks_guard = threading.RLock()
        self._artifact_locks: dict[TaskId, _ArtifactLockEntry] = {}

    def register(
        self,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        scope: ReportArtifactScope,
    ) -> None:
        """命名空间分配后、任何业务文件/对象 I/O 前立即登记；重复调用幂等。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(scope, ReportArtifactScope) or scope.task_id != task_id:
            raise ValueError("scope 不属于当前 task_id")
        created = self._store.create(
            ReportResourceRecord(
                task_id=task_id,
                business_ref=business_ref,
                scope=scope,
            )
        )
        if (
            created.task_id != task_id
            or created.business_ref != business_ref
            or created.scope != scope
        ):
            raise ReportPortContractError("资源 Store 幂等登记返回了其他任务记录")

    def track_rag_cleanup(
        self,
        task_id: TaskId,
        cleanup_ref: ReportRagCleanupRef,
    ) -> None:
        if not isinstance(cleanup_ref, ReportRagCleanupRef):
            raise TypeError("cleanup_ref 必须是 ReportRagCleanupRef")
        record = self._required(task_id)
        if record.cleanup_ref is not None:
            if record.cleanup_ref != cleanup_ref:
                raise ReportCleanupError("同一任务出现不同 RAG cleanup ref")
            return
        self._save(
            replace(
                record,
                cleanup_ref=cleanup_ref,
                external_state=ReportCleanupPartState.PENDING,
            )
        )

    def track_audit(self, receipt: ReportAuditReceipt) -> None:
        if not isinstance(receipt, ReportAuditReceipt):
            raise TypeError("receipt 必须是 ReportAuditReceipt")
        record = self._required(receipt.task_id)
        if record.audit_receipt is not None:
            if record.audit_receipt != receipt:
                raise ReportCleanupError("同一任务出现不同 Audit Receipt")
            return
        self._save(replace(record, audit_receipt=receipt))

    def track_final_artifact(self, artifact: ReportArtifactRef) -> None:
        if not isinstance(artifact, ReportArtifactRef):
            raise TypeError("artifact 必须是 ReportArtifactRef")
        record = self._required(artifact.task_id)
        if record.final_artifact is not None:
            if record.final_artifact != artifact:
                raise ReportCleanupError("同一任务出现不同最终 Artifact")
            return
        self._save(replace(record, final_artifact=artifact))

    def cleanup(self, task_id: TaskId) -> ReportResourceCleanupResult:
        """终态提交后准备并执行一次资源收口。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        record = self._store.prepare_cleanup(task_id)
        return self._resume(record)

    def recover(self, task_id: TaskId) -> ReportResourceCleanupResult:
        """恢复一份已持久化记录；不会越过 quarantine 自动重试未知副作用。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        record = self._store.get(task_id)
        if record is None:
            return ReportResourceCleanupResult(
                ReportResourceCleanupOutcome.NOT_FOUND
            )
        if record.state is ReportResourceState.CLEANED:
            return ReportResourceCleanupResult(ReportResourceCleanupOutcome.CLEANED)
        if record.state is ReportResourceState.QUARANTINED:
            return self._result(record, ReportResourceCleanupOutcome.QUARANTINED)
        if record.state is ReportResourceState.TRACKING:
            try:
                record = self._store.prepare_cleanup(task_id)
            except ReportResourceNotReadyError:
                return ReportResourceCleanupResult(
                    ReportResourceCleanupOutcome.NOT_READY,
                    pending_external=record.cleanup_ref is not None,
                )
        return self._resume(record)

    def sweep(self, *, limit: int) -> ReportResourceSweepResult:
        """有界扫描可恢复记录；单个坏任务不会阻断同一批次的其他任务。"""

        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TypeError("limit 必须是 int")
        if limit < 1 or limit > 1000:
            raise ValueError("limit 必须在 1~1000 之间")
        task_ids = tuple(self._store.list_recoverable(limit=limit))
        if len(task_ids) > limit or any(not isinstance(item, TaskId) for item in task_ids):
            raise ReportPortContractError("资源 Store 返回了无效的有界扫描结果")
        if len(set(task_ids)) != len(task_ids):
            raise ReportPortContractError("资源 Store 返回了重复 task_id")

        counts = {
            ReportResourceCleanupOutcome.CLEANED: 0,
            ReportResourceCleanupOutcome.PENDING: 0,
            ReportResourceCleanupOutcome.QUARANTINED: 0,
            ReportResourceCleanupOutcome.NOT_READY: 0,
            ReportResourceCleanupOutcome.NOT_FOUND: 0,
        }
        failed: list[TaskId] = []
        deferred: list[tuple[TaskId, str]] = []
        for task_id in task_ids:
            try:
                result = self.recover(task_id)
                if not isinstance(result, ReportResourceCleanupResult):
                    raise ReportPortContractError("资源恢复返回类型错误")
                counts[result.outcome] += 1
                if result.outcome in {
                    ReportResourceCleanupOutcome.PENDING,
                    ReportResourceCleanupOutcome.NOT_READY,
                }:
                    deferred.append((task_id, f"outcome:{result.outcome.value}"))
            except Exception as exc:
                failed.append(task_id)
                deferred.append(
                    (
                        task_id,
                        f"exception:{type(exc).__module__}.{type(exc).__qualname__}"[
                            :256
                        ],
                    )
                )
                logger.exception(
                    "报告资源有界恢复失败，继续处理同批其他任务: task_id=%s",
                    task_id,
                )

        self._defer_sweep_items(deferred)

        summary = ReportResourceSweepResult(
            requested_limit=limit,
            scanned_count=len(task_ids),
            cleaned_count=counts[ReportResourceCleanupOutcome.CLEANED],
            pending_count=counts[ReportResourceCleanupOutcome.PENDING],
            quarantined_count=counts[ReportResourceCleanupOutcome.QUARANTINED],
            not_ready_count=counts[ReportResourceCleanupOutcome.NOT_READY],
            missing_count=counts[ReportResourceCleanupOutcome.NOT_FOUND],
            failed_task_ids=tuple(failed),
        )
        logger.log(
            logging.ERROR if failed else logging.DEBUG,
            "报告资源有界恢复扫描完成: requested_limit=%d scanned=%d cleaned=%d "
            "pending=%d quarantined=%d not_ready=%d missing=%d failed=%d",
            limit,
            summary.scanned_count,
            summary.cleaned_count,
            summary.pending_count,
            summary.quarantined_count,
            summary.not_ready_count,
            summary.missing_count,
            len(summary.failed_task_ids),
        )
        return summary

    def _defer_sweep_items(
        self,
        items: list[tuple[TaskId, str]],
    ) -> None:
        """把 pending/坏记录移出下一扫描首页；列表大小受当前 sweep limit 严格约束。"""

        if not items:
            return
        retry_epoch = _clock_value(self._clock) + self._sweep_retry_delay_seconds
        retry_at = datetime.fromtimestamp(retry_epoch, timezone.utc).isoformat()
        for task_id, reason in items:
            try:
                deferred = self._store.defer_recovery(
                    task_id,
                    retry_at=retry_at,
                    reason=reason,
                )
                if not isinstance(deferred, bool):
                    raise ReportPortContractError(
                        "资源 Store defer_recovery 必须返回 bool"
                    )
                if not deferred:
                    logger.info(
                        "资源记录已收敛或被其他恢复者推进，跳过扫描冷却: task_id=%s",
                        task_id,
                    )
            except Exception:
                # 恢复结果本身仍按原分类返回；调度元数据失败单独告警，不能把已经明确的
                # cleanup 结果伪装成另一个业务状态。
                logger.exception(
                    "报告资源恢复冷却写入失败，后续扫描可能再次遇到该记录: task_id=%s",
                    task_id,
                )

    def quarantine(self, task_id: TaskId, *, stage: str, reason: str) -> None:
        """显式冻结不能安全自动清理的现场。"""

        record = self._required(task_id)
        self._quarantine_record(record, stage=stage, reason=reason)

    def _resume(self, record: ReportResourceRecord) -> ReportResourceCleanupResult:
        if record.state is ReportResourceState.QUARANTINED:
            return self._result(record, ReportResourceCleanupOutcome.QUARANTINED)
        if record.state is ReportResourceState.CLEANED:
            return ReportResourceCleanupResult(ReportResourceCleanupOutcome.CLEANED)
        if record.external_attempt_open:
            heartbeat_at = record.external_attempt_heartbeat_at
            if heartbeat_at is None:  # pragma: no cover - 领域对象已防御该损坏状态。
                raise ReportPortContractError("外部清理占用缺少心跳时间")
            now = _clock_value(self._clock)
            if now < heartbeat_at + self._external_attempt_timeout_seconds:
                # 另一恢复执行者仍可能正在事务外调用供应商。期限内只能观察为 pending，
                # 不能把正常并发误判成进程崩溃，更不能发起第二次删除。
                logger.info(
                    "报告外部资源清理仍由其他执行者占用: task_id=%s "
                    "attempt_count=%d heartbeat_at_epoch=%.3f",
                    record.task_id,
                    record.attempt_count,
                    heartbeat_at,
                )
                return self._result(
                    record,
                    ReportResourceCleanupOutcome.PENDING,
                )
            # 清理命令只允许调用幂等 DELETE，且每个结果事件都会在下一步之前落库。
            # 过期租约因此可以安全关闭：已记录步骤先追加审计，最后一个“删完但未落库”
            # 的窄窗口由下一次 DELETE 的 404 幂等收敛，不再永久隔离整项任务。
            observed_version = record.version
            try:
                record = self._close_interrupted_attempt(
                    record,
                    stage="rag_cleanup_interrupted",
                    message="外部清理心跳过期，按幂等删除协议恢复",
                )
            except ReportResourceConcurrencyError:
                latest = self._required(record.task_id)
                if latest.version == observed_version:
                    # Adapter 报告 CAS 冲突却没有出现新版本，说明端口违反内部契约；
                    # 此时不能把异常伪装成正常竞态。
                    raise ReportPortContractError(
                        "资源 Store 报告并发冲突，但版本未发生变化"
                    )
                # 恢复者与旧执行者可能同时命中到期边界。CAS 失败后以最新事实为准，
                # 不能把正常心跳竞态升级为批次失败或覆盖刚写入的检查点。
                logger.info(
                    "报告外部清理租约已被并发推进，放弃本次过期接管: task_id=%s",
                    record.task_id,
                )
                if latest.state is ReportResourceState.CLEANED:
                    return ReportResourceCleanupResult(
                        ReportResourceCleanupOutcome.CLEANED
                    )
                if latest.state is ReportResourceState.QUARANTINED:
                    return self._result(
                        latest,
                        ReportResourceCleanupOutcome.QUARANTINED,
                    )
                return self._result(
                    latest,
                    ReportResourceCleanupOutcome.PENDING,
                )

        if record.pending_events:
            appended = self._append_pending_events(record)
            if appended is None:
                latest = self._required(record.task_id)
                return self._result(latest, ReportResourceCleanupOutcome.PENDING)
            record = appended

        if record.external_state in {
            ReportCleanupPartState.PENDING,
            ReportCleanupPartState.FAILED,
        }:
            record = self._run_external_cleanup(record)
            if record.external_attempt_open:
                # 当前执行者已经失去 token，或持久层暂时不可用；不得在另一清理租约仍
                # 打开时继续推进本地 Artifact。
                return self._result(record, ReportResourceCleanupOutcome.PENDING)
            if record.state is ReportResourceState.CLEANED:
                return ReportResourceCleanupResult(
                    ReportResourceCleanupOutcome.CLEANED
                )
            if record.state in {
                ReportResourceState.AUDIT_PENDING,
                ReportResourceState.QUARANTINED,
            }:
                outcome = (
                    ReportResourceCleanupOutcome.QUARANTINED
                    if record.state is ReportResourceState.QUARANTINED
                    else ReportResourceCleanupOutcome.PENDING
                )
                return self._result(record, outcome)

        if record.artifact_state in {
            ReportCleanupPartState.PENDING,
            ReportCleanupPartState.FAILED,
        }:
            record = self._run_artifact_cleanup_exclusive(record)
            if record.state is ReportResourceState.CLEANED:
                return ReportResourceCleanupResult(
                    ReportResourceCleanupOutcome.CLEANED
                )
            if record.state is ReportResourceState.QUARANTINED:
                return self._result(
                    record,
                    ReportResourceCleanupOutcome.QUARANTINED,
                )

        complete = (
            record.external_state
            in {
                ReportCleanupPartState.NOT_REQUIRED,
                ReportCleanupPartState.SUCCEEDED,
            }
            and record.artifact_state is ReportCleanupPartState.SUCCEEDED
        )
        if complete:
            record = self._save(
                replace(
                    record,
                    state=ReportResourceState.CLEANED,
                    pending_artifacts=(),
                    last_error_stage="",
                    last_error_message="",
                )
            )
            logger.info("报告任务资源清理已收敛: task_id=%s", record.task_id)
            return ReportResourceCleanupResult(ReportResourceCleanupOutcome.CLEANED)

        if record.state is not ReportResourceState.CLEANUP_PENDING:
            record = self._save(
                replace(record, state=ReportResourceState.CLEANUP_PENDING)
            )
        return self._result(record, ReportResourceCleanupOutcome.PENDING)

    def _run_external_cleanup(
        self,
        record: ReportResourceRecord,
    ) -> ReportResourceRecord:
        if record.cleanup_ref is None:
            return self._save(
                replace(
                    record,
                    external_state=ReportCleanupPartState.NOT_REQUIRED,
                )
            )
        if record.audit_receipt is None:
            return self._quarantine_record(
                record,
                stage="cleanup_audit_missing",
                reason="外部资源存在但缺少已提交审计凭据",
            )

        raw_token = self._attempt_token_factory()
        if not isinstance(raw_token, str):
            raise TypeError("attempt_token_factory 必须返回 str")
        attempt_token = raw_token.strip()
        if not attempt_token or len(attempt_token) > 128:
            raise ValueError("外部清理 attempt token 长度必须为 1~128")
        started_at = _clock_value(self._clock)
        opened = self._save(
            replace(
                record,
                state=ReportResourceState.CLEANUP_PENDING,
                external_attempt_open=True,
                external_attempt_token=attempt_token,
                external_attempt_started_at=started_at,
                external_attempt_heartbeat_at=started_at,
                attempt_count=record.attempt_count + 1,
                last_error_stage="",
                last_error_message="",
            )
        )
        current = opened

        def heartbeat() -> None:
            nonlocal current
            self._require_attempt_owner(current, attempt_token)
            now = max(
                _clock_value(self._clock),
                current.external_attempt_heartbeat_at or started_at,
            )
            current = self._save(
                replace(current, external_attempt_heartbeat_at=now)
            )

        def checkpoint(event: ReportRagLifecycleEvent) -> None:
            nonlocal current
            if not isinstance(event, ReportRagLifecycleEvent):
                raise ReportPortContractError("RAG cleanup 检查点事件类型错误")
            self._require_attempt_owner(current, attempt_token)
            expected_sequence = current.next_sequence_no
            if expected_sequence is not None and event.sequence_no != expected_sequence:
                raise ReportPortContractError("RAG cleanup 检查点序号不连续")
            heartbeat_at = max(
                _clock_value(self._clock),
                current.external_attempt_heartbeat_at or started_at,
            )
            operation_attempts = dict(current.operation_attempts)
            expected_attempt = operation_attempts.get(event.operation, 0) + 1
            if event.attempt_no != expected_attempt:
                raise ReportPortContractError(
                    "RAG cleanup 检查点 attempt_no 未基于持久化历史连续递增"
                )
            operation_attempts[event.operation] = event.attempt_no
            current = self._save(
                replace(
                    current,
                    pending_events=current.pending_events + (event,),
                    # 尝试尚未结束时不能依据部分成功事件宣称整个外部集合已清理。
                    pending_events_succeeded=False,
                    next_sequence_no=event.sequence_no + 1,
                    operation_attempts=tuple(sorted(operation_attempts.items())),
                    external_attempt_heartbeat_at=heartbeat_at,
                )
            )
        try:
            events = tuple(
                self._rag.cleanup(
                    CleanupReportRag(
                        opened.cleanup_ref,
                        sequence_start=opened.next_sequence_no,
                        attempt_baselines=opened.operation_attempts,
                        event_checkpoint=checkpoint,
                        heartbeat=heartbeat,
                    )
                )
            )
            if not events or any(
                not isinstance(item, ReportRagLifecycleEvent) for item in events
            ):
                raise ReportPortContractError("RAG cleanup 必须返回生命周期事件")
            deletion_events = tuple(
                item for item in events if item.operation in _DELETION_OPERATIONS
            )
            if not deletion_events:
                raise ReportPortContractError("RAG cleanup 缺少资源删除事件")
            if tuple(current.pending_events) != events:
                raise ReportPortContractError(
                    "RAG cleanup 返回事件与逐步持久化检查点不一致"
                )
            external_succeeded = all(item.success for item in deletion_events)
            next_sequence = max(item.sequence_no for item in events) + 1
            pending = self._save(
                replace(
                    current,
                    state=ReportResourceState.AUDIT_PENDING,
                    external_attempt_open=False,
                    external_attempt_token="",
                    external_attempt_started_at=None,
                    external_attempt_heartbeat_at=None,
                    pending_events_succeeded=external_succeeded,
                    next_sequence_no=next_sequence,
                )
            )
        except Exception as exc:
            logger.exception(
                "报告外部资源清理被中断，将基于已持久化步骤幂等恢复: task_id=%s",
                record.task_id,
            )
            latest = self._required(record.task_id)
            if (
                not latest.external_attempt_open
                or latest.external_attempt_token != attempt_token
            ):
                # CAS/fencing 已由另一恢复执行者推进，旧执行者不得覆盖新事实。
                return latest
            return self._close_interrupted_attempt(
                latest,
                stage="rag_cleanup_interrupted",
                message=f"{type(exc).__name__}: 外部清理将在下一轮幂等恢复",
            )

        appended = self._append_pending_events(pending)
        return pending if appended is None else appended

    def _close_interrupted_attempt(
        self,
        record: ReportResourceRecord,
        *,
        stage: str,
        message: str,
    ) -> ReportResourceRecord:
        """关闭过期/异常退出租约，并保留已经逐项落库的事件。"""

        if not record.external_attempt_open:
            return record
        has_events = bool(record.pending_events)
        closed = self._save(
            replace(
                record,
                state=(
                    ReportResourceState.AUDIT_PENDING
                    if has_events
                    else ReportResourceState.CLEANUP_PENDING
                ),
                external_state=ReportCleanupPartState.FAILED,
                external_attempt_open=False,
                external_attempt_token="",
                external_attempt_started_at=None,
                external_attempt_heartbeat_at=None,
                pending_events_succeeded=False if has_events else None,
                last_error_stage=stage,
                last_error_message=message,
            )
        )
        logger.warning(
            "报告外部清理租约已关闭并等待幂等恢复: task_id=%s "
            "persisted_event_count=%d",
            record.task_id,
            len(record.pending_events),
        )
        return closed

    @staticmethod
    def _require_attempt_owner(
        record: ReportResourceRecord,
        attempt_token: str,
    ) -> None:
        if (
            not record.external_attempt_open
            or record.external_attempt_token != attempt_token
        ):
            raise ReportCleanupError("外部清理租约已失效")

    def _append_pending_events(
        self,
        record: ReportResourceRecord,
    ) -> ReportResourceRecord | None:
        if record.audit_receipt is None or not record.pending_events:
            raise ReportPortContractError("audit_pending 资源记录不完整")
        try:
            self._audit.append_lifecycle_events(
                AppendReportLifecycleEvents(
                    record.audit_receipt,
                    record.pending_events,
                )
            )
        except Exception as exc:
            logger.exception(
                "报告清理事件审计追加失败，精确事件已持久化待恢复: task_id=%s",
                record.task_id,
            )
            self._save_error_best_effort(
                record,
                stage="cleanup_audit_append",
                message=f"{type(exc).__name__}: 清理事件审计追加失败",
            )
            return None

        external_state = (
            ReportCleanupPartState.SUCCEEDED
            if record.pending_events_succeeded
            else ReportCleanupPartState.FAILED
        )
        error_message = ""
        if external_state is ReportCleanupPartState.FAILED:
            error_message = "；".join(
                dict.fromkeys(
                    event.error_message or "外部资源删除失败"
                    for event in record.pending_events
                    if event.operation in _DELETION_OPERATIONS and not event.success
                )
            )
        return self._save(
            replace(
                record,
                state=ReportResourceState.CLEANUP_PENDING,
                external_state=external_state,
                pending_events=(),
                pending_events_succeeded=None,
                last_error_stage=(
                    "rag_cleanup" if external_state is ReportCleanupPartState.FAILED else ""
                ),
                last_error_message=error_message,
            )
        )

    def _run_artifact_cleanup(
        self,
        record: ReportResourceRecord,
    ) -> ReportResourceRecord:
        try:
            result = self._artifacts.cleanup_unretained(
                record.scope,
                retain=record.retained,
            )
            if not isinstance(result, ReportArtifactCleanupResult):
                raise ReportPortContractError("Artifact cleanup 返回类型错误")
            if any(
                item.task_id != record.task_id
                for item in result.cleaned + result.pending
            ):
                raise ReportPortContractError("Artifact cleanup 返回跨任务引用")
        except Exception as exc:
            logger.exception(
                "报告 Artifact 清理失败，已保留持久恢复事实: task_id=%s",
                record.task_id,
            )
            return self._save(
                replace(
                    record,
                    artifact_state=ReportCleanupPartState.FAILED,
                    last_error_stage="artifact_cleanup",
                    last_error_message=f"{type(exc).__name__}: Artifact 清理失败",
                )
            )

        state = (
            ReportCleanupPartState.FAILED
            if result.pending
            else ReportCleanupPartState.SUCCEEDED
        )
        return self._save(
            replace(
                record,
                artifact_state=state,
                pending_artifacts=result.pending,
                last_error_stage="artifact_cleanup" if result.pending else record.last_error_stage,
                last_error_message=(
                    f"仍有 {len(result.pending)} 个 Artifact 待清理"
                    if result.pending
                    else record.last_error_message
                ),
            )
        )

    def _run_artifact_cleanup_exclusive(
        self,
        record: ReportResourceRecord,
    ) -> ReportResourceRecord:
        """串行同一任务的本地删除，并在取得锁后重读最新 CAS 事实。

        外部 RAG 删除已有持久租约，允许并发恢复者观察 ``pending``；本地文件删除没有
        跨进程租约。阶段 1C 的单实例执行链因此只在 Artifact 副作用周围加按 TaskId
        进程锁，既不全局阻塞其他任务，也避免独立 sweeper 与执行 Worker 重复删除。
        """

        entry = self._acquire_artifact_lock(record.task_id)
        try:
            latest = self._required(record.task_id)
            if latest.state in {
                ReportResourceState.CLEANED,
                ReportResourceState.QUARANTINED,
            }:
                return latest
            if latest.artifact_state not in {
                ReportCleanupPartState.PENDING,
                ReportCleanupPartState.FAILED,
            }:
                return latest
            updated = self._run_artifact_cleanup(latest)
            complete = (
                updated.external_state
                in {
                    ReportCleanupPartState.NOT_REQUIRED,
                    ReportCleanupPartState.SUCCEEDED,
                }
                and updated.artifact_state is ReportCleanupPartState.SUCCEEDED
            )
            if not complete:
                return updated
            cleaned = self._save(
                replace(
                    updated,
                    state=ReportResourceState.CLEANED,
                    pending_artifacts=(),
                    last_error_stage="",
                    last_error_message="",
                )
            )
            logger.info("报告任务资源清理已收敛: task_id=%s", cleaned.task_id)
            return cleaned
        finally:
            self._release_artifact_lock(record.task_id, entry)

    def _acquire_artifact_lock(self, task_id: TaskId) -> _ArtifactLockEntry:
        with self._artifact_locks_guard:
            entry = self._artifact_locks.get(task_id)
            if entry is None:
                entry = _ArtifactLockEntry(threading.RLock())
                self._artifact_locks[task_id] = entry
            entry.user_count += 1
        entry.lock.acquire()
        return entry

    def _release_artifact_lock(
        self,
        task_id: TaskId,
        entry: _ArtifactLockEntry,
    ) -> None:
        entry.lock.release()
        with self._artifact_locks_guard:
            entry.user_count -= 1
            if entry.user_count == 0:
                self._artifact_locks.pop(task_id, None)

    def _quarantine_record(
        self,
        record: ReportResourceRecord,
        *,
        stage: str,
        reason: str,
    ) -> ReportResourceRecord:
        if record.state is ReportResourceState.QUARANTINED:
            # 隔离原因是首次发现未知副作用时的审计事实，重复恢复或重复异常处理不得
            # 用较晚、信息更少的错误覆盖它。
            logger.info(
                "报告资源已处于隔离状态，保留首次原因: task_id=%s stage=%s",
                record.task_id,
                record.last_error_stage,
            )
            return record
        if record.state is ReportResourceState.CLEANED:
            raise ReportCleanupError("已清理的报告资源不得回退为 quarantined")
        normalized_stage = str(stage or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_stage or not normalized_reason:
            raise ValueError("quarantine 必须包含 stage 和 reason")
        quarantined = self._save(
            replace(
                record,
                state=ReportResourceState.QUARANTINED,
                external_attempt_open=False,
                external_attempt_token="",
                external_attempt_started_at=None,
                external_attempt_heartbeat_at=None,
                last_error_stage=normalized_stage,
                last_error_message=normalized_reason,
            )
        )
        logger.critical(
            "报告资源已隔离，禁止自动清理: task_id=%s stage=%s",
            record.task_id,
            normalized_stage,
        )
        return quarantined

    def _save_error_best_effort(
        self,
        record: ReportResourceRecord,
        *,
        stage: str,
        message: str,
    ) -> None:
        try:
            self._save(
                replace(
                    record,
                    last_error_stage=stage,
                    last_error_message=message,
                )
            )
        except Exception:
            # record 已经在 audit_pending 状态保存了精确事件；补写说明失败不能抹去该事实。
            logger.exception(
                "报告资源恢复错误说明补写失败: task_id=%s stage=%s",
                record.task_id,
                stage,
            )

    def _required(self, task_id: TaskId) -> ReportResourceRecord:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        record = self._store.get(task_id)
        if record is None:
            raise ReportCleanupError("报告资源恢复记录不存在")
        return record

    def _save(self, record: ReportResourceRecord) -> ReportResourceRecord:
        return self._store.save(record, expected_version=record.version)

    @staticmethod
    def _result(
        record: ReportResourceRecord,
        outcome: ReportResourceCleanupOutcome,
    ) -> ReportResourceCleanupResult:
        return ReportResourceCleanupResult(
            outcome,
            pending_external=record.external_state
            in {ReportCleanupPartState.PENDING, ReportCleanupPartState.FAILED},
            pending_artifact_count=len(record.pending_artifacts),
        )


__all__ = ["ReportResourceRecoveryService"]
