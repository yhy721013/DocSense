"""Weaponry v2 Runner 的 Authority-aware Step 与终态事务协调器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

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
from app.modules.weaponry.application.errors import WeaponryTaskPersistenceError

from .execution_steps import resolve_weaponry_step
from app.modules.tasks.application.checkpoint_resume import expected_retry_step
from .execution_uow import (
    WeaponryExecutionUnitOfWork,
    WeaponryExecutionUnitOfWorkFactory,
)


_RECOVERY_POLICY_VERSION = "weaponry-recovery-matrix.v1"


@dataclass(frozen=True, slots=True)
class ActiveWeaponryStep:
    step_key: str
    step_attempt_no: int


ComponentMutation = Callable[[WeaponryExecutionUnitOfWork], None]


class WeaponryStepRuntime:
    """以完整 Authority 和同连接 UoW 提交每一笔 Weaponry 控制事实。"""

    def __init__(
        self,
        *,
        uow_factory: WeaponryExecutionUnitOfWorkFactory,
        clock: ClockPort,
    ) -> None:
        if not isinstance(uow_factory, WeaponryExecutionUnitOfWorkFactory):
            raise TypeError("uow_factory 必须实现 WeaponryExecutionUnitOfWorkFactory")
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
    ) -> ActiveWeaponryStep:
        """先持久化 Step Intent；组件本地事实可在同一事务原子登记。"""

        # 与 Report 使用同一停止边界：不得丢弃已经返回的外部结果，但收到正常停机
        # 后禁止登记下一 Step Intent，更不能开始新的远端副作用。
        if context.stop_requested():
            raise TaskExecutionStopRequested(
                LeaseSupervisorResult(LeaseSupervisorOutcome.STOPPED)
            )

        definition = resolve_weaponry_step(step_key)
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

        outcome = self._run_mutation(context, mutation, stage=f"step_begin:{step_key}")
        if outcome is TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT:
            raise WeaponryTaskPersistenceError(
                f"Weaponry Step 已存在，必须进入恢复决策: {step_key}"
            )
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            raise WeaponryTaskPersistenceError(
                f"Weaponry Step intent 未提交: {step_key}"
            )
        return ActiveWeaponryStep(step_key, holder["attempt_no"])

    def load_resume_continuation(
        self,
        context: TaskWorkflowContextPort,
        *,
        execution_profile_fingerprint: str,
    ) -> TaskStepContinuationSnapshot | None:
        """只读加载恢复目标的业务快照；缺失或摘要漂移立即失败关闭。"""

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
            raise WeaponryTaskPersistenceError("Weaponry 恢复目标缺少业务续跑快照")
        if (
            snapshot.draft.input_payload_fingerprint
            != context.loaded_input.input_payload_fingerprint
            or snapshot.draft.execution_profile_fingerprint
            != execution_profile_fingerprint
        ):
            raise WeaponryTaskPersistenceError("Weaponry 续跑快照与冻结输入/Profile 不一致")
        return snapshot

    def succeed(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveWeaponryStep,
        checkpoint: TaskStepCheckpoint,
        *,
        component_mutation: ComponentMutation | None = None,
    ) -> None:
        if not isinstance(active, ActiveWeaponryStep):
            raise TypeError("active 必须是 ActiveWeaponryStep")
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

        self._require_applied(
            context,
            mutation,
            stage=f"step_complete:{active.step_key}",
        )

    def fail(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveWeaponryStep,
        *,
        error_code: str,
        outcome_unknown: bool,
        component_mutation: ComponentMutation | None = None,
    ) -> None:
        if not isinstance(active, ActiveWeaponryStep):
            raise TypeError("active 必须是 ActiveWeaponryStep")
        if not isinstance(error_code, str) or not error_code.strip():
            raise ValueError("error_code 不能为空")
        transition = (
            TaskStepTransition.MARK_OUTCOME_UNKNOWN
            if outcome_unknown
            else TaskStepTransition.FAIL
        )
        isolation = (
            TaskRecoveryIsolation(
                case_id=f"weaponry-{uuid4().hex}",
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

        self._require_applied(
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

        self._require_applied(context, mutation, stage="progress")

    def finish(
        self,
        context: TaskWorkflowContextPort,
        active: ActiveWeaponryStep,
        *,
        business_ref: TaskBusinessRef,
        succeeded: bool,
        public_status: str,
        message: str,
        result_ref: str,
        terminal_checkpoint: TaskStepCheckpoint,
        component_mutation: ComponentMutation | None = None,
    ) -> None:
        """原子提交 terminal Step、组件事实、Task/Attempt 与 Callback 资格。"""

        if not isinstance(business_ref, TaskBusinessRef) or business_ref.business_type != "weaponry":
            raise TypeError("business_ref 必须是 Weaponry TaskBusinessRef")

        def mutation(authority):
            with self._uow_factory() as unit_of_work:
                if component_mutation is not None:
                    component_mutation(unit_of_work)
                completed_at = self._clock.now_utc()
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
                    raise WeaponryTaskPersistenceError(
                        "Weaponry Callback eligibility 未能与终态原子提交"
                    )
                unit_of_work.commit()
                return TaskExecutionMutationOutcome.APPLIED

        self._require_applied(context, mutation, stage="terminal_commit")

    @staticmethod
    def _required_context(context: TaskWorkflowContextPort) -> None:
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")

    def _run_mutation(self, context, mutation, *, stage: str):
        self._required_context(context)
        try:
            return context.session.run_mutation(mutation)
        except TaskExecutionStopRequested:
            raise
        except Exception as exc:
            if isinstance(exc, WeaponryTaskPersistenceError):
                raise
            raise WeaponryTaskPersistenceError(
                f"Weaponry 条件写失败: {stage}"
            ) from exc

    def _require_applied(self, context, mutation, *, stage: str) -> None:
        outcome = self._run_mutation(context, mutation, stage=stage)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            raise WeaponryTaskPersistenceError(
                f"Weaponry 条件写未提交: {stage}"
            )


__all__ = ["ActiveWeaponryStep", "WeaponryStepRuntime"]
