"""Recovery Case 领取、探测意图、Observation 与 Decision 的唯一应用协调器。

每个数据库写入都使用独立短 Recovery UoW。``RecoveryOperationPort.execute`` 明确位于
``begin_operation`` 提交之后、``append_observation`` 之前，因此 Adapter 不可能在持有
SQLite 写事务时访问外部系统。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import logging
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.modules.tasks.domain import (
    RecoveryAuthority,
    RecoveryDecisionKind,
    RecoveryObservationKind,
    RecoveryOperationKind,
    RecoveryOperationState,
    TaskId,
    TaskRecoveryDecision,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    add_persisted_utc_seconds,
)
from app.modules.tasks.ports import (
    CallbackControlMutationOutcome,
    ClockPort,
    RecoveryCallbackEligibilityCommand,
    TaskLeaseTokenFactoryPort,
    TaskRecoveryClaimRequest,
    TaskRecoveryHeartbeatCommand,
    TaskRecoveryMutationOutcome,
    TaskRecoveryOperationIntentCommand,
    TaskRecoveryPolicyPort,
    TaskRecoverySnapshot,
    TaskRecoveryUnitOfWorkFactory,
)


logger = logging.getLogger(__name__)


def _required_text(value: object, *, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if len(normalized) > maximum:
        raise ValueError(f"{name} 最多 {maximum} 个字符")
    return normalized


@dataclass(frozen=True, slots=True)
class ClaimRecoveryCaseCommand:
    """严格领取命令；所有 expected 字段用于拒绝陈旧运维快照。"""

    task_id: TaskId
    case_id: str
    generation: int
    expected_task_row_version: int
    source_attempt_no: int
    source_fencing_token: int
    expected_recovery_fencing_token: int
    owner_id: str
    operator_marker: str
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        for name in ("case_id", "owner_id", "operator_marker", "reason_code"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name, maximum=128),
            )
        for name in ("generation", "source_attempt_no", "source_fencing_token"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        for name in ("expected_task_row_version", "expected_recovery_fencing_token"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} 必须是非负整数")


@dataclass(frozen=True, slots=True)
class RecoveryOperationRequest:
    """事务外探测/补偿的稳定输入；不允许携带正文、凭据或原始响应。"""

    operation_id: str
    kind: RecoveryOperationKind
    step_key: str
    idempotency_key: str
    intent_digest: str
    external_ref: str = ""

    def __post_init__(self) -> None:
        for name in ("operation_id", "step_key", "idempotency_key"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name))
        if not isinstance(self.kind, RecoveryOperationKind):
            raise TypeError("kind 必须是 RecoveryOperationKind")
        digest = _required_text(self.intent_digest, name="intent_digest", maximum=64)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise ValueError("intent_digest 必须是 SHA-256 hex")
        object.__setattr__(self, "intent_digest", digest.lower())
        if not isinstance(self.external_ref, str):
            raise TypeError("external_ref 必须是 str")
        object.__setattr__(self, "external_ref", self.external_ref.strip())


@dataclass(frozen=True, slots=True)
class RecoveryOperationResult:
    kind: RecoveryObservationKind
    evidence_digest: str
    reason_code: str
    external_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RecoveryObservationKind):
            raise TypeError("kind 必须是 RecoveryObservationKind")
        digest = _required_text(self.evidence_digest, name="evidence_digest", maximum=64)
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            raise ValueError("evidence_digest 必须是 SHA-256 hex")
        object.__setattr__(self, "evidence_digest", digest.lower())
        object.__setattr__(
            self,
            "reason_code",
            _required_text(self.reason_code, name="reason_code", maximum=128),
        )
        if not isinstance(self.external_ref, str):
            raise TypeError("external_ref 必须是 str")
        object.__setattr__(self, "external_ref", self.external_ref.strip())


@runtime_checkable
class RecoveryOperationPort(Protocol):
    """一个有界、本地优先的探测或受控补偿能力。"""

    def execute(self, request: RecoveryOperationRequest) -> RecoveryOperationResult:
        ...


@dataclass(frozen=True, slots=True)
class RecoveryCaseSession:
    """Coordinator 返回的当前恢复能力；lease token 默认不进入 repr。"""

    authority: RecoveryAuthority = field(repr=False)
    snapshot: TaskRecoverySnapshot
    operator_marker: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class RecoveryCoordinatorResult:
    outcome: TaskRecoveryMutationOutcome
    session: RecoveryCaseSession | None = None
    observation: TaskRecoveryObservation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskRecoveryMutationOutcome):
            raise TypeError("outcome 必须是 TaskRecoveryMutationOutcome")
        if self.outcome is TaskRecoveryMutationOutcome.APPLIED:
            if self.session is None and self.observation is None:
                raise ValueError("applied 结果必须携带 Session 或 Observation")
        elif self.session is not None or self.observation is not None:
            raise ValueError("未应用结果不得携带恢复能力或 Observation")


class RecoveryCoordinator:
    """以独立 Recovery Authority 协调一个 Case，不持有跨 I/O 事务。"""

    def __init__(
        self,
        *,
        clock: ClockPort,
        recovery_uow_factory: TaskRecoveryUnitOfWorkFactory,
        lease_token_factory: TaskLeaseTokenFactoryPort,
        policies: Mapping[str, TaskRecoveryPolicyPort],
        recovery_lease_seconds: float,
    ) -> None:
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not callable(recovery_uow_factory):
            raise TypeError("recovery_uow_factory 必须可调用")
        if not isinstance(lease_token_factory, TaskLeaseTokenFactoryPort):
            raise TypeError("lease_token_factory 必须实现 TaskLeaseTokenFactoryPort")
        normalized = dict(policies)
        if set(normalized) != {"report", "weaponry", "file"} or any(
            not isinstance(item, TaskRecoveryPolicyPort)
            for item in normalized.values()
        ):
            raise ValueError("policies 必须精确覆盖三个业务且实现 TaskRecoveryPolicyPort")
        if isinstance(recovery_lease_seconds, bool) or not isinstance(
            recovery_lease_seconds, (int, float)
        ) or recovery_lease_seconds <= 0:
            raise ValueError("recovery_lease_seconds 必须是正数")
        self._clock = clock
        self._recovery_uow_factory = recovery_uow_factory
        self._lease_token_factory = lease_token_factory
        self._policies = normalized
        self._recovery_lease_seconds = float(recovery_lease_seconds)

    def claim(self, command: ClaimRecoveryCaseCommand) -> RecoveryCoordinatorResult:
        if not isinstance(command, ClaimRecoveryCaseCommand):
            raise TypeError("command 必须是 ClaimRecoveryCaseCommand")
        claimed_at = self._clock.now_utc()
        with self._recovery_uow_factory() as unit_of_work:
            snapshot = unit_of_work.recovery.load_case_snapshot(command.case_id)
            if snapshot is None:
                return RecoveryCoordinatorResult(TaskRecoveryMutationOutcome.MISSING)
            if not self._matches_claim_snapshot(command, snapshot):
                return RecoveryCoordinatorResult(
                    TaskRecoveryMutationOutcome.SOURCE_CHANGED
                )
            result = unit_of_work.recovery.claim_case(
                TaskRecoveryClaimRequest(
                    case_id=command.case_id,
                    generation=command.generation,
                    owner_id=command.owner_id,
                    lease_token=self._lease_token_factory.new_token(),
                    claimed_at=claimed_at,
                    lease_expires_at=add_persisted_utc_seconds(
                        claimed_at,
                        seconds=self._recovery_lease_seconds,
                    ),
                    expected_current_fencing_token=(
                        command.expected_recovery_fencing_token
                    ),
                )
            )
            if result.outcome is not TaskRecoveryMutationOutcome.APPLIED:
                return RecoveryCoordinatorResult(result.outcome)
            assert result.authority is not None and result.case is not None
            unit_of_work.commit()
        updated_snapshot = TaskRecoverySnapshot(
            task=snapshot.task,
            case=result.case,
            steps=snapshot.steps,
            operations=snapshot.operations,
            observations=snapshot.observations,
        )
        logger.info(
            "Recovery Case 已由 Coordinator 领取: task_id=%s case_id=%s "
            "generation=%d recovery_fencing=%d operator=%s reason_code=%s",
            command.task_id,
            command.case_id,
            command.generation,
            result.authority.fencing_token,
            command.operator_marker,
            command.reason_code,
        )
        return RecoveryCoordinatorResult(
            TaskRecoveryMutationOutcome.APPLIED,
            session=RecoveryCaseSession(
                authority=result.authority,
                snapshot=updated_snapshot,
                operator_marker=command.operator_marker,
                reason_code=command.reason_code,
            ),
        )

    def execute_operation(
        self,
        session: RecoveryCaseSession,
        request: RecoveryOperationRequest,
        operation_port: RecoveryOperationPort,
    ) -> RecoveryCoordinatorResult:
        """先提交 Intent，事务外执行能力，再续租并原子追加 Observation。"""

        if not isinstance(session, RecoveryCaseSession):
            raise TypeError("session 必须是 RecoveryCaseSession")
        if not isinstance(request, RecoveryOperationRequest):
            raise TypeError("request 必须是 RecoveryOperationRequest")
        if not isinstance(operation_port, RecoveryOperationPort):
            raise TypeError("operation_port 必须实现 RecoveryOperationPort")
        authority = session.authority
        intent_at = self._clock.now_utc()
        operation = TaskRecoveryOperation(
            operation_id=request.operation_id,
            case_id=authority.case_id,
            generation=authority.generation,
            recovery_fencing_token=authority.fencing_token,
            kind=request.kind,
            step_key=request.step_key,
            idempotency_key=request.idempotency_key,
            intent_digest=request.intent_digest,
            external_ref=request.external_ref,
            state=RecoveryOperationState.INTENT_RECORDED,
            intent_at=intent_at,
        )
        with self._recovery_uow_factory() as unit_of_work:
            outcome = unit_of_work.recovery.begin_operation(
                TaskRecoveryOperationIntentCommand(
                    authority=authority,
                    operation=operation,
                )
            )
            if outcome is not TaskRecoveryMutationOutcome.APPLIED:
                return RecoveryCoordinatorResult(outcome)
            unit_of_work.commit()

        try:
            operation_result = operation_port.execute(request)
            if not isinstance(operation_result, RecoveryOperationResult):
                raise TypeError("RecoveryOperationPort 返回值类型不正确")
        except Exception as exc:
            # Intent 已提交，Adapter 异常只能形成“结果未知”的脱敏证据，绝不能假装未执行。
            digest = hashlib.sha256(
                f"probe_adapter_error:{type(exc).__name__}".encode("utf-8")
            ).hexdigest()
            operation_result = RecoveryOperationResult(
                kind=RecoveryObservationKind.OUTCOME_UNKNOWN,
                evidence_digest=digest,
                reason_code="recovery_operation_adapter_error",
            )

        renewed = self.heartbeat(authority)
        if renewed.outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return RecoveryCoordinatorResult(renewed.outcome)
        assert renewed.session is not None
        renewed_authority = renewed.session.authority
        observed_at = self._clock.now_utc()
        observation = TaskRecoveryObservation(
            observation_id=f"recovery-observation-{uuid4().hex}",
            operation_id=operation.operation_id,
            case_id=renewed_authority.case_id,
            generation=renewed_authority.generation,
            recovery_fencing_token=renewed_authority.fencing_token,
            kind=operation_result.kind,
            evidence_digest=operation_result.evidence_digest,
            observed_at=observed_at,
            step_key=request.step_key,
            external_ref=operation_result.external_ref,
            reason_code=operation_result.reason_code,
        )
        with self._recovery_uow_factory() as unit_of_work:
            outcome = unit_of_work.recovery.append_observation(
                renewed_authority,
                observation,
            )
            if outcome is not TaskRecoveryMutationOutcome.APPLIED:
                return RecoveryCoordinatorResult(outcome)
            unit_of_work.commit()
        logger.info(
            "Recovery Operation 已收敛 Observation: case_id=%s operation_id=%s "
            "kind=%s recovery_fencing=%d",
            authority.case_id,
            operation.operation_id,
            observation.kind.value,
            renewed_authority.fencing_token,
        )
        return RecoveryCoordinatorResult(
            TaskRecoveryMutationOutcome.APPLIED,
            session=RecoveryCaseSession(
                authority=renewed_authority,
                snapshot=renewed.session.snapshot,
                operator_marker=session.operator_marker,
                reason_code=session.reason_code,
            ),
            observation=observation,
        )

    def heartbeat(self, authority: RecoveryAuthority) -> RecoveryCoordinatorResult:
        if not isinstance(authority, RecoveryAuthority):
            raise TypeError("authority 必须是 RecoveryAuthority")
        heartbeat_at = self._clock.now_utc()
        lease_expires_at = add_persisted_utc_seconds(
            authority.lease_expires_at,
            seconds=self._recovery_lease_seconds,
        )
        with self._recovery_uow_factory() as unit_of_work:
            result = unit_of_work.recovery.heartbeat_case(
                TaskRecoveryHeartbeatCommand(
                    authority=authority,
                    heartbeat_at=heartbeat_at,
                    lease_expires_at=lease_expires_at,
                )
            )
            if result.outcome is not TaskRecoveryMutationOutcome.APPLIED:
                return RecoveryCoordinatorResult(result.outcome)
            assert result.authority is not None
            unit_of_work.commit()
        with self._recovery_uow_factory() as unit_of_work:
            snapshot = unit_of_work.recovery.load_case_snapshot(authority.case_id)
        assert snapshot is not None
        return RecoveryCoordinatorResult(
            TaskRecoveryMutationOutcome.APPLIED,
            session=RecoveryCaseSession(
                authority=result.authority,
                snapshot=snapshot,
                operator_marker="automatic-recovery-heartbeat",
                reason_code="recovery_lease_renewed",
            ),
        )

    def decide(
        self,
        session: RecoveryCaseSession,
        decision: TaskRecoveryDecision,
    ) -> TaskRecoveryMutationOutcome:
        """原子提交证据 Decision；恢复终态同时核验结果并登记 Callback 资格。"""

        if not isinstance(session, RecoveryCaseSession):
            raise TypeError("session 必须是 RecoveryCaseSession")
        if not isinstance(decision, TaskRecoveryDecision):
            raise TypeError("decision 必须是 TaskRecoveryDecision")
        if decision.actor_marker != session.operator_marker:
            return TaskRecoveryMutationOutcome.INVALID_STATE
        with self._recovery_uow_factory() as unit_of_work:
            snapshot = unit_of_work.recovery.load_case_snapshot(
                session.authority.case_id
            )
            if snapshot is None:
                return TaskRecoveryMutationOutcome.MISSING
            policy = self._policies[snapshot.task.task_type]
            if not policy.authorize_decision(snapshot, decision):
                return TaskRecoveryMutationOutcome.INVALID_STATE
            if decision.kind is RecoveryDecisionKind.RETRY_AUTHORIZED:
                try:
                    resume_ok = unit_of_work.resume_preflight.verify(
                        snapshot,
                        decision,
                    )
                except Exception:
                    logger.exception(
                        "Recovery 业务续跑预检失败，Decision 事务已回滚: "
                        "task_id=%s case_id=%s decision_id=%s",
                        decision.task_id,
                        decision.case_id,
                        decision.decision_id,
                    )
                    return TaskRecoveryMutationOutcome.INVALID_STATE
                if not resume_ok:
                    return TaskRecoveryMutationOutcome.INVALID_STATE
            if decision.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
                try:
                    preflight_ok = unit_of_work.finalization_preflight.verify(
                        snapshot,
                        decision,
                    )
                except Exception:
                    logger.exception(
                        "Recovery 业务结果预检失败，终态事务已回滚: "
                        "task_id=%s case_id=%s decision_id=%s",
                        decision.task_id,
                        decision.case_id,
                        decision.decision_id,
                    )
                    return TaskRecoveryMutationOutcome.INVALID_STATE
                if not preflight_ok:
                    return TaskRecoveryMutationOutcome.INVALID_STATE
            outcome = unit_of_work.recovery.decide_if_current(
                session.authority,
                decision,
            )
            if outcome is TaskRecoveryMutationOutcome.APPLIED:
                if decision.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
                    projection = decision.terminal_projection
                    assert projection is not None
                    callback_outcome = (
                        unit_of_work.callback_delivery.mark_recovery_eligible(
                            RecoveryCallbackEligibilityCommand(
                                authority=session.authority,
                                decision_id=decision.decision_id,
                                task_id=decision.task_id,
                                business_ref=snapshot.task.business_ref,
                                source_step_key=projection.source_step_key,
                                source_step_attempt_no=(
                                    projection.source_step_attempt_no
                                ),
                                checkpoint_code=projection.checkpoint_code,
                                checkpoint_digest=projection.checkpoint_digest,
                                eligible_at=decision.decided_at,
                            )
                        )
                    )
                    if callback_outcome is not CallbackControlMutationOutcome.APPLIED:
                        logger.error(
                            "Recovery Callback eligibility 未生效，终态事务已回滚: "
                            "task_id=%s case_id=%s outcome=%s",
                            decision.task_id,
                            decision.case_id,
                            callback_outcome.value,
                        )
                        return TaskRecoveryMutationOutcome.INVALID_STATE
                unit_of_work.commit()
            return outcome

    @staticmethod
    def _matches_claim_snapshot(
        command: ClaimRecoveryCaseCommand,
        snapshot: TaskRecoverySnapshot,
    ) -> bool:
        return bool(
            snapshot.task.task_id == command.task_id
            and snapshot.case.case_id == command.case_id
            and snapshot.case.generation == command.generation
            and snapshot.task.row_version == command.expected_task_row_version
            and snapshot.case.source_attempt_no == command.source_attempt_no
            and snapshot.case.source_fencing_token == command.source_fencing_token
            and snapshot.case.recovery_fencing_token
            == command.expected_recovery_fencing_token
        )


__all__ = [
    "ClaimRecoveryCaseCommand",
    "RecoveryCaseSession",
    "RecoveryCoordinator",
    "RecoveryCoordinatorResult",
    "RecoveryOperationPort",
    "RecoveryOperationRequest",
    "RecoveryOperationResult",
]
