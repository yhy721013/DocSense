"""阶段 2-6 基于完整 Authority 与持久 Step 的 Analysis v2 Workflow。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from app.modules.analysis.domain.architecture_recall import (
    ArchitecturePromptBudgetError,
    ArchitectureRecallError,
)
from app.modules.analysis.domain.architecture_tree import ArchitectureTreeValidationError
from app.modules.analysis.domain.callback_payloads import build_file_callback_payload
from app.modules.analysis.domain.errors import AnalysisContractError, ArchitectureContractError
from app.modules.analysis.domain.execution_profile import AnalysisExecutionProfile
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_BUSINESS_TYPE,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V5,
    AnalysisTaskInputV5,
    AnalysisTranslationProfile,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
    AnalysisAuditPort,
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackPort,
    AnalysisCallbackRequest,
    AnalysisExecutionRef,
    AnalysisDocumentPreparationPort,
    AnalysisDocumentPreparationRequest,
    AnalysisKnowledgePort,
    AnalysisRagExecutionError,
    AnalysisRagPort,
    AnalysisRagPortFactory,
    AnalysisRagResult,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenRequest,
    AnalysisRagSessionOpenResult,
    AnalysisResourcePort,
    AnalysisTranslationPort,
    AnalysisTaskWorkspacePort,
    AnalysisSourceAcquisitionPort,
    AnalysisSourceAcquisitionRequest,
    AnalysisSourceResolutionRequest,
    PreparedAnalysisDocument,
)
from app.modules.tasks.domain import ProgressKey, TaskBusinessRef, TaskStepCheckpoint
from app.modules.tasks.ports import (
    ProgressPublication,
    ProgressPublisherPort,
    TaskExecutionStopRequested,
    TaskStepContinuationDraft,
    TaskWorkflowContextPort,
    TaskWorkflowRunnerPort,
)
from .audit_lifecycle import _AnalysisAuditLifecycle
from .external_step_ports import AnalysisKnowledgeStepPort, AnalysisTranslationStepPort
from .knowledge_handoff import _AnalysisKnowledgeHandoff
from .model_workflow import _AnalysisModelWorkflow
from .rag_step_observer import AnalysisRagStepObserver
from .recover_resources import AnalysisResourceLifecycle, AnalysisResourceLifecycleError
from .step_runtime import ActiveAnalysisStep, AnalysisStepRuntime
from .workflow_models import (
    AnalysisApplicationContractError,
    AnalysisTaskPersistenceError,
    RunAnalysisOutcome,
    RunAnalysisResult,
    _AnalysisKnownFailure,
    _RagWorkflowState,
    _build_rag_upload_descriptor,
)


logger = logging.getLogger(__name__)

_PUBLIC_PROCESSING_STATUS = "1"
_PUBLIC_SUCCEEDED_STATUS = "2"
_PUBLIC_FAILED_STATUS = "3"


class _DeferredFactoryScope:
    """把 Transport 释放延后到 Workflow 失败收敛之后。

    业务异常必须先在仍可使用的任务级 RAG Port 上追加审计/close；原 Factory 的
    ``__exit__`` 只负责本地 Transport 释放，且释放异常不能覆盖已提交业务终态。
    """

    def __init__(self, context_manager) -> None:  # type: ignore[no-untyped-def]
        self._context_manager = context_manager
        self._entered = False

    def __enter__(self):  # type: ignore[no-untyped-def]
        value = self._context_manager.__enter__()
        self._entered = True
        return value

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        # 不吞异常；只把真实 Factory exit 延后到外层 finally。
        return False

    def close(self, *, task_id) -> None:  # type: ignore[no-untyped-def]
        if not self._entered:
            return
        self._entered = False
        try:
            self._context_manager.__exit__(None, None, None)
        except Exception as error:
            logger.critical(
                "Analysis v2 RAG Factory 退出失败，既有业务事实保持: "
                "task_id=%s error_type=%s",
                task_id,
                type(error).__name__,
                exc_info=True,
            )


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _digest_text(canonical)


class RunAnalysisV2Workflow(TaskWorkflowRunnerPort):
    """只消费 Runtime 解码的 Input v5 与可轮换 Authority Session。

    本类不 claim、不 start、不读环境，也不从旧 ``LLMTaskService`` 补造 Authority。
    所有 Task/Step/Progress/Terminal 条件写均经 ``AnalysisStepRuntime``；网络、文件、
    模型、审计和 Callback 始终在事务外执行。
    """

    def __init__(
        self,
        *,
        steps: AnalysisStepRuntime,
        progress_publisher: ProgressPublisherPort,
        workspaces: AnalysisTaskWorkspacePort,
        files: AnalysisSourceAcquisitionPort,
        rag_factory: AnalysisRagPortFactory,
        knowledge: AnalysisKnowledgePort,
        audit: AnalysisAuditPort,
        translation: AnalysisTranslationPort,
        resources: AnalysisResourcePort,
        callbacks: AnalysisCallbackPort,
        callback_url: str,
        execution_profile: AnalysisExecutionProfile,
        translation_profile: AnalysisTranslationProfile,
        resource_close_running_grace_seconds: float = 300.0,
        maintenance_wakeup: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(steps, AnalysisStepRuntime):
            raise TypeError("steps 必须是 AnalysisStepRuntime")
        for name, dependency, protocol in (
            ("progress_publisher", progress_publisher, ProgressPublisherPort),
            ("workspaces", workspaces, AnalysisTaskWorkspacePort),
            ("source_files", files, AnalysisSourceAcquisitionPort),
            ("document_files", files, AnalysisDocumentPreparationPort),
            ("rag_factory", rag_factory, AnalysisRagPortFactory),
            ("knowledge", knowledge, AnalysisKnowledgePort),
            ("audit", audit, AnalysisAuditPort),
            ("translation", translation, AnalysisTranslationPort),
            ("resources", resources, AnalysisResourcePort),
            ("callbacks", callbacks, AnalysisCallbackPort),
        ):
            if not isinstance(dependency, protocol):
                raise TypeError(f"{name} 未实现所需 Port")
        if not isinstance(callback_url, str):
            raise TypeError("callback_url 必须是 str")
        if not isinstance(execution_profile, AnalysisExecutionProfile):
            raise TypeError("execution_profile 必须是 AnalysisExecutionProfile")
        if not isinstance(translation_profile, AnalysisTranslationProfile):
            raise TypeError("translation_profile 必须是 TranslationProfile")
        if (
            isinstance(resource_close_running_grace_seconds, bool)
            or not isinstance(resource_close_running_grace_seconds, (int, float))
        ):
            raise TypeError("resource_close_running_grace_seconds 必须是正有限数字")
        normalized_close_grace = float(resource_close_running_grace_seconds)
        if (
            normalized_close_grace != normalized_close_grace
            or normalized_close_grace in (float("inf"), float("-inf"))
            or not 0.0 < normalized_close_grace <= 7 * 24 * 60 * 60
        ):
            raise ValueError("resource_close_running_grace_seconds 必须是正有限数字")
        if maintenance_wakeup is not None and not callable(maintenance_wakeup):
            raise TypeError("maintenance_wakeup 必须可调用或为 None")
        self._steps = steps
        self._progress_publisher = progress_publisher
        self._workspaces = workspaces
        self._source_files = files
        self._document_files = files
        self._rag_factory = rag_factory
        self._knowledge = knowledge
        self._translation = translation
        self._resources = resources
        self._callbacks = callbacks
        self._callback_url = callback_url.strip()
        self._execution_profile = execution_profile
        self._translation_profile = translation_profile
        self._resource_close_running_grace_seconds = normalized_close_grace
        self._maintenance_wakeup = maintenance_wakeup or (lambda: None)
        self._model = _AnalysisModelWorkflow()
        self._audit = _AnalysisAuditLifecycle(audit)
        self._last_result: RunAnalysisResult | None = None

    @property
    def last_result(self) -> RunAnalysisResult | None:
        return self._last_result

    def run(self, context: TaskWorkflowContextPort) -> None:
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")
        execution_snapshot = context.loaded_input.snapshot
        snapshot = execution_snapshot.input_snapshot
        task_id = execution_snapshot.task_id
        expected_ref = TaskBusinessRef(ANALYSIS_BUSINESS_TYPE, getattr(snapshot, "file_name", ""))
        # 该门禁发生在 Workspace 创建、Source 下载和任何远端调用之前。历史 v1~v4
        # 只可诊断读取，普通 Worker 不得用当前环境为其回填 Profile。
        if (
            execution_snapshot.task_type != ANALYSIS_BUSINESS_TYPE
            or not isinstance(snapshot, AnalysisTaskInputV5)
            or snapshot.schema_version != ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V5
            or snapshot.execution_profile != self._execution_profile
            or snapshot.translation_profile != self._translation_profile
            or snapshot.task_id != task_id.value
            or execution_snapshot.business_ref != expected_ref
            or snapshot.accepted_at != execution_snapshot.accepted_at
            or snapshot.trace_id != execution_snapshot.trace_id
        ):
            raise AnalysisApplicationContractError("Analysis v2 Workflow 冻结输入身份不一致")

        execution = AnalysisExecutionRef(
            task_id=task_id,
            file_name=snapshot.file_name,
            batch_id=snapshot.batch_id,
            batch_sequence=snapshot.batch_sequence,
        )
        state = _RagWorkflowState()
        current_steps: list[ActiveAnalysisStep] = []
        rag_observer: AnalysisRagStepObserver | None = None
        knowledge_step_port: AnalysisKnowledgeStepPort | None = None
        rag_scope: _DeferredFactoryScope | None = None
        started_at = time.perf_counter()
        initial_continuation = TaskStepContinuationDraft(
            schema_version=1,
            input_payload_fingerprint=context.loaded_input.input_payload_fingerprint,
            execution_profile_fingerprint=self._execution_profile.fingerprint,
            payload={
                "business_key": snapshot.file_name,
                "resolver": "analysis.source_download.v1",
                "step_key": "source.download",
                "task_id": task_id.value,
            },
        )
        restored = self._steps.load_resume_continuation(
            context,
            execution_profile_fingerprint=self._execution_profile.fingerprint,
        )
        resume_target = restored.step_key if restored is not None else ""
        if restored is not None:
            if restored.step_key == "source.download":
                if restored.draft != initial_continuation:
                    raise AnalysisTaskPersistenceError(
                        "Analysis Source 续跑快照解析结果不一致"
                    )
                initial_continuation = restored.draft
            elif restored.step_key == "document.prepare":
                payload = restored.draft.payload
                if (
                    payload.get("resolver") != "analysis.document_prepare.v1"
                    or payload.get("step_key") != "document.prepare"
                    or payload.get("task_id") != task_id.value
                    or not isinstance(payload.get("source_basename"), str)
                    or not isinstance(payload.get("source_sha256"), str)
                    or restored.draft.predecessor_checkpoint_digest
                    != payload.get("source_sha256")
                ):
                    raise AnalysisTaskPersistenceError(
                        "Analysis Document 续跑快照字段不完整或身份不一致"
                    )
            else:
                raise AnalysisTaskPersistenceError(
                    "Analysis 恢复目标尚未注册真实业务解析器"
                )
        try:
            source_identity = _digest_text(snapshot.file_path)
            if resume_target == "document.prepare":
                # Resolver 只读复核已有目录和 Source，不能在目标 Step intent 前补建目录、
                # 重新下载或调用 DocumentProcessing。
                workspace = self._workspaces.resolve(execution)
                assert restored is not None
                restored_payload = restored.draft.payload
                acquired = self._source_files.resolve_source(
                    AnalysisSourceResolutionRequest(
                        execution=execution,
                        task_root=workspace.root_path,
                        source_basename=str(restored_payload["source_basename"]),
                        source_sha256=str(restored_payload["source_sha256"]),
                    )
                )
                document_continuation = restored.draft
            else:
                source_step = self._steps.begin(
                    context,
                    step_key="source.download",
                    idempotency_key=f"analysis:{task_id.value}:source:{source_identity}",
                    continuation=initial_continuation,
                )
                current_steps.append(source_step)
                workspace = self._workspaces.create(execution)
                if workspace.execution != execution:
                    raise AnalysisApplicationContractError("任务目录不属于当前 execution")
                acquired = self._source_files.acquire_source(
                    AnalysisSourceAcquisitionRequest(
                        execution=execution,
                        source_url=snapshot.file_path,
                        task_root=workspace.root_path,
                    )
                )
                if acquired.execution != execution:
                    raise AnalysisApplicationContractError("下载 Source 不属于当前 execution")
                source_digest = acquired.source_sha256
                self._steps.succeed(
                    context,
                    source_step,
                    TaskStepCheckpoint(
                        code="source_downloaded_v1",
                        result_ref=f"analysis-source:v1:{source_digest}",
                        result_digest=source_digest,
                    ),
                )
                current_steps.remove(source_step)
                document_continuation = TaskStepContinuationDraft(
                    schema_version=1,
                    input_payload_fingerprint=(
                        context.loaded_input.input_payload_fingerprint
                    ),
                    execution_profile_fingerprint=self._execution_profile.fingerprint,
                    predecessor_checkpoint_digest=source_digest,
                    payload={
                        "resolver": "analysis.document_prepare.v1",
                        "source_basename": acquired.source_basename,
                        "source_sha256": acquired.source_sha256,
                        "step_key": "document.prepare",
                        "task_id": task_id.value,
                    },
                )
            document_step = self._steps.begin(
                context,
                step_key="document.prepare",
                idempotency_key=(
                    f"analysis:{task_id.value}:prepared:{source_identity}:"
                    f"{snapshot.document_processing_policy.processing_policy_fingerprint}"
                ),
                continuation=document_continuation,
            )
            current_steps.append(document_step)
            prepared = self._document_files.prepare_document(
                AnalysisDocumentPreparationRequest(
                    execution=execution,
                    task_root=workspace.root_path,
                    source=acquired,
                    document_processing_policy=snapshot.document_processing_policy,
                )
            )
            self._require_prepared(prepared, execution)
            upload_descriptor = _build_rag_upload_descriptor(
                snapshot=snapshot,
                prepared=prepared,
            )
            state.upload_descriptor = upload_descriptor
            prepared_digest = self._prepared_digest(prepared)
            self._steps.succeed(
                context,
                document_step,
                TaskStepCheckpoint(
                    code="document_prepared_v1",
                    result_ref=f"analysis-prepared:v1:{prepared_digest}",
                    result_digest=prepared_digest,
                ),
                component_mutation=lambda unit_of_work: self._register_resource(
                    unit_of_work,
                    execution=execution,
                    workspace_root=workspace.root_path,
                    prepared=prepared,
                    state=state,
                    upload_descriptor=upload_descriptor,
                ),
            )
            current_steps.remove(document_step)
            self._update_progress(context, execution_snapshot, 0.35, "正在执行文档解析")

            plan = self._model.build_plan(snapshot, prepared.original_text)
            recall_step = self._steps.begin(
                context,
                step_key="recall.reserve",
                idempotency_key=f"analysis-recall:{task_id.value}",
            )
            current_steps.append(recall_step)
            try:
                state.recall_receipt = self._audit.reserve_recall(execution, plan)
            except Exception as error:
                self._steps.fail(
                    context,
                    recall_step,
                    error_code="analysis_recall_reserve_outcome_unknown",
                    outcome_unknown=True,
                )
                current_steps.remove(recall_step)
                raise AnalysisTaskPersistenceError("Analysis 召回预留结果未知") from error
            recall_digest = _digest_json(
                {
                    "audit_id": state.recall_receipt.audit_id,
                    "idempotency_key": state.recall_receipt.idempotency_key,
                    "version": state.recall_receipt.version,
                }
            )
            self._steps.succeed(
                context,
                recall_step,
                TaskStepCheckpoint(
                    code="recall_reserved_v1",
                    result_ref=f"analysis-recall:v1:{state.recall_receipt.audit_id}",
                    result_digest=recall_digest,
                ),
                component_mutation=lambda unit_of_work: self._record_recall_resource(
                    unit_of_work,
                    execution,
                    state,
                ),
            )
            current_steps.remove(recall_step)

            rag_scope = _DeferredFactoryScope(self._rag_factory.create(execution))
            with rag_scope as raw_rag:
                if not isinstance(raw_rag, AnalysisRagPort):
                    raise AnalysisApplicationContractError("AnalysisRagPortFactory 返回类型错误")
                rag_observer = AnalysisRagStepObserver(
                    delegate=raw_rag,
                    runtime=self._steps,
                    context=context,
                    result_component_mutation=(
                        lambda unit_of_work, result: self._record_rag_result(
                            unit_of_work,
                            execution,
                            state,
                            result,
                        )
                    ),
                    failure_component_mutation=(
                        lambda unit_of_work: self._record_rag_failure(
                            unit_of_work,
                            execution,
                            state,
                        )
                    ),
                    upload_intent_component_mutation=(
                        lambda unit_of_work: self._prepare_resource_upload(
                            unit_of_work,
                            execution,
                        )
                    ),
                )
                opened = rag_observer.open_session(
                    AnalysisRagSessionOpenRequest(
                        execution=execution,
                        upload_path=prepared.upload_path,
                        upload_descriptor=upload_descriptor,
                    )
                )
                state.session = opened.session
                state.opened = True
                state.lifecycle_events.extend(opened.lifecycle_events)
                architecture_id, parsed_result = self._model.run_model_workflow(
                    execution=execution,
                    snapshot=snapshot,
                    plan=plan,
                    state=state,
                    rag=rag_observer,
                )

                self._finalize_recall_success(
                    context,
                    state,
                    architecture_id=architecture_id,
                    returned_rank=self._model.returned_rank(plan, architecture_id),
                    started_at=started_at,
                )
                self._persist_interaction(
                    context,
                    execution,
                    snapshot,
                    state,
                    outcome=AnalysisAuditOutcome.SUCCEEDED,
                    error_code="",
                )
                if rag_observer.finalize_failures_after_audit():
                    self._last_result = RunAnalysisResult(
                        task_id,
                        RunAnalysisOutcome.RECOVERY_REQUIRED,
                        error_code="analysis_rag_outcome_unknown",
                        stage="analysis_extraction",
                    )
                    return

                result_step = self._steps.begin(
                    context,
                    step_key="result.map",
                    idempotency_key=(
                        f"analysis:{task_id.value}:result:"
                        f"{_digest_json(parsed_result)}:{_digest_json(snapshot.effective_ranges.to_dict())}"
                    ),
                )
                current_steps.append(result_step)
                mapped_result = self._model.map_result(
                    parsed_result,
                    plan,
                    original_text=prepared.original_text,
                    architecture_id=architecture_id,
                    internal_prepared_basename=prepared.internal_prepared_basename,
                )
                mapped_digest = _digest_json(mapped_result)
                self._steps.succeed(
                    context,
                    result_step,
                    TaskStepCheckpoint(
                        code="analysis_result_mapped_v1",
                        result_ref=f"analysis-mapped-result:v1:{mapped_digest}",
                        result_digest=mapped_digest,
                    ),
                )
                current_steps.remove(result_step)

                knowledge_step_port = AnalysisKnowledgeStepPort(
                    delegate=self._knowledge,
                    runtime=self._steps,
                    context=context,
                    result_component_mutation=(
                        lambda unit_of_work, request, result: self._record_knowledge_resource(
                            unit_of_work,
                            execution,
                            request,
                            result,
                        )
                    ),
                    failure_component_mutation=(
                        lambda unit_of_work: self._quarantine_resource(
                            unit_of_work,
                            execution,
                            stage="knowledge_index",
                            reason="knowledge_unclassified_outcome_unknown",
                        )
                    ),
                )
                translation_key = _digest_json(
                    {
                        "task_id": task_id.value,
                        "prepared": prepared_digest,
                        "profile_id": snapshot.translation_profile.profile_id,
                        "target_language": "Chinese",
                    }
                )
                translation_step_port = AnalysisTranslationStepPort(
                    delegate=self._translation,
                    runtime=self._steps,
                    context=context,
                    translation_key=translation_key,
                )
                handoff = _AnalysisKnowledgeHandoff(
                    knowledge_step_port,
                    translation_step_port,
                )
                handoff.persist_knowledge(
                    execution=execution,
                    snapshot=snapshot,
                    plan=plan,
                    state=state,
                    mapped_result=mapped_result,
                )
                self._update_progress(context, execution_snapshot, 0.65, "正在翻译文档")
                handoff.enrich_translations(
                    execution=execution,
                    prepared=prepared,
                    mapped_result=mapped_result,
                )
                mapped_result = self._model.sanitize_public_result(
                    mapped_result,
                    internal_prepared_basename=prepared.internal_prepared_basename,
                    business_file_name=(
                        snapshot.original_file_name
                        if snapshot.original_file_name.strip()
                        else snapshot.file_name
                    ),
                )
                self._update_progress(context, execution_snapshot, 0.95, "翻译完成，准备回调")
                payload = FrozenJsonObject.from_mapping(
                    build_file_callback_payload(
                        snapshot.file_name,
                        mapped_result,
                        status=_PUBLIC_SUCCEEDED_STATUS,
                    ),
                    name="analysis_v2_success_callback",
                )
                self._finish_terminal(
                    context,
                    execution_snapshot.business_ref,
                    payload,
                    execution=execution,
                    state=state,
                    succeeded=True,
                    public_status=_PUBLIC_SUCCEEDED_STATUS,
                    message="解析完成",
                )
                self._publish_progress(execution_snapshot, 1.0, "", "succeeded")
                self._deliver_callback(execution, payload)
                self._close_audited_resources(execution, state, rag_observer)
                self._wake_maintenance(task_id.value)
                self._last_result = RunAnalysisResult(task_id, RunAnalysisOutcome.SUCCEEDED)
        except TaskExecutionStopRequested:
            logger.warning("Analysis v2 Workflow 已按停止信号退出: task_id=%s", task_id)
            raise
        except AnalysisTaskPersistenceError:
            logger.critical(
                "Analysis v2 持久事实结果不确定，禁止二次终态: task_id=%s",
                task_id,
                exc_info=True,
            )
            raise
        except Exception as error:
            if self._last_result is not None:
                # ``with`` 的 __exit__ 发生在业务终态、Callback 和 close 协调之后。
                # 此时任何 Transport 释放异常都不能反向创建第二个 terminal Step。
                logger.critical(
                    "Analysis v2 RAG Factory 退出失败，已保留既有业务结果: "
                    "task_id=%s outcome=%s error_type=%s",
                    task_id,
                    self._last_result.outcome.value,
                    type(error).__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )
                return
            self._handle_failure(
                context,
                execution_snapshot,
                snapshot,
                execution,
                state,
                error,
                current_steps=current_steps,
                rag_observer=rag_observer,
                knowledge_recovery_required=(
                    knowledge_step_port.recovery_required
                    if knowledge_step_port is not None
                    else False
                ),
                started_at=started_at,
            )
        finally:
            if rag_scope is not None:
                rag_scope.close(task_id=task_id)

    def _finalize_recall_success(
        self,
        context,
        state,
        *,
        architecture_id: int,
        returned_rank: int,
        started_at: float,
    ) -> None:
        active = self._steps.begin(
            context,
            step_key="recall.finalize",
            idempotency_key=state.recall_receipt.idempotency_key,
        )
        try:
            self._audit.finalize_recall_success(
                state,
                architecture_id,
                returned_rank,
                started_at,
            )
        except Exception as error:
            self._steps.fail(
                context,
                active,
                error_code="analysis_recall_finalize_outcome_unknown",
                outcome_unknown=True,
            )
            raise AnalysisTaskPersistenceError("Analysis 召回终结结果未知") from error
        digest = _digest_json(
            {"architecture_id": architecture_id, "returned_rank": returned_rank}
        )
        self._steps.succeed(
            context,
            active,
            TaskStepCheckpoint(
                code="recall_finalized_v1",
                result_ref=f"analysis-recall-final:v1:{digest}",
                result_digest=digest,
            ),
            component_mutation=lambda unit_of_work: self._record_recall_resource(
                unit_of_work,
                state.recall_receipt.execution,
                state,
            ),
        )

    def _persist_interaction(
        self,
        context,
        execution,
        snapshot,
        state,
        *,
        outcome: AnalysisAuditOutcome,
        error_code: str,
    ) -> None:
        active = self._steps.begin(
            context,
            step_key="interaction_audit.commit",
            idempotency_key=f"analysis-rag:{execution.task_id.value}",
        )
        try:
            state.interaction_receipt = self._audit.persist_interaction(
                execution=execution,
                snapshot=snapshot,
                state=state,
                outcome=outcome,
                error_code=error_code,
            )
        except Exception as error:
            self._steps.fail(
                context,
                active,
                error_code="analysis_interaction_audit_outcome_unknown",
                outcome_unknown=True,
            )
            raise AnalysisTaskPersistenceError("Analysis Interaction Audit 结果未知") from error
        digest = _digest_json(
            {
                "audit_id": state.interaction_receipt.audit_id,
                "idempotency_key": state.interaction_receipt.idempotency_key,
            }
        )
        self._steps.succeed(
            context,
            active,
            TaskStepCheckpoint(
                code="interaction_audit_committed_v1",
                result_ref=f"analysis-audit:v1:{state.interaction_receipt.audit_id}",
                result_digest=digest,
            ),
            component_mutation=lambda unit_of_work: self._record_interaction_resource(
                unit_of_work,
                execution,
                state,
            ),
        )

    def _handle_failure(
        self,
        context,
        execution_snapshot,
        snapshot,
        execution,
        state,
        error: BaseException,
        *,
        current_steps: list[ActiveAnalysisStep],
        rag_observer: AnalysisRagStepObserver | None,
        knowledge_recovery_required: bool,
        started_at: float,
    ) -> None:
        error_code = self._safe_error_code(error)
        stage = self._failure_stage(error)
        if isinstance(error, AnalysisRagSessionOpenError):
            state.session = error.partial_session
            state.lifecycle_events.extend(error.lifecycle_events)
            state.preserve_scene = error.outcome_unknown
        logger.error(
            "Analysis v2 Workflow 执行失败: task_id=%s stage=%s error_code=%s error_type=%s",
            execution.task_id,
            stage,
            error_code,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        if rag_observer is not None and rag_observer.has_deferred_failures:
            # 外部证据必须先完整写审计库，再把仍在 running 的 Step 标记失败/unknown。
            if state.recall_receipt is not None and not state.recall_finalized:
                self._finalize_recall_failure(
                    context,
                    state,
                    error,
                    stage=stage,
                    started_at=started_at,
                )
            if state.lifecycle_events:
                self._persist_interaction(
                    context,
                    execution,
                    snapshot,
                    state,
                    outcome=AnalysisAuditOutcome.FAILED,
                    error_code=error_code,
                )
            if rag_observer.finalize_failures_after_audit():
                self._last_result = RunAnalysisResult(
                    execution.task_id,
                    RunAnalysisOutcome.RECOVERY_REQUIRED,
                    error_code=error_code,
                    stage=stage,
                )
                return
        if knowledge_recovery_required:
            self._last_result = RunAnalysisResult(
                execution.task_id,
                RunAnalysisOutcome.RECOVERY_REQUIRED,
                error_code=error_code,
                stage=stage,
            )
            return
        for active in tuple(current_steps):
            self._steps.fail(context, active, error_code=error_code)
            current_steps.remove(active)
        payload = FrozenJsonObject.from_mapping(
            build_file_callback_payload(
                snapshot.file_name,
                {},
                status=_PUBLIC_FAILED_STATUS,
            ),
            name="analysis_v2_failure_callback",
        )
        self._finish_terminal(
            context,
            execution_snapshot.business_ref,
            payload,
            execution=execution,
            state=state,
            succeeded=False,
            public_status=_PUBLIC_FAILED_STATUS,
            message=f"解析失败（{stage}）：{error_code}",
        )
        self._publish_progress(execution_snapshot, 1.0, "", "failed")
        self._deliver_callback(execution, payload)
        if rag_observer is not None:
            self._close_audited_resources(execution, state, rag_observer)
        self._wake_maintenance(execution.task_id.value)
        self._last_result = RunAnalysisResult(
            execution.task_id,
            RunAnalysisOutcome.FAILED,
            error_code=error_code,
            stage=stage,
        )

    def _finalize_recall_failure(self, context, state, error, *, stage, started_at) -> None:
        active = self._steps.begin(
            context,
            step_key="recall.finalize",
            idempotency_key=state.recall_receipt.idempotency_key,
        )
        self._audit.finalize_recall_failure(
            state,
            error,
            stage,
            started_at,
            self._safe_error_code,
        )
        if not state.recall_finalized:
            self._steps.fail(
                context,
                active,
                error_code="analysis_recall_finalize_outcome_unknown",
                outcome_unknown=True,
            )
            raise AnalysisTaskPersistenceError("Analysis 失败召回终结未确认")
        digest = _digest_text(self._safe_error_code(error))
        self._steps.succeed(
            context,
            active,
            TaskStepCheckpoint(
                code="recall_failure_finalized_v1",
                result_ref=f"analysis-recall-failure:v1:{digest}",
                result_digest=digest,
            ),
            component_mutation=lambda unit_of_work: self._record_recall_resource(
                unit_of_work,
                state.recall_receipt.execution,
                state,
            ),
        )

    def _finish_terminal(
        self,
        context,
        business_ref,
        payload,
        *,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        succeeded: bool,
        public_status: str,
        message: str,
    ) -> None:
        payload_digest = _digest_json(payload.to_dict())
        # 完整公开 Callback 结果先作为独立业务 Checkpoint 落盘。这样 Worker 在
        # ``result.snapshot`` 与本地终态事务之间退出时，Recovery Coordinator 可以只
        # 核验既有结果并提交终态，不必重新调用模型、Knowledge 或 Translation。
        self._steps.checkpoint_result_snapshot(
            context,
            business_ref=business_ref,
            payload=payload,
            result_digest=payload_digest,
        )
        terminal = self._steps.begin(
            context,
            step_key="terminal.commit",
            idempotency_key=(
                f"analysis:{context.loaded_input.snapshot.task_id.value}:terminal:{payload_digest}"
            ),
        )
        self._steps.finish(
            context,
            terminal,
            business_ref=business_ref,
            succeeded=succeeded,
            public_status=public_status,
            message=message,
            result_ref=f"analysis-result:v1:{payload_digest}",
            terminal_checkpoint=TaskStepCheckpoint(
                code="terminal_committed_v1",
                result_ref=f"analysis-terminal:v1:{payload_digest}",
                result_digest=payload_digest,
            ),
            component_mutation=lambda unit_of_work: self._plan_resource_cleanup(
                unit_of_work,
                execution,
                state,
            ),
        )

    def _update_progress(self, context, execution, progress: float, message: str) -> None:
        self._steps.update_progress(
            context,
            progress=progress,
            message=message,
            public_status=_PUBLIC_PROCESSING_STATUS,
        )
        self._publish_progress(execution, progress, message, "running")

    def _publish_progress(self, execution, progress, message, internal_state) -> None:
        try:
            self._progress_publisher.publish(
                ProgressPublication(
                    key=ProgressKey(ANALYSIS_BUSINESS_TYPE, execution.business_ref.business_key),
                    expected_task_id=execution.task_id,
                    progress=progress,
                    message=message,
                    internal_state=internal_state,
                )
            )
        except Exception:
            logger.exception(
                "Analysis v2 Progress 通知失败，持久事实不回滚: task_id=%s progress=%s",
                execution.task_id,
                progress,
            )

    def _deliver_callback(self, execution: AnalysisExecutionRef, payload: FrozenJsonObject) -> str:
        try:
            acquired = self._callbacks.acquire(
                AnalysisCallbackRequest(
                    execution=execution,
                    callback_url=self._callback_url,
                    payload=payload,
                )
            )
            if not isinstance(acquired, AnalysisCallbackAcquireResult):
                raise AnalysisApplicationContractError("Analysis Callback acquire 类型错误")
            if acquired.outcome is not AnalysisCallbackAcquireOutcome.ACQUIRED:
                return acquired.outcome.value
            lease = acquired.lease
            if lease is None:
                raise AnalysisApplicationContractError("Analysis Callback acquire 缺少 Lease")
            delivery = self._callbacks.deliver(
                AnalysisCallbackDeliveryRequest(
                    lease=lease,
                    callback_url=self._callback_url,
                    payload=payload,
                )
            )
            if not isinstance(delivery, AnalysisCallbackDelivery):
                raise AnalysisApplicationContractError("Analysis Callback deliver 类型错误")
            if delivery.outcome is AnalysisCallbackDeliveryOutcome.STALE:
                return delivery.outcome.value
            if not self._callbacks.complete(lease, delivery, payload):
                raise AnalysisApplicationContractError("Analysis Callback Guard 完成权已过期")
            return delivery.outcome.value
        except Exception:
            logger.exception(
                "Analysis v2 Callback 异常，业务终态保持: task_id=%s",
                execution.task_id,
            )
            return "port_error"

    def _wake_maintenance(self, task_id: str) -> None:
        try:
            self._maintenance_wakeup()
        except Exception:
            logger.warning(
                "Analysis v2 维护提示失败，持久扫描仍为恢复真相: task_id=%s",
                task_id,
                exc_info=True,
            )

    def _attach_resource(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
    ) -> AnalysisResourceLifecycle:
        record = unit_of_work.resources.get(execution)
        if record is None:
            raise AnalysisTaskPersistenceError("Analysis 资源记录不存在")
        return AnalysisResourceLifecycle.attach(
            store=unit_of_work.resources,
            execution=execution,
            record=record,
            close_running_grace_seconds=self._resource_close_running_grace_seconds,
        )

    def _register_resource(
        self,
        unit_of_work,
        *,
        execution: AnalysisExecutionRef,
        workspace_root: str,
        prepared: PreparedAnalysisDocument,
        state: _RagWorkflowState,
        upload_descriptor,
    ) -> None:
        lifecycle = AnalysisResourceLifecycle(
            store=unit_of_work.resources,
            execution=execution,
            close_running_grace_seconds=self._resource_close_running_grace_seconds,
        )
        try:
            lifecycle.register(
                task_root=workspace_root,
                source_path=prepared.source_path,
                processing_path=prepared.processing_path,
                upload_path=prepared.upload_path,
                state=state,
                upload_descriptor=upload_descriptor,
            )
        finally:
            lifecycle.finish_worker()

    def _record_rag_result(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        result: object,
    ) -> None:
        # checkpoint 只需隔离会被追加的生命周期列表；其他字段保持当前调用内的
        # 同一事实引用。使用 dataclass replace 明确列出复制边界，避免隐式浅拷贝
        # 把以后新增的可变字段悄悄带入持久化快照。
        working = replace(state, lifecycle_events=list(state.lifecycle_events))
        if isinstance(result, AnalysisRagSessionOpenResult):
            working.session = result.session
            working.opened = True
            working.lifecycle_events.extend(result.lifecycle_events)
        elif isinstance(result, AnalysisRagResult):
            working.session = result.session
            working.lifecycle_events.extend(result.lifecycle_events)
        else:
            raise TypeError("RAG 资源 checkpoint 结果类型无效")
        self._attach_resource(unit_of_work, execution).checkpoint_rag_state(working)

    def _prepare_resource_upload(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
    ) -> None:
        self._attach_resource(unit_of_work, execution).prepare_document_upload()

    def _record_rag_failure(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
    ) -> None:
        lifecycle = self._attach_resource(unit_of_work, execution)
        try:
            lifecycle.checkpoint_rag_state(state)
        except AnalysisResourceLifecycleError:
            # unknown lifecycle 会先把资源推进 quarantined 再抛出“禁止继续”信号。
            # 在组合 UoW 内必须吞掉该业务信号，让同事务的 Step isolation 一并提交；
            # SQLite/CAS 异常仍会在 _advance 内包装后抛出且当前记录不会变为 quarantined。
            if lifecycle.record is None or lifecycle.record.state.value != "quarantined":
                raise

    def _record_recall_resource(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
    ) -> None:
        self._attach_resource(unit_of_work, execution).record_recall_state(state)

    def _record_interaction_resource(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
    ) -> None:
        if state.interaction_receipt is None:
            raise AnalysisTaskPersistenceError("Analysis 交互审计 Receipt 缺失")
        self._attach_resource(unit_of_work, execution).record_interaction_receipt(
            state.interaction_receipt
        )

    def _record_knowledge_resource(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        request,
        result,
    ) -> None:
        self._attach_resource(unit_of_work, execution).record_knowledge_result(
            request,
            result,
        )

    def _quarantine_resource(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        *,
        stage: str,
        reason: str,
    ) -> None:
        self._attach_resource(unit_of_work, execution).quarantine(
            stage=stage,
            reason=reason,
        )

    def _plan_resource_cleanup(
        self,
        unit_of_work,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
    ) -> None:
        record = unit_of_work.resources.get(execution)
        if record is None:
            # Workspace/下载前失败不会产生资源记录；终态仍可提交。
            return
        AnalysisResourceLifecycle.attach(
            store=unit_of_work.resources,
            execution=execution,
            record=record,
            close_running_grace_seconds=self._resource_close_running_grace_seconds,
        ).prepare_close(retain_document=state.retain_document)

    def _close_audited_resources(
        self,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
    ) -> None:
        record = self._resources.get(execution)
        if record is None:
            return
        if state.session is None or state.interaction_receipt is None:
            logger.warning(
                "Analysis v2 终态资源缺少可审计 Session，保留 cleanup_pending: task_id=%s",
                execution.task_id,
            )
            return
        lifecycle = AnalysisResourceLifecycle.attach(
            store=self._resources,
            execution=execution,
            record=record,
            close_running_grace_seconds=self._resource_close_running_grace_seconds,
        )
        try:
            lifecycle.mark_close_running()
        except Exception:
            logger.critical(
                "Analysis v2 RAG close 前资源意图未确认，禁止外部关闭: task_id=%s",
                execution.task_id,
                exc_info=True,
            )
            return
        self._audit.close_audited_session(
            execution=execution,
            state=state,
            rag=rag,
            retain_document=state.retain_document,
            on_close_result=lifecycle.record_close_result,
            on_lifecycle_audited=lifecycle.mark_close_audited,
            on_close_failure=lifecycle.record_close_failure,
        )

    @staticmethod
    def _require_prepared(prepared: object, execution: AnalysisExecutionRef) -> None:
        if not isinstance(prepared, PreparedAnalysisDocument):
            raise AnalysisApplicationContractError(
                "AnalysisDocumentPreparationPort.prepare_document 返回类型错误"
            )
        if prepared.execution != execution:
            raise AnalysisApplicationContractError("准备文件不属于当前 execution")

    @staticmethod
    def _prepared_digest(prepared: PreparedAnalysisDocument) -> str:
        artifact = prepared.rag_upload_artifact
        if artifact is not None:
            return artifact.metadata.sha256
        return _digest_json(
            {
                "source": prepared.source_sha256,
                "upload_name": prepared.internal_prepared_basename,
                "projection": prepared.rag_projection_profile_id,
            }
        )

    @staticmethod
    def _safe_error_code(error: BaseException) -> str:
        if isinstance(error, _AnalysisKnownFailure):
            return error.error_code
        if isinstance(error, AnalysisRagExecutionError):
            return error.error_code
        if isinstance(error, (AnalysisContractError, ArchitectureRecallError, ValueError)):
            text = " ".join(str(error).split())
            return text[:500] or "analysis_contract_error"
        return f"analysis_unexpected_{type(error).__name__.lower()}"

    @staticmethod
    def _failure_stage(error: BaseException) -> str:
        if isinstance(error, _AnalysisKnownFailure):
            return error.stage
        if isinstance(error, AnalysisRagSessionOpenError):
            return f"rag_open_{error.stage.value}"
        if isinstance(error, ArchitectureTreeValidationError):
            return "architecture_index"
        if isinstance(error, ArchitecturePromptBudgetError):
            return "architecture_prompt_budget"
        if isinstance(error, ArchitectureRecallError):
            return "architecture_recall"
        if isinstance(error, (ArchitectureContractError, AnalysisContractError)):
            return "architecture_contract"
        if isinstance(error, AnalysisRagExecutionError):
            return "analysis_extraction"
        return "analysis_execution"


__all__ = ["RunAnalysisV2Workflow"]
