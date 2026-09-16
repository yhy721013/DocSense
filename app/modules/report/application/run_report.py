"""只按 TaskId 恢复并编排一次报告执行的框架无关 Application。"""

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

from app.modules.report.domain import (
    REPORT_STATUS_FAILED,
    REPORT_STATUS_SUCCEEDED,
    ReportAuditError,
    ReportCallbackPayload,
    ReportDomainValidationError,
    ReportError,
    ReportInputSnapshot,
    ReportPortContractError,
    ReportResult,
    ReportSourceNormalizationError,
    ReportSubmission,
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
    ReportCallbackAcquireResult,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackPort,
    ReportFilePort,
    ReportInteractionAuditPort,
    ReportRagAuditOutcome,
    ReportRagCleanupRef,
    ReportRagExecutionError,
    ReportRagPort,
    ReportRagRequest,
    ReportRagResponse,
    ReportRagTrace,
    ReportResourceCleanupOutcome,
    ReportResourceCleanupResult,
    ReportResourceRecoveryPort,
    ReportSourceDownload,
    ReportTemplateDownload,
)

from .submit_report import REPORT_PUBLIC_PROCESSING_STATUS, REPORT_TASK_TYPE


logger = logging.getLogger(__name__)

_PROGRESS_DOWNLOADING = (0.15, "正在下载报告文件")
_PROGRESS_TEMPLATE = (0.25, "正在解析报告模板")
_PROGRESS_GENERATING = (0.35, "正在生成报告")


class RunReportOutcome(str, Enum):
    """Worker 内部执行结果，不映射为新的公开任务状态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    MISSING = "missing"
    NOT_CLAIMED = "not_claimed"


@dataclass(frozen=True)
class ReportTaskCompletion:
    """Task Adapter 需要原子保存的报告终态数据。"""

    callback_payload: ReportCallbackPayload
    report_result: ReportResult | None = None
    report_artifact: ReportArtifactRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.callback_payload, ReportCallbackPayload):
            raise TypeError("callback_payload 必须是 ReportCallbackPayload")
        if self.callback_payload.status == REPORT_STATUS_SUCCEEDED:
            if not isinstance(self.report_result, ReportResult):
                raise TypeError("成功终态必须包含 ReportResult")
            if not isinstance(self.report_artifact, ReportArtifactRef):
                raise TypeError("成功终态必须包含报告 Artifact")
            if self.report_result.report_id != self.callback_payload.report_id:
                raise ValueError("成功结果与回调 report_id 不一致")
        elif self.report_result is not None or self.report_artifact is not None:
            raise ValueError("失败终态不得伪装为成功报告产物")


@dataclass(frozen=True)
class RunReportResult:
    """一次 Worker 调用的内部收敛结果。"""

    task_id: TaskId
    outcome: RunReportOutcome
    error_code: str = ""
    callback_outcome: str = ""
    empty_rag_result: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.outcome, RunReportOutcome):
            raise TypeError("outcome 必须是 RunReportOutcome")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")
        if not isinstance(self.callback_outcome, str):
            raise TypeError("callback_outcome 必须是 str")
        if not isinstance(self.empty_rag_result, bool):
            raise TypeError("empty_rag_result 必须是 bool")


class RunReportTask:
    """报告 Worker 用例。

    本类不读取 Flask request、SQLite、真实路径或环境变量。所有外部步骤都通过 Port，
    并在完整 RAG trace 审计成功之前禁止写成功终态、成功 Progress 或成功回调。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[
            ReportSubmission,
            ReportInputSnapshot,
            ReportTaskCompletion,
        ],
        progress_publisher: ProgressPublisherPort,
        files: ReportFilePort,
        artifacts: ReportArtifactPort,
        rag: ReportRagPort,
        audit: ReportInteractionAuditPort,
        callbacks: ReportCallbackPort,
        resources: ReportResourceRecoveryPort,
    ) -> None:
        self._task_commands = task_commands
        self._progress_publisher = progress_publisher
        self._files = files
        self._artifacts = artifacts
        self._rag = rag
        self._audit = audit
        self._callbacks = callbacks
        if not isinstance(resources, ReportResourceRecoveryPort):
            raise TypeError("resources 必须实现 ReportResourceRecoveryPort")
        self._resources = resources

    @property
    def callbacks(self) -> ReportCallbackPort:
        """暴露只读依赖身份，供组合根阻止正常 Worker 与恢复入口使用两套发送权。"""

        return self._callbacks

    def execute(self, task_id: TaskId) -> RunReportResult:
        """恢复、领取并执行一个报告任务；重复派发会幂等收敛。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        loaded = self._task_commands.get_execution(task_id)
        if loaded is None:
            logger.warning("报告执行不存在，跳过派发: task_id=%s", task_id)
            return RunReportResult(task_id, RunReportOutcome.MISSING)
        self._validate_execution(loaded, task_id)

        claim = self._task_commands.claim(task_id)
        if not isinstance(claim, TaskClaimResult):
            raise ReportPortContractError("TaskCommandPort.claim 返回类型错误")
        if claim.outcome is TaskClaimOutcome.MISSING:
            return RunReportResult(task_id, RunReportOutcome.MISSING)
        if claim.outcome is not TaskClaimOutcome.CLAIMED:
            logger.info(
                "报告任务未取得执行权，幂等跳过: task_id=%s outcome=%s",
                task_id,
                claim.outcome.value,
            )
            return RunReportResult(task_id, RunReportOutcome.NOT_CLAIMED)
        execution = claim.execution
        if not isinstance(execution, TaskExecutionSnapshot):
            raise ReportPortContractError("claimed 结果缺少执行快照")
        snapshot = self._validate_execution(execution, task_id)
        if execution.execution_state != "running":
            raise ReportPortContractError("claimed 执行快照必须处于 running")

        scope: ReportArtifactScope | None = None
        cleanup_ref: ReportRagCleanupRef | None = None
        audit_receipt: ReportAuditReceipt | None = None
        rag_prompt: str | None = None
        rag_started = False
        cleanup_allowed = True
        resources_registered = False
        try:
            scope = self._checked_scope(self._artifacts.begin(task_id), task_id)
            self._track_resource_fact(
                "register",
                lambda: self._resources.register(
                    task_id,
                    execution.business_ref,
                    scope,
                ),
            )
            resources_registered = True
            if not self._update_progress(execution, *_PROGRESS_DOWNLOADING):
                self._cleanup(task_id)
                return RunReportResult(task_id, RunReportOutcome.STALE)

            upload_files = self._prepare_sources(snapshot, scope, task_id)
            if not self._update_progress(execution, *_PROGRESS_TEMPLATE):
                self._cleanup(task_id)
                return RunReportResult(task_id, RunReportOutcome.STALE)
            template_text = self._prepare_template(snapshot, scope, task_id)

            if not self._update_progress(execution, *_PROGRESS_GENERATING):
                self._cleanup(task_id)
                return RunReportResult(task_id, RunReportOutcome.STALE)
            rag_prompt = build_report_prompt(
                template_desc=snapshot.template_desc,
                template_outline=template_text,
                requirement=snapshot.requirement,
            )
            rag_started = True
            rag_response = self._generate_report(
                snapshot,
                task_id,
                upload_files,
                rag_prompt,
            )
            cleanup_ref = rag_response.cleanup_ref
            if cleanup_ref is not None:
                self._track_resource_fact(
                    "rag_cleanup_ref",
                    lambda: self._resources.track_rag_cleanup(task_id, cleanup_ref),
                )
            audit_receipt = self._persist_trace(
                execution,
                rag_response.trace,
                prompt=rag_prompt,
                outcome=ReportRagAuditOutcome.SUCCEEDED,
            )
            self._track_resource_fact(
                "audit_receipt",
                lambda: self._resources.track_audit(audit_receipt),
            )

            try:
                public_content = sanitize_public_report_content(
                    rag_response.raw_content,
                    source_urls=snapshot.source_urls,
                    artifact_sources=tuple(
                        (artifact.artifact_id, artifact.sequence_no)
                        for artifact in upload_files
                    ),
                )
            except ReportDomainValidationError as exc:
                raise ReportPortContractError(str(exc)) from exc
            report_result = build_report_result(
                snapshot.report_id,
                public_content,
            )
            if report_result.empty_rag_result:
                logger.warning(
                    "报告RAG返回空内容，按兼容契约继续成功: "
                    "task_id=%s report_id=%s empty_rag_result=true",
                    task_id,
                    snapshot.report_id.public_value,
                )
            artifact = self._checked_artifact(
                self._artifacts.persist_report_html(
                    scope,
                    report_result.html_details,
                ),
                task_id,
            )
            if artifact.category is not ReportArtifactCategory.REPORT_HTML:
                raise ReportPortContractError("最终报告 Artifact 类别必须是 report_html")
            self._track_resource_fact(
                "final_artifact",
                lambda: self._resources.track_final_artifact(artifact),
            )
            callback_payload = build_report_callback(
                snapshot.report_id,
                report_result.html_details,
                status=REPORT_STATUS_SUCCEEDED,
            )
            completion = ReportTaskCompletion(
                callback_payload=callback_payload,
                report_result=report_result,
                report_artifact=artifact,
            )
            if not self._finish_if_current(
                execution,
                completion,
                execution_state="succeeded",
                public_status=REPORT_STATUS_SUCCEEDED,
                message="报告生成完成",
            ):
                # 旧执行没有提交对该最终 Artifact 的所有权，继续 retain 会制造无法追踪的
                # 孤儿产物。清理端口失败时仍会返回 pending，交由后续清理 Worker 处理。
                self._cleanup(task_id)
                return RunReportResult(task_id, RunReportOutcome.STALE)

            self._publish_terminal_progress(execution, "succeeded")
            callback_outcome = self._deliver_callback(
                execution,
                snapshot,
                callback_payload,
            )
            self._cleanup(task_id)
            logger.info(
                "报告任务应用用例执行完成: task_id=%s report_id=%s callback_outcome=%s",
                task_id,
                snapshot.report_id.public_value,
                callback_outcome,
            )
            return RunReportResult(
                task_id,
                RunReportOutcome.SUCCEEDED,
                callback_outcome=callback_outcome,
                empty_rag_result=report_result.empty_rag_result,
            )
        except ReportTaskPersistenceError as error:
            # 进度/终态条件写可能已经提交但响应丢失。此时绝不能再写“业务失败”，否则会
            # 把成功事实覆盖成失败；也不能清理现场。Dispatcher/后续 Reaper 应重新读取
            # TaskId 决定是否重试或人工恢复。
            logger.critical(
                "报告任务事实写入结果不确定，停止二次终态写并保留现场: "
                "task_id=%s error_code=%s",
                task_id,
                error.code,
                exc_info=True,
            )
            raise
        except ReportRagExecutionError as error:
            cleanup_ref = error.cleanup_ref
            try:
                if cleanup_ref is not None:
                    self._track_resource_fact(
                        "rag_cleanup_ref",
                        lambda: self._resources.track_rag_cleanup(
                            task_id,
                            cleanup_ref,
                        ),
                    )
                if rag_prompt is None:
                    raise ReportPortContractError("RAG失败发生在Prompt冻结之前")
                if error.trace.trace_id != snapshot.trace_id:
                    raise ReportPortContractError("失败 RAG trace_id 与任务输入不一致")
                expected_context_name = build_report_context_name(
                    snapshot.report_id,
                    task_id.value,
                )
                if error.trace.context_name != expected_context_name:
                    raise ReportPortContractError(
                        "失败 RAG trace context_name 与请求不一致"
                    )
                audit_receipt = self._persist_failed_trace(
                    execution,
                    error,
                    prompt=rag_prompt,
                )
                self._track_resource_fact(
                    "audit_receipt",
                    lambda: self._resources.track_audit(audit_receipt),
                )
                if error.external_outcome_unknown:
                    # 交互轨迹已经可靠落库，但供应商写操作可能成功且本地没有拿到完整删除
                    # 引用。此时不能执行常规清理，也不能把“未知”伪装成已删除；先写入
                    # 明确隔离事实，等待运维核查或未来供应商侧幂等键/查询能力恢复。
                    self._track_resource_fact(
                        "rag_side_effect_outcome_unknown",
                        lambda: self._resources.quarantine(
                            task_id,
                            stage="rag_side_effect_outcome_unknown",
                            reason="AnythingLLM 写操作结果未知，禁止自动清理",
                        ),
                    )
                    cleanup_allowed = False
            except ReportTaskPersistenceError as persistence_error:
                # 资源恢复引用写入失败与进度/终态写失败同属提交结果不确定，不能被降级为
                # 普通审计错误后继续补写失败终态。
                logger.critical(
                    "报告RAG失败路径的资源事实写入结果不确定，停止二次终态写: "
                    "task_id=%s error_code=%s",
                    task_id,
                    persistence_error.code,
                    exc_info=True,
                )
                raise
            except Exception as audit_error:
                cleanup_allowed = False
                if isinstance(audit_error, ReportPortContractError):
                    error = audit_error
                else:
                    error = ReportAuditError("报告RAG失败轨迹审计未完成")
            return self._finish_failure(
                execution,
                snapshot,
                error,
                cleanup_allowed=cleanup_allowed,
                resources_registered=resources_registered,
            )
        except Exception as error:
            # RAG 已经开始但 Adapter 没有按端口协议携带 trace 时，无法证明资源范围；为避免
            # 错误清理仍被外部系统引用的资源，保留现场并交由后续运维处理。
            if rag_started and audit_receipt is None:
                cleanup_allowed = False
            return self._finish_failure(
                execution,
                snapshot,
                error,
                cleanup_allowed=cleanup_allowed,
                resources_registered=resources_registered,
            )

    @staticmethod
    def _validate_execution(
        execution: object,
        expected_task_id: TaskId,
    ) -> ReportInputSnapshot:
        if not isinstance(execution, TaskExecutionSnapshot):
            raise ReportPortContractError("任务执行读取结果必须是 TaskExecutionSnapshot")
        snapshot = execution.input_snapshot
        if not isinstance(snapshot, ReportInputSnapshot):
            raise ReportPortContractError("报告任务输入必须是 ReportInputSnapshot")
        expected_ref = TaskBusinessRef(
            REPORT_TASK_TYPE,
            snapshot.report_id.business_key,
        )
        if (
            execution.task_id != expected_task_id
            or execution.task_type != REPORT_TASK_TYPE
            or execution.business_ref != expected_ref
            or snapshot.task_id != expected_task_id.value
            or snapshot.accepted_at != execution.accepted_at
            or execution.trace_id != snapshot.trace_id
        ):
            raise ReportPortContractError("报告执行快照身份不一致")
        return snapshot

    @staticmethod
    def _checked_scope(scope: object, task_id: TaskId) -> ReportArtifactScope:
        if not isinstance(scope, ReportArtifactScope) or scope.task_id != task_id:
            raise ReportPortContractError("Artifact scope 不属于当前任务")
        return scope

    @staticmethod
    def _checked_artifact(artifact: object, task_id: TaskId) -> ReportArtifactRef:
        if not isinstance(artifact, ReportArtifactRef) or artifact.task_id != task_id:
            raise ReportPortContractError("Artifact 引用不属于当前任务")
        return artifact

    def _prepare_sources(
        self,
        snapshot: ReportInputSnapshot,
        scope: ReportArtifactScope,
        task_id: TaskId,
    ) -> tuple[ReportArtifactRef, ...]:
        prepared: list[ReportArtifactRef] = []
        for sequence_no, source_url in enumerate(snapshot.source_urls, start=1):
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
            normalized = downloaded
            try:
                normalized = self._checked_artifact(
                    self._files.normalize_source(downloaded),
                    task_id,
                )
                if (
                    normalized.category
                    is not ReportArtifactCategory.NORMALIZED_SOURCE
                    or normalized.sequence_no != sequence_no
                ):
                    raise ReportPortContractError(
                        "规范化 Artifact 类别或源文件顺序不一致"
                    )
            except ReportSourceNormalizationError:
                logger.warning(
                    "报告源文件规范化失败，按兼容规则回退原文件: "
                    "task_id=%s sequence_no=%s",
                    task_id,
                    sequence_no,
                )
            upload_items = tuple(self._files.prepare_upload_files(normalized))
            if not upload_items:
                raise ReportPortContractError("文件准备端口不得返回空上传列表")
            for item in upload_items:
                checked = self._checked_artifact(item, task_id)
                if (
                    checked.category is not ReportArtifactCategory.RAG_INPUT
                    or checked.sequence_no != sequence_no
                ):
                    raise ReportPortContractError(
                        "RAG 上传 Artifact 类别或源文件顺序不一致"
                    )
                prepared.append(checked)
        return tuple(prepared)

    def _prepare_template(
        self,
        snapshot: ReportInputSnapshot,
        scope: ReportArtifactScope,
        task_id: TaskId,
    ) -> str:
        template = self._checked_artifact(
            self._files.download_template(
                ReportTemplateDownload(scope, snapshot.template_outline_url)
            ),
            task_id,
        )
        if template.category is not ReportArtifactCategory.TEMPLATE:
            raise ReportPortContractError("模板 Artifact 类别必须是 template")
        template_text = self._files.extract_template_text(template)
        if not isinstance(template_text, str):
            raise ReportPortContractError("模板提取端口必须返回 str")
        normalized = template_text.strip()
        if not normalized:
            raise ReportTemplateError("Word模板未提取到有效文字内容")
        return normalized

    def _generate_report(
        self,
        snapshot: ReportInputSnapshot,
        task_id: TaskId,
        upload_files: tuple[ReportArtifactRef, ...],
        prompt: str,
    ) -> ReportRagResponse:
        request = ReportRagRequest(
            task_id=task_id,
            trace_id=snapshot.trace_id,
            ordered_source_files=upload_files,
            prompt=prompt,
            context_name=build_report_context_name(
                snapshot.report_id,
                task_id.value,
            ),
            conversation_name=build_report_conversation_name(snapshot.report_id),
        )
        response = self._rag.generate(request)
        if not isinstance(response, ReportRagResponse):
            raise ReportPortContractError("ReportRagPort.generate 返回类型错误")
        if response.trace.trace_id != snapshot.trace_id:
            raise ReportPortContractError("RAG trace_id 与任务输入不一致")
        if response.trace.context_name != request.context_name:
            raise ReportPortContractError("RAG trace context_name 与请求不一致")
        return response

    def _persist_trace(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        trace: ReportRagTrace,
        *,
        prompt: str,
        outcome: ReportRagAuditOutcome,
        error_code: str = "",
    ) -> ReportAuditReceipt:
        task_id = execution.task_id
        idempotency_key = f"report-rag:{task_id.value}"
        try:
            receipt = self._audit.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=execution.business_ref,
                    idempotency_key=idempotency_key,
                    prompt=prompt,
                    trace=trace,
                    outcome=outcome,
                    error_code=error_code,
                )
            )
        except Exception as error:
            logger.critical(
                "报告交互审计失败，禁止成功终态并保留现场: "
                "task_id=%s error_type=%s",
                task_id,
                type(error).__name__,
                exc_info=True,
            )
            if isinstance(error, ReportAuditError):
                raise
            raise ReportAuditError("报告RAG交互审计失败") from error
        if (
            not isinstance(receipt, ReportAuditReceipt)
            or receipt.task_id != task_id
            or receipt.idempotency_key != idempotency_key
        ):
            raise ReportPortContractError("Audit Receipt 与当前任务不一致")
        return receipt

    def _persist_failed_trace(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        error: ReportRagExecutionError,
        *,
        prompt: str,
    ) -> ReportAuditReceipt:
        return self._persist_trace(
            execution,
            error.trace,
            prompt=prompt,
            outcome=ReportRagAuditOutcome.FAILED,
            error_code=error.code,
        )

    def _update_progress(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
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
                    public_status=REPORT_PUBLIC_PROCESSING_STATUS,
                )
            )
        except Exception as error:
            raise ReportTaskPersistenceError("报告进度事实写入失败") from error
        if not isinstance(updated, bool):
            raise ReportPortContractError("进度条件写必须返回 bool")
        if not updated:
            logger.info(
                "报告进度条件写发现旧执行，停止后续业务步骤: task_id=%s progress=%s",
                execution.task_id,
                progress,
            )
            return False
        self._publish_progress(execution, progress, message, "running")
        return True

    def _publish_progress(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        progress: float,
        message: str,
        internal_state: str,
    ) -> None:
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
            # Progress 是通知投影，不是任务事实。通知失败不能把已经提交的业务状态回滚。
            logger.exception(
                "报告Progress通知失败，任务事实保持不变: "
                "task_id=%s progress=%s internal_state=%s",
                execution.task_id,
                progress,
                internal_state,
            )

    def _publish_terminal_progress(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        internal_state: str,
    ) -> None:
        self._publish_progress(execution, 1.0, "", internal_state)

    def _finish_if_current(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        result: ReportTaskCompletion,
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
            raise ReportTaskPersistenceError("报告终态事实写入失败") from error
        if not isinstance(finished, bool):
            raise ReportPortContractError("终态条件写必须返回 bool")
        if not finished:
            logger.info(
                "报告终态条件写发现旧执行，禁止Progress和回调: task_id=%s",
                execution.task_id,
            )
        return finished

    def _finish_failure(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        snapshot: ReportInputSnapshot,
        error: BaseException,
        *,
        cleanup_allowed: bool,
        resources_registered: bool,
    ) -> RunReportResult:
        error_code = error.code if isinstance(error, ReportError) else "report_unexpected_error"
        logger.exception(
            "报告任务应用用例执行失败: task_id=%s report_id=%s "
            "error_code=%s error_type=%s",
            execution.task_id,
            snapshot.report_id.public_value,
            error_code,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        callback_payload = build_report_callback(
            snapshot.report_id,
            "",
            status=REPORT_STATUS_FAILED,
        )
        completion = ReportTaskCompletion(callback_payload=callback_payload)
        try:
            finished = self._finish_if_current(
                execution,
                completion,
                execution_state="failed",
                public_status=REPORT_STATUS_FAILED,
                message="报告生成失败",
            )
        except Exception:
            logger.exception(
                "报告失败终态持久化异常: task_id=%s original_error_code=%s",
                execution.task_id,
                error_code,
            )
            if not cleanup_allowed:
                self._preserve_scene(
                    execution.task_id,
                    error_code,
                    resources_registered=resources_registered,
                )
            raise
        if not finished:
            if cleanup_allowed:
                if resources_registered:
                    self._cleanup(execution.task_id)
            else:
                self._preserve_scene(
                    execution.task_id,
                    error_code,
                    resources_registered=resources_registered,
                )
            return RunReportResult(
                execution.task_id,
                RunReportOutcome.STALE,
                error_code=error_code,
            )

        self._publish_terminal_progress(execution, "failed")
        callback_outcome = self._deliver_callback(
            execution,
            snapshot,
            callback_payload,
        )
        if cleanup_allowed:
            if resources_registered:
                self._cleanup(execution.task_id)
        else:
            self._preserve_scene(
                execution.task_id,
                error_code,
                resources_registered=resources_registered,
            )
        return RunReportResult(
            execution.task_id,
            RunReportOutcome.FAILED,
            error_code=error_code,
            callback_outcome=callback_outcome,
        )

    def _deliver_callback(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
        snapshot: ReportInputSnapshot,
        payload: ReportCallbackPayload,
    ) -> str:
        # 进入本方法前业务终态已经成功提交。latest 查询、Guard 存储和 HTTP 投递属于
        # 独立的回调交付维度；其中任一步失败都不能逃逸到 execute 的业务失败分支，
        # 否则可能再次提交失败终态并覆盖已经确定的成功/失败事实。
        try:
            latest = self._task_commands.is_latest(
                execution.task_id,
                execution.business_ref,
            )
            if not isinstance(latest, bool):
                raise ReportPortContractError("is_latest 必须返回 bool")
            if not latest:
                logger.info(
                    "报告回调前发现旧执行，跳过网络调用: task_id=%s",
                    execution.task_id,
                )
                return ReportCallbackDeliveryOutcome.STALE.value

            acquire = self._callbacks.acquire(
                ReportCallbackAcquire(execution.task_id, snapshot.report_id)
            )
            if not isinstance(acquire, ReportCallbackAcquireResult):
                raise ReportPortContractError("Callback acquire 返回类型错误")
            if acquire.outcome is not ReportCallbackAcquireOutcome.ACQUIRED:
                logger.warning(
                    "报告回调未取得发送权: task_id=%s outcome=%s",
                    execution.task_id,
                    acquire.outcome.value,
                )
                return acquire.outcome.value
            lease = acquire.lease
            if lease is None or lease.task_id != execution.task_id:
                raise ReportPortContractError("Callback Guard Lease 与任务不一致")

            delivery = self._callbacks.deliver(
                DeliverReportCallback(lease, payload)
            )
            if not isinstance(delivery, ReportCallbackDeliveryResult):
                raise ReportPortContractError("Callback deliver 返回类型错误")
            if delivery.outcome is ReportCallbackDeliveryOutcome.STALE:
                # Adapter 已在网络调用前完成权威 Guard 复核。未通过时没有发生 HTTP 副作用，
                # 且 Guard 可能已经被过期冻结或由新任务接管；旧 Worker 不再尝试完成旧租约。
                logger.info(
                    "报告回调发送前已判定为过期，跳过完成旧租约: task_id=%s detail=%s",
                    execution.task_id,
                    delivery.detail,
                )
                return ReportCallbackDeliveryOutcome.STALE.value
            completed = self._callbacks.complete(lease, delivery, payload)
            if not isinstance(completed, bool):
                raise ReportPortContractError("Callback complete 必须返回 bool")
            if not completed:
                # token/fencing CAS 未命中说明当前 Worker 已失去完成权。HTTP 可能已经发出，
                # 因而不能把它降级为普通失败或静默成功；Adapter 会记录可检索告警。
                raise ReportPortContractError("Callback Guard 完成权已过期")
            return delivery.outcome.value
        except Exception:
            # 回调维度异常不能覆盖已确定的业务终态。Guard 的发送权和精确 outcome 均由
            # Adapter 持久化；这里保留 port_error 与堆栈供 Dispatcher/Reaper 告警。
            logger.exception(
                "报告回调端口执行异常，业务终态保持不变: task_id=%s",
                execution.task_id,
            )
            return "port_error"

    def _cleanup(
        self,
        task_id: TaskId,
    ) -> None:
        try:
            cleanup_result = self._resources.cleanup(task_id)
            if not isinstance(cleanup_result, ReportResourceCleanupResult):
                raise ReportPortContractError("Resource cleanup 返回类型错误")
            if cleanup_result.outcome is not ReportResourceCleanupOutcome.CLEANED:
                logger.warning(
                    "报告资源清理尚未收敛: task_id=%s outcome=%s "
                    "pending_external=%s pending_artifact_count=%s",
                    task_id,
                    cleanup_result.outcome.value,
                    cleanup_result.pending_external,
                    cleanup_result.pending_artifact_count,
                )
        except Exception:
            logger.exception(
                "报告资源清理端口失败，业务终态保持不变: task_id=%s",
                task_id,
            )

    @staticmethod
    def _track_resource_fact(stage: str, operation: object) -> None:
        """把资源引用持久化失败升级为任务事实不确定，禁止继续形成成功终态。"""

        if not callable(operation):
            raise TypeError("operation 必须可调用")
        try:
            operation()
        except Exception as error:
            raise ReportTaskPersistenceError(
                f"报告资源恢复事实写入失败: {stage}"
            ) from error

    def _preserve_scene(
        self,
        task_id: TaskId,
        error_code: str,
        *,
        resources_registered: bool,
    ) -> None:
        if resources_registered:
            try:
                self._resources.quarantine(
                    task_id,
                    stage="audit_gate",
                    reason=f"审计证据不完整: {error_code}",
                )
            except Exception:
                logger.exception(
                    "报告资源隔离事实写入失败: task_id=%s error_code=%s",
                    task_id,
                    error_code,
                )
        self._log_preserved_scene(task_id, error_code)

    @staticmethod
    def _log_preserved_scene(task_id: TaskId, error_code: str) -> None:
        logger.critical(
            "报告审计证据不完整，禁止自动清理并保留现场: "
            "task_id=%s error_code=%s",
            task_id,
            error_code,
        )


__all__ = [
    "ReportTaskCompletion",
    "RunReportOutcome",
    "RunReportResult",
    "RunReportTask",
]
