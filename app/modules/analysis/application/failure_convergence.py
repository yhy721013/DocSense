"""文件分析的 expected TaskId 条件写、失败收敛与进度通知协作器。"""

from __future__ import annotations

import logging
from typing import Any

from app.modules.tasks.domain import (
    ProgressKey,
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
)
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    GuardedProgressPublisherPort,
    ProgressPublication,
    TaskCommandPort,
)

from app.modules.analysis.domain.architecture_recall import (
    ArchitecturePromptBudgetError,
    ArchitectureRecallError,
)
from app.modules.analysis.domain.architecture_tree import ArchitectureTreeValidationError
from app.modules.analysis.domain.callback_payloads import build_file_callback_payload
from app.modules.analysis.domain.errors import (
    AnalysisContractError,
    ArchitectureContractError,
)
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_BUSINESS_TYPE,
    AnalysisTaskInputV1,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackPort,
    AnalysisCallbackRequest,
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisRagExecutionError,
    AnalysisRagPort,
    PreparedAnalysisDocument,
)

from .audit_lifecycle import _AnalysisAuditLifecycle
from .recover_resources import AnalysisResourceLifecycle
from .workflow_models import (
    AnalysisApplicationContractError,
    AnalysisTaskCompletion,
    AnalysisTaskPersistenceError,
    RunAnalysisOutcome,
    RunAnalysisResult,
    _AnalysisKnownFailure,
    _RagWorkflowState,
)


# 这些值仅复用现有 ``llm_tasks`` 的公开存储投影，不能被当成新增 API 枚举或响应字段。
_ANALYSIS_PUBLIC_PROCESSING_STATUS = "1"
_ANALYSIS_PUBLIC_SUCCEEDED_STATUS = "2"
_ANALYSIS_PUBLIC_FAILED_STATUS = "3"
_ANALYSIS_TASK_TYPE = ANALYSIS_BUSINESS_TYPE


# 保持拆分前的日志分类，避免日志采集和既有检索规则因模块路径变化而失效。
logger = logging.getLogger("app.modules.analysis.application.run_analysis")


class _AnalysisFailureConvergence:
    """把条件写、通知与失败路径集中到一个可测试的内部协作器。"""

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[object, AnalysisTaskInputV1, AnalysisTaskCompletion],
        progress_publisher: GuardedProgressPublisherPort,
        audit_lifecycle: _AnalysisAuditLifecycle,
        callbacks: AnalysisCallbackPort | None = None,
        callback_url: str = "",
    ) -> None:
        if not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(progress_publisher, GuardedProgressPublisherPort):
            raise TypeError("progress_publisher 必须实现 GuardedProgressPublisherPort")
        if not isinstance(audit_lifecycle, _AnalysisAuditLifecycle):
            raise TypeError("audit_lifecycle 必须是 _AnalysisAuditLifecycle")
        if callbacks is not None and not isinstance(callbacks, AnalysisCallbackPort):
            raise TypeError("callbacks 必须实现 AnalysisCallbackPort 或为 None")
        if not isinstance(callback_url, str):
            raise TypeError("callback_url 必须是 str")
        if callbacks is None and callback_url.strip():
            raise ValueError("未注入 callbacks 时不得配置 callback_url")
        self._task_commands = task_commands
        self._progress_publisher = progress_publisher
        self._audit_lifecycle = audit_lifecycle
        self._callbacks = callbacks
        self._callback_url = callback_url.strip()

    def finish_pre_rag_failure(
        self,
        *,
        execution: AnalysisExecutionRef,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        state: _RagWorkflowState,
        error: BaseException,
        started_at: float,
    ) -> RunAnalysisResult:
        """文件准备/纯规则/召回预留阶段失败时仅收敛一次任务终态。"""

        self._audit_lifecycle.finalize_recall_failure(
            state,
            error,
            self.failure_stage(error),
            started_at,
            self.safe_error_code,
        )
        return self.finish_failure(
            task_execution=task_execution,
            snapshot=snapshot,
            error=error,
            stage=self.failure_stage(error),
        )

    def finish_rag_failure(
        self,
        *,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        error: BaseException,
        stage: str,
        started_at: float,
        resources: AnalysisResourceLifecycle | None = None,
    ) -> RunAnalysisResult:
        """先补足审计，再写失败终态；审计不完整或 unknown 时禁止自动 close。"""

        self._audit_lifecycle.finalize_recall_failure(
            state,
            error,
            stage,
            started_at,
            self.safe_error_code,
        )
        if resources is not None:
            try:
                resources.record_recall_state(state, failed=True)
            except Exception:
                # 已有 RAG 生命周期而资源事实无法推进时，仍要尽力提交任务失败终态，但绝不
                # 自动 close/delete；资源记录保留给后续人工/恢复排查。
                state.preserve_scene = True
                logger.critical(
                    "文件分析失败后无法持久化召回审计资源事实，禁止自动清理: task_id=%s",
                    execution.task_id,
                    exc_info=True,
                )
        audit_error: BaseException | None = None
        if state.interaction_audit_attempted and state.interaction_receipt is None:
            # ``persist_interaction`` 在发起 Port 调用前就记录 attempted。这样当调用抛出
            # 异常时，失败收敛路径不会以同一个幂等键盲目重放可能已提交的审计写入；同时
            # 必须明确把故障归类为审计故障并保留 RAG 现场，等待后续人工或恢复任务处理。
            audit_error = error
            state.preserve_scene = True
            logger.critical(
                "文件分析交互审计调用结果不确定，禁止重试和自动清理: task_id=%s error_type=%s",
                execution.task_id,
                type(error).__name__,
                exc_info=True,
            )
        elif (
            state.interaction_receipt is None
            and state.lifecycle_events
        ):
            try:
                state.interaction_receipt = self._audit_lifecycle.persist_interaction(
                    execution=execution,
                    snapshot=snapshot,
                    state=state,
                    outcome=AnalysisAuditOutcome.FAILED,
                    error_code=self.safe_error_code(error),
                )
                if resources is not None:
                    resources.record_interaction_receipt(state.interaction_receipt)
            except Exception as exc:
                audit_error = exc
                state.preserve_scene = True
                logger.critical(
                    "文件分析失败路径交互审计未完成，禁止自动清理: task_id=%s error_type=%s",
                    execution.task_id,
                    type(exc).__name__,
                    exc_info=True,
                )
        if resources is not None and (
            audit_error is not None or state.interaction_receipt is None
        ):
            try:
                resources.mark_audit_pending(audit_error or error)
            except Exception:
                state.preserve_scene = True
                logger.critical(
                    "文件分析审计失败现场未能写入资源记录，禁止自动清理: task_id=%s",
                    execution.task_id,
                    exc_info=True,
                )
        effective_error = audit_error or error
        effective_stage = "audit" if audit_error is not None else stage
        result = self.finish_failure(
            task_execution=task_execution,
            snapshot=snapshot,
            error=effective_error,
            stage=effective_stage,
        )
        if (
            state.interaction_receipt is not None
            and state.session is not None
            and state.opened
            and not state.preserve_scene
        ):
            self.close_audited_session(
                execution=execution,
                state=state,
                rag=rag,
                retain_document=state.retain_document,
                resources=resources,
            )
        elif state.session is not None and (state.preserve_scene or audit_error is not None):
            logger.critical(
                "文件分析失败保留 RAG 现场，等待后续资源恢复: task_id=%s stage=%s",
                execution.task_id,
                effective_stage,
            )
        return result

    def close_audited_session(
        self,
        *,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        retain_document: bool,
        resources: AnalysisResourceLifecycle | None = None,
    ) -> None:
        """以“意图 -> running -> 外部结果 -> 审计”顺序关闭已审计 RAG 会话。

        没有注入资源协作器时保持 1F-3 的纯审计行为，供仍未切线的兼容测试使用；新
        1F-6 组合必须传入 ``resources``，否则不会声称拥有可恢复清理事实。
        """

        if state.session is None or state.interaction_receipt is None:
            return
        if resources is not None and not isinstance(resources, AnalysisResourceLifecycle):
            raise TypeError("resources 必须是 AnalysisResourceLifecycle 或 None")
        if resources is not None:
            try:
                # 两次 CAS 都必须成功，才允许发起 RAG close。这样进程在远端调用前中断时
                # 至少留下 planned/running 意图，恢复器仍会 fail closed 而不是猜测删除。
                resources.prepare_close(retain_document=retain_document)
                resources.mark_close_running()
            except Exception:
                state.preserve_scene = True
                logger.critical(
                    "文件分析 RAG close 前无法保存清理意图，禁止外部关闭: task_id=%s",
                    execution.task_id,
                    exc_info=True,
                )
                return
        self._audit_lifecycle.close_audited_session(
            execution=execution,
            state=state,
            rag=rag,
            retain_document=retain_document,
            on_close_result=(resources.record_close_result if resources is not None else None),
            on_lifecycle_audited=(resources.mark_close_audited if resources is not None else None),
            on_close_failure=(resources.record_close_failure if resources is not None else None),
        )

    def finish_success(
        self,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        mapped_result: dict[str, Any],
    ) -> bool:
        """提交成功终态及其已有 Callback 投影。"""

        callback_payload = FrozenJsonObject.from_mapping(
            build_file_callback_payload(
                snapshot.file_name,
                mapped_result,
                status=_ANALYSIS_PUBLIC_SUCCEEDED_STATUS,
            ),
            name="analysis_success_callback",
        )
        completion = AnalysisTaskCompletion(
            callback_payload=callback_payload,
            succeeded=True,
            mapped_result=FrozenJsonObject.from_mapping(
                mapped_result,
                name="analysis_mapped_result",
            ),
        )
        finished = self.finish_if_current(
            task_execution,
            completion,
            execution_state="succeeded",
            public_status=_ANALYSIS_PUBLIC_SUCCEEDED_STATUS,
            message="解析完成",
        )
        if finished:
            self.publish_progress(task_execution, 1.0, "", "succeeded")
            self._deliver_terminal_callback(
                task_execution=task_execution,
                snapshot=snapshot,
                payload=completion.callback_payload,
            )
        return finished

    def finish_failure(
        self,
        *,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        error: BaseException,
        stage: str,
    ) -> RunAnalysisResult:
        """提交失败终态；被 stale 拒绝时绝不反向覆盖已有终态。"""

        error_code = self.safe_error_code(error)
        logger.exception(
            "文件分析任务新 Application 执行失败: task_id=%s file_name=%s stage=%s error_code=%s error_type=%s",
            task_execution.task_id,
            snapshot.file_name,
            stage,
            error_code,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        completion = AnalysisTaskCompletion(
            callback_payload=FrozenJsonObject.from_mapping(
                build_file_callback_payload(
                    snapshot.file_name,
                    {},
                    status=_ANALYSIS_PUBLIC_FAILED_STATUS,
                ),
                name="analysis_failure_callback",
            ),
            succeeded=False,
        )
        finished = self.finish_if_current(
            task_execution,
            completion,
            execution_state="failed",
            public_status=_ANALYSIS_PUBLIC_FAILED_STATUS,
            message=f"解析失败（{stage}）：{error_code}",
        )
        if not finished:
            return RunAnalysisResult(
                task_execution.task_id,
                RunAnalysisOutcome.STALE,
                error_code=error_code,
                stage=stage,
            )
        self.publish_progress(task_execution, 1.0, "", "failed")
        self._deliver_terminal_callback(
            task_execution=task_execution,
            snapshot=snapshot,
            payload=completion.callback_payload,
        )
        return RunAnalysisResult(
            task_execution.task_id,
            RunAnalysisOutcome.FAILED,
            error_code=error_code,
            stage=stage,
        )

    def _deliver_terminal_callback(
        self,
        *,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        payload: FrozenJsonObject,
    ) -> None:
        """在终态条件写成功后至多投递一次，不让 Callback 覆盖任务事实。

        ``callbacks`` 未注入时属于尚未切线的内部兼容路径；一旦注入，无论 URL 是否为空都
        会先获取 Guard，再由 Adapter 把空地址收敛为 ``skipped``，避免遗留可发送状态。
        """

        if self._callbacks is None:
            return
        execution = AnalysisExecutionRef(
            task_id=task_execution.task_id,
            file_name=snapshot.file_name,
            batch_id=snapshot.batch_id,
            batch_sequence=snapshot.batch_sequence,
        )
        try:
            acquired = self._callbacks.acquire(
                AnalysisCallbackRequest(
                    execution=execution,
                    callback_url=self._callback_url,
                    payload=payload,
                )
            )
            if not isinstance(acquired, AnalysisCallbackAcquireResult):
                raise AnalysisApplicationContractError(
                    "Analysis Callback acquire 返回类型错误"
                )
            if acquired.outcome is not AnalysisCallbackAcquireOutcome.ACQUIRED:
                logger.info(
                    "文件分析终态回调未获得发送权，保持 Guard 现有事实: "
                    "task_id=%s file_name=%s outcome=%s",
                    execution.task_id,
                    execution.file_name,
                    acquired.outcome.value,
                )
                return
            lease = acquired.lease
            if lease is None or lease.execution != execution:
                raise AnalysisApplicationContractError("Analysis Callback lease 与执行身份不一致")
            delivery = self._callbacks.deliver(
                AnalysisCallbackDeliveryRequest(
                    lease=lease,
                    callback_url=self._callback_url,
                    payload=payload,
                )
            )
            if not isinstance(delivery, AnalysisCallbackDelivery):
                raise AnalysisApplicationContractError(
                    "Analysis Callback deliver 返回类型错误"
                )
            if delivery.outcome is AnalysisCallbackDeliveryOutcome.STALE:
                logger.info(
                    "文件分析终态回调发送前已失去 Guard，禁止触网后完成: task_id=%s",
                    execution.task_id,
                )
                return
            completed = self._callbacks.complete(lease, delivery, payload)
            if not isinstance(completed, bool):
                raise AnalysisApplicationContractError(
                    "Analysis Callback complete 必须返回 bool"
                )
            if not completed:
                logger.error(
                    "文件分析终态回调 Guard 完成 CAS 未确认，禁止当前调用重发: task_id=%s",
                    execution.task_id,
                )
                return
            logger.log(
                logging.INFO
                if delivery.outcome is AnalysisCallbackDeliveryOutcome.DELIVERED
                else logging.WARNING,
                "文件分析终态回调已收敛: task_id=%s file_name=%s outcome=%s",
                execution.task_id,
                execution.file_name,
                delivery.outcome.value,
            )
        except Exception:
            # Callback 是任务终态后的投影。异常不能反向改写 execution，也不能在本次
            # Worker 调用内重试，因为 HTTP 是否抵达接收方可能无法判断。
            logger.exception(
                "文件分析终态回调失败，任务终态保持不变: task_id=%s",
                execution.task_id,
            )

    def update_progress(
        self,
        execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        progress: float,
        message: str,
    ) -> bool:
        """执行 expected TaskId 进度条件写，并在成功后投影 Guarded Progress。"""

        try:
            updated = self._task_commands.update_progress_if_current(
                ExpectedProgressUpdate(
                    expected_task_id=execution.task_id,
                    business_ref=execution.business_ref,
                    progress=progress,
                    message=message,
                    execution_state="running",
                    public_status=_ANALYSIS_PUBLIC_PROCESSING_STATUS,
                )
            )
        except Exception as exc:
            raise AnalysisTaskPersistenceError("文件分析进度事实写入失败") from exc
        if not isinstance(updated, bool):
            raise AnalysisApplicationContractError("进度条件写必须返回 bool")
        if not updated:
            logger.info(
                "文件分析进度条件写发现旧执行，停止后续步骤: task_id=%s progress=%s",
                execution.task_id,
                progress,
            )
            return False
        self.publish_progress(execution, progress, message, "running")
        return True

    def finish_if_current(
        self,
        execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        completion: AnalysisTaskCompletion,
        *,
        execution_state: str,
        public_status: str,
        message: str,
    ) -> bool:
        """执行最终 expected TaskId 条件写。"""

        try:
            finished = self._task_commands.finish_if_current(
                ExpectedTaskCompletion(
                    expected_task_id=execution.task_id,
                    business_ref=execution.business_ref,
                    execution_state=execution_state,
                    public_status=public_status,
                    message=message,
                    result=completion,
                )
            )
        except Exception as exc:
            raise AnalysisTaskPersistenceError("文件分析终态事实写入失败") from exc
        if not isinstance(finished, bool):
            raise AnalysisApplicationContractError("终态条件写必须返回 bool")
        if not finished:
            logger.info(
                "文件分析终态条件写发现旧执行，禁止终态 Progress/回调: task_id=%s",
                execution.task_id,
            )
        return finished

    def publish_progress(
        self,
        execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        progress: float,
        message: str,
        internal_state: str,
    ) -> None:
        """发布可丢失的进度通知，不回滚已提交的任务事实。"""

        publication = ProgressPublication(
            key=ProgressKey(_ANALYSIS_TASK_TYPE, execution.business_ref.business_key),
            expected_task_id=execution.task_id,
            progress=progress,
            message=message,
            internal_state=internal_state,
        )
        try:
            published = self._progress_publisher.publish_guarded(
                publication,
                is_current=lambda: self.is_latest(execution),
            )
            if not isinstance(published, bool):
                raise AnalysisApplicationContractError("Guarded Progress 发布必须返回 bool")
            if not published:
                logger.info(
                    "文件分析 Progress Guard 发现旧执行，跳过通知: task_id=%s progress=%s",
                    execution.task_id,
                    progress,
                )
        except Exception:
            # Progress 是通知投影；通知错误不回滚已经提交的任务事实。
            logger.exception(
                "文件分析 Progress 通知失败，任务事实保持不变: task_id=%s progress=%s state=%s",
                execution.task_id,
                progress,
                internal_state,
            )

    def is_latest(self, execution: TaskExecutionSnapshot[AnalysisTaskInputV1]) -> bool:
        """查询当前 TaskId 是否仍为唯一 owner。"""

        try:
            latest = self._task_commands.is_latest(execution.task_id, execution.business_ref)
        except Exception as exc:
            raise AnalysisTaskPersistenceError("文件分析 latest owner 查询失败") from exc
        if not isinstance(latest, bool):
            raise AnalysisApplicationContractError("is_latest 必须返回 bool")
        return latest

    @staticmethod
    def validate_execution(
        execution: object,
        expected_task_id: TaskId,
    ) -> AnalysisTaskInputV1:
        """验证读取/领取快照与输入冻结事实属于同一任务。"""

        if not isinstance(execution, TaskExecutionSnapshot):
            raise AnalysisApplicationContractError("任务执行读取结果必须是 TaskExecutionSnapshot")
        snapshot = execution.input_snapshot
        if not isinstance(snapshot, AnalysisTaskInputV1):
            raise AnalysisApplicationContractError("文件分析任务输入必须是 AnalysisTaskInputV1")
        expected_business_ref = TaskBusinessRef(ANALYSIS_BUSINESS_TYPE, snapshot.file_name)
        if (
            execution.task_id != expected_task_id
            or execution.task_type != _ANALYSIS_TASK_TYPE
            or execution.business_ref != expected_business_ref
            or snapshot.task_id != expected_task_id.value
            or snapshot.accepted_at != execution.accepted_at
            or snapshot.trace_id != execution.trace_id
        ):
            raise AnalysisApplicationContractError("文件分析执行快照身份不一致")
        return snapshot

    @staticmethod
    def require_prepared_document(
        prepared: object,
        execution: AnalysisExecutionRef,
    ) -> None:
        """验证文件准备结果的运行身份，禁止跨任务文件泄漏。"""

        if not isinstance(prepared, PreparedAnalysisDocument):
            raise AnalysisApplicationContractError("FilePreparationPort.prepare 返回类型错误")
        if prepared.execution != execution:
            raise AnalysisApplicationContractError("准备文件不属于当前 execution")

    @staticmethod
    def safe_error_code(error: BaseException) -> str:
        """把异常映射为既有稳定错误码，禁止泄漏正文或未控异常文本。"""

        if isinstance(error, _AnalysisKnownFailure):
            return error.error_code
        if isinstance(error, AnalysisRagExecutionError):
            return error.error_code
        if isinstance(error, (AnalysisContractError, ArchitectureRecallError, ValueError)):
            text = " ".join(str(error).split())
            return text[:500] or "analysis_contract_error"
        return f"analysis_unexpected_{type(error).__name__.lower()}"

    @staticmethod
    def failure_stage(error: BaseException) -> str:
        """把受控领域失败映射为既有稳定阶段标签。"""

        if isinstance(error, _AnalysisKnownFailure):
            return error.stage
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
