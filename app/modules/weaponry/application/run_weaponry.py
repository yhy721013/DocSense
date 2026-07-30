"""只按 TaskId 恢复并执行一次武器谱任务的框架无关 Application。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging

from app.modules.tasks.domain import (
    ProgressKey,
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
)
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    ProgressPublication,
    ProgressPublisherPort,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskCommandPort,
)
from app.modules.weaponry.domain import (
    WEAPONRY_FAILURE_MESSAGE,
    WEAPONRY_STATUS_FAILED,
    WEAPONRY_STATUS_SUCCEEDED,
    WEAPONRY_SUCCESS_MESSAGE,
    WeaponryExecutionIdentity,
    WeaponryInputSnapshot,
    WeaponryResult,
    WeaponrySubmission,
    validate_weaponry_result_completeness,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCallback,
    DeliverWeaponryCallback,
    IdempotentOperationResult,
    OpenTargetEvidenceScope,
    PrepareWeaponryResourceCleanup,
    QuarantineWeaponryResources,
    RegisterWeaponryResource,
    TargetEvidenceRetrievalPort,
    TargetEvidenceScope,
    WeaponryCallbackAcquireOutcome,
    WeaponryCallbackAcquireResult,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCallbackPort,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryPortError,
    WeaponryPortStateError,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponryResourceStorePort,
    WeaponrySourceBoundaryError,
    WeaponryTrackedResource,
)

from .errors import (
    WeaponryApplicationError,
    WeaponryAuditError,
    WeaponryExecutionError,
    WeaponryPortContractError,
    WeaponryScenePreservationError,
    WeaponryStaleExecutionError,
    WeaponryTaskPersistenceError,
)
from .field_execution import WeaponryFieldExecutor
from .submit_weaponry import (
    WEAPONRY_PUBLIC_PROCESSING_STATUS,
    WEAPONRY_TASK_TYPE,
)


logger = logging.getLogger(__name__)

_PROGRESS_PREPARING = (0.05, "正在查找知识库")
_PROGRESS_SCOPE_READY = (0.10, "正在创建检索会话")
_PROGRESS_CLEANUP = (0.92, "正在清理检索会话")
_RESOURCE_CAS_ATTEMPTS = 8


class RunWeaponryOutcome(str, Enum):
    """Worker 内部稳定结果，不新增任何公开任务状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    MISSING = "missing"
    NOT_CLAIMED = "not_claimed"


@dataclass(frozen=True)
class RunWeaponryResult:
    """一次 ``RunWeaponryTask`` 调用的内部收敛摘要。

    ``diagnostic_error_codes`` 只承载字段级降级事实，不进入持久化公开结果或 Callback。
    Dispatcher 依靠它把供应商容量错误、内部输入契约错误与真正的业务零结果分开计数。
    """

    task_id: TaskId
    outcome: RunWeaponryOutcome
    error_code: str = ""
    callback_outcome: str = ""
    cleanup_state: str = ""
    selected_evidence_count: int = 0
    model_call_count: int = 0
    diagnostic_error_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.outcome, RunWeaponryOutcome):
            raise TypeError("outcome 必须是 RunWeaponryOutcome")
        for name in ("error_code", "callback_outcome", "cleanup_state"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} 必须是 str")
        for name in ("selected_evidence_count", "model_call_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if not isinstance(self.diagnostic_error_codes, tuple):
            raise TypeError("diagnostic_error_codes 必须是 tuple")
        if any(
            not isinstance(code, str) or not code.strip()
            for code in self.diagnostic_error_codes
        ):
            raise ValueError("diagnostic_error_codes 只能包含非空字符串")
        if len(set(self.diagnostic_error_codes)) != len(
            self.diagnostic_error_codes
        ):
            raise ValueError("diagnostic_error_codes 不得重复")


class RunWeaponryTask:
    """武器谱 Worker 应用用例。

    本类只接收内部 ``TaskId``。它不读取原始请求、环境变量、数据库行、真实 URL 或供应商
    Client；所有慢 I/O 均发生在 Task/Resource/Audit 的短事务之外，并在返回后重新核对 latest。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[
            WeaponrySubmission,
            WeaponryInputSnapshot,
            WeaponryResult,
        ],
        progress_publisher: ProgressPublisherPort,
        retrieval: TargetEvidenceRetrievalPort,
        field_executor: WeaponryFieldExecutor,
        callbacks: WeaponryCallbackPort,
        resources: WeaponryResourceStorePort,
    ) -> None:
        self._task_commands = task_commands
        self._progress_publisher = progress_publisher
        self._retrieval = retrieval
        if not isinstance(field_executor, WeaponryFieldExecutor):
            raise TypeError("field_executor 必须是 WeaponryFieldExecutor")
        self._field_executor = field_executor
        self._callbacks = callbacks
        self._resources = resources

    @property
    def task_commands(
        self,
    ) -> TaskCommandPort[WeaponrySubmission, WeaponryInputSnapshot, WeaponryResult]:
        """供组合根证明 Submit、Run 与 Dispatcher 共享同一 Repository Adapter。"""

        return self._task_commands

    @property
    def progress_publisher(self) -> ProgressPublisherPort:
        return self._progress_publisher

    @property
    def retrieval(self) -> TargetEvidenceRetrievalPort:
        return self._retrieval

    @property
    def field_executor(self) -> WeaponryFieldExecutor:
        return self._field_executor

    @property
    def callbacks(self) -> WeaponryCallbackPort:
        return self._callbacks

    @property
    def resources(self) -> WeaponryResourceStorePort:
        return self._resources

    def execute(self, task_id: TaskId) -> RunWeaponryResult:
        """领取并执行一个任务；重复、终态或 stale 派发会幂等跳过。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        loaded = self._task_commands.get_execution(task_id)
        if loaded is None:
            logger.warning("武器谱 execution 不存在，跳过派发: task_id=%s", task_id.value)
            return RunWeaponryResult(task_id, RunWeaponryOutcome.MISSING)
        self._validate_execution(loaded, task_id)

        claim = self._task_commands.claim(task_id)
        if not isinstance(claim, TaskClaimResult):
            raise WeaponryPortContractError("TaskCommandPort.claim 返回类型错误")
        if claim.outcome is TaskClaimOutcome.MISSING:
            return RunWeaponryResult(task_id, RunWeaponryOutcome.MISSING)
        if claim.outcome is not TaskClaimOutcome.CLAIMED:
            logger.info(
                "武器谱任务未取得执行权，幂等跳过: task_id=%s outcome=%s",
                task_id.value,
                claim.outcome.value,
            )
            return RunWeaponryResult(task_id, RunWeaponryOutcome.NOT_CLAIMED)
        execution = claim.execution
        if not isinstance(execution, TaskExecutionSnapshot):
            raise WeaponryPortContractError("claimed 结果缺少执行快照")
        snapshot = self._validate_execution(execution, task_id)
        if execution.execution_state != "running":
            raise WeaponryPortContractError("claimed 执行快照必须处于 running")

        scope: TargetEvidenceScope | None = None
        resource_record_created = False
        selected_evidence_count = 0
        model_call_count = 0
        diagnostic_error_codes: list[str] = []
        preserve_error_code = ""
        try:
            if not self._update_progress(execution, *_PROGRESS_PREPARING):
                return RunWeaponryResult(task_id, RunWeaponryOutcome.STALE)

            try:
                self._create_resource_record(execution)
            except WeaponryScenePreservationError as error:
                # 同一 TaskId 已经留下非空资源事实，说明此前执行可能在外部副作用之后
                # 中断。此时不得把它当作一条全新任务继续，也不能因本次 create 失败而
                # 忘记已有现场；先把记录标记为“已存在”，统一进入失败终态和隔离流程。
                resource_record_created = True
                preserve_error_code = error.code
                raise
            resource_record_created = True
            if not snapshot.document_scope.documents:
                raise WeaponryExecutionError(
                    "受理时冻结的文档范围为空",
                    error_code="weaponry_document_scope_empty",
                )

            try:
                scope = self._retrieval.open_scope(
                    OpenTargetEvidenceScope(
                        task_id=task_id,
                        document_scope=snapshot.document_scope,
                        policy=snapshot.evidence_selection_policy,
                    )
                )
            except WeaponryExternalOperationError as error:
                if error.outcome is WeaponryExternalOutcome.OUTCOME_UNKNOWN:
                    preserve_error_code = error.error_code
                raise WeaponryExecutionError(
                    "武器谱检索范围创建失败",
                    error_code=error.error_code,
                ) from error
            except (WeaponrySourceBoundaryError, WeaponryPortStateError) as error:
                raise WeaponryExecutionError(
                    "武器谱检索范围不满足执行契约",
                    error_code=error.error_code,
                ) from error
            except Exception as error:
                raise WeaponryExecutionError(
                    "武器谱检索范围创建失败",
                    error_code="retrieval_scope_open_failed",
                ) from error
            expected_document_keys = tuple(
                document.document_key
                for document in snapshot.document_scope.documents
            )
            if (
                not isinstance(scope, TargetEvidenceScope)
                or scope.task_id != task_id
                or scope.allowed_document_keys != expected_document_keys
                or scope.selection_profile_id
                != snapshot.evidence_selection_policy.profile_id
                or scope.provider_fingerprint
                != snapshot.evidence_selection_policy.provider_fingerprint
                or scope.embedding_fingerprint
                != snapshot.evidence_selection_policy.embedding_fingerprint
            ):
                preserve_error_code = "retrieval_scope_identity_invalid"
                raise WeaponryPortContractError(
                    "Retrieval Scope 与当前任务、文档范围或策略指纹不一致"
                )
            self._register_retrieval_scope(task_id, scope)

            if not self._update_progress(execution, *_PROGRESS_SCOPE_READY):
                cleanup_state = self._finalize_resources(
                    task_id,
                    scope=scope,
                    resource_record_created=resource_record_created,
                )
                return RunWeaponryResult(
                    task_id,
                    RunWeaponryOutcome.STALE,
                    cleanup_state=cleanup_state,
                )

            field_results = []
            total_fields = len(snapshot.fields)
            for field_sequence, field in enumerate(snapshot.fields, start=1):
                executed = self._field_executor.execute(
                    task_id=task_id,
                    business_ref=execution.business_ref,
                    snapshot=snapshot,
                    scope=scope,
                    field=field,
                    field_sequence=field_sequence,
                    is_current=lambda: self._is_latest(execution),
                )
                field_results.append(executed.result)
                selected_evidence_count += executed.selected_evidence_count
                model_call_count += executed.model_call_count
                diagnostic_error_codes.extend(
                    executed.diagnostic_error_codes
                )
                progress = 0.15 + (field_sequence / total_fields) * 0.75
                if not self._update_progress(
                    execution,
                    progress,
                    f"正在提取字段 ({field_sequence}/{total_fields})",
                ):
                    cleanup_state = self._finalize_resources(
                        task_id,
                        scope=scope,
                        resource_record_created=resource_record_created,
                    )
                    return RunWeaponryResult(
                        task_id,
                        RunWeaponryOutcome.STALE,
                        cleanup_state=cleanup_state,
                        selected_evidence_count=selected_evidence_count,
                        model_call_count=model_call_count,
                        diagnostic_error_codes=tuple(
                            dict.fromkeys(diagnostic_error_codes)
                        ),
                    )

            if not self._update_progress(execution, *_PROGRESS_CLEANUP):
                cleanup_state = self._finalize_resources(
                    task_id,
                    scope=scope,
                    resource_record_created=resource_record_created,
                )
                return RunWeaponryResult(
                    task_id,
                    RunWeaponryOutcome.STALE,
                    cleanup_state=cleanup_state,
                    selected_evidence_count=selected_evidence_count,
                    model_call_count=model_call_count,
                    diagnostic_error_codes=tuple(
                        dict.fromkeys(diagnostic_error_codes)
                    ),
                )

            result = WeaponryResult(
                identity=WeaponryExecutionIdentity(
                    task_id.value,
                    snapshot.architecture_id,
                ),
                status=WEAPONRY_STATUS_SUCCEEDED,
                fields=tuple(field_results),
                message=WEAPONRY_SUCCESS_MESSAGE,
            )
            validate_weaponry_result_completeness(snapshot, result)
            if not self._finish_if_current(
                execution,
                result,
                execution_state="succeeded",
                public_status=WEAPONRY_STATUS_SUCCEEDED,
                message="解析完成",
            ):
                cleanup_state = self._finalize_resources(
                    task_id,
                    scope=scope,
                    resource_record_created=resource_record_created,
                )
                return RunWeaponryResult(
                    task_id,
                    RunWeaponryOutcome.STALE,
                    cleanup_state=cleanup_state,
                    selected_evidence_count=selected_evidence_count,
                    model_call_count=model_call_count,
                    diagnostic_error_codes=tuple(
                        dict.fromkeys(diagnostic_error_codes)
                    ),
                )

            self._publish_terminal_progress(execution, "succeeded")
            callback_outcome = self._deliver_callback(
                execution,
                snapshot,
                result,
            )
            cleanup_state = self._finalize_resources(
                task_id,
                scope=scope,
                resource_record_created=resource_record_created,
            )
            logger.info(
                "武器谱 Application 执行完成: task_id=%s architecture_id=%s "
                "field_count=%d selected_evidence_count=%d model_call_count=%d "
                "callback_outcome=%s cleanup_state=%s",
                task_id.value,
                snapshot.architecture_id,
                len(field_results),
                selected_evidence_count,
                model_call_count,
                callback_outcome,
                cleanup_state,
            )
            return RunWeaponryResult(
                task_id,
                RunWeaponryOutcome.SUCCEEDED,
                callback_outcome=callback_outcome,
                cleanup_state=cleanup_state,
                selected_evidence_count=selected_evidence_count,
                model_call_count=model_call_count,
                diagnostic_error_codes=tuple(
                    dict.fromkeys(diagnostic_error_codes)
                ),
            )
        except WeaponryStaleExecutionError:
            logger.info(
                "武器谱慢调用返回后发现旧 execution，禁止后续进度、终态和回调: "
                "task_id=%s",
                task_id.value,
            )
            cleanup_state = self._finalize_resources(
                task_id,
                scope=scope,
                resource_record_created=resource_record_created,
            )
            return RunWeaponryResult(
                task_id,
                RunWeaponryOutcome.STALE,
                cleanup_state=cleanup_state,
                selected_evidence_count=selected_evidence_count,
                model_call_count=model_call_count,
                diagnostic_error_codes=tuple(
                    dict.fromkeys(diagnostic_error_codes)
                ),
            )
        except WeaponryTaskPersistenceError:
            # 进度/终态提交可能已经成功但响应丢失；补写失败终态会破坏单终态语义。
            logger.critical(
                "武器谱任务事实写入结果不确定，禁止补写第二终态并保留资源: "
                "task_id=%s",
                task_id.value,
                exc_info=True,
            )
            self._finalize_resources(
                task_id,
                scope=scope,
                resource_record_created=resource_record_created,
                preserve_error_code="weaponry_task_persistence_unknown",
            )
            raise
        except Exception as error:
            preserve = preserve_error_code
            if isinstance(error, (WeaponryAuditError, WeaponryScenePreservationError)):
                preserve = error.code
            return self._finish_failure(
                execution,
                snapshot,
                error,
                scope=scope,
                resource_record_created=resource_record_created,
                preserve_error_code=preserve,
                selected_evidence_count=selected_evidence_count,
                model_call_count=model_call_count,
                diagnostic_error_codes=tuple(
                    dict.fromkeys(diagnostic_error_codes)
                ),
            )

    @staticmethod
    def _validate_execution(
        execution: object,
        expected_task_id: TaskId,
    ) -> WeaponryInputSnapshot:
        if not isinstance(execution, TaskExecutionSnapshot):
            raise WeaponryPortContractError("任务读取结果必须是 TaskExecutionSnapshot")
        snapshot = execution.input_snapshot
        if not isinstance(snapshot, WeaponryInputSnapshot):
            raise WeaponryPortContractError("任务输入必须是 WeaponryInputSnapshot")
        expected_ref = TaskBusinessRef(WEAPONRY_TASK_TYPE, snapshot.business_key)
        if (
            execution.task_id != expected_task_id
            or execution.task_type != WEAPONRY_TASK_TYPE
            or execution.business_ref != expected_ref
            or snapshot.task_id != expected_task_id.value
            or snapshot.accepted_at != execution.accepted_at
            or execution.trace_id != snapshot.trace_id
        ):
            raise WeaponryPortContractError("武器谱 execution 快照身份不一致")
        return snapshot

    def _update_progress(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
        progress: float,
        message: str,
    ) -> bool:
        try:
            updated = self._task_commands.update_progress_if_current(
                ExpectedProgressUpdate(
                    expected_task_id=execution.task_id,
                    business_ref=execution.business_ref,
                    progress=progress,
                    message=message,
                    execution_state="running",
                    public_status=WEAPONRY_PUBLIC_PROCESSING_STATUS,
                )
            )
        except Exception as error:
            raise WeaponryTaskPersistenceError("武器谱进度事实写入失败") from error
        if not isinstance(updated, bool):
            raise WeaponryPortContractError("进度条件写必须返回 bool")
        if not updated:
            logger.info(
                "武器谱进度条件写发现旧 execution: task_id=%s progress=%.4f",
                execution.task_id.value,
                progress,
            )
            return False
        self._publish_progress(execution, progress, message, "running")
        return True

    def _publish_progress(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
        progress: float,
        message: str,
        internal_state: str,
    ) -> None:
        try:
            self._progress_publisher.publish(
                ProgressPublication(
                    key=ProgressKey(
                        WEAPONRY_TASK_TYPE,
                        execution.business_ref.business_key,
                    ),
                    expected_task_id=execution.task_id,
                    progress=progress,
                    message=message,
                    internal_state=internal_state,
                )
            )
        except Exception:
            # Progress 是通知投影，不是任务权威事实；失败不能回滚已提交状态。
            logger.exception(
                "武器谱 Progress 通知失败，任务事实保持不变: "
                "task_id=%s progress=%.4f internal_state=%s",
                execution.task_id.value,
                progress,
                internal_state,
            )

    def _publish_terminal_progress(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
        internal_state: str,
    ) -> None:
        self._publish_progress(execution, 1.0, "", internal_state)

    def _finish_if_current(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
        result: WeaponryResult,
        *,
        execution_state: str,
        public_status: str,
        message: str,
    ) -> bool:
        try:
            finished = self._task_commands.finish_if_current(
                ExpectedTaskCompletion(
                    expected_task_id=execution.task_id,
                    business_ref=execution.business_ref,
                    execution_state=execution_state,
                    public_status=public_status,
                    message=message,
                    result=result,
                )
            )
        except Exception as error:
            raise WeaponryTaskPersistenceError("武器谱终态事实写入失败") from error
        if not isinstance(finished, bool):
            raise WeaponryPortContractError("终态条件写必须返回 bool")
        if not finished:
            logger.info(
                "武器谱终态 CAS 失权，禁止终态 Progress 和回调: task_id=%s",
                execution.task_id.value,
            )
        return finished

    def _finish_failure(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
        snapshot: WeaponryInputSnapshot,
        error: BaseException,
        *,
        scope: TargetEvidenceScope | None,
        resource_record_created: bool,
        preserve_error_code: str,
        selected_evidence_count: int,
        model_call_count: int,
        diagnostic_error_codes: tuple[str, ...],
    ) -> RunWeaponryResult:
        error_code = self._error_code(error)
        logger.error(
            "武器谱 Application 执行失败: task_id=%s architecture_id=%s "
            "error_code=%s error_type=%s preserve_scene=%s",
            execution.task_id.value,
            snapshot.architecture_id,
            error_code,
            type(error).__name__,
            bool(preserve_error_code),
            exc_info=(type(error), error, error.__traceback__),
        )
        result = WeaponryResult(
            identity=WeaponryExecutionIdentity(
                execution.task_id.value,
                snapshot.architecture_id,
            ),
            status=WEAPONRY_STATUS_FAILED,
            fields=(),
            message=WEAPONRY_FAILURE_MESSAGE,
        )
        try:
            finished = self._finish_if_current(
                execution,
                result,
                execution_state="failed",
                public_status=WEAPONRY_STATUS_FAILED,
                message=WEAPONRY_FAILURE_MESSAGE,
            )
        except WeaponryTaskPersistenceError:
            self._finalize_resources(
                execution.task_id,
                scope=scope,
                resource_record_created=resource_record_created,
                preserve_error_code=(
                    preserve_error_code or "weaponry_failure_terminal_unknown"
                ),
            )
            raise
        if not finished:
            cleanup_state = self._finalize_resources(
                execution.task_id,
                scope=scope,
                resource_record_created=resource_record_created,
                preserve_error_code=preserve_error_code,
            )
            return RunWeaponryResult(
                execution.task_id,
                RunWeaponryOutcome.STALE,
                error_code=error_code,
                cleanup_state=cleanup_state,
                selected_evidence_count=selected_evidence_count,
                model_call_count=model_call_count,
                diagnostic_error_codes=diagnostic_error_codes,
            )

        self._publish_terminal_progress(execution, "failed")
        callback_outcome = self._deliver_callback(
            execution,
            snapshot,
            result,
        )
        cleanup_state = self._finalize_resources(
            execution.task_id,
            scope=scope,
            resource_record_created=resource_record_created,
            preserve_error_code=preserve_error_code,
        )
        return RunWeaponryResult(
            execution.task_id,
            RunWeaponryOutcome.FAILED,
            error_code=error_code,
            callback_outcome=callback_outcome,
            cleanup_state=cleanup_state,
            selected_evidence_count=selected_evidence_count,
            model_call_count=model_call_count,
            diagnostic_error_codes=diagnostic_error_codes,
        )

    def _deliver_callback(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
        snapshot: WeaponryInputSnapshot,
        result: WeaponryResult,
    ) -> str:
        try:
            # DTO 投影也属于终态之后的独立交付维度。即便未来领域映射出现缺陷，异常也
            # 必须留在本方法内，绝不能逃逸到 execute 后补写第二个失败终态。
            payload = result.to_callback()
            if not self._is_latest(execution):
                logger.info(
                    "武器谱回调前发现旧 execution，跳过网络调用: task_id=%s",
                    execution.task_id.value,
                )
                return WeaponryCallbackDeliveryOutcome.STALE.value
            acquire = self._callbacks.acquire(
                AcquireWeaponryCallback(
                    task_id=execution.task_id,
                    architecture_id=snapshot.architecture_id,
                )
            )
            if not isinstance(acquire, WeaponryCallbackAcquireResult):
                raise WeaponryPortContractError("Callback acquire 返回类型错误")
            if acquire.outcome is not WeaponryCallbackAcquireOutcome.ACQUIRED:
                logger.warning(
                    "武器谱回调未取得发送权: task_id=%s outcome=%s",
                    execution.task_id.value,
                    acquire.outcome.value,
                )
                return acquire.outcome.value
            lease = acquire.lease
            if (
                lease is None
                or lease.task_id != execution.task_id
                or lease.architecture_id != snapshot.architecture_id
            ):
                raise WeaponryPortContractError("Callback Guard Lease 与任务不一致")
            delivery = self._callbacks.deliver(
                DeliverWeaponryCallback(lease=lease, payload=payload)
            )
            if not isinstance(delivery, WeaponryCallbackDeliveryResult):
                raise WeaponryPortContractError("Callback deliver 返回类型错误")
            if delivery.outcome is WeaponryCallbackDeliveryOutcome.STALE:
                logger.info(
                    "武器谱 Callback Adapter 发送前判定旧任务: task_id=%s",
                    execution.task_id.value,
                )
                return delivery.outcome.value
            completed = self._callbacks.complete(lease, delivery, payload)
            if not isinstance(completed, bool):
                raise WeaponryPortContractError("Callback complete 必须返回 bool")
            if not completed:
                raise WeaponryPortContractError("Callback Guard 完成权已过期")
            return delivery.outcome.value
        except Exception:
            # Callback 是业务终态之后的独立交付维度，任何异常都不得补写第二个终态。
            logger.exception(
                "武器谱回调端口执行异常，业务终态保持不变: task_id=%s",
                execution.task_id.value,
            )
            return "port_error"

    def _create_resource_record(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
    ) -> None:
        expected = WeaponryResourceRecord(
            task_id=execution.task_id,
            business_ref=execution.business_ref,
        )
        try:
            actual = self._resources.create(expected)
        except WeaponryPortStateError as error:
            if error.error_code == "resource_record_exists":
                raise WeaponryScenePreservationError(
                    "当前任务已存在非空资源现场，禁止盲目重新执行",
                    error_code="weaponry_resource_record_preexisting",
                ) from error
            raise WeaponryExecutionError(
                "武器谱资源记录创建失败",
                error_code=error.error_code,
            ) from error
        except Exception as error:
            raise WeaponryExecutionError(
                "武器谱资源记录创建失败",
                error_code="weaponry_resource_record_create_failed",
            ) from error
        if not isinstance(actual, WeaponryResourceRecord):
            raise WeaponryPortContractError("Resource create 返回类型错误")
        if (
            actual.task_id != expected.task_id
            or actual.business_ref != expected.business_ref
        ):
            raise WeaponryPortContractError("新建资源记录与当前任务身份不一致")
        if actual != expected:
            # 某些未来 Repository 可能用“返回既有记录”表达幂等冲突。只要该记录已经
            # 包含版本、状态或资源差异，就必须视为待对账现场，不能继续创建第二套资源。
            raise WeaponryScenePreservationError(
                "当前任务已存在非空资源现场，禁止盲目重新执行",
                error_code="weaponry_resource_record_preexisting",
            )

    def _register_retrieval_scope(
        self,
        task_id: TaskId,
        scope: TargetEvidenceScope,
    ) -> None:
        # 真实 Retrieval Adapter 会在远端 workspace 创建后立即登记；严格 Fake 不知道
        # Resource Store。这里按完全相同的稳定身份幂等补登记，使两种装配都能证明顺序。
        resource = WeaponryTrackedResource(
            resource_id=f"retrieval-scope:{scope.scope_ref}",
            kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
            external_ref=scope.scope_ref,
            ownership=WeaponryResourceOwnership.OWNED,
            idempotency_key=f"weaponry:{task_id.value}:retrieval-scope",
        )
        for attempt in range(1, _RESOURCE_CAS_ATTEMPTS + 1):
            record = self._resources.get(task_id)
            if not isinstance(record, WeaponryResourceRecord):
                raise WeaponryPortContractError("Retrieval Scope 登记前资源记录不存在")
            try:
                updated = self._resources.register(
                    RegisterWeaponryResource(
                        task_id=task_id,
                        resource=resource,
                        expected_version=record.version,
                    )
                )
            except WeaponryPortStateError as error:
                if error.error_code == "resource_version_conflict":
                    logger.debug(
                        "武器谱 Retrieval Scope 登记发生 CAS 竞争: "
                        "task_id=%s attempt=%d",
                        task_id.value,
                        attempt,
                    )
                    continue
                raise WeaponryExecutionError(
                    "武器谱 Retrieval Scope 登记失败",
                    error_code=error.error_code,
                ) from error
            except Exception as error:
                raise WeaponryExecutionError(
                    "武器谱 Retrieval Scope 登记失败",
                    error_code="retrieval_scope_registration_failed",
                ) from error
            if not isinstance(updated, WeaponryResourceRecord):
                raise WeaponryPortContractError("Resource register 返回类型错误")
            if resource not in updated.resources:
                raise WeaponryPortContractError("Retrieval Scope 未进入资源事实")
            return
        raise WeaponryExecutionError(
            "武器谱 Retrieval Scope 登记 CAS 连续失权",
            error_code="retrieval_scope_registration_cas_exhausted",
        )

    def _finalize_resources(
        self,
        task_id: TaskId,
        *,
        scope: TargetEvidenceScope | None,
        resource_record_created: bool,
        preserve_error_code: str = "",
    ) -> str:
        if not resource_record_created:
            return "not_created"
        if preserve_error_code:
            return self._quarantine_resources(task_id, preserve_error_code)

        # 必须先把所有 owned 资源持久切换为 cleanup_pending，之后才允许执行远端删除。
        # 旧顺序在“远端删除成功、本地 prepare_cleanup 尚未提交”之间存在崩溃窗口；终态
        # 任务会留下 tracking 记录，恢复扫描无法判断其清理意图。这里宁可暂时多保留一份
        # 可恢复资源，也不能产生没有权威清理事实的外部副作用。
        cleanup_state = self._prepare_cleanup(task_id)
        if cleanup_state not in {
            WeaponryResourceRecordState.CLEANUP_PENDING.value,
            WeaponryResourceRecordState.CLEANED.value,
        }:
            logger.critical(
                "武器谱资源清理意图未可靠提交，禁止执行远端关闭: "
                "task_id=%s cleanup_state=%s",
                task_id.value,
                cleanup_state,
            )
            return cleanup_state

        if scope is not None:
            try:
                close_result = self._retrieval.close_scope(scope)
                if not isinstance(close_result, IdempotentOperationResult):
                    logger.error(
                        "武器谱检索范围关闭返回类型错误，保留 cleanup pending: task_id=%s",
                        task_id.value,
                    )
                elif not close_result.success:
                    logger.warning(
                        "武器谱检索范围尚未关闭，交由资源恢复: "
                        "task_id=%s error_code=%s",
                        task_id.value,
                        close_result.error_code,
                    )
            except WeaponryExternalOperationError as error:
                if error.outcome is WeaponryExternalOutcome.OUTCOME_UNKNOWN:
                    return self._quarantine_resources(task_id, error.error_code)
                logger.warning(
                    "武器谱检索范围关闭明确失败，保留 cleanup pending: "
                    "task_id=%s error_code=%s",
                    task_id.value,
                    error.error_code,
                )
            except Exception:
                logger.exception(
                    "武器谱检索范围关闭异常，保留 cleanup pending: task_id=%s",
                    task_id.value,
                )
        return cleanup_state

    def _prepare_cleanup(self, task_id: TaskId) -> str:
        for attempt in range(1, _RESOURCE_CAS_ATTEMPTS + 1):
            try:
                record = self._resources.get(task_id)
                if not isinstance(record, WeaponryResourceRecord):
                    logger.error("武器谱清理准备找不到资源记录: task_id=%s", task_id.value)
                    return "missing"
                if record.state is WeaponryResourceRecordState.QUARANTINED:
                    return record.state.value
                updated = self._resources.prepare_cleanup(
                    PrepareWeaponryResourceCleanup(
                        task_id=task_id,
                        expected_version=record.version,
                    )
                )
                if not isinstance(updated, WeaponryResourceRecord):
                    raise WeaponryPortContractError("prepare_cleanup 返回类型错误")
                if updated.task_id != task_id:
                    raise WeaponryPortContractError("cleanup 资源记录任务身份不一致")
                if updated.state not in {
                    WeaponryResourceRecordState.CLEANUP_PENDING,
                    WeaponryResourceRecordState.CLEANED,
                }:
                    raise WeaponryPortContractError("cleanup 资源记录未进入可恢复状态")
                if updated.state is WeaponryResourceRecordState.CLEANUP_PENDING:
                    logger.info(
                        "武器谱资源已进入 cleanup pending: task_id=%s "
                        "owned_pending_count=%d",
                        task_id.value,
                        len(updated.owned_cleanup_candidates),
                    )
                return updated.state.value
            except WeaponryPortStateError as error:
                if error.error_code == "resource_version_conflict":
                    continue
                logger.exception(
                    "武器谱资源清理准备失败: task_id=%s error_code=%s",
                    task_id.value,
                    error.error_code,
                )
                return "port_error"
            except Exception:
                logger.exception(
                    "武器谱资源清理准备异常，业务终态保持不变: task_id=%s",
                    task_id.value,
                )
                return "port_error"
        logger.error("武器谱资源清理准备 CAS 连续失权: task_id=%s", task_id.value)
        return "cas_exhausted"

    def _quarantine_resources(self, task_id: TaskId, error_code: str) -> str:
        for _ in range(_RESOURCE_CAS_ATTEMPTS):
            try:
                record = self._resources.get(task_id)
                if not isinstance(record, WeaponryResourceRecord):
                    return "missing"
                if record.state is WeaponryResourceRecordState.QUARANTINED:
                    return record.state.value
                updated = self._resources.quarantine(
                    QuarantineWeaponryResources(
                        task_id=task_id,
                        expected_version=record.version,
                        error_code=error_code,
                        # 只保存稳定原因，不拼接异常正文、URL 或供应商响应。
                        reason="外部结果或审计事实不完整，禁止自动清理",
                    )
                )
                if not isinstance(updated, WeaponryResourceRecord):
                    raise WeaponryPortContractError("resource quarantine 返回类型错误")
                logger.critical(
                    "武器谱资源现场已隔离: task_id=%s error_code=%s",
                    task_id.value,
                    error_code,
                )
                return updated.state.value
            except WeaponryPortStateError as error:
                if error.error_code == "resource_version_conflict":
                    continue
                logger.exception(
                    "武器谱资源隔离失败: task_id=%s error_code=%s",
                    task_id.value,
                    error.error_code,
                )
                return "port_error"
            except Exception:
                logger.exception("武器谱资源隔离异常: task_id=%s", task_id.value)
                return "port_error"
        return "cas_exhausted"

    def _is_latest(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
    ) -> bool:
        latest = self._task_commands.is_latest(
            execution.task_id,
            execution.business_ref,
        )
        if not isinstance(latest, bool):
            raise WeaponryPortContractError("is_latest 必须返回 bool")
        return latest

    @staticmethod
    def _error_code(error: BaseException) -> str:
        if isinstance(error, WeaponryApplicationError):
            return error.code
        if isinstance(error, WeaponryPortError):
            return error.error_code
        return "weaponry_unexpected_error"


__all__ = ["RunWeaponryOutcome", "RunWeaponryResult", "RunWeaponryTask"]
