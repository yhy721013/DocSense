"""为 Analysis 复合外部 Port 补齐持久 Step 边界。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from app.modules.analysis.ports import (
    AnalysisKnowledgePort,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
    AnalysisTranslationOutcome,
    AnalysisTranslationPort,
    AnalysisTranslationRequest,
    AnalysisTranslationResult,
)
from app.modules.tasks.domain import TaskStepCheckpoint
from app.modules.tasks.ports import TaskWorkflowContextPort

from .step_runtime import AnalysisStepRuntime


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AnalysisKnowledgeStepPort(AnalysisKnowledgePort):
    """把 Workspace ensure 与 Document bind 分别登记为可恢复 Step。

    既有 Adapter 的 ``persist`` 是一次复合调用。两个 intent 必须都早于调用；返回三态
    之后再分别收敛。这样进程中断虽然会保守地产生两个待对账 Step，但不会出现远端
    已写入而 Control Store 完全没有意图的窗口。
    """

    def __init__(
        self,
        *,
        delegate: AnalysisKnowledgePort,
        runtime: AnalysisStepRuntime,
        context: TaskWorkflowContextPort,
        result_component_mutation: Callable[[object, AnalysisKnowledgeWriteRequest, AnalysisKnowledgeWriteResult], None] | None = None,
        failure_component_mutation: Callable[[object], None] | None = None,
    ) -> None:
        if not isinstance(delegate, AnalysisKnowledgePort):
            raise TypeError("delegate 必须实现 AnalysisKnowledgePort")
        self._delegate = delegate
        self._runtime = runtime
        self._context = context
        self._result_component_mutation = result_component_mutation
        self._failure_component_mutation = failure_component_mutation
        self._recovery_required = False

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    def persist(
        self,
        request: AnalysisKnowledgeWriteRequest,
    ) -> AnalysisKnowledgeWriteResult:
        workspace = self._runtime.begin(
            self._context,
            step_key="knowledge.workspace.ensure",
            idempotency_key=f"{request.idempotency_key}:workspace",
        )
        document = self._runtime.begin(
            self._context,
            step_key="knowledge.document.bind",
            idempotency_key=request.idempotency_key,
        )
        try:
            result = self._delegate.persist(request)
        except Exception:
            # 复合 Port 未提供可判定结果，两个外部写都必须隔离，不能根据异常类型猜测
            # ensure_collection 或 bind_document 是否实际到达供应商。
            self._runtime.fail(
                self._context,
                workspace,
                error_code="knowledge_workspace_outcome_unknown",
                outcome_unknown=True,
                component_mutation=self._failure_component_mutation,
            )
            # 首个 unknown 会立即使当前 Authority 失效；document intent 保持 running，
            # 由同一 Recovery Case 联合对账，不能继续以旧 Authority 写第二次隔离。
            self._recovery_required = True
            raise

        evidence = _digest(
            "\0".join(
                (
                    request.idempotency_key,
                    result.outcome.value,
                    result.external_ref,
                    result.detail_code,
                )
            )
        )
        if result.outcome is AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN:
            self._runtime.fail(
                self._context,
                workspace,
                error_code=result.detail_code,
                outcome_unknown=True,
                component_mutation=(
                    (lambda unit_of_work: self._result_component_mutation(unit_of_work, request, result))
                    if self._result_component_mutation is not None
                    else None
                ),
            )
            self._recovery_required = True
            return result

        # NOT_APPLIED 表示文档写明确未提交，但 ensure_collection 的可重复调用已经返回；
        # Workspace Step 可作为确定 checkpoint，Document Step 则记录普通失败。
        self._runtime.succeed(
            self._context,
            workspace,
            TaskStepCheckpoint(
                code="knowledge_workspace_ensured_v1",
                result_ref=f"analysis-knowledge-workspace:v1:{evidence}",
                result_digest=evidence,
            ),
        )
        if result.outcome is AnalysisKnowledgeWriteOutcome.COMMITTED:
            self._runtime.succeed(
                self._context,
                document,
                TaskStepCheckpoint(
                    code="knowledge_document_bound_v1",
                    result_ref=result.external_ref,
                    result_digest=evidence,
                ),
                component_mutation=(
                    (lambda unit_of_work: self._result_component_mutation(unit_of_work, request, result))
                    if self._result_component_mutation is not None
                    else None
                ),
            )
        else:
            self._runtime.fail(
                self._context,
                document,
                error_code=result.detail_code,
                component_mutation=(
                    (lambda unit_of_work: self._result_component_mutation(unit_of_work, request, result))
                    if self._result_component_mutation is not None
                    else None
                ),
            )
        return result


class AnalysisTranslationStepPort(AnalysisTranslationPort):
    """记录全文翻译的成功或既有可降级决定，不把异常正文写入 checkpoint。"""

    def __init__(
        self,
        *,
        delegate: AnalysisTranslationPort,
        runtime: AnalysisStepRuntime,
        context: TaskWorkflowContextPort,
        translation_key: str,
    ) -> None:
        if not isinstance(delegate, AnalysisTranslationPort):
            raise TypeError("delegate 必须实现 AnalysisTranslationPort")
        if not isinstance(translation_key, str) or not translation_key.strip():
            raise ValueError("translation_key 必须是非空 str")
        self._delegate = delegate
        self._runtime = runtime
        self._context = context
        self._translation_key = translation_key.strip()

    def translate(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        active = self._runtime.begin(
            self._context,
            step_key="translation.execute",
            idempotency_key=self._translation_key,
        )
        try:
            result = self._delegate.translate(request)
        except Exception as error:
            # 既有业务合同把翻译异常降级为空字段。仍需持久化“已决定降级”，否则恢复器
            # 不能判断是否应再次触发外部翻译。
            digest = _digest(type(error).__name__)
            self._runtime.succeed(
                self._context,
                active,
                TaskStepCheckpoint(
                    code="translation_degraded_exception_v1",
                    result_ref=f"analysis-translation-degrade:v1:{digest}",
                    result_digest=digest,
                ),
            )
            raise
        if not isinstance(result, AnalysisTranslationResult):
            self._runtime.fail(
                self._context,
                active,
                error_code="translation_result_contract_invalid",
                outcome_unknown=True,
            )
            raise TypeError("AnalysisTranslationPort.translate 返回类型错误")
        body_digest = _digest(
            "\0".join(
                (
                    result.outcome.value,
                    result.document_translation_one,
                    result.document_translation_two,
                    result.error_code,
                )
            )
        )
        self._runtime.succeed(
            self._context,
            active,
            TaskStepCheckpoint(
                code=(
                    "translation_completed_v1"
                    if result.outcome is AnalysisTranslationOutcome.SUCCEEDED
                    else "translation_degraded_v1"
                ),
                result_ref=f"analysis-translation:v1:{body_digest}",
                result_digest=body_digest,
            ),
        )
        return result


__all__ = ["AnalysisKnowledgeStepPort", "AnalysisTranslationStepPort"]
