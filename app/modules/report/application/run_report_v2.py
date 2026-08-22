"""阶段 2-4 基于完整 Authority 与持久 Step 的 Report v2 Workflow。"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable

from app.modules.report.domain import (
    REPORT_INPUT_SCHEMA_VERSION_V2,
    REPORT_STATUS_FAILED,
    REPORT_STATUS_SUCCEEDED,
    ReportError,
    ReportExecutionProfile,
    ReportInputSnapshot,
    ReportPortContractError,
    ReportSourceNormalizationError,
    ReportTaskPersistenceError,
    ReportTemplateError,
    build_report_callback,
    build_report_context_name,
    build_report_conversation_name,
    build_report_prompt,
    build_report_result,
    sanitize_public_report_content,
)
from app.modules.report.ports import (
    DeliverReportCallback,
    PersistReportRagTrace,
    ReportArtifactCategory,
    ReportArtifactPort,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportCallbackAcquire,
    ReportCallbackAcquireOutcome,
    ReportCallbackDeliveryOutcome,
    ReportCallbackPort,
    ReportFilePort,
    ReportInteractionAuditPort,
    ReportRagAuditOutcome,
    ReportRagExecutionError,
    ReportRagPort,
    ReportRagRequest,
    ReportRagResponse,
    ReportResourceRecoveryPort,
    ReportSourceDownload,
    ReportTemplateDownload,
)
from app.modules.tasks.domain import ProgressKey, TaskBusinessRef, TaskId, TaskStepCheckpoint
from app.modules.tasks.ports import (
    ProgressPublication,
    ProgressPublisherPort,
    TaskExecutionStopRequested,
    TaskStepContinuationDraft,
    TaskWorkflowContextPort,
    TaskWorkflowRunnerPort,
)

from .run_report import RunReportOutcome, RunReportResult
from .step_runtime import ActiveReportStep, ReportRagStepObserver, ReportStepRuntime
from .submit_report import REPORT_PUBLIC_PROCESSING_STATUS, REPORT_TASK_TYPE


logger = logging.getLogger(__name__)


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


def _artifact_digest(artifact: ReportArtifactRef) -> str:
    if artifact.checksum.strip():
        return artifact.checksum.strip().lower()
    return _digest_json(
        {
            "task_id": artifact.task_id.value,
            "artifact_id": artifact.artifact_id,
            "category": artifact.category.value,
            "sequence_no": artifact.sequence_no,
            "size_bytes": artifact.size_bytes,
        }
    )


class RunReportV2Workflow(TaskWorkflowRunnerPort):
    """只消费 Runtime 传入的冻结 Input v2 与可轮换 Authority Session。

    所有外部动作前先提交 Step intent；动作完成后仅在同一完整 Authority 下提交摘要型
    checkpoint。该类不 claim、不 start、不读取环境变量，也不会从数据库补造 Authority。
    """

    def __init__(
        self,
        *,
        steps: ReportStepRuntime,
        progress_publisher: ProgressPublisherPort,
        files: ReportFilePort,
        artifacts: ReportArtifactPort,
        rag: ReportRagPort,
        audit: ReportInteractionAuditPort,
        callbacks: ReportCallbackPort,
        resources: ReportResourceRecoveryPort,
        execution_profile: ReportExecutionProfile,
        maintenance_wakeup: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(steps, ReportStepRuntime):
            raise TypeError("steps 必须是 ReportStepRuntime")
        for name, dependency, protocol in (
            ("progress_publisher", progress_publisher, ProgressPublisherPort),
            ("files", files, ReportFilePort),
            ("artifacts", artifacts, ReportArtifactPort),
            ("rag", rag, ReportRagPort),
            ("audit", audit, ReportInteractionAuditPort),
            ("callbacks", callbacks, ReportCallbackPort),
            ("resources", resources, ReportResourceRecoveryPort),
        ):
            if not isinstance(dependency, protocol):
                raise TypeError(f"{name} 未实现所需 Port")
        if not isinstance(execution_profile, ReportExecutionProfile):
            raise TypeError("execution_profile 必须是 ReportExecutionProfile")
        if maintenance_wakeup is not None and not callable(maintenance_wakeup):
            raise TypeError("maintenance_wakeup 必须可调用或为 None")
        self._steps = steps
        self._progress_publisher = progress_publisher
        self._files = files
        self._artifacts = artifacts
        self._rag = rag
        self._audit = audit
        self._callbacks = callbacks
        self._resources = resources
        self._maintenance_wakeup = maintenance_wakeup or (lambda: None)
        # Worker 当前能力必须由装配根显式注入。Runner 只做冻结快照的严格等值门禁，
        # 绝不能读取环境变量或用当前配置替历史任务补造 Profile。
        self._execution_profile = execution_profile
        self._last_result: RunReportResult | None = None

    @property
    def last_result(self) -> RunReportResult | None:
        return self._last_result

    def run(self, context: TaskWorkflowContextPort) -> None:
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")
        execution = context.loaded_input.snapshot
        snapshot = execution.input_snapshot
        task_id = execution.task_id
        if (
            execution.task_type != REPORT_TASK_TYPE
            or not isinstance(snapshot, ReportInputSnapshot)
            or snapshot.schema_version != REPORT_INPUT_SCHEMA_VERSION_V2
            or snapshot.execution_profile is None
            or snapshot.execution_profile != self._execution_profile
            or snapshot.task_id != task_id.value
            or execution.business_ref
            != TaskBusinessRef(REPORT_TASK_TYPE, snapshot.report_id.business_key)
            or snapshot.accepted_at != execution.accepted_at
            or snapshot.trace_id != execution.trace_id
        ):
            raise ReportPortContractError("Report v2 Workflow 冻结输入身份不一致")

        scope: ReportArtifactScope | None = None
        current_step: ActiveReportStep | None = None
        rag_observer: ReportRagStepObserver | None = None
        audit_receipt: ReportAuditReceipt | None = None
        prompt = ""
        resources_registered = False
        initial_continuation = TaskStepContinuationDraft(
            schema_version=1,
            input_payload_fingerprint=context.loaded_input.input_payload_fingerprint,
            execution_profile_fingerprint=self._execution_profile.fingerprint,
            payload={
                "business_key": snapshot.report_id.business_key,
                "resolver": "report.artifact_scope.v1",
                "step_key": "artifact.scope.begin",
                "task_id": task_id.value,
            },
        )
        restored = self._steps.load_resume_continuation(
            context,
            execution_profile_fingerprint=self._execution_profile.fingerprint,
        )
        if restored is not None:
            if (
                restored.step_key != "artifact.scope.begin"
                or restored.draft != initial_continuation
            ):
                raise ReportTaskPersistenceError("Report 初始续跑快照解析结果不一致")
            initial_continuation = restored.draft
        try:
            current_step = self._steps.begin(
                context,
                step_key="artifact.scope.begin",
                idempotency_key=f"report:{task_id.value}:artifact-scope",
                continuation=initial_continuation,
            )
            scope = self._checked_scope(self._artifacts.begin(task_id), task_id)
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="artifact_scope_registered_v1",
                    result_ref=scope.namespace,
                    result_digest=_digest_text(scope.namespace),
                ),
                resource_mutation=lambda facts: facts.register(
                    task_id,
                    execution.business_ref,
                    scope,
                ),
            )
            current_step = None
            resources_registered = True
            self._update_progress(context, execution, 0.15, "正在下载报告文件")

            upload_files: list[ReportArtifactRef] = []
            for sequence_no, source_url in enumerate(snapshot.source_urls, start=1):
                source_identity = _digest_text(source_url)
                current_step = self._steps.begin(
                    context,
                    step_key=f"source.download:{sequence_no}",
                    idempotency_key=(
                        f"report:{task_id.value}:source:{sequence_no}:{source_identity}"
                    ),
                )
                downloaded = self._checked_artifact(
                    self._files.download_source(
                        ReportSourceDownload(scope, source_url, sequence_no)
                    ),
                    task_id,
                )
                if (
                    downloaded.category is not ReportArtifactCategory.SOURCE
                    or downloaded.sequence_no != sequence_no
                ):
                    raise ReportPortContractError("下载源 Artifact 类别或顺序不一致")
                downloaded_digest = _artifact_digest(downloaded)
                self._steps.succeed(
                    context,
                    current_step,
                    TaskStepCheckpoint(
                        code="source_downloaded_v1",
                        result_ref=downloaded.artifact_id,
                        result_digest=downloaded_digest,
                    ),
                )
                current_step = None

                current_step = self._steps.begin(
                    context,
                    step_key=f"document.prepare:{sequence_no}",
                    idempotency_key=(
                        f"report:{task_id.value}:prepared:{sequence_no}:"
                        f"{downloaded_digest}:"
                        f"{snapshot.execution_profile.document_processing_fingerprint}"
                    ),
                )
                normalized = downloaded
                try:
                    candidate = self._checked_artifact(
                        self._files.normalize_source(downloaded),
                        task_id,
                    )
                    if (
                        candidate.category
                        is not ReportArtifactCategory.NORMALIZED_SOURCE
                        or candidate.sequence_no != sequence_no
                    ):
                        raise ReportPortContractError(
                            "规范化 Artifact 类别或顺序不一致"
                        )
                    normalized = candidate
                except ReportSourceNormalizationError:
                    logger.warning(
                        "Report v2 源规范化失败，按冻结兼容规则回退: "
                        "task_id=%s sequence_no=%d",
                        task_id,
                        sequence_no,
                    )
                prepared = tuple(self._files.prepare_upload_files(normalized))
                if not prepared:
                    raise ReportPortContractError("文件准备端口不得返回空上传列表")
                for artifact in prepared:
                    checked = self._checked_artifact(artifact, task_id)
                    if (
                        checked.category is not ReportArtifactCategory.RAG_INPUT
                        or checked.sequence_no != sequence_no
                    ):
                        raise ReportPortContractError("RAG 输入 Artifact 身份不一致")
                    upload_files.append(checked)
                prepared_digest = _digest_json(
                    [
                        (item.artifact_id, _artifact_digest(item), item.size_bytes)
                        for item in prepared
                    ]
                )
                self._steps.succeed(
                    context,
                    current_step,
                    TaskStepCheckpoint(
                        code="document_prepared_v1",
                        result_ref=f"report-prepared:v1:{prepared_digest}",
                        result_digest=prepared_digest,
                    ),
                )
                current_step = None

            self._update_progress(context, execution, 0.25, "正在解析报告模板")
            template_identity = _digest_text(snapshot.template_outline_url)
            current_step = self._steps.begin(
                context,
                step_key="template.download",
                idempotency_key=f"report:{task_id.value}:template:{template_identity}",
            )
            template = self._checked_artifact(
                self._files.download_template(
                    ReportTemplateDownload(scope, snapshot.template_outline_url)
                ),
                task_id,
            )
            if template.category is not ReportArtifactCategory.TEMPLATE:
                raise ReportPortContractError("模板 Artifact 类别必须是 template")
            template_digest = _artifact_digest(template)
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="template_downloaded_v1",
                    result_ref=template.artifact_id,
                    result_digest=template_digest,
                ),
            )
            current_step = self._steps.begin(
                context,
                step_key="template.extract",
                idempotency_key=(
                    f"report:{task_id.value}:template-text:{template_digest}:"
                    f"{snapshot.execution_profile.template_extractor_profile_id}"
                ),
            )
            template_text = self._files.extract_template_text(template)
            if not isinstance(template_text, str) or not template_text.strip():
                raise ReportTemplateError("Word模板未提取到有效文字内容")
            template_text = template_text.strip()
            template_text_digest = _digest_text(template_text)
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="template_extracted_v1",
                    result_ref=f"report-template-text:v1:{template_text_digest}",
                    result_digest=template_text_digest,
                ),
            )
            current_step = None

            self._update_progress(context, execution, 0.35, "正在生成报告")
            prompt = build_report_prompt(
                template_desc=snapshot.template_desc,
                template_outline=template_text,
                requirement=snapshot.requirement,
            )
            rag_observer = self._steps.observer(context)
            rag_response = self._rag.generate(
                ReportRagRequest(
                    task_id=task_id,
                    trace_id=snapshot.trace_id,
                    ordered_source_files=tuple(upload_files),
                    prompt=prompt,
                    context_name=build_report_context_name(
                        snapshot.report_id,
                        task_id.value,
                    ),
                    conversation_name=build_report_conversation_name(
                        snapshot.report_id
                    ),
                    step_observer=rag_observer,
                )
            )
            if not isinstance(rag_response, ReportRagResponse):
                raise ReportPortContractError("ReportRagPort.generate 返回类型错误")
            rag_observer.finalize_generate(cleanup_ref=rag_response.cleanup_ref)

            audit_receipt = self._persist_audit_step(
                context,
                execution,
                prompt=prompt,
                trace=rag_response.trace,
                outcome=ReportRagAuditOutcome.SUCCEEDED,
            )
            current_step = self._steps.begin(
                context,
                step_key="report.render",
                idempotency_key=(
                    f"report:{task_id.value}:render:"
                    f"{_digest_text(rag_response.raw_content or '')}:"
                    f"{snapshot.execution_profile.sanitizer_profile_id}:"
                    f"{snapshot.execution_profile.renderer_profile_id}"
                ),
            )
            public_content = sanitize_public_report_content(
                rag_response.raw_content,
                source_urls=snapshot.source_urls,
                artifact_sources=tuple(
                    (artifact.artifact_id, artifact.sequence_no)
                    for artifact in upload_files
                ),
            )
            report_result = build_report_result(snapshot.report_id, public_content)
            html_digest = _digest_text(report_result.html_details)
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="report_rendered_v1",
                    result_ref=f"report-html-content:v1:{html_digest}",
                    result_digest=html_digest,
                ),
            )
            current_step = self._steps.begin(
                context,
                step_key="artifact.publish",
                idempotency_key=(
                    f"report:{task_id.value}:report-html:{html_digest}"
                ),
            )
            final_artifact = self._checked_artifact(
                self._artifacts.persist_report_html(scope, report_result.html_details),
                task_id,
            )
            if final_artifact.category is not ReportArtifactCategory.REPORT_HTML:
                raise ReportPortContractError("最终报告 Artifact 类别错误")
            self._steps.succeed(
                context,
                current_step,
                TaskStepCheckpoint(
                    code="artifact_published_v1",
                    result_ref=final_artifact.artifact_id,
                    result_digest=_artifact_digest(final_artifact),
                ),
                resource_mutation=lambda facts: facts.track_final_artifact(
                    final_artifact
                ),
            )
            current_step = self._steps.begin(
                context,
                step_key="terminal.commit",
                idempotency_key=(
                    f"report:{task_id.value}:terminal:{_artifact_digest(final_artifact)}"
                ),
            )
            terminal_digest = _digest_json(
                {
                    "report_id": snapshot.report_id.business_key,
                    "artifact": final_artifact.artifact_id,
                    "checksum": final_artifact.checksum,
                }
            )
            self._steps.finish(
                context,
                current_step,
                succeeded=True,
                public_status=REPORT_STATUS_SUCCEEDED,
                message="报告生成完成",
                terminal_checkpoint=TaskStepCheckpoint(
                    code="terminal_committed_v1",
                    result_ref=f"report-terminal:v1:{terminal_digest}",
                    result_digest=terminal_digest,
                ),
                business_ref=execution.business_ref,
                final_artifact=final_artifact,
            )
            current_step = None
            self._publish_progress(execution, 1.0, "", "succeeded")
            payload = build_report_callback(
                snapshot.report_id,
                report_result.html_details,
                status=REPORT_STATUS_SUCCEEDED,
            )
            callback_outcome = self._deliver_callback(
                task_id,
                snapshot.report_id,
                payload,
            )
            # 业务终态与 Callback 已经独立收敛；外部 DELETE 不再占用业务 Worker。
            # 这里只发送可丢提示，维护线程必须从 Resource Store 的 terminal/tracking
            # 持久事实重新扫描，因此即使进程在提示前退出，启动/周期扫描仍可恢复。
            self._wake_maintenance(task_id)
            self._last_result = RunReportResult(
                task_id,
                RunReportOutcome.SUCCEEDED,
                callback_outcome=callback_outcome,
                empty_rag_result=report_result.empty_rag_result,
            )
        except TaskExecutionStopRequested:
            logger.warning(
                "Report v2 Workflow 已按执行停止信号退出: task_id=%s",
                task_id,
            )
            raise
        except ReportTaskPersistenceError:
            logger.critical(
                "Report v2 持久化结果不确定，保留现场且禁止二次终态: task_id=%s",
                task_id,
                exc_info=True,
            )
            raise
        except ReportRagExecutionError as error:
            self._handle_rag_failure(
                context,
                execution,
                snapshot,
                error,
                prompt=prompt,
                observer=rag_observer,
                resources_registered=resources_registered,
            )
        except Exception as error:
            if rag_observer is not None and rag_observer.active is not None:
                self._handle_unclassified_rag_failure(
                    context,
                    execution,
                    error,
                    observer=rag_observer,
                )
            else:
                self._handle_regular_failure(
                    context,
                    execution,
                    snapshot,
                    error,
                    current_step=current_step,
                    resources_registered=resources_registered,
                    audit_complete=audit_receipt is not None,
                )

    def _persist_audit_step(
        self,
        context,
        execution,
        *,
        prompt,
        trace,
        outcome,
        error_code: str = "",
    ) -> ReportAuditReceipt:
        task_id = execution.task_id
        active = self._steps.begin(
            context,
            step_key="interaction_audit.commit",
            idempotency_key=f"report-rag:{task_id.value}",
        )
        try:
            receipt = self._audit.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=execution.business_ref,
                    idempotency_key=f"report-rag:{task_id.value}",
                    prompt=prompt,
                    trace=trace,
                    outcome=outcome,
                    error_code=error_code,
                )
            )
        except Exception as exc:
            # 审计库与 Task Control 不在同一数据库。异常不能证明审计事务未提交，因此
            # 必须把 audit Step 隔离为 unknown，禁止普通 Worker 补写失败终态或清理现场。
            self._steps.fail(
                context,
                active,
                error_code="report_audit_outcome_unknown",
                outcome_unknown=True,
                resource_mutation=lambda facts: facts.quarantine(
                    task_id,
                    stage="interaction_audit_outcome_unknown",
                    reason="交互审计提交结果未知，等待幂等核验",
                ),
            )
            raise ReportTaskPersistenceError("报告RAG交互审计提交结果未知") from exc
        if (
            not isinstance(receipt, ReportAuditReceipt)
            or receipt.task_id != task_id
            or receipt.idempotency_key != f"report-rag:{task_id.value}"
        ):
            self._steps.fail(
                context,
                active,
                error_code="report_audit_receipt_mismatch",
                outcome_unknown=True,
                resource_mutation=lambda facts: facts.quarantine(
                    task_id,
                    stage="interaction_audit_receipt_mismatch",
                    reason="交互审计回执身份不一致，禁止自动清理",
                ),
            )
            raise ReportTaskPersistenceError("Audit Receipt 与当前任务不一致")
        audit_digest = _digest_json(
            {
                "task_id": task_id.value,
                "audit_id": receipt.audit_id,
                "idempotency_key": receipt.idempotency_key,
            }
        )
        self._steps.succeed(
            context,
            active,
            TaskStepCheckpoint(
                code="interaction_audit_committed_v1",
                result_ref=f"report-audit:{receipt.audit_id}",
                result_digest=audit_digest,
            ),
            resource_mutation=lambda facts: facts.track_audit(receipt),
        )
        return receipt

    def _handle_rag_failure(
        self,
        context,
        execution,
        snapshot,
        error: ReportRagExecutionError,
        *,
        prompt: str,
        observer: ReportRagStepObserver | None,
        resources_registered: bool,
    ) -> None:
        task_id = execution.task_id
        if observer is None or observer.active is None:
            raise ReportTaskPersistenceError("RAG 失败缺少持久 Step intent") from error
        if (
            error.active_step_key
            and error.active_step_key != observer.active.step_key
        ):
            # Adapter 报告的失效位置必须与 Control Store 中唯一 running Step 一致；
            # 不一致时不能猜测哪次供应商写入生效，交由恢复流程人工/对账处理。
            raise ReportTaskPersistenceError("RAG 失败位置与持久 Step intent 不一致") from error
        # 审计使用独立已登记 Step；它与仍在 running 的 RAG Step 并存，但不会产生供应商副作用。
        receipt = self._persist_audit_step(
            context,
            execution,
            prompt=prompt,
            trace=error.trace,
            outcome=ReportRagAuditOutcome.FAILED,
            error_code=error.code,
        )

        def resource_mutation(facts):
            if error.cleanup_ref is not None:
                facts.track_rag_cleanup(task_id, error.cleanup_ref)
            facts.track_audit(receipt)
            if error.external_outcome_unknown:
                facts.quarantine(
                    task_id,
                    stage="rag_side_effect_outcome_unknown",
                    reason="AnythingLLM 写操作结果未知，禁止自动清理",
                )

        observer.fail_active(
            error_code=(
                "report_rag_outcome_unknown"
                if error.external_outcome_unknown
                else error.code
            ),
            outcome_unknown=error.external_outcome_unknown,
            resource_mutation=resource_mutation,
        )
        if error.external_outcome_unknown:
            self._last_result = RunReportResult(
                task_id,
                RunReportOutcome.RECOVERY_REQUIRED,
                error_code="report_rag_outcome_unknown",
            )
            return
        self._finish_failed_terminal(
            context,
            execution,
            snapshot,
            error_code=error.code,
            resources_registered=resources_registered,
        )

    def _handle_unclassified_rag_failure(
        self,
        context,
        execution,
        error: BaseException,
        *,
        observer: ReportRagStepObserver,
    ) -> None:
        """隔离未按 RAG Port 契约分类、但已越过持久 intent 的异常。

        此时请求是否到达供应商无法由 Runner 证明；即使异常看似本地类型错误，也不能把
        活动外部写 Step 当作普通失败后继续提交业务终态或删除现场。
        """

        logger.error(
            "Report RAG 活动 Step 出现未分类异常，已隔离: task_id=%s "
            "step_key=%s error_type=%s",
            execution.task_id,
            observer.active.step_key if observer.active is not None else "missing",
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        observer.fail_active(
            error_code="report_rag_unclassified_outcome_unknown",
            outcome_unknown=True,
            resource_mutation=lambda facts: facts.quarantine(
                execution.task_id,
                stage="rag_unclassified_outcome_unknown",
                reason="RAG 外部操作异常未提供可靠结果分类，等待恢复对账",
            ),
        )
        self._last_result = RunReportResult(
            execution.task_id,
            RunReportOutcome.RECOVERY_REQUIRED,
            error_code="report_rag_unclassified_outcome_unknown",
        )

    def _handle_regular_failure(
        self,
        context,
        execution,
        snapshot,
        error: BaseException,
        *,
        current_step: ActiveReportStep | None,
        resources_registered: bool,
        audit_complete: bool,
    ) -> None:
        error_code = error.code if isinstance(error, ReportError) else "report_unexpected_error"
        logger.exception(
            "Report v2 Workflow 执行失败: task_id=%s error_code=%s error_type=%s",
            execution.task_id,
            error_code,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        if current_step is not None:
            self._steps.fail(context, current_step, error_code=error_code)
        if not audit_complete and resources_registered:
            # 非 RAG 阶段无需模型审计；RAG 已经开始却没有 Trace 的异常由 Adapter 契约
            # 转为 ReportRagExecutionError，若仍漏出则保守隔离而不清理。
            pass
        self._finish_failed_terminal(
            context,
            execution,
            snapshot,
            error_code=error_code,
            resources_registered=resources_registered,
        )

    def _finish_failed_terminal(
        self,
        context,
        execution,
        snapshot,
        *,
        error_code: str,
        resources_registered: bool,
    ) -> None:
        terminal = self._steps.begin(
            context,
            step_key="terminal.commit",
            idempotency_key=(
                f"report:{execution.task_id.value}:terminal:{_digest_text(error_code)}"
            ),
        )
        terminal_digest = _digest_json(
            {"report_id": snapshot.report_id.business_key, "error_code": error_code}
        )
        self._steps.finish(
            context,
            terminal,
            succeeded=False,
            public_status=REPORT_STATUS_FAILED,
            message="报告生成失败",
            terminal_checkpoint=TaskStepCheckpoint(
                code="terminal_failed_v1",
                result_ref=f"report-terminal-failure:v1:{terminal_digest}",
                result_digest=terminal_digest,
            ),
            business_ref=execution.business_ref,
            final_artifact=None,
        )
        self._publish_progress(execution, 1.0, "", "failed")
        payload = build_report_callback(
            snapshot.report_id,
            "",
            status=REPORT_STATUS_FAILED,
        )
        callback_outcome = self._deliver_callback(
            execution.task_id,
            snapshot.report_id,
            payload,
        )
        if resources_registered:
            self._wake_maintenance(execution.task_id)
        self._last_result = RunReportResult(
            execution.task_id,
            RunReportOutcome.FAILED,
            error_code=error_code,
            callback_outcome=callback_outcome,
        )

    def _update_progress(self, context, execution, progress: float, message: str) -> None:
        self._steps.update_progress(
            context,
            progress=progress,
            message=message,
            public_status=REPORT_PUBLIC_PROCESSING_STATUS,
        )
        self._publish_progress(execution, progress, message, "running")

    def _publish_progress(self, execution, progress, message, internal_state) -> None:
        try:
            self._progress_publisher.publish(
                ProgressPublication(
                    key=ProgressKey(
                        REPORT_TASK_TYPE,
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
                "Report v2 Progress 通知失败，持久事实不回滚: task_id=%s progress=%s",
                execution.task_id,
                progress,
            )

    def _deliver_callback(self, task_id, report_id, payload) -> str:
        try:
            acquired = self._callbacks.acquire(ReportCallbackAcquire(task_id, report_id))
            if acquired.outcome is not ReportCallbackAcquireOutcome.ACQUIRED:
                return acquired.outcome.value
            lease = acquired.lease
            if lease is None:
                raise ReportPortContractError("Callback acquire 缺少 Lease")
            delivery = self._callbacks.deliver(DeliverReportCallback(lease, payload))
            if delivery.outcome is ReportCallbackDeliveryOutcome.STALE:
                return delivery.outcome.value
            if not self._callbacks.complete(lease, delivery, payload):
                raise ReportPortContractError("Callback Guard 完成权已过期")
            return delivery.outcome.value
        except Exception:
            logger.exception(
                "Report v2 Callback 异常，业务终态保持: task_id=%s",
                task_id,
            )
            return "port_error"

    def _wake_maintenance(self, task_id: TaskId) -> None:
        try:
            self._maintenance_wakeup()
        except Exception:
            # Event 只是性能提示，失败不能覆盖已提交终态、触发资源 DELETE 或修改
            # Callback 结果；持久扫描仍会发现 terminal 后的 tracking 资源记录。
            logger.warning(
                "Report v2 终态后维护唤醒失败，等待启动/周期扫描: task_id=%s",
                task_id,
                exc_info=True,
            )

    @staticmethod
    def _checked_scope(value: object, task_id: TaskId) -> ReportArtifactScope:
        if not isinstance(value, ReportArtifactScope) or value.task_id != task_id:
            raise ReportPortContractError("Artifact scope 不属于当前 Task")
        return value

    @staticmethod
    def _checked_artifact(value: object, task_id: TaskId) -> ReportArtifactRef:
        if not isinstance(value, ReportArtifactRef) or value.task_id != task_id:
            raise ReportPortContractError("Artifact 引用不属于当前 Task")
        return value


__all__ = ["RunReportV2Workflow"]
