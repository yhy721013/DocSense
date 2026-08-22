"""阶段 2-7 三业务恢复策略共享的严格 Registry 分类骨架。

本模块只处理调用方传入的冻结领域事实，不读取数据库、不访问文件或网络。业务模块通过
自身 Step Registry resolver 构造独立 Policy；未知、歧义或与 Registry 漂移的 Step 一律
进入 ``reconcile_required``，不能因为 lease 过期自动重放。
"""

from __future__ import annotations

from collections.abc import Callable

from app.modules.tasks.domain import (
    RecoveryClassification,
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationState,
    TaskRecoveryCandidate,
    TaskRecoveryDecision,
    TaskRecoveryObservation,
    TaskStep,
    TaskStepState,
)
from app.modules.tasks.ports.task_recovery import TaskRecoverySnapshot


StepDefinitionResolver = Callable[[str], object]
ResumableStepPredicate = Callable[[str], bool]


class RegistryTaskRecoveryPolicy:
    """按单一业务 Step Registry 进行保守、可复核的五类分类。"""

    def __init__(
        self,
        *,
        task_type: str,
        policy_version: str,
        resolve_step: StepDefinitionResolver,
        finalization_step_key: str,
        resumable_step: ResumableStepPredicate,
    ) -> None:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type 必须是非空 str")
        if not isinstance(policy_version, str) or not policy_version.strip():
            raise ValueError("policy_version 必须是非空 str")
        if not callable(resolve_step):
            raise TypeError("resolve_step 必须可调用")
        if not isinstance(finalization_step_key, str) or not finalization_step_key.strip():
            raise ValueError("finalization_step_key 必须是非空 str")
        if not callable(resumable_step):
            raise TypeError("resumable_step 必须可调用")
        self._task_type = task_type.strip()
        self.policy_version = policy_version.strip()
        self._resolve_step = resolve_step
        self._finalization_step_key = finalization_step_key.strip()
        self._resumable_step = resumable_step

    def classify(
        self,
        candidate: TaskRecoveryCandidate,
        *,
        steps: tuple[TaskStep, ...],
        observations: tuple[TaskRecoveryObservation, ...],
    ) -> RecoveryClassification:
        """只在尚未创建任何 Step 时自动 retry；其他现场保守建 Case。"""

        if not isinstance(candidate, TaskRecoveryCandidate):
            raise TypeError("candidate 必须是 TaskRecoveryCandidate")
        if candidate.task.task_type != self._task_type:
            raise ValueError("Recovery Policy 收到错误 task_type")
        if not isinstance(steps, tuple) or any(
            not isinstance(step, TaskStep) for step in steps
        ):
            raise TypeError("steps 必须是 TaskStep tuple")
        if not isinstance(observations, tuple) or any(
            not isinstance(item, TaskRecoveryObservation) for item in observations
        ):
            raise TypeError("observations 必须是 TaskRecoveryObservation tuple")
        if any(step.task_id != candidate.task.task_id for step in steps):
            raise ValueError("steps 包含其他 task_id")

        if not self._registry_matches(steps):
            return RecoveryClassification.RECONCILE_REQUIRED

        # 只有完全没有 Step Intent 的现场能够证明尚未越过任何业务副作用边界。
        if not steps and candidate.latest_is_current:
            return RecoveryClassification.RETRY_SAFE

        uncertain_states = {
            TaskStepState.RUNNING,
            TaskStepState.OUTCOME_UNKNOWN,
            TaskStepState.FAILED,
        }
        if any(step.state in uncertain_states for step in steps):
            return RecoveryClassification.RECONCILE_REQUIRED
        if not candidate.latest_is_current:
            return RecoveryClassification.MARK_STALE

        terminal_source = next(
            (step for step in steps if step.step_key == self._finalization_step_key),
            None,
        )
        if (
            terminal_source is not None
            and terminal_source.state is TaskStepState.SUCCEEDED
            and terminal_source.checkpoint is not None
            and all(
                step.state in {TaskStepState.SUCCEEDED, TaskStepState.SKIPPED}
                for step in steps
            )
        ):
            return RecoveryClassification.FINALIZE_FROM_CHECKPOINT

        return RecoveryClassification.RECONCILE_REQUIRED

    def _registry_matches(self, steps: tuple[TaskStep, ...]) -> bool:
        for step in steps:
            try:
                definition = self._resolve_step(step.step_key)
            except Exception:
                return False
            if (
                getattr(definition, "definition_version", None)
                != step.definition_version
                or getattr(definition, "effect_kind", None) is not step.effect_kind
                or getattr(definition, "replay_policy", None) is not step.replay_policy
                or not getattr(definition, "recovery_matrix_ref", "")
            ):
                return False
        return True

    def authorize_decision(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool:
        """验证 Decision 与业务 Registry、Case 和已收敛证据完全绑定。"""

        if not isinstance(snapshot, TaskRecoverySnapshot):
            raise TypeError("snapshot 必须是 TaskRecoverySnapshot")
        if not isinstance(decision, TaskRecoveryDecision):
            raise TypeError("decision 必须是 TaskRecoveryDecision")
        if (
            snapshot.task.task_type != self._task_type
            or decision.task_id != snapshot.task.task_id
            or decision.case_id != snapshot.case.case_id
            or decision.generation != snapshot.case.generation
            or decision.source_attempt_no != snapshot.case.source_attempt_no
            or decision.source_fencing_token
            != snapshot.case.source_fencing_token
            or decision.expected_task_row_version != snapshot.task.row_version
            or decision.policy_version != self.policy_version
            or not self._registry_matches(snapshot.steps)
        ):
            return False

        if decision.kind is RecoveryDecisionKind.KEEP_QUARANTINED:
            return True
        if decision.kind is RecoveryDecisionKind.MARK_STALE:
            # latest 条件只能由同一事务中的 Store 重新核对；Policy 不根据快照猜测。
            return True
        if decision.kind is RecoveryDecisionKind.RETRY_AUTHORIZED:
            resolution = decision.step_resolution
            if resolution is None:
                return False
            step = next(
                (
                    item
                    for item in snapshot.steps
                    if item.step_key == resolution.source_step_key
                ),
                None,
            )
            operation = next(
                (
                    item
                    for item in snapshot.operations
                    if item.operation_id == resolution.operation_id
                ),
                None,
            )
            observation = next(
                (
                    item
                    for item in snapshot.observations
                    if item.observation_id == resolution.observation_id
                ),
                None,
            )
            return bool(
                step is not None
                and self._resumable_step(step.step_key)
                and step.state is TaskStepState.OUTCOME_UNKNOWN
                and all(
                    item.step_key == step.step_key
                    or item.state
                    in {TaskStepState.SUCCEEDED, TaskStepState.SKIPPED}
                    for item in snapshot.steps
                )
                and step.current_step_attempt_no
                == resolution.source_step_attempt_no
                and step.row_version == resolution.expected_step_row_version
                and operation is not None
                and operation.state is RecoveryOperationState.OBSERVATION_RECORDED
                and operation.step_key == step.step_key
                and observation is not None
                and observation.operation_id == operation.operation_id
                and observation.step_key == step.step_key
                and observation.evidence_digest == resolution.evidence_digest
                and observation.kind
                in {
                    RecoveryObservationKind.DEFINITELY_NOT_SENT,
                    RecoveryObservationKind.NO_EFFECT_CONFIRMED,
                    RecoveryObservationKind.COMPENSATION_CONFIRMED,
                }
            )
        if decision.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
            projection = decision.terminal_projection
            if projection is None:
                return False
            source = next(
                (
                    item
                    for item in snapshot.steps
                    if item.step_key == projection.source_step_key
                ),
                None,
            )
            return bool(
                source is not None
                and source.state is TaskStepState.SUCCEEDED
                and source.current_step_attempt_no
                == projection.source_step_attempt_no
                and source.checkpoint is not None
                and source.checkpoint.code == projection.checkpoint_code
                and source.checkpoint.result_digest
                == projection.checkpoint_digest
                and all(
                    item.state
                    not in {
                        TaskStepState.RUNNING,
                        TaskStepState.OUTCOME_UNKNOWN,
                        TaskStepState.FAILED,
                    }
                    for item in snapshot.steps
                )
            )
        return False


__all__ = ["RegistryTaskRecoveryPolicy"]
