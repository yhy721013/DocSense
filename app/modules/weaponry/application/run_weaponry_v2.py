"""阶段 2-5 基于完整 Authority 与持久 Step 的 Weaponry v2 Workflow。"""

from __future__ import annotations

import hashlib
import json
import logging

from app.modules.tasks.domain import (
    ProgressKey,
    TaskBusinessRef,
    TaskId,
    TaskStepCheckpoint,
)
from app.modules.tasks.ports import (
    ClockPort,
    LeaseSupervisorOutcome,
    LeaseSupervisorResult,
    ProgressPublication,
    ProgressPublisherPort,
    TaskExecutionStopRequested,
    TaskWorkflowContextPort,
    TaskWorkflowRunnerPort,
)
from app.modules.weaponry.domain import (
    WEAPONRY_FAILURE_MESSAGE,
    WEAPONRY_INPUT_SCHEMA_VERSION,
    WEAPONRY_STATUS_FAILED,
    WEAPONRY_STATUS_SUCCEEDED,
    WEAPONRY_SUCCESS_MESSAGE,
    WeaponryExecutionIdentity,
    WeaponryInputSnapshot,
    WeaponryResult,
    validate_weaponry_result_completeness,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCallback,
    DeliverWeaponryCallback,
    OpenTargetEvidenceScope,
    TargetEvidenceRetrievalPort,
    TargetEvidenceScope,
    WeaponryCallbackAcquireOutcome,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackPort,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryResourceRecord,
    WeaponryResourceStorePort,
    WeaponryTaskDocumentSnapshotStorePort,
)

from .errors import (
    WeaponryApplicationError,
    WeaponryPortContractError,
    WeaponryScenePreservationError,
    WeaponryTaskPersistenceError,
)
from .field_execution import WeaponryFieldExecutor
from .field_step_observer import WeaponryFieldStepObserver
from .run_weaponry import RunWeaponryOutcome, RunWeaponryResult, RunWeaponryTask
from .step_runtime import ActiveWeaponryStep, WeaponryStepRuntime
from .submit_weaponry import WEAPONRY_PUBLIC_PROCESSING_STATUS, WEAPONRY_TASK_TYPE


logger = logging.getLogger(__name__)

_PROGRESS_PREPARING = (0.05, "正在查找知识库")
_PROGRESS_SCOPE_READY = (0.10, "正在创建检索会话")
_PROGRESS_CLEANUP = (0.92, "正在清理检索会话")


def _digest(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RunWeaponryV2Workflow(RunWeaponryTask, TaskWorkflowRunnerPort):
    """只消费 Runtime 已 claim/start 的 Input v2，不读取或补造执行权。

    继承旧类只复用资源 cleanup/quarantine 的稳定业务算法；``run`` 不调用旧
    ``execute``，也不持有 ``TaskCommandPort``。所有 Task 条件写均经过 Step Runtime
    和当前可轮换 Authority Session。
    """

    def __init__(
        self,
        *,
        steps: WeaponryStepRuntime,
        clock: ClockPort,
        progress_publisher: ProgressPublisherPort,
        retrieval: TargetEvidenceRetrievalPort,
        field_executor: WeaponryFieldExecutor,
        callbacks: WeaponryCallbackPort,
        resources: WeaponryResourceStorePort,
        document_snapshots: WeaponryTaskDocumentSnapshotStorePort,
        result_observer=None,
    ) -> None:
        if not isinstance(steps, WeaponryStepRuntime):
            raise TypeError("steps 必须是 WeaponryStepRuntime")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        for name, dependency, protocol in (
            ("progress_publisher", progress_publisher, ProgressPublisherPort),
            ("retrieval", retrieval, TargetEvidenceRetrievalPort),
            ("callbacks", callbacks, WeaponryCallbackPort),
            ("resources", resources, WeaponryResourceStorePort),
            (
                "document_snapshots",
                document_snapshots,
                WeaponryTaskDocumentSnapshotStorePort,
            ),
        ):
            if not isinstance(dependency, protocol):
                raise TypeError(f"{name} 未实现所需 Port")
        if not isinstance(field_executor, WeaponryFieldExecutor):
            raise TypeError("field_executor 必须是 WeaponryFieldExecutor")
        if result_observer is not None and not callable(result_observer):
            raise TypeError("result_observer 必须可调用或为 None")
        self._steps = steps
        self._clock = clock
        self._progress_publisher = progress_publisher
        self._retrieval = retrieval
        self._base_field_executor = field_executor
        self._callbacks = callbacks
        self._resources = resources
        self._document_snapshots = document_snapshots
        self._result_observer = result_observer or (lambda result: None)
        self._last_result: RunWeaponryResult | None = None

    @property
    def last_result(self) -> RunWeaponryResult | None:
        return self._last_result

    def run(self, context: TaskWorkflowContextPort) -> None:
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")
        execution = context.loaded_input.snapshot
        snapshot = execution.input_snapshot
        task_id = execution.task_id
        if (
            execution.task_type != WEAPONRY_TASK_TYPE
            or not isinstance(snapshot, WeaponryInputSnapshot)
            or snapshot.schema_version != WEAPONRY_INPUT_SCHEMA_VERSION
            or snapshot.task_id != task_id.value
            or execution.business_ref
            != TaskBusinessRef(WEAPONRY_TASK_TYPE, snapshot.business_key)
            or snapshot.accepted_at != execution.accepted_at
            or snapshot.trace_id != execution.trace_id
        ):
            raise WeaponryPortContractError("Weaponry v2 Workflow 冻结输入身份不一致")

        scope: TargetEvidenceScope | None = None
        resource_record_created = False
        current_step: ActiveWeaponryStep | None = None
        selected_evidence_count = 0
        model_call_count = 0
        diagnostic_error_codes: list[str] = []
        try:
            current_step = self._steps.begin(
                context,
                step_key="resource.record.begin",
                idempotency_key=f"weaponry:{task_id.value}:resource-record",
                component_mutation=lambda unit_of_work: unit_of_work.resources.create(
                    WeaponryResourceRecord(task_id, execution.business_ref)
                ),
            )
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="weaponry_resource_recorded_v1",
                    result_ref=f"weaponry-resource:{task_id.value}",
                    result_digest=_digest(
                        {"task_id": task_id.value, "business_key": snapshot.business_key}
                    ),
                ),
            )
            current_step = None
            resource_record_created = True
            self._update_progress(context, execution, *_PROGRESS_PREPARING)

            current_step = self._steps.begin(
                context,
                step_key="document_scope.load",
                idempotency_key=(
                    f"weaponry:{task_id.value}:document-scope:"
                    f"{context.loaded_input.input_payload_fingerprint}"
                ),
            )
            persisted_documents = self._document_snapshots.list_for_task(task_id)
            if not persisted_documents or persisted_documents != snapshot.document_scope.documents:
                raise WeaponryPortContractError(
                    "Weaponry 文档组件快照与冻结 Input v2 不一致"
                )
            documents_digest = _digest(
                tuple(
                    (item.sequence_no, item.document_key, item.external_document_ref)
                    for item in persisted_documents
                )
            )
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="weaponry_document_scope_loaded_v1",
                    result_ref=f"weaponry-documents:v1:{documents_digest}",
                    result_digest=documents_digest,
                ),
            )
            current_step = None

            workspace_step = self._steps.begin(
                context,
                step_key="rag.workspace.create",
                idempotency_key=f"weaponry:{task_id.value}:retrieval-workspace",
            )
            bind_steps = tuple(
                self._steps.begin(
                    context,
                    step_key=f"rag.document.bind:{document.sequence_no}",
                    idempotency_key=(
                        f"weaponry:{task_id.value}:document-bind:"
                        f"{document.sequence_no}:{document.document_key}"
                    ),
                )
                for document in persisted_documents
            )
            try:
                scope = self._retrieval.open_scope(
                    OpenTargetEvidenceScope(
                        task_id=task_id,
                        document_scope=snapshot.document_scope,
                        policy=snapshot.evidence_selection_policy,
                    )
                )
                self._validate_scope(scope, task_id, snapshot)
            except BaseException as exc:
                unknown = self._preserve_scene(exc)
                if not unknown:
                    for active in bind_steps:
                        self._steps.fail(
                            context,
                            active,
                            error_code=self._error_code(exc),
                            outcome_unknown=False,
                        )
                # 结果未知时只由 workspace 主 Step 原子隔离整次 Attempt；其余预声明
                # bind Step 保留现场，由 Recovery Fact Collector 一并识别，不能在首个
                # isolation 之后继续拿已经失效的 Authority 补写多笔失败。
                self._steps.fail(
                    context,
                    workspace_step,
                    error_code=self._error_code(exc),
                    outcome_unknown=unknown,
                )
                raise
            scope_digest = _digest(
                {
                    "scope_ref": scope.scope_ref,
                    "documents": scope.allowed_document_keys,
                    "profile": scope.selection_profile_id,
                }
            )
            self._steps.succeed(
                context,
                workspace_step,
                TaskStepCheckpoint(
                    code="weaponry_workspace_created_v1",
                    result_ref=scope.scope_ref,
                    result_digest=scope_digest,
                ),
            )
            for document, active in zip(persisted_documents, bind_steps, strict=True):
                digest = _digest(
                    (scope.scope_ref, document.sequence_no, document.document_key)
                )
                self._steps.succeed(
                    context,
                    active,
                    TaskStepCheckpoint(
                        code="weaponry_document_bound_v1",
                        result_ref=f"{scope.scope_ref}:{document.document_key}",
                        result_digest=digest,
                    ),
                )
            self._register_retrieval_scope(task_id, scope)
            self._update_progress(context, execution, *_PROGRESS_SCOPE_READY)

            field_executor = WeaponryFieldExecutor(
                retrieval=self._base_field_executor.retrieval,
                extraction=self._base_field_executor.extraction,
                guidance=self._base_field_executor.guidance,
                translation=self._base_field_executor.translation,
                audit=self._base_field_executor.audit,
                step_observer=WeaponryFieldStepObserver(
                    context=context,
                    runtime=self._steps,
                ),
            )
            field_results = []
            total_fields = len(snapshot.fields)
            for field_sequence, field in enumerate(snapshot.fields, start=1):
                executed = field_executor.execute(
                    task_id=task_id,
                    business_ref=execution.business_ref,
                    snapshot=snapshot,
                    scope=scope,
                    field=field,
                    field_sequence=field_sequence,
                    is_current=lambda: self._authority_is_current(context),
                )
                field_results.append(executed.result)
                selected_evidence_count += executed.selected_evidence_count
                model_call_count += executed.model_call_count
                diagnostic_error_codes.extend(executed.diagnostic_error_codes)
                self._update_progress(
                    context,
                    execution,
                    0.15 + (field_sequence / total_fields) * 0.75,
                    f"正在提取字段 ({field_sequence}/{total_fields})",
                )

            self._update_progress(context, execution, *_PROGRESS_CLEANUP)
            result = WeaponryResult(
                identity=WeaponryExecutionIdentity(task_id.value, snapshot.architecture_id),
                status=WEAPONRY_STATUS_SUCCEEDED,
                fields=tuple(field_results),
                message=WEAPONRY_SUCCESS_MESSAGE,
            )
            validate_weaponry_result_completeness(snapshot, result)
            self._commit_result(context, execution, result)
            self._publish_terminal_progress(execution, "succeeded")
            callback_outcome = self._deliver_v2_callback(execution, snapshot, result)
            cleanup_state = self._finalize_resources(
                task_id,
                scope=scope,
                resource_record_created=resource_record_created,
            )
            self._finish_result(
                RunWeaponryResult(
                    task_id,
                    RunWeaponryOutcome.SUCCEEDED,
                    callback_outcome=callback_outcome,
                    cleanup_state=cleanup_state,
                    selected_evidence_count=selected_evidence_count,
                    model_call_count=model_call_count,
                    diagnostic_error_codes=tuple(dict.fromkeys(diagnostic_error_codes)),
                )
            )
        except TaskExecutionStopRequested:
            logger.warning(
                "Weaponry v2 Workflow 已按执行停止信号退出，保留现场: task_id=%s",
                task_id,
            )
            raise
        except WeaponryTaskPersistenceError:
            logger.critical(
                "Weaponry v2 持久化结果不确定，禁止补写第二终态: task_id=%s",
                task_id,
                exc_info=True,
            )
            raise
        except Exception as exc:
            if current_step is not None:
                self._steps.fail(
                    context,
                    current_step,
                    error_code=self._error_code(exc),
                    outcome_unknown=self._preserve_scene(exc),
                )
            if self._preserve_scene(exc):
                cleanup_state = self._quarantine_resources(
                    task_id,
                    self._error_code(exc),
                ) if resource_record_created else "not_created"
                self._finish_result(
                    RunWeaponryResult(
                        task_id,
                        RunWeaponryOutcome.RECOVERY_REQUIRED,
                        error_code=self._error_code(exc),
                        cleanup_state=cleanup_state,
                        selected_evidence_count=selected_evidence_count,
                        model_call_count=model_call_count,
                        diagnostic_error_codes=tuple(dict.fromkeys(diagnostic_error_codes)),
                    )
                )
                return
            self._finish_failed(
                context,
                execution,
                snapshot,
                exc,
                scope=scope,
                resource_record_created=resource_record_created,
                selected_evidence_count=selected_evidence_count,
                model_call_count=model_call_count,
                diagnostic_error_codes=tuple(dict.fromkeys(diagnostic_error_codes)),
            )

    def _commit_result(self, context, execution, result: WeaponryResult) -> None:
        payload = result.to_callback()
        result_digest = _digest(payload.to_public_dict())
        mapping = self._steps.begin(
            context,
            step_key="result.map",
            idempotency_key=f"weaponry:{execution.task_id.value}:result:{result_digest}",
        )
        self._steps.succeed(
            context,
            mapping,
            TaskStepCheckpoint(
                code="weaponry_result_mapped_v1",
                result_ref=f"weaponry-result:v1:{result_digest}",
                result_digest=result_digest,
            ),
        )
        terminal = self._steps.begin(
            context,
            step_key="terminal.commit",
            idempotency_key=f"weaponry:{execution.task_id.value}:terminal:{result_digest}",
        )
        self._steps.finish(
            context,
            terminal,
            business_ref=execution.business_ref,
            succeeded=result.status == WEAPONRY_STATUS_SUCCEEDED,
            public_status=result.status,
            message=("解析完成" if result.status == WEAPONRY_STATUS_SUCCEEDED else WEAPONRY_FAILURE_MESSAGE),
            result_ref=f"weaponry-result:v1:{result_digest}",
            terminal_checkpoint=TaskStepCheckpoint(
                code="weaponry_terminal_committed_v1",
                result_ref=f"weaponry-result:v1:{result_digest}",
                result_digest=result_digest,
            ),
            component_mutation=lambda unit_of_work: unit_of_work.results.save(
                task_id=execution.task_id,
                business_ref=execution.business_ref,
                payload=payload,
                created_at=self._clock.now_utc(),
            ),
        )

    def _finish_failed(
        self,
        context,
        execution,
        snapshot,
        exc,
        *,
        scope,
        resource_record_created,
        selected_evidence_count,
        model_call_count,
        diagnostic_error_codes,
    ) -> None:
        error_code = self._error_code(exc)
        logger.error(
            "Weaponry v2 Workflow 执行失败: task_id=%s architecture_id=%s "
            "error_code=%s error_type=%s",
            execution.task_id,
            snapshot.architecture_id,
            error_code,
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        result = WeaponryResult(
            identity=WeaponryExecutionIdentity(
                execution.task_id.value,
                snapshot.architecture_id,
            ),
            status=WEAPONRY_STATUS_FAILED,
            message=WEAPONRY_FAILURE_MESSAGE,
        )
        self._commit_result(context, execution, result)
        self._publish_terminal_progress(execution, "failed")
        callback_outcome = self._deliver_v2_callback(execution, snapshot, result)
        cleanup_state = self._finalize_resources(
            execution.task_id,
            scope=scope,
            resource_record_created=resource_record_created,
        )
        self._finish_result(
            RunWeaponryResult(
                execution.task_id,
                RunWeaponryOutcome.FAILED,
                error_code=error_code,
                callback_outcome=callback_outcome,
                cleanup_state=cleanup_state,
                selected_evidence_count=selected_evidence_count,
                model_call_count=model_call_count,
                diagnostic_error_codes=diagnostic_error_codes,
            )
        )

    def _update_progress(self, context, execution, progress: float, message: str) -> None:
        self._steps.update_progress(
            context,
            progress=progress,
            message=message,
            public_status=WEAPONRY_PUBLIC_PROCESSING_STATUS,
        )
        self._publish_progress(execution, progress, message, "running")

    def _publish_progress(self, execution, progress, message, internal_state) -> None:
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
            logger.exception(
                "Weaponry v2 Progress 通知失败，持久事实不回滚: task_id=%s",
                execution.task_id,
            )

    def _publish_terminal_progress(self, execution, internal_state: str) -> None:
        self._publish_progress(execution, 1.0, "", internal_state)

    def _deliver_v2_callback(self, execution, snapshot, result) -> str:
        try:
            acquired = self._callbacks.acquire(
                AcquireWeaponryCallback(
                    task_id=execution.task_id,
                    architecture_id=snapshot.architecture_id,
                )
            )
            if acquired.outcome is not WeaponryCallbackAcquireOutcome.ACQUIRED:
                return acquired.outcome.value
            lease = acquired.lease
            if lease is None:
                raise WeaponryPortContractError("Callback acquire 缺少 Lease")
            payload = result.to_callback()
            delivery = self._callbacks.deliver(DeliverWeaponryCallback(lease, payload))
            if delivery.outcome is WeaponryCallbackDeliveryOutcome.STALE:
                return delivery.outcome.value
            if not self._callbacks.complete(lease, delivery, payload):
                raise WeaponryPortContractError("Callback Guard 完成权已过期")
            return delivery.outcome.value
        except Exception:
            logger.exception(
                "Weaponry v2 Callback 异常，业务终态保持: task_id=%s",
                execution.task_id,
            )
            return "port_error"

    @staticmethod
    def _authority_is_current(context: TaskWorkflowContextPort) -> bool:
        # FieldExecutor 会在每个慢调用前后执行该探针。正常停机与失权一样必须
        # 立即中断后续副作用，但使用 STOPPED 明确保留二者的诊断差异。
        if context.stop_requested():
            raise TaskExecutionStopRequested(
                LeaseSupervisorResult(LeaseSupervisorOutcome.STOPPED)
            )
        return bool(context.session.run_authorized(lambda authority: True))

    @staticmethod
    def _validate_scope(scope, task_id: TaskId, snapshot: WeaponryInputSnapshot) -> None:
        expected_keys = tuple(
            item.document_key for item in snapshot.document_scope.documents
        )
        if (
            not isinstance(scope, TargetEvidenceScope)
            or scope.task_id != task_id
            or scope.allowed_document_keys != expected_keys
            or scope.selection_profile_id != snapshot.evidence_selection_policy.profile_id
            or scope.provider_fingerprint
            != snapshot.evidence_selection_policy.provider_fingerprint
            or scope.embedding_fingerprint
            != snapshot.evidence_selection_policy.embedding_fingerprint
        ):
            raise WeaponryScenePreservationError(
                "Retrieval Scope 与冻结身份不一致",
                error_code="retrieval_scope_identity_invalid",
            )

    @staticmethod
    def _preserve_scene(exc: BaseException) -> bool:
        return bool(
            isinstance(exc, WeaponryScenePreservationError)
            or (
                isinstance(exc, WeaponryExternalOperationError)
                and exc.outcome is WeaponryExternalOutcome.OUTCOME_UNKNOWN
            )
        )

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        if isinstance(exc, WeaponryApplicationError):
            return exc.code
        return str(getattr(exc, "error_code", "")).strip() or "weaponry_unexpected_error"

    def _finish_result(self, result: RunWeaponryResult) -> None:
        self._last_result = result
        try:
            self._result_observer(result)
        except Exception:
            logger.warning(
                "Weaponry v2 内部结果观测失败，不改变业务事实: task_id=%s",
                result.task_id,
                exc_info=True,
            )


__all__ = ["RunWeaponryV2Workflow"]
