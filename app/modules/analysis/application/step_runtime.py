"""Analysis v2 的 Authority-aware Step、进度与终态事务协调器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from app.modules.analysis.application.execution_uow import (
    AnalysisExecutionUnitOfWork,
    AnalysisExecutionUnitOfWorkFactory,
)
from app.modules.analysis.application.workflow_models import AnalysisTaskPersistenceError
from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskRecoveryIsolation,
    TaskStepCheckpoint,
    TaskStepTransition,
    TaskTransition,
)
from app.modules.tasks.ports import (
    CallbackControlMutationOutcome,
    CallbackEligibilityCommand,
    ClockPort,
    LeaseSupervisorOutcome,
    LeaseSupervisorResult,
    TaskExecutionMutationOutcome,
    TaskExecutionStopRequested,
    TaskProgressCommand,
    TaskStepCompletionCommand,
    TaskStepIntentCommand,
    TaskStepContinuationDraft,
    TaskStepContinuationSnapshot,
    TaskTerminalCommand,
    TaskWorkflowContextPort,
)

from .execution_steps import resolve_analysis_step
from app.modules.tasks.application.checkpoint_resume import expected_retry_step


_RECOVERY_POLICY_VERSION = "analysis-recovery-matrix.v1"


@dataclass(frozen=True, slots=True)
class ActiveAnalysisStep:
    step_key: str
    step_attempt_no: int


ComponentMutation = Callable[[AnalysisExecutionUnitOfWork], None]


class AnalysisStepRuntime:
    """所有条件写只通过 Runtime 当前 Authority Session 进入短事务。"""

    def __init__(
        self,
        *,
        uow_factory: AnalysisExecutionUnitOfWorkFactory,
        clock: ClockPort,
    ) -> None:
        if not callable(uow_factory):
            raise TypeError("uow_factory 必须可调用")
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
        component_mutation: ComponentMutation | None = None,
        continuation: TaskStepContinuationDraft | None = None,
    ) -> ActiveAnalysisStep:
        if context.stop_requested():
            raise TaskExecutionStopRequested(
                LeaseSupervisorResult(LeaseSupervisorOutcome.STOPPED)
            )
        definition = resolve_analysis_step(step_key)
        retry_step = expected_retry_step(
            context,
            step_key=step_key,
            idempotency_key=idempotency_key,
            definition=definition,
        )
        holder: dict[str, int] = {}

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                source_attempt_no = 0
                if retry_step is not None:
                    source_attempt_no = retry_step.current_step_attempt_no
                    source_snapshot = unit_of_work.continuations.get(
                        authority.task_id,
                        step_key,
                        source_attempt_no,
                    )
                    if (
                        continuation is None
                        or source_snapshot is None
                        or source_snapshot.draft != continuation
                    ):
                        return TaskExecutionMutationOutcome.INVALID_STATE
                existing = unit_of_work.execution.get_step(authority.task_id, step_key)
                if existing is not None:
                    if retry_step is None or existing != retry_step:
                        holder["attempt_no"] = existing.current_step_attempt_no
                        return TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT
                    step = existing
                else:
                    if retry_step is not None:
                        return TaskExecutionMutationOutcome.INVALID_STATE
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
                    if component_mutation is not None:
                        component_mutation(unit_of_work)
                    next_attempt_no = step.current_step_attempt_no + 1
                    if continuation is not None:
                        unit_of_work.continuations.save(
                            authority=authority,
                            step_key=step_key,
                            step_attempt_no=next_attempt_no,
                            source_step_attempt_no=source_attempt_no,
                            draft=continuation,
                            created_at=self._clock.now_utc(),
                        )
                    holder["attempt_no"] = next_attempt_no
                    unit_of_work.commit()
                return outcome

        outcome = self._run(context, mutation, stage=f"step_begin:{step_key}")
        if outcome is TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT:
            raise AnalysisTaskPersistenceError(
                f"Analysis Step 已存在，必须进入恢复决策: {step_key}"
            )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            raise AnalysisTaskPersistenceError(f"Analysis Step intent 未提交: {step_key}")
        return ActiveAnalysisStep(step_key, holder["attempt_no"])

    def load_resume_continuation(
        self,
        context: TaskWorkflowContextPort,
        *,
        execution_profile_fingerprint: str,
    ) -> TaskStepContinuationSnapshot | None:
        """在创建 Workspace 或调用文件/供应商 Port 前恢复原 Step 快照。"""

        retry_from = context.loaded_input.retry_from_step_key
        if not retry_from:
            return None
        step = next(
            item
            for item in context.loaded_input.recovery_steps
            if item.step_key == retry_from
        )
        with self._uow_factory() as unit_of_work:
            snapshot = unit_of_work.continuations.get(
                context.loaded_input.snapshot.task_id,
                retry_from,
                step.current_step_attempt_no,
            )
        if snapshot is None:
            raise AnalysisTaskPersistenceError("Analysis 恢复目标缺少业务续跑快照")
        if (
            snapshot.draft.input_payload_fingerprint
            != context.loaded_input.input_payload_fingerprint
            or snapshot.draft.execution_profile_fingerprint
            != execution_profile_fingerprint
        ):
            raise AnalysisTaskPersistenceError("Analysis 续跑快照与冻结输入/Profile 不一致")
        return snapshot

    def succeed(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveAnalysisStep,
        checkpoint: TaskStepCheckpoint,
        *,
        component_mutation: ComponentMutation | None = None,
    ) -> None:
        if not isinstance(active, ActiveAnalysisStep):
            raise TypeError("active 必须是 ActiveAnalysisStep")
        if not isinstance(checkpoint, TaskStepCheckpoint):
            raise TypeError("checkpoint 必须是 TaskStepCheckpoint")

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if component_mutation is not None:
                    component_mutation(unit_of_work)
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

        self._require_applied(context, mutation, stage=f"step_complete:{active.step_key}")

    def checkpoint_result_snapshot(
        self,
        context: TaskWorkflowContextPort,
        *,
        business_ref: TaskBusinessRef,
        payload: FrozenJsonObject,
        result_digest: str,
    ) -> None:
        """以一个纯本地事务写入结果快照并完成 ``result.snapshot`` Step。

        该步骤没有事务外副作用，因此 Intent、业务快照与成功 Checkpoint 可以合并为
        一个原子组，消除“快照已写但 Step 仍 running”的不必要恢复窗口。
        """

        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        definition = resolve_analysis_step("result.snapshot")
        checkpoint = TaskStepCheckpoint(
            code="analysis_result_snapshot_v1",
            result_ref=f"analysis-result:v1:{result_digest}",
            result_digest=result_digest,
        )

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if unit_of_work.execution.get_step(authority.task_id, "result.snapshot") is not None:
                    return TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT
                now = self._clock.now_utc()
                begin_outcome = unit_of_work.execution.begin_step(
                    TaskStepIntentCommand(
                        authority=authority,
                        step=definition.new_step(
                            task_id=authority.task_id,
                            step_key="result.snapshot",
                            idempotency_key=(
                                f"analysis:{authority.task_id.value}:"
                                f"result-snapshot:{result_digest}"
                            ),
                        ),
                        intent_at=now,
                    )
                )
                if begin_outcome is not TaskExecutionMutationOutcome.APPLIED:
                    return begin_outcome
                unit_of_work.results.save(
                    task_id=authority.task_id,
                    business_ref=business_ref,
                    payload=payload,
                    created_at=now,
                )
                complete_outcome = unit_of_work.execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key="result.snapshot",
                        step_attempt_no=1,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=checkpoint,
                        error_code="",
                        completed_at=now,
                    )
                )
                if complete_outcome is TaskExecutionMutationOutcome.APPLIED:
                    unit_of_work.commit()
                return complete_outcome

        self._require_applied(context, mutation, stage="result_snapshot")

    def fail(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveAnalysisStep,
        *,
        error_code: str,
        outcome_unknown: bool = False,
        component_mutation: ComponentMutation | None = None,
    ) -> None:
        if not isinstance(active, ActiveAnalysisStep):
            raise TypeError("active 必须是 ActiveAnalysisStep")
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("error_code 不能为空")
        transition = (
            TaskStepTransition.MARK_OUTCOME_UNKNOWN
            if outcome_unknown
            else TaskStepTransition.FAIL
        )
        isolation = (
            TaskRecoveryIsolation(
                case_id=f"analysis-{uuid4().hex}",
                reason_code=error_code.strip(),
                policy_version=_RECOVERY_POLICY_VERSION,
            )
            if outcome_unknown
            else None
        )

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if component_mutation is not None:
                    component_mutation(unit_of_work)
                outcome = unit_of_work.execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=active.step_key,
                        step_attempt_no=active.step_attempt_no,
                        transition=transition,
                        checkpoint=None,
                        error_code=error_code.strip(),
                        recovery_isolation=isolation,
                        completed_at=self._clock.now_utc(),
                    )
                )
                if outcome is TaskExecutionMutationOutcome.APPLIED:
                    unit_of_work.commit()
                return outcome

        self._require_applied(context, mutation, stage=f"step_failure:{active.step_key}")

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

        self._require_applied(context, mutation, stage="progress")

    def finish(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveAnalysisStep,
        *,
        business_ref: TaskBusinessRef,
        succeeded: bool,
        public_status: str,
        message: str,
        result_ref: str,
        terminal_checkpoint: TaskStepCheckpoint,
        component_mutation: ComponentMutation | None = None,
    ) -> None:
        """原子提交结果快照、terminal Step、Task/Attempt 和 Callback eligibility。"""

        if (
            not isinstance(business_ref, TaskBusinessRef)
            or business_ref.business_type != "file"
        ):
            raise TypeError("business_ref 必须是 file TaskBusinessRef")
        if component_mutation is not None and not callable(component_mutation):
            raise TypeError("component_mutation 必须可调用或为 None")

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                completed_at = self._clock.now_utc()
                if component_mutation is not None:
                    component_mutation(unit_of_work)
                step_outcome = unit_of_work.execution.complete_step(
                    TaskStepCompletionCommand(
                        authority=authority,
                        step_key=active.step_key,
                        step_attempt_no=active.step_attempt_no,
                        transition=TaskStepTransition.SUCCEED,
                        checkpoint=terminal_checkpoint,
                        error_code="",
                        completed_at=completed_at,
                    )
                )
                if step_outcome is not TaskExecutionMutationOutcome.APPLIED:
                    return step_outcome
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
                    raise AnalysisTaskPersistenceError(
                        "Analysis Callback eligibility 未与终态原子提交"
                    )
                unit_of_work.commit()
                return TaskExecutionMutationOutcome.APPLIED

        self._require_applied(context, mutation, stage="terminal_commit")

    def _run(self, context, mutation, *, stage: str):
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")
        try:
            return context.session.run_mutation(mutation)
        except TaskExecutionStopRequested:
            raise
        except Exception as exc:
            if isinstance(exc, AnalysisTaskPersistenceError):
                raise
            raise AnalysisTaskPersistenceError(f"Analysis 条件写失败: {stage}") from exc

    def _require_applied(self, context, mutation, *, stage: str) -> None:
        if self._run(context, mutation, stage=stage) is not TaskExecutionMutationOutcome.APPLIED:
            raise AnalysisTaskPersistenceError(f"Analysis 条件写未提交: {stage}")


__all__ = ["ActiveAnalysisStep", "AnalysisStepRuntime"]
