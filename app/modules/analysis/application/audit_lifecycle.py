"""文件分析 RAG 生命周期和审计事实协作器。

协作器只操作调用方传入的单次 ``_RagWorkflowState``，不会缓存 Session、Receipt 或外部
资源。异常后的保留现场策略仍由失败收敛协作器决定，避免审计层擅自补偿。
"""

from __future__ import annotations

import logging
from typing import Callable

from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.analysis.domain.task_inputs import AnalysisTaskInputV1, FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
    AnalysisAuditPort,
    AnalysisExecutionRef,
    AnalysisInteractionAuditReceipt,
    AnalysisInteractionAuditRecord,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseRequest,
    AnalysisRagCloseResult,
    AnalysisRagPort,
    AnalysisRecallAuditReceipt,
    AnalysisRecallAuditRecord,
    AppendAnalysisLifecycleEvents,
    FinalizeAnalysisRecallAudit,
)

from .model_workflow import _AnalysisModelWorkflow
from .workflow_models import (
    AnalysisApplicationContractError,
    _AnalysisWorkflowPlan,
    _RagWorkflowState,
)


# 保持拆分前的日志分类，避免日志采集和既有检索规则因模块路径变化而失效。
logger = logging.getLogger("app.modules.analysis.application.run_analysis")


class _AnalysisAuditLifecycle:
    """负责 RAG 生命周期对应的召回、交互和关闭审计。"""

    def __init__(self, audit: AnalysisAuditPort) -> None:
        if not isinstance(audit, AnalysisAuditPort):
            raise TypeError("audit 必须实现 AnalysisAuditPort")
        self._audit = audit

    def reserve_recall(
        self,
        execution: AnalysisExecutionRef,
        plan: _AnalysisWorkflowPlan,
    ) -> AnalysisRecallAuditReceipt:
        """预留当前 execution 的召回审计事实。"""

        record = AnalysisRecallAuditRecord(
            execution=execution,
            idempotency_key=f"analysis-recall:{execution.task_id.value}",
            payload=plan.recall_payload,
        )
        receipt = self._audit.reserve_recall(record)
        if (
            not isinstance(receipt, AnalysisRecallAuditReceipt)
            or receipt.execution != execution
            or receipt.idempotency_key != record.idempotency_key
        ):
            raise AnalysisApplicationContractError("召回审计 Receipt 与当前任务不一致")
        return receipt

    def finalize_recall_success(
        self,
        state: _RagWorkflowState,
        architecture_id: int,
        returned_rank: int,
        started_at: float,
    ) -> None:
        """在完整模型结果产生后终结成功召回审计。"""

        if state.recall_receipt is None or state.recall_finalized:
            return
        command = FinalizeAnalysisRecallAudit(
            receipt=state.recall_receipt,
            expected_version=state.recall_receipt.version,
            outcome=AnalysisAuditOutcome.SUCCEEDED,
            payload=FrozenJsonObject.from_mapping(
                {
                    "returned_architecture_id": architecture_id,
                    "returned_rank": returned_rank,
                    "total_elapsed_ms": _AnalysisModelWorkflow.elapsed_ms(started_at),
                    "failure_stage": None,
                    "error_message": "",
                },
                name="recall_success",
            ),
        )
        receipt = self._audit.finalize_recall(command)
        if (
            not isinstance(receipt, AnalysisRecallAuditReceipt)
            or receipt.execution != state.recall_receipt.execution
            or not receipt.finalized
        ):
            raise AnalysisApplicationContractError("召回审计终结 Receipt 无效")
        state.recall_finalized = True

    def finalize_recall_failure(
        self,
        state: _RagWorkflowState,
        error: BaseException,
        stage: str,
        started_at: float,
        safe_error_code: Callable[[BaseException], str],
    ) -> None:
        """尽力终结已预留召回审计；失败只阻断后续副作用，不覆盖原任务异常。"""

        if state.recall_receipt is None or state.recall_finalized:
            return
        stable_stage = stage if stage in {
            "architecture_index",
            "architecture_recall",
            "architecture_prompt_budget",
            "architecture_contract",
            "analysis_extraction",
        } else "architecture_contract"
        try:
            receipt = self._audit.finalize_recall(
                FinalizeAnalysisRecallAudit(
                    receipt=state.recall_receipt,
                    expected_version=state.recall_receipt.version,
                    outcome=AnalysisAuditOutcome.FAILED,
                    payload=FrozenJsonObject.from_mapping(
                        {
                            "returned_architecture_id": None,
                            "returned_rank": None,
                            "total_elapsed_ms": _AnalysisModelWorkflow.elapsed_ms(started_at),
                            "failure_stage": stable_stage,
                            "error_message": safe_error_code(error),
                        },
                        name="recall_failure",
                    ),
                    error_code=safe_error_code(error),
                )
            )
            if not isinstance(receipt, AnalysisRecallAuditReceipt) or not receipt.finalized:
                raise AnalysisApplicationContractError("失败召回审计终结 Receipt 无效")
            state.recall_finalized = True
        except Exception:
            # 召回审计本身已无法作为可靠事实，后续处理会走失败路径；这里不抛出覆盖原始
            # RAG/领域错误，以便最终日志仍可定位根因。
            state.preserve_scene = True
            logger.critical(
                "文件分析失败后无法终结召回审计: task_id=%s stage=%s",
                state.recall_receipt.execution.task_id,
                stable_stage,
                exc_info=True,
            )

    def persist_interaction(
        self,
        *,
        execution: AnalysisExecutionRef,
        snapshot: AnalysisTaskInputV1,
        state: _RagWorkflowState,
        outcome: AnalysisAuditOutcome,
        error_code: str,
    ) -> AnalysisInteractionAuditReceipt:
        """写入所有已发生的生命周期事件与模型尝试。"""

        if not state.lifecycle_events:
            raise AnalysisApplicationContractError("交互审计缺少生命周期事件")
        prompt = state.last_prompt or "文件分析会话打开失败"
        record = AnalysisInteractionAuditRecord(
            execution=execution,
            idempotency_key=f"analysis-rag:{execution.task_id.value}",
            session=state.session,
            context_name=f"llm-file-{execution.task_id.value}",
            trace_id=snapshot.trace_id,
            prompt=prompt,
            attempts=tuple(state.attempts),
            lifecycle_events=tuple(state.lifecycle_events),
            outcome=outcome,
            error_code=error_code,
        )
        state.interaction_audit_attempted = True
        receipt = self._audit.persist_interaction(record)
        if (
            not isinstance(receipt, AnalysisInteractionAuditReceipt)
            or receipt.execution != execution
            or receipt.idempotency_key != record.idempotency_key
        ):
            raise AnalysisApplicationContractError("交互审计 Receipt 与当前任务不一致")
        return receipt

    def close_audited_session(
        self,
        *,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        retain_document: bool,
        on_close_result: Callable[[AnalysisRagCloseResult], None] | None = None,
        on_lifecycle_audited: Callable[[], None] | None = None,
        on_close_failure: Callable[[BaseException, AnalysisRagCloseResult | None], None]
        | None = None,
    ) -> None:
        """关闭已审计 Session；任何异常只保留恢复证据，绝不二次写业务终态。

        三个可选回调只服务资源事实持久化：调用方先持久化 close 意图，close 返回后写入
        三态结果，生命周期审计追加成功后才允许标记资源已清理。它们不是对外 Callback，
        也不会改变已有接口参数或响应。
        """

        if state.session is None or state.interaction_receipt is None:
            return
        for name, callback in (
            ("on_close_result", on_close_result),
            ("on_lifecycle_audited", on_lifecycle_audited),
            ("on_close_failure", on_close_failure),
        ):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} 必须可调用或为 None")
        result: AnalysisRagCloseResult | None = None
        try:
            result = rag.close_session(
                AnalysisRagCloseRequest(
                    execution=execution,
                    session=state.session,
                    retain_document=retain_document,
                )
            )
            if result.execution != execution or result.session != state.session:
                raise AnalysisApplicationContractError("RAG 关闭结果与当前 Session 不一致")
            if not result.lifecycle_events:
                raise AnalysisApplicationContractError("RAG 关闭结果缺少生命周期事件")
            if on_close_result is not None:
                # 远端 close 已返回时先保存结果，避免审计追加失败后误把已知结果当成可重放
                # 的外部动作。回调若失败会由 except 分支转入 audit_pending 现场。
                on_close_result(result)
            self._audit.append_lifecycle_events(
                AppendAnalysisLifecycleEvents(
                    receipt=state.interaction_receipt,
                    events=result.lifecycle_events,
                )
            )
            if on_lifecycle_audited is not None:
                on_lifecycle_audited()
            if result.outcome is AnalysisRagCloseOutcome.OUTCOME_UNKNOWN:
                logger.critical(
                    "文件分析 RAG 关闭结果未知，已保留恢复现场: task_id=%s",
                    execution.task_id,
                )
            else:
                logger.info(
                    "文件分析 RAG 会话关闭已审计: task_id=%s outcome=%s retain_document=%s",
                    execution.task_id,
                    result.outcome.value,
                    retain_document,
                )
        except Exception as error:
            if on_close_failure is not None:
                try:
                    # 此处即使资源记录也写失败，也只能记录严重日志；绝不能把当前业务终态
                    # 改写为失败或对 RAG 再发一次 close/delete。
                    on_close_failure(error, result)
                except Exception:
                    logger.critical(
                        "文件分析关闭失败后的资源事实也未能保存，禁止自动补偿: task_id=%s",
                        execution.task_id,
                        exc_info=True,
                    )
            logger.critical(
                "文件分析 RAG 关闭或关闭审计失败，业务终态保持不变: task_id=%s",
                execution.task_id,
                exc_info=True,
            )
