"""把既有 Analysis RAG Port 投影为 Authority-aware 持久 Step。

当前供应商适配器的首次 ``execute`` 会在一个网络调用中完成文档上传、绑定和模型
请求。这里不会把三种副作用合并为一个恢复事实：调用前分别提交 intent，调用返回后
分别提交摘要型 checkpoint；异常分支先由 Workflow 落完整 Interaction Audit，再由
``finalize_failures_after_audit`` 收敛 Step，避免先隔离后丢失可证明的供应商证据。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from collections.abc import Callable

from app.modules.analysis.ports import (
    AnalysisRagExecutionError,
    AnalysisRagOperation,
    AnalysisRagPort,
    AnalysisRagRequest,
    AnalysisRagResult,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenRequest,
    AnalysisRagSessionOpenResult,
)
from app.modules.tasks.domain import TaskStepCheckpoint
from app.modules.tasks.ports import TaskWorkflowContextPort

from .step_runtime import ActiveAnalysisStep, AnalysisStepRuntime


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _DeferredFailure:
    active: ActiveAnalysisStep
    error_code: str
    outcome_unknown: bool
    optional_degrade: bool = False


class AnalysisRagStepObserver(AnalysisRagPort):
    """绑定单次 Workflow Context 的 RAG Port 装饰器。

    装饰器不缓存跨任务客户端；其生命周期严格受外层任务级 RAG Factory 控制。模型
    Step 的序号按同一 Registry 家族递增，因此 classification repair 不会与首轮
    classification 争用 ``classification.execute:1``。
    """

    def __init__(
        self,
        *,
        delegate: AnalysisRagPort,
        runtime: AnalysisStepRuntime,
        context: TaskWorkflowContextPort,
        result_component_mutation: Callable[[object, object], None] | None = None,
        failure_component_mutation: Callable[[object], None] | None = None,
        upload_intent_component_mutation: Callable[[object], None] | None = None,
    ) -> None:
        if not isinstance(delegate, AnalysisRagPort):
            raise TypeError("delegate 必须实现 AnalysisRagPort")
        if not isinstance(runtime, AnalysisStepRuntime):
            raise TypeError("runtime 必须是 AnalysisStepRuntime")
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")
        self._delegate = delegate
        self._runtime = runtime
        self._context = context
        self._result_component_mutation = result_component_mutation
        self._failure_component_mutation = failure_component_mutation
        self._upload_intent_component_mutation = upload_intent_component_mutation
        self._family_attempts = {"classification": 0, "extraction": 0, "combined": 0}
        self._deferred: list[_DeferredFailure] = []

    @property
    def has_deferred_failures(self) -> bool:
        return bool(self._deferred)

    @property
    def recovery_required(self) -> bool:
        return any(item.outcome_unknown for item in self._deferred)

    def open_session(
        self,
        request: AnalysisRagSessionOpenRequest,
    ) -> AnalysisRagSessionOpenResult:
        active = self._runtime.begin(
            self._context,
            step_key="rag.session.open",
            idempotency_key=f"analysis:{request.execution.task_id.value}:rag-session",
        )
        try:
            result = self._delegate.open_session(request)
        except AnalysisRagSessionOpenError as error:
            self._deferred.append(
                _DeferredFailure(
                    active,
                    "rag_session_open_outcome_unknown" if error.outcome_unknown else "rag_session_open_failed",
                    error.outcome_unknown,
                )
            )
            raise
        except Exception:
            # intent 已提交而 Port 没有提供三态证据；即使异常类型看似本地，也不能证明
            # Context/Conversation 未在远端创建。
            self._deferred.append(
                _DeferredFailure(active, "rag_session_open_unclassified", True)
            )
            raise
        session = result.session
        digest = _digest(
            "\0".join((session.context_ref, session.conversation_ref, session.session_ref))
        )
        self._runtime.succeed(
            self._context,
            active,
            TaskStepCheckpoint(
                code="rag_session_opened_v1",
                result_ref=f"analysis-rag-session:v1:{digest}",
                result_digest=digest,
            ),
            component_mutation=(
                (lambda unit_of_work: self._result_component_mutation(unit_of_work, result))
                if self._result_component_mutation is not None
                else None
            ),
        )
        return result

    def execute(self, request: AnalysisRagRequest) -> AnalysisRagResult:
        upload: ActiveAnalysisStep | None = None
        bind: ActiveAnalysisStep | None = None
        if not request.session.document_bound:
            upload = self._runtime.begin(
                self._context,
                step_key="rag.document.upload",
                idempotency_key=(
                    f"analysis:{request.execution.task_id.value}:rag-upload:"
                    f"{_digest(request.prompt)}"
                ),
                component_mutation=self._upload_intent_component_mutation,
            )
            bind = self._runtime.begin(
                self._context,
                step_key="rag.document.bind",
                idempotency_key=(
                    f"analysis:{request.execution.task_id.value}:rag-bind:"
                    f"{_digest(request.session.context_ref)}"
                ),
            )
        step_key, family_attempt = self._next_model_step(request.operation)
        model = self._runtime.begin(
            self._context,
            step_key=step_key,
            idempotency_key=(
                f"analysis:{request.execution.task_id.value}:"
                f"{step_key.replace('.', ':')}:{_digest(request.prompt)}"
            ),
        )
        try:
            result = self._delegate.execute(request)
        except AnalysisRagExecutionError as error:
            # 首次复合调用失败时，即使模型错误本身可判定，也不能仅凭输入 SessionRef
            # 推断上传/绑定是否已提交；两者保守进入对账隔离。
            if upload is not None:
                self._deferred.append(
                    _DeferredFailure(upload, "rag_document_upload_requires_reconcile", True)
                )
            if bind is not None:
                self._deferred.append(
                    _DeferredFailure(bind, "rag_document_bind_requires_reconcile", True)
                )
            self._deferred.append(
                _DeferredFailure(
                    model,
                    error.error_code,
                    error.outcome_unknown,
                    optional_degrade=(
                        request.operation is AnalysisRagOperation.IDENTITY_RESELECT
                        and not error.outcome_unknown
                    ),
                )
            )
            raise
        except Exception:
            for active, code in (
                (upload, "rag_document_upload_unclassified"),
                (bind, "rag_document_bind_unclassified"),
                (model, "analysis_rag_unclassified_outcome_unknown"),
            ):
                if active is not None:
                    self._deferred.append(_DeferredFailure(active, code, True))
            raise

        if upload is not None and bind is not None:
            if not result.session.document_bound:
                self._deferred.extend(
                    (
                        _DeferredFailure(upload, "rag_upload_result_incomplete", True),
                        _DeferredFailure(bind, "rag_bind_result_incomplete", True),
                        _DeferredFailure(model, "rag_model_result_incomplete", True),
                    )
                )
                raise RuntimeError("首次 RAG 成功结果缺少已绑定文档身份")
            document_digest = _digest(
                "\0".join(
                    (
                        result.session.document_ref,
                        result.session.document_location,
                        result.session.content_sha256,
                        result.session.ingested_file_name,
                    )
                )
            )
            self._runtime.succeed(
                self._context,
                upload,
                TaskStepCheckpoint(
                    code="rag_document_uploaded_v1",
                    result_ref=f"analysis-rag-document:v1:{document_digest}",
                    result_digest=document_digest,
                ),
                component_mutation=(
                    (lambda unit_of_work: self._result_component_mutation(unit_of_work, result))
                    if self._result_component_mutation is not None
                    else None
                ),
            )
            self._runtime.succeed(
                self._context,
                bind,
                TaskStepCheckpoint(
                    code="rag_document_bound_v1",
                    result_ref=f"analysis-rag-bind:v1:{document_digest}",
                    result_digest=document_digest,
                ),
            )
        response_digest = _digest(result.answer)
        self._runtime.succeed(
            self._context,
            model,
            TaskStepCheckpoint(
                code="analysis_model_attempt_completed_v1",
                result_ref=f"analysis-model-result:v1:{response_digest}",
                result_digest=response_digest,
            ),
            component_mutation=(
                (lambda unit_of_work: self._result_component_mutation(unit_of_work, result))
                if self._result_component_mutation is not None and upload is None
                else None
            ),
        )
        return result

    def close_session(self, request):  # type: ignore[no-untyped-def]
        # RAG close 是终态后的资源维护，不属于 17 类业务 Step；步骤 6 会用持久资源
        # recovery intent/observation 管理它，这里只透传既有三态 Port。
        return self._delegate.close_session(request)

    def finalize_failures_after_audit(self) -> bool:
        """在 Interaction Audit 已提交后收敛延迟失败；返回是否需要恢复。"""

        recovery_required = False
        deferred, self._deferred = self._deferred, []
        resource_mutation_pending = self._failure_component_mutation
        for item in deferred:
            if item.optional_degrade:
                digest = _digest(item.error_code)
                self._runtime.succeed(
                    self._context,
                    item.active,
                    TaskStepCheckpoint(
                        code="analysis_optional_degraded_v1",
                        result_ref=f"analysis-optional-degrade:v1:{digest}",
                        result_digest=digest,
                    ),
                    component_mutation=resource_mutation_pending,
                )
                resource_mutation_pending = None
                continue
            self._runtime.fail(
                self._context,
                item.active,
                error_code=item.error_code,
                outcome_unknown=item.outcome_unknown,
                component_mutation=resource_mutation_pending,
            )
            resource_mutation_pending = None
            recovery_required = recovery_required or item.outcome_unknown
            if item.outcome_unknown:
                # 首个 unknown 已把 Task/Attempt 原子推进为 recovery_required；同一旧
                # Authority 随即失效。其余 running intent 保留给同一 Recovery Case
                # 联合判断，不能用失效 Authority 继续伪造第二个隔离写。
                break
        return recovery_required

    def _next_model_step(self, operation: AnalysisRagOperation) -> tuple[str, int]:
        if operation in {
            AnalysisRagOperation.CLASSIFICATION,
            AnalysisRagOperation.CLASSIFICATION_REPAIR,
        }:
            family = "classification"
        elif operation in {
            AnalysisRagOperation.EXTRACTION,
            AnalysisRagOperation.EXTRACTION_REPAIR,
        }:
            family = "extraction"
        elif operation is AnalysisRagOperation.COMBINED:
            family = "combined"
        elif operation is AnalysisRagOperation.IDENTITY_RESELECT:
            return "identity.reselect", 1
        else:  # pragma: no cover - Enum 已封闭
            raise ValueError("Analysis RAG operation 未登记 Step")
        self._family_attempts[family] += 1
        attempt = self._family_attempts[family]
        return f"{family}.execute:{attempt}", attempt


__all__ = ["AnalysisRagStepObserver"]
