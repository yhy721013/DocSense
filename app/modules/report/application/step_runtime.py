"""Report v2 Runner 的 Authority-aware Step 与终态事务协调器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from app.modules.report.domain import ReportTaskPersistenceError
from app.modules.report.ports import (
    ReportArtifactRef,
    ReportRagStepObserverPort,
)
from .artifact_identity import report_artifact_result_ref
from .execution_steps import resolve_report_step
from .execution_uow import ReportExecutionUnitOfWorkFactory
from app.modules.tasks.domain import (
    TaskRecoveryIsolation,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
)
from app.modules.tasks.ports import (
    CallbackControlMutationOutcome,
    CallbackEligibilityCommand,
    ClockPort,
    TaskExecutionMutationOutcome,
    TaskExecutionStopRequested,
    TaskProgressCommand,
    TaskStepCompletionCommand,
    TaskStepIntentCommand,
    TaskTerminalCommand,
    TaskWorkflowContextPort,
)

from .resource_facts import ReportResourceFactService


_RECOVERY_POLICY_VERSION = "report-recovery-matrix.v1"


@dataclass(frozen=True, slots=True)
class ActiveReportStep:
    step_key: str
    step_attempt_no: int


ResourceMutation = Callable[[ReportResourceFactService], None]


class ReportStepRuntime:
    """把每笔短写放入 Authority Session 和 Report 组合 UoW 的双重门禁。"""

    def __init__(
        self,
        *,
        uow_factory: ReportExecutionUnitOfWorkFactory,
        clock: ClockPort,
    ) -> None:
        if not isinstance(uow_factory, ReportExecutionUnitOfWorkFactory):
            raise TypeError("uow_factory 必须实现 ReportExecutionUnitOfWorkFactory")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        self._uow_factory = uow_factory
        self._clock = clock

    def begin(
        self,
        context: TaskWorkflowContextPort,
        *,
        step_key: str,
        idempotency_key: str,
    ) -> ActiveReportStep:
        definition = resolve_report_step(step_key)
        holder: dict[str, int] = {}

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                existing = unit_of_work.execution.get_step(authority.task_id, step_key)
                if existing is not None:
                    # 阶段 2-7 才能依据 Observation/Decision 重放。普通 Worker 遇到任何
                    # 既有投影都失败关闭，不能凭 checkpoint 或幂等键自行推断。
                    holder["attempt_no"] = existing.current_step_attempt_no
                    return TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT
                step = definition.new_step(
                    task_id=authority.task_id,
                    step_key=step_key,
                    idempotency_key=idempotency_key,
                )
                outcome = unit_of_work.execution.begin_step(
                    TaskStepIntentCommand(
                        authority=authority,
                        step=step,
                        intent_at=self._clock.now_utc(),
                    )
                )
                if outcome is TaskExecutionMutationOutcome.APPLIED:
                    holder["attempt_no"] = 1
                    unit_of_work.commit()
                return outcome

        try:
            outcome = context.session.run_mutation(mutation)
        except TaskExecutionStopRequested:
            raise
        except Exception as exc:
            raise ReportTaskPersistenceError(
                f"Report Step intent 写入失败: {step_key}"
            ) from exc
        if outcome is TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT:
            raise ReportTaskPersistenceError(
                f"Report Step 已存在，必须进入恢复决策: {step_key}"
            )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            raise ReportTaskPersistenceError(f"Report Step intent 未提交: {step_key}")
        return ActiveReportStep(step_key, holder["attempt_no"])

    def succeed(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveReportStep,
        checkpoint: TaskStepCheckpoint,
        *,
        resource_mutation: ResourceMutation | None = None,
    ) -> None:
        if not isinstance(active, ActiveReportStep):
            raise TypeError("active 必须是 ActiveReportStep")
        if not isinstance(checkpoint, TaskStepCheckpoint):
            raise TypeError("checkpoint 必须是 TaskStepCheckpoint")
        if resource_mutation is not None and not callable(resource_mutation):
            raise TypeError("resource_mutation 必须可调用或为 None")

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if resource_mutation is not None:
                    resource_mutation(ReportResourceFactService(unit_of_work.resources))
                outcome = unit_of_work.execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=active.step_key,
                        step_attempt_no=active.step_attempt_no,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=checkpoint,
                        error_code="",
                        completed_at=self._clock.now_utc(),
                    )
                )
                if outcome is TaskExecutionMutationOutcome.APPLIED:
                    unit_of_work.commit()
                return outcome

        self._run_required_mutation(
            context,
            mutation,
            stage=f"step_complete:{active.step_key}",
        )

    def fail(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveReportStep,
        *,
        error_code: str,
        outcome_unknown: bool = False,
        resource_mutation: ResourceMutation | None = None,
    ) -> None:
        if not isinstance(active, ActiveReportStep):
            raise TypeError("active 必须是 ActiveReportStep")
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("error_code 不能为空")
        if not isinstance(outcome_unknown, bool):
            raise TypeError("outcome_unknown 必须是 bool")
        if resource_mutation is not None and not callable(resource_mutation):
            raise TypeError("resource_mutation 必须可调用或为 None")
        error_code = error_code.strip()
        transition = (
            TaskStepTransition.MARK_OUTCOME_UNKNOWN
            if outcome_unknown
            else TaskStepTransition.FAIL
        )
        isolation = (
            TaskRecoveryIsolation(
                case_id=f"report-{uuid4().hex}",
                reason_code=error_code,
                policy_version=_RECOVERY_POLICY_VERSION,
            )
            if outcome_unknown
            else None
        )

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if resource_mutation is not None:
                    resource_mutation(ReportResourceFactService(unit_of_work.resources))
                outcome = unit_of_work.execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=active.step_key,
                        step_attempt_no=active.step_attempt_no,
                        transition=transition,
                        checkpoint=None,
                        error_code=error_code,
                        recovery_isolation=isolation,
                        completed_at=self._clock.now_utc(),
                    )
                )
                if outcome is TaskExecutionMutationOutcome.APPLIED:
                    unit_of_work.commit()
                return outcome

        self._run_required_mutation(
            context,
            mutation,
            stage=f"step_failure:{active.step_key}",
        )

    def update_progress(
        self,
        context: TaskWorkflowContextPort,
        *,
        progress: float,
        message: str,
        public_status: str,
    ) -> None:
        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                outcome = unit_of_work.execution.update_progress(
                    TaskProgressCommand(
                        authority=authority,
                        progress=progress,
                        message=message,
                        public_status=public_status,
                        updated_at=self._clock.now_utc(),
                    )
                )
                if outcome is TaskExecutionMutationOutcome.APPLIED:
                    unit_of_work.commit()
                return outcome

        self._run_required_mutation(context, mutation, stage="progress")

    def finish(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveReportStep,
        *,
        succeeded: bool,
        public_status: str,
        message: str,
        terminal_checkpoint: TaskStepCheckpoint,
        business_ref,
        final_artifact: ReportArtifactRef | None,
    ) -> None:
        """在一个事务中收敛 terminal Step、资源引用、Task/Attempt 和 Callback 资格。"""

        result_ref = (
            report_artifact_result_ref(final_artifact)
            if final_artifact is not None
            else ""
        )

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if final_artifact is not None:
                    ReportResourceFactService(unit_of_work.resources).track_final_artifact(
                        final_artifact
                    )
                step_outcome = unit_of_work.execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=active.step_key,
                        step_attempt_no=active.step_attempt_no,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=terminal_checkpoint,
                        error_code="",
                        completed_at=self._clock.now_utc(),
                    )
                )
                if step_outcome is not TaskExecutionMutationOutcome.APPLIED:
                    return step_outcome
                completed_at = self._clock.now_utc()
                terminal_outcome = unit_of_work.execution.finish(
                    TaskTerminalCommand(
                        authority=authority,
                        transition=(
                            TaskTransition.BUSINESS_SUCCEEDED
                            if succeeded
                            else TaskTransition.BUSINESS_FAILED
                        ),
                        public_status=public_status,
                        message=message,
                        result_ref=result_ref,
                        completed_at=completed_at,
                    )
                )
                if terminal_outcome is not TaskExecutionMutationOutcome.APPLIED:
                    return terminal_outcome
                callback_outcome = unit_of_work.callback_delivery.mark_eligible(
                    CallbackEligibilityCommand(
                        authority=authority,
                        business_ref=business_ref,
                        eligible_at=completed_at,
                    )
                )
                if callback_outcome is not CallbackControlMutationOutcome.APPLIED:
                    raise ReportTaskPersistenceError(
                        "Report Callback eligibility 未能与终态原子提交"
                    )
                unit_of_work.commit()
                return TaskExecutionMutationOutcome.APPLIED

        self._run_required_mutation(context, mutation, stage="terminal_commit")

    def observer(self, context: TaskWorkflowContextPort) -> "ReportRagStepObserver":
        return ReportRagStepObserver(runtime=self, context=context)

    @staticmethod
    def _required_context(context: TaskWorkflowContextPort) -> None:
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")

    def _run_required_mutation(self, context, mutation, *, stage: str) -> None:
        self._required_context(context)
        try:
            outcome = context.session.run_mutation(mutation)
        except TaskExecutionStopRequested:
            raise
        except Exception as exc:
            if isinstance(exc, ReportTaskPersistenceError):
                raise
            raise ReportTaskPersistenceError(f"Report 条件写失败: {stage}") from exc
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            raise ReportTaskPersistenceError(f"Report 条件写未提交: {stage}")


class ReportRagStepObserver(ReportRagStepObserverPort):
    """绑定一次 Workflow Context 的顺序 RAG Step Observer。"""

    def __init__(
        self,
        *,
        runtime: ReportStepRuntime,
        context: TaskWorkflowContextPort,
    ) -> None:
        self._runtime = runtime
        self._context = context
        self._active: ActiveReportStep | None = None
        self._deferred_generate_checkpoint: TaskStepCheckpoint | None = None

    @property
    def active(self) -> ActiveReportStep | None:
        return self._active

    def begin(self, step_key: str, idempotency_key: str) -> None:
        if self._active is not None:
            raise ReportTaskPersistenceError("RAG Step 尚未完成，禁止开始下一 Step")
        self._active = self._runtime.begin(
            self._context,
            step_key=step_key,
            idempotency_key=idempotency_key,
        )

    def succeed(self, step_key: str, checkpoint: TaskStepCheckpoint) -> None:
        active = self._active
        if active is None or active.step_key != step_key:
            raise ReportTaskPersistenceError("RAG Step 完成身份不一致")
        if step_key == "rag.generate":
            # cleanup_ref 只有 Adapter 完整返回时才可获得。先缓存检查点，Runner 随后把
            # cleanup_ref 资源事实与 rag.generate 成功一次提交，消除两次事务崩溃窗口。
            self._deferred_generate_checkpoint = checkpoint
            return
        self._runtime.succeed(self._context, active, checkpoint)
        self._active = None

    def finalize_generate(self, *, cleanup_ref) -> None:
        active = self._active
        checkpoint = self._deferred_generate_checkpoint
        if (
            active is None
            or active.step_key != "rag.generate"
            or checkpoint is None
        ):
            raise ReportTaskPersistenceError("rag.generate 缺少待提交检查点")
        active_task_id = self._context.session.current_authority().task_id
        resource_mutation = (
            (lambda facts: facts.track_rag_cleanup(active_task_id, cleanup_ref))
            if cleanup_ref is not None
            else None
        )
        self._runtime.succeed(
            self._context,
            active,
            checkpoint,
            resource_mutation=resource_mutation,
        )
        self._active = None
        self._deferred_generate_checkpoint = None

    def fail_active(
        self,
        *,
        error_code: str,
        outcome_unknown: bool,
        resource_mutation: ResourceMutation | None = None,
    ) -> None:
        active = self._active
        if active is None:
            raise ReportTaskPersistenceError("RAG 失败缺少活动 Step intent")
        self._runtime.fail(
            self._context,
            active,
            error_code=error_code,
            outcome_unknown=outcome_unknown,
            resource_mutation=resource_mutation,
        )
        self._active = None
        self._deferred_generate_checkpoint = None


__all__ = [
    "ActiveReportStep",
    "ReportRagStepObserver",
    "ReportStepRuntime",
]
