"""阶段 2-7 默认只读、写入严格绑定快照的内部恢复用例。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import uuid4

from app.modules.tasks.domain import (
    RecoveryDecisionKind,
    TaskId,
    TaskRecoveryDecision,
    TaskRecoveryStepResolution,
    TaskRecoveryTerminalProjection,
    TaskState,
    TaskStepTransition,
)
from app.modules.tasks.ports import (
    require_persisted_utc,
    TaskRecoveryMutationOutcome,
    TaskRecoverySnapshot,
    TaskRecoveryUnitOfWorkFactory,
)

from .reconcile_recovery_case import ClaimRecoveryCaseCommand, RecoveryCoordinator


class RecoveryOperatorAction(str, Enum):
    KEEP_QUARANTINED = "keep_quarantined"
    RETRY_AUTHORIZED = "retry_authorized"
    FINALIZE_FROM_CHECKPOINT = "finalize_from_checkpoint"
    MARK_STALE = "mark_stale"


@dataclass(frozen=True, slots=True)
class RecoveryCaseInspection:
    task_id: str
    task_type: str
    task_state: str
    task_row_version: int
    case_id: str
    generation: int
    case_state: str
    source_attempt_no: int
    source_fencing_token: int
    recovery_fencing_token: int
    step_states: tuple[tuple[str, str, int, int], ...]
    operation_count: int
    observation_count: int


@dataclass(frozen=True, slots=True)
class StrictRecoveryDecisionCommand:
    """写模式完整输入；缺省值只允许表达与当前 action 无关的字段。"""

    task_id: TaskId
    case_id: str
    generation: int
    expected_task_row_version: int
    source_attempt_no: int
    source_fencing_token: int
    expected_recovery_fencing_token: int
    operator: str
    reason_code: str
    evidence_digest: str
    action: RecoveryOperatorAction
    decided_at: str
    next_observation_at: str = ""
    retry_from_step_key: str = ""
    source_step_attempt_no: int = 0
    expected_step_row_version: int = 0
    operation_id: str = ""
    observation_id: str = ""
    terminal_state: TaskState | None = None
    checkpoint_code: str = ""
    checkpoint_digest: str = ""
    public_status: str = ""
    message: str = ""
    result_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.action, RecoveryOperatorAction):
            raise TypeError("action 必须是 RecoveryOperatorAction")
        for name in ("case_id", "operator", "reason_code"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 不能为空")
            object.__setattr__(self, name, value.strip())
        for name in ("generation", "source_attempt_no", "source_fencing_token"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("expected_task_row_version", "expected_recovery_fencing_token"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} 必须是非负整数")
        digest = self.evidence_digest.strip().lower() if isinstance(
            self.evidence_digest, str
        ) else ""
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("evidence_digest 必须是 SHA-256 hex")
        object.__setattr__(self, "evidence_digest", digest)
        decided_at = require_persisted_utc(self.decided_at, name="decided_at")
        object.__setattr__(self, "decided_at", decided_at)
        if self.next_observation_at:
            next_at = require_persisted_utc(
                self.next_observation_at,
                name="next_observation_at",
            )
            if next_at <= decided_at:
                raise ValueError("next_observation_at 必须晚于 decided_at")
            object.__setattr__(self, "next_observation_at", next_at)
        self._validate_action_fields()

    def _validate_action_fields(self) -> None:
        retry_fields = (
            self.retry_from_step_key,
            self.operation_id,
            self.observation_id,
        )
        terminal_fields = (
            self.checkpoint_code,
            self.checkpoint_digest,
            self.public_status,
        )
        if self.action is RecoveryOperatorAction.RETRY_AUTHORIZED:
            if not all(retry_fields) or self.source_step_attempt_no <= 0:
                raise ValueError("retry_authorized 缺少精确 Step/Operation/Observation 身份")
            if (
                self.expected_step_row_version < 0
                or any(terminal_fields)
                or self.next_observation_at
            ):
                raise ValueError("retry_authorized 字段组合无效")
        elif self.action is RecoveryOperatorAction.FINALIZE_FROM_CHECKPOINT:
            checkpoint_digest = (
                self.checkpoint_digest.strip().lower()
                if isinstance(self.checkpoint_digest, str)
                else ""
            )
            if (
                not all(terminal_fields)
                or not self.retry_from_step_key
                or self.source_step_attempt_no <= 0
                or self.terminal_state not in {TaskState.SUCCEEDED, TaskState.FAILED}
                or any((self.operation_id, self.observation_id))
                or self.next_observation_at
                or len(checkpoint_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in checkpoint_digest
                )
            ):
                raise ValueError("finalize_from_checkpoint 缺少终态或 Checkpoint 身份")
            object.__setattr__(self, "checkpoint_digest", checkpoint_digest)
        elif (
            any(retry_fields)
            or any(terminal_fields)
            or self.terminal_state is not None
            or (
                self.action is RecoveryOperatorAction.MARK_STALE
                and self.next_observation_at
            )
        ):
            raise ValueError("当前 action 不得携带重试或终态字段")


class RecoveryOperatorService:
    """运维脚本唯一可调用的 Application 边界；不暴露 Store 或 SQLite。"""

    def __init__(
        self,
        *,
        recovery_uow_factory: TaskRecoveryUnitOfWorkFactory,
        coordinator: RecoveryCoordinator,
    ) -> None:
        if not callable(recovery_uow_factory):
            raise TypeError("recovery_uow_factory 必须可调用")
        if not isinstance(coordinator, RecoveryCoordinator):
            raise TypeError("coordinator 必须是 RecoveryCoordinator")
        self._recovery_uow_factory = recovery_uow_factory
        self._coordinator = coordinator

    def inspect(self, task_id: TaskId, case_id: str) -> RecoveryCaseInspection | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("case_id 不能为空")
        with self._recovery_uow_factory() as unit_of_work:
            snapshot = unit_of_work.recovery.load_case_snapshot(case_id.strip())
        if snapshot is None:
            return None
        if snapshot.task.task_id != task_id:
            raise ValueError("task_id 与 Recovery Case 不一致")
        return self._inspection(snapshot)

    def execute(self, command: StrictRecoveryDecisionCommand) -> TaskRecoveryMutationOutcome:
        if not isinstance(command, StrictRecoveryDecisionCommand):
            raise TypeError("command 必须是 StrictRecoveryDecisionCommand")
        claimed = self._coordinator.claim(
            ClaimRecoveryCaseCommand(
                task_id=command.task_id,
                case_id=command.case_id,
                generation=command.generation,
                expected_task_row_version=command.expected_task_row_version,
                source_attempt_no=command.source_attempt_no,
                source_fencing_token=command.source_fencing_token,
                expected_recovery_fencing_token=(
                    command.expected_recovery_fencing_token
                ),
                owner_id=f"stage2-operator:{command.operator}",
                operator_marker=command.operator,
                reason_code=command.reason_code,
            )
        )
        if claimed.outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return claimed.outcome
        assert claimed.session is not None
        policy_versions = {
            "report": "report-task-recovery.v1",
            "weaponry": "weaponry-task-recovery.v1",
            "file": "analysis-task-recovery.v1",
        }
        policy_version = policy_versions.get(claimed.session.snapshot.task.task_type)
        if policy_version is None:
            return TaskRecoveryMutationOutcome.INVALID_STATE
        decision = self._decision(
            command,
            claimed.session.authority.fencing_token,
            policy_version,
        )
        return self._coordinator.decide(claimed.session, decision)

    @staticmethod
    def _inspection(snapshot: TaskRecoverySnapshot) -> RecoveryCaseInspection:
        return RecoveryCaseInspection(
            task_id=snapshot.task.task_id.value,
            task_type=snapshot.task.task_type,
            task_state=snapshot.task.state.value,
            task_row_version=snapshot.task.row_version,
            case_id=snapshot.case.case_id,
            generation=snapshot.case.generation,
            case_state=snapshot.case.state.value,
            source_attempt_no=snapshot.case.source_attempt_no,
            source_fencing_token=snapshot.case.source_fencing_token,
            recovery_fencing_token=snapshot.case.recovery_fencing_token,
            step_states=tuple(
                (
                    step.step_key,
                    step.state.value,
                    step.current_step_attempt_no,
                    step.row_version,
                )
                for step in snapshot.steps
            ),
            operation_count=len(snapshot.operations),
            observation_count=len(snapshot.observations),
        )

    @staticmethod
    def _decision(
        command: StrictRecoveryDecisionCommand,
        recovery_fencing_token: int,
        policy_version: str,
    ) -> TaskRecoveryDecision:
        kind = RecoveryDecisionKind(command.action.value)
        resolution = None
        projection = None
        retry_from = ""
        if command.action is RecoveryOperatorAction.RETRY_AUTHORIZED:
            retry_from = command.retry_from_step_key
            resolution = TaskRecoveryStepResolution(
                source_step_key=command.retry_from_step_key,
                source_step_attempt_no=command.source_step_attempt_no,
                expected_step_row_version=command.expected_step_row_version,
                operation_id=command.operation_id,
                observation_id=command.observation_id,
                evidence_digest=command.evidence_digest,
                target_transition=TaskStepTransition.RETRY_AUTHORIZED,
            )
        elif command.action is RecoveryOperatorAction.FINALIZE_FROM_CHECKPOINT:
            projection = TaskRecoveryTerminalProjection(
                source_step_key=command.retry_from_step_key,
                source_step_attempt_no=command.source_step_attempt_no,
                checkpoint_code=command.checkpoint_code,
                checkpoint_digest=command.checkpoint_digest,
                public_status=command.public_status,
                message=command.message,
                result_ref=command.result_ref,
            )
        return TaskRecoveryDecision(
            decision_id=f"operator-decision-{uuid4().hex}",
            task_id=command.task_id,
            case_id=command.case_id,
            generation=command.generation,
            recovery_fencing_token=recovery_fencing_token,
            expected_task_row_version=command.expected_task_row_version,
            source_attempt_no=command.source_attempt_no,
            source_fencing_token=command.source_fencing_token,
            kind=kind,
            evidence_digest=command.evidence_digest,
            reason_code=command.reason_code,
            policy_version=policy_version,
            actor_marker=command.operator,
            decided_at=command.decided_at,
            retry_from_step_key=retry_from,
            terminal_state=command.terminal_state,
            next_observation_at=command.next_observation_at,
            terminal_projection=projection,
            step_resolution=resolution,
        )


__all__ = [
    "RecoveryCaseInspection",
    "RecoveryOperatorAction",
    "RecoveryOperatorService",
    "StrictRecoveryDecisionCommand",
]
