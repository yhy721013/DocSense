"""阶段 2 Task Control 的严格内存 Fake。

本文件只服务纯单元测试：不启动线程、不访问数据库、不执行网络或文件副作用。Fake
保留 Authority、租约、latest 与 Callback 冲突判断，不会为了让用例通过而自动修复
错误输入；同时允许显式注入“毒返回”，供后续 Application 验证边界失败关闭。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from app.modules.tasks.domain import (
    RecoveryAuthority,
    RecoveryCaseState,
    RecoveryObservationKind,
    RecoveryOperationState,
    TaskAttempt,
    TaskAttemptState,
    TaskAttemptTransition,
    TaskBusinessRef,
    TaskExecutionAuthority,
    TaskRecord,
    TaskRecoveryCase,
    TaskRecoveryDecision,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    TaskState,
    TaskStep,
    TaskStepAttempt,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
    apply_recovery_decision,
    apply_recovery_step_resolution,
    claim_recovery_case,
    converge_recovery_operation,
    create_recovery_case,
    take_over_expired_recovery_case,
    transition_attempt_state,
    transition_step_state,
    transition_task_state,
)
from app.modules.tasks.ports import (
    CallbackAdmissionConflict,
    ClockAnomalyError,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskAdmissionResult,
    TaskClaimRequest,
    TaskExecutionClaimResult,
    TaskExecutionMutationOutcome,
    TaskDispatchDeferralCommand,
    TaskHeartbeatCommand,
    TaskHeartbeatResult,
    TaskProgressCommand,
    TaskRecoveryClaimRequest,
    TaskRecoveryClaimResult,
    TaskRecoveryHeartbeatCommand,
    TaskRecoveryHeartbeatResult,
    TaskRecoveryMutationOutcome,
    TaskRecoveryOperationIntentCommand,
    TaskRecoverySnapshot,
    TaskStepCompletionCommand,
    TaskStepIntentCommand,
    TaskStepSkipCommand,
    TaskTerminalCommand,
    require_persisted_utc,
    validate_task_admission_batch,
)


_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_NO_POISON = object()


def _parse_utc(value: str) -> datetime:
    require_persisted_utc(value)
    return datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(_UTC_FORMAT)


class FakeClock:
    """无需 sleep 的确定性 UTC 时钟；任何显式回拨都会 fail closed。"""

    def __init__(self, initial_utc: str = "2026-08-12T00:00:00.000000Z") -> None:
        self._current = _parse_utc(initial_utc)
        self._last_safe = self._current
        self._unsafe_reason = ""

    def now_utc(self) -> str:
        if self._unsafe_reason:
            raise ClockAnomalyError(self._unsafe_reason)
        if self._current < self._last_safe:
            self._unsafe_reason = "FakeClock 检测到墙上时钟回拨"
            raise ClockAnomalyError(self._unsafe_reason)
        self._last_safe = self._current
        return _format_utc(self._current)

    def advance(self, *, seconds: float) -> str:
        """确定性推进时钟；禁止用负数绕过显式回拨 API。"""

        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("seconds 必须是数字")
        if seconds < 0:
            raise ValueError("advance 不接受负数；请用 rollback 测试异常")
        self._current += timedelta(seconds=float(seconds))
        return self.now_utc()

    def rollback(self, *, seconds: float) -> None:
        """模拟单机 wall clock 回拨；后续读时钟必须失败关闭。"""

        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("seconds 必须是数字")
        if seconds <= 0:
            raise ValueError("rollback seconds 必须大于 0")
        self._current -= timedelta(seconds=float(seconds))
        self._unsafe_reason = "FakeClock 检测到墙上时钟回拨"


class StrictTaskControlFake:
    """同时实现 Admission、Callback 冲突和 Authority 条件写的严格 Fake。"""

    def __init__(self, clock: FakeClock) -> None:
        if not isinstance(clock, FakeClock):
            raise TypeError("clock 必须是 FakeClock")
        self._clock = clock
        self._tasks: dict[object, TaskRecord] = {}
        self._attempts: dict[object, TaskAttempt] = {}
        self._steps: dict[tuple[object, str], TaskStep] = {}
        self._step_attempts: dict[tuple[object, str, int], TaskStepAttempt] = {}
        self._recovery_cases: dict[str, TaskRecoveryCase] = {}
        self._recovery_authorities: dict[str, RecoveryAuthority] = {}
        self._recovery_operations: dict[str, TaskRecoveryOperation] = {}
        self._recovery_operation_keys: dict[tuple[str, str], str] = {}
        self._recovery_observations: dict[str, TaskRecoveryObservation] = {}
        self._recovery_decisions: dict[str, TaskRecoveryDecision] = {}
        self._latest: dict[TaskBusinessRef, object] = {}
        self._callback_conflicts: dict[TaskBusinessRef, CallbackAdmissionConflict] = {}
        self._cleanup_unknown: set[object] = set()
        self._poisoned_returns: dict[str, object] = {}

    def poison_next_return(self, method_name: str, value: object) -> None:
        """显式配置一次越界返回；Fake 不清洗它，便于上层验证失败关闭。"""

        if not isinstance(method_name, str) or not method_name.strip():
            raise ValueError("method_name 必须是非空 str")
        self._poisoned_returns[method_name.strip()] = value

    def _take_poison(self, method_name: str) -> object:
        return self._poisoned_returns.pop(method_name, _NO_POISON)

    def put_task(self, task: TaskRecord, *, as_latest: bool = True) -> None:
        if not isinstance(task, TaskRecord):
            raise TypeError("task 必须是 TaskRecord")
        self._tasks[task.task_id] = task
        if as_latest:
            self._latest[task.business_ref] = task.task_id

    def get_task(self, task_id: object) -> TaskRecord | None:
        poisoned = self._take_poison("get_task")
        if poisoned is not _NO_POISON:
            return poisoned  # type: ignore[return-value]
        return self._tasks.get(task_id)

    def get_recovery_case(self, case_id: str) -> TaskRecoveryCase | None:
        """读取 Recovery Case；返回不可变对象，不暴露 Fake 内部容器。"""

        return self._recovery_cases.get(case_id)

    def get_case(self, case_id: str) -> TaskRecoveryCase | None:
        """与 ``TaskRecoveryPort`` 保持一致的读取别名。"""

        return self.get_recovery_case(case_id)

    def load_case_snapshot(self, case_id: str) -> TaskRecoverySnapshot | None:
        recovery_case = self._recovery_cases.get(case_id)
        if recovery_case is None:
            return None
        task = self._tasks.get(recovery_case.task_id)
        if task is None:
            raise RuntimeError("Recovery Case 引用的 Task 不存在")
        return TaskRecoverySnapshot(
            task=task,
            case=recovery_case,
            steps=tuple(
                step
                for (task_id, _step_key), step in sorted(
                    self._steps.items(), key=lambda item: item[0][1]
                )
                if task_id == recovery_case.task_id
            ),
            operations=self.list_operations(case_id),
            observations=self.list_observations(case_id),
        )

    def get_step(self, task_id: object, step_key: str) -> TaskStep | None:
        """仅供纯测试核对当前 Step 投影。"""

        return self._steps.get((task_id, step_key))

    def get_step_attempt(
        self,
        task_id: object,
        step_key: str,
        step_attempt_no: int,
    ) -> TaskStepAttempt | None:
        """读取追加 Step Attempt，验证恢复不会覆盖旧结果。"""

        return self._step_attempts.get((task_id, step_key, step_attempt_no))

    def set_cleanup_unknown(self, task_id: object, *, enabled: bool = True) -> None:
        if task_id not in self._tasks:
            raise KeyError("只能为已存在 Task 配置 cleanup unknown")
        if enabled:
            self._cleanup_unknown.add(task_id)
        else:
            self._cleanup_unknown.discard(task_id)

    def set_callback_conflict(
        self,
        business_ref: TaskBusinessRef,
        conflict: CallbackAdmissionConflict,
    ) -> None:
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(conflict, CallbackAdmissionConflict):
            raise TypeError("conflict 必须是 CallbackAdmissionConflict")
        self._callback_conflicts[business_ref] = conflict

    def get_admission_conflict(self, business_ref: TaskBusinessRef) -> CallbackAdmissionConflict:
        return self._callback_conflicts.get(business_ref, CallbackAdmissionConflict.NONE)

    def _classify_admission(self, request: TaskAdmissionRequest[Any]) -> TaskAdmissionOutcome:
        callback_conflict = self.get_admission_conflict(request.business_ref)
        if callback_conflict is CallbackAdmissionConflict.SENDING:
            return TaskAdmissionOutcome.CALLBACK_SENDING
        if callback_conflict is CallbackAdmissionConflict.OUTCOME_UNKNOWN:
            return TaskAdmissionOutcome.CALLBACK_OUTCOME_UNKNOWN
        latest_id = self._latest.get(request.business_ref)
        latest = self._tasks.get(latest_id)
        if latest is not None and latest.state in {
            TaskState.ACCEPTED,
            TaskState.RUNNING,
            TaskState.RECOVERY_REQUIRED,
        }:
            return TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT
        # terminal 后的 cleanup unknown 是独立维护事实，不能回滚终态或扩大既有冲突。
        return TaskAdmissionOutcome.ACCEPTED

    @staticmethod
    def _new_task(request: TaskAdmissionRequest[Any]) -> TaskRecord:
        return TaskRecord(
            task_id=request.task_id,
            task_type=request.task_type,
            business_ref=request.business_ref,
            state=TaskState.ACCEPTED,
            current_attempt_no=0,
            fencing_token=0,
            row_version=1,
            recovery_generation=0,
        )

    def admit_one(self, request: TaskAdmissionRequest[Any]) -> TaskAdmissionResult:
        poisoned = self._take_poison("admit_one")
        if poisoned is not _NO_POISON:
            return poisoned  # type: ignore[return-value]
        if not isinstance(request, TaskAdmissionRequest):
            raise TypeError("request 必须是 TaskAdmissionRequest")
        # 单文件 Analysis 同样是一项长度为 1 的批次，序号必须从 1 开始。
        validate_task_admission_batch((request,))
        outcome = self._classify_admission(request)
        task = self._new_task(request) if outcome is TaskAdmissionOutcome.ACCEPTED else None
        if task is not None:
            self.put_task(task)
        return TaskAdmissionResult(
            task_id=request.task_id,
            business_ref=request.business_ref,
            outcome=outcome,
            task=task,
        )

    def admit_many(
        self,
        requests: tuple[TaskAdmissionRequest[Any], ...],
    ) -> tuple[TaskAdmissionResult, ...]:
        poisoned = self._take_poison("admit_many")
        if poisoned is not _NO_POISON:
            return poisoned  # type: ignore[return-value]
        batch = tuple(requests)
        validate_task_admission_batch(batch)
        if len({item.business_ref for item in batch}) != len(batch):
            raise ValueError("同一批量受理不得包含重复业务键")
        outcomes = tuple(self._classify_admission(item) for item in batch)
        if any(outcome is not TaskAdmissionOutcome.ACCEPTED for outcome in outcomes):
            return tuple(
                TaskAdmissionResult(
                    task_id=item.task_id,
                    business_ref=item.business_ref,
                    outcome=(
                        outcome
                        if outcome is not TaskAdmissionOutcome.ACCEPTED
                        else TaskAdmissionOutcome.BATCH_REJECTED
                    ),
                )
                for item, outcome in zip(batch, outcomes, strict=True)
            )
        # 已在批次级完成连续序号校验；不能再把第 2..N 项降格成“单项批次”重复校验。
        # 这里直接应用整批内存变更，保持与未来 SQLite 同事务批量插入的语义一致。
        results: list[TaskAdmissionResult] = []
        for item in batch:
            task = self._new_task(item)
            self.put_task(task)
            results.append(
                TaskAdmissionResult(
                    task_id=item.task_id,
                    business_ref=item.business_ref,
                    outcome=TaskAdmissionOutcome.ACCEPTED,
                    task=task,
                )
            )
        return tuple(results)

    def claim(self, request: TaskClaimRequest) -> TaskExecutionClaimResult:
        poisoned = self._take_poison("claim")
        if poisoned is not _NO_POISON:
            return poisoned  # type: ignore[return-value]
        task = self._tasks.get(request.task_id)
        if task is None:
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.MISSING)
        if task.task_type != request.task_type or task.state is not TaskState.ACCEPTED:
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.NOT_RUNNABLE)
        now = self._clock.now_utc()
        if request.lease_expires_at <= now:
            return TaskExecutionClaimResult(TaskExecutionMutationOutcome.LEASE_EXPIRED)
        attempt_no = task.current_attempt_no + 1
        fencing_token = task.fencing_token + 1
        authority = TaskExecutionAuthority(
            task_id=task.task_id,
            attempt_no=attempt_no,
            owner_id=request.owner_id,
            lease_token=request.lease_token,
            fencing_token=fencing_token,
            lease_expires_at=request.lease_expires_at,
        )
        updated_task = replace(
            task,
            state=transition_task_state(task.state, TaskTransition.CLAIM),
            current_attempt_no=attempt_no,
            fencing_token=fencing_token,
            row_version=task.row_version + 1,
        )
        attempt = TaskAttempt(
            authority=authority,
            state=TaskAttemptState.LEASED,
            claimed_at=request.claimed_at,
            heartbeat_at=request.claimed_at,
        )
        self._tasks[task.task_id] = updated_task
        self._attempts[task.task_id] = attempt
        return TaskExecutionClaimResult(TaskExecutionMutationOutcome.APPLIED, updated_task, attempt)

    def _authority_outcome(
        self,
        authority: TaskExecutionAuthority,
    ) -> tuple[TaskExecutionMutationOutcome, TaskRecord | None, TaskAttempt | None]:
        task = self._tasks.get(authority.task_id)
        attempt = self._attempts.get(authority.task_id)
        if task is None or attempt is None:
            return TaskExecutionMutationOutcome.MISSING, task, attempt
        if attempt.authority != authority:
            return TaskExecutionMutationOutcome.AUTHORITY_LOST, task, attempt
        if self._clock.now_utc() >= authority.lease_expires_at:
            return TaskExecutionMutationOutcome.LEASE_EXPIRED, task, attempt
        return TaskExecutionMutationOutcome.APPLIED, task, attempt

    def start(
        self,
        authority: TaskExecutionAuthority,
        *,
        started_at: str,
    ) -> TaskExecutionMutationOutcome:
        require_persisted_utc(started_at, name="started_at")
        outcome, _task, attempt = self._authority_outcome(authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert attempt is not None
        if attempt.state is not TaskAttemptState.LEASED:
            return TaskExecutionMutationOutcome.INVALID_STATE
        self._attempts[authority.task_id] = replace(
            attempt,
            state=TaskAttemptState.RUNNING,
            started_at=started_at,
        )
        return TaskExecutionMutationOutcome.APPLIED

    def heartbeat(self, command: TaskHeartbeatCommand) -> TaskHeartbeatResult:
        if command.lease_expires_at <= command.authority.lease_expires_at:
            return TaskHeartbeatResult(TaskExecutionMutationOutcome.INVALID_STATE)
        outcome, _task, attempt = self._authority_outcome(command.authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return TaskHeartbeatResult(outcome)
        assert attempt is not None
        renewed = replace(command.authority, lease_expires_at=command.lease_expires_at)
        self._attempts[command.authority.task_id] = replace(
            attempt,
            authority=renewed,
            heartbeat_at=command.heartbeat_at,
        )
        return TaskHeartbeatResult(TaskExecutionMutationOutcome.APPLIED, renewed)

    def defer_dispatch(
        self,
        command: TaskDispatchDeferralCommand,
    ) -> TaskExecutionMutationOutcome:
        task = self._tasks.get(command.task_id)
        if task is None:
            return TaskExecutionMutationOutcome.MISSING
        if task.task_type != command.task_type or task.state is not TaskState.ACCEPTED:
            return TaskExecutionMutationOutcome.NOT_RUNNABLE
        if self._latest.get(task.business_ref) != task.task_id:
            return TaskExecutionMutationOutcome.STALE_LATEST
        return TaskExecutionMutationOutcome.APPLIED

    def begin_step(self, command: TaskStepIntentCommand) -> TaskExecutionMutationOutcome:
        outcome, task, attempt = self._authority_outcome(command.authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task is not None and attempt is not None
        if attempt.state is not TaskAttemptState.RUNNING or task.state is not TaskState.RUNNING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        key = (command.authority.task_id, command.step.step_key)
        current = self._steps.get(key)
        if current is not None and current.state is TaskStepState.RUNNING:
            current_attempt = self._step_attempts.get(
                (key[0], key[1], current.current_step_attempt_no)
            )
            same_frozen_definition = (
                current.task_id == command.step.task_id
                and current.step_key == command.step.step_key
                and current.definition_version == command.step.definition_version
                and current.effect_kind is command.step.effect_kind
                and current.replay_policy is command.step.replay_policy
                and current.idempotency_key == command.step.idempotency_key
                and current.current_step_attempt_no
                == command.step.current_step_attempt_no + 1
                and current.row_version == command.step.row_version + 1
            )
            same_intent = (
                current_attempt is not None
                and current_attempt.state is TaskStepState.RUNNING
                and current_attempt.task_attempt_no == command.authority.attempt_no
                and current_attempt.fencing_token == command.authority.fencing_token
                and current_attempt.idempotency_key == command.step.idempotency_key
                and current_attempt.intent_at == command.intent_at
            )
            if same_frozen_definition and same_intent:
                return TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT
            return TaskExecutionMutationOutcome.INVALID_STATE
        if current is not None and current != command.step:
            # pending 投影也必须与调用方冻结值完全一致；Fake 不自动合并旧 row_version。
            return TaskExecutionMutationOutcome.INVALID_STATE
        base = current or command.step
        if base.state is not TaskStepState.PENDING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        next_attempt_no = base.current_step_attempt_no + 1
        self._steps[key] = replace(
            base,
            state=transition_step_state(base.state, TaskStepTransition.BEGIN),
            current_step_attempt_no=next_attempt_no,
            row_version=base.row_version + 1,
        )
        self._step_attempts[(key[0], key[1], next_attempt_no)] = TaskStepAttempt(
            task_id=command.authority.task_id,
            step_key=base.step_key,
            step_attempt_no=next_attempt_no,
            task_attempt_no=command.authority.attempt_no,
            fencing_token=command.authority.fencing_token,
            state=TaskStepState.RUNNING,
            idempotency_key=base.idempotency_key,
            intent_at=command.intent_at,
        )
        return TaskExecutionMutationOutcome.APPLIED

    def complete_step(self, command: TaskStepCompletionCommand) -> TaskExecutionMutationOutcome:
        outcome, task, attempt = self._authority_outcome(command.authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task is not None and attempt is not None
        if attempt.state is not TaskAttemptState.RUNNING or task.state is not TaskState.RUNNING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        key = (command.authority.task_id, command.step_key)
        step = self._steps.get(key)
        if (
            step is None
            or step.state is not TaskStepState.RUNNING
            or step.current_step_attempt_no != command.step_attempt_no
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        target_state = transition_step_state(step.state, command.transition)
        step_attempt_key = (
            command.authority.task_id,
            command.step_key,
            command.step_attempt_no,
        )
        step_attempt = self._step_attempts.get(step_attempt_key)
        if (
            step_attempt is None
            or step_attempt.state is not TaskStepState.RUNNING
            or step_attempt.task_attempt_no != command.authority.attempt_no
            or step_attempt.fencing_token != command.authority.fencing_token
        ):
            return TaskExecutionMutationOutcome.INVALID_STATE
        self._steps[key] = replace(
            step,
            state=target_state,
            checkpoint=command.checkpoint,
            row_version=step.row_version + 1,
        )
        self._step_attempts[step_attempt_key] = replace(
            step_attempt,
            state=target_state,
            result_at=command.completed_at,
            checkpoint=command.checkpoint,
            error_code=command.error_code,
        )

        if command.transition is TaskStepTransition.MARK_OUTCOME_UNKNOWN:
            isolation = command.recovery_isolation
            assert isolation is not None
            isolated, recovery_case = create_recovery_case(
                task,
                case_id=isolation.case_id,
                source_attempt_no=command.authority.attempt_no,
                source_fencing_token=command.authority.fencing_token,
                reason_code=isolation.reason_code,
                policy_version=isolation.policy_version,
                created_at=command.completed_at,
            )
            # Fake 同样模拟一次条件写应产生的 row_version 变化，避免纯测试掩盖旧 CAS。
            self._tasks[task.task_id] = replace(
                isolated,
                row_version=task.row_version + 1,
            )
            self._attempts[task.task_id] = replace(
                attempt,
                state=transition_attempt_state(
                    attempt.state,
                    TaskAttemptTransition.ISOLATE_FOR_RECOVERY,
                ),
                completed_at=command.completed_at,
                error_code=command.error_code,
            )
            self._recovery_cases[recovery_case.case_id] = recovery_case
        return TaskExecutionMutationOutcome.APPLIED

    def skip_step(self, command: TaskStepSkipCommand) -> TaskExecutionMutationOutcome:
        outcome, _task, attempt = self._authority_outcome(command.authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert attempt is not None
        if attempt.state is not TaskAttemptState.RUNNING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        key = (command.authority.task_id, command.step.step_key)
        current = self._steps.get(key, command.step)
        if current.state is not TaskStepState.PENDING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        self._steps[key] = replace(
            current,
            state=transition_step_state(current.state, TaskStepTransition.SKIP),
            current_step_attempt_no=current.current_step_attempt_no + 1,
            row_version=current.row_version + 1,
        )
        step_attempt_no = current.current_step_attempt_no + 1
        self._step_attempts[(key[0], key[1], step_attempt_no)] = TaskStepAttempt(
            task_id=command.authority.task_id,
            step_key=current.step_key,
            step_attempt_no=step_attempt_no,
            task_attempt_no=command.authority.attempt_no,
            fencing_token=command.authority.fencing_token,
            state=TaskStepState.SKIPPED,
            idempotency_key=current.idempotency_key,
            intent_at=command.skipped_at,
            result_at=command.skipped_at,
            error_code=command.reason_code,
        )
        return TaskExecutionMutationOutcome.APPLIED

    def claim_case(self, request: TaskRecoveryClaimRequest) -> TaskRecoveryClaimResult:
        """领取或接管 Recovery Case；未过期 observing lease 永不被抢占。"""

        case = self._recovery_cases.get(request.case_id)
        if case is None:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.MISSING)
        if case.generation != request.generation:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.SOURCE_CHANGED)
        if (
            request.expected_current_fencing_token is not None
            and case.recovery_fencing_token
            != request.expected_current_fencing_token
        ):
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.SOURCE_CHANGED)
        if request.lease_expires_at <= self._clock.now_utc():
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.LEASE_EXPIRED)
        try:
            if case.state in {
                RecoveryCaseState.OPEN,
                RecoveryCaseState.AWAITING_EVIDENCE,
            }:
                claimed, authority = claim_recovery_case(
                    case,
                    owner_id=request.owner_id,
                    lease_token=request.lease_token,
                    lease_expires_at=request.lease_expires_at,
                )
            elif case.state is RecoveryCaseState.OBSERVING:
                current = self._recovery_authorities.get(case.case_id)
                if current is None:
                    return TaskRecoveryClaimResult(
                        TaskRecoveryMutationOutcome.SOURCE_CHANGED
                    )
                claimed, authority = take_over_expired_recovery_case(
                    case,
                    current,
                    claimed_at=request.claimed_at,
                    owner_id=request.owner_id,
                    lease_token=request.lease_token,
                    lease_expires_at=request.lease_expires_at,
                )
            else:
                return TaskRecoveryClaimResult(
                    TaskRecoveryMutationOutcome.INVALID_STATE
                )
        except ValueError:
            return TaskRecoveryClaimResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        self._recovery_cases[case.case_id] = claimed
        self._recovery_authorities[case.case_id] = authority
        return TaskRecoveryClaimResult(
            TaskRecoveryMutationOutcome.APPLIED,
            claimed,
            authority,
        )

    def _recovery_authority_outcome(
        self,
        authority: RecoveryAuthority,
    ) -> tuple[TaskRecoveryMutationOutcome, TaskRecoveryCase | None]:
        """统一核对 Recovery Authority，避免 Fake 各写路径出现宽松差异。"""

        case = self._recovery_cases.get(authority.case_id)
        current = self._recovery_authorities.get(authority.case_id)
        if case is None or current is None:
            return TaskRecoveryMutationOutcome.MISSING, case
        if current != authority:
            return TaskRecoveryMutationOutcome.AUTHORITY_LOST, case
        if self._clock.now_utc() >= authority.lease_expires_at:
            return TaskRecoveryMutationOutcome.LEASE_EXPIRED, case
        if case.state is not RecoveryCaseState.OBSERVING:
            return TaskRecoveryMutationOutcome.INVALID_STATE, case
        return TaskRecoveryMutationOutcome.APPLIED, case

    def heartbeat_case(
        self,
        command: TaskRecoveryHeartbeatCommand,
    ) -> TaskRecoveryHeartbeatResult:
        if command.lease_expires_at <= command.authority.lease_expires_at:
            return TaskRecoveryHeartbeatResult(TaskRecoveryMutationOutcome.INVALID_STATE)
        outcome, _case = self._recovery_authority_outcome(command.authority)
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return TaskRecoveryHeartbeatResult(outcome)
        renewed = replace(
            command.authority,
            lease_expires_at=command.lease_expires_at,
        )
        self._recovery_authorities[command.authority.case_id] = renewed
        return TaskRecoveryHeartbeatResult(
            TaskRecoveryMutationOutcome.APPLIED,
            renewed,
        )

    def begin_operation(
        self,
        command: TaskRecoveryOperationIntentCommand,
    ) -> TaskRecoveryMutationOutcome:
        """提交恢复 Intent；operation ID 和 Case 内幂等键均不可换壳复用。"""

        outcome, _case = self._recovery_authority_outcome(command.authority)
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return outcome
        existing = self._recovery_operations.get(command.operation.operation_id)
        if existing is not None:
            return (
                TaskRecoveryMutationOutcome.DUPLICATE_OPERATION
                if existing == command.operation
                else TaskRecoveryMutationOutcome.SOURCE_CHANGED
            )
        key = (command.operation.case_id, command.operation.idempotency_key)
        if key in self._recovery_operation_keys:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        self._recovery_operations[command.operation.operation_id] = command.operation
        self._recovery_operation_keys[key] = command.operation.operation_id
        return TaskRecoveryMutationOutcome.APPLIED

    def append_observation(
        self,
        authority: RecoveryAuthority,
        observation: TaskRecoveryObservation,
    ) -> TaskRecoveryMutationOutcome:
        """追加唯一 Observation，并在同一内存临界区收敛对应 Operation。"""

        outcome, _case = self._recovery_authority_outcome(authority)
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return outcome
        if (
            observation.case_id != authority.case_id
            or observation.generation != authority.generation
            or observation.recovery_fencing_token != authority.fencing_token
        ):
            return TaskRecoveryMutationOutcome.AUTHORITY_LOST
        existing = self._recovery_observations.get(observation.observation_id)
        if existing is not None:
            return (
                TaskRecoveryMutationOutcome.DUPLICATE_OBSERVATION
                if existing == observation
                else TaskRecoveryMutationOutcome.SOURCE_CHANGED
            )
        operation = self._recovery_operations.get(observation.operation_id)
        if operation is None:
            return TaskRecoveryMutationOutcome.MISSING
        if operation.state is not RecoveryOperationState.INTENT_RECORDED:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        try:
            converged = converge_recovery_operation(operation, observation)
        except ValueError:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        self._recovery_operations[operation.operation_id] = converged
        self._recovery_observations[observation.observation_id] = observation
        return TaskRecoveryMutationOutcome.APPLIED

    def decide_if_current(
        self,
        authority: RecoveryAuthority,
        decision: TaskRecoveryDecision,
    ) -> TaskRecoveryMutationOutcome:
        """以 Case、Task、证据和 Step 四重 CAS 模拟原子恢复决定。"""

        existing = self._recovery_decisions.get(decision.decision_id)
        if existing is not None:
            return (
                TaskRecoveryMutationOutcome.DUPLICATE_DECISION
                if existing == decision
                else TaskRecoveryMutationOutcome.SOURCE_CHANGED
            )
        outcome, case = self._recovery_authority_outcome(authority)
        if outcome is not TaskRecoveryMutationOutcome.APPLIED:
            return outcome
        assert case is not None
        task = self._tasks.get(case.task_id)
        if task is None:
            return TaskRecoveryMutationOutcome.MISSING

        updated_step: TaskStep | None = None
        resolution = decision.step_resolution
        if resolution is not None:
            operation = self._recovery_operations.get(resolution.operation_id)
            observation = self._recovery_observations.get(
                resolution.observation_id
            )
            if operation is None or observation is None:
                return TaskRecoveryMutationOutcome.MISSING
            if (
                operation.state is not RecoveryOperationState.OBSERVATION_RECORDED
                or observation.operation_id != operation.operation_id
                or observation.evidence_digest != resolution.evidence_digest
                or observation.kind
                not in {
                    RecoveryObservationKind.DEFINITELY_NOT_SENT,
                    RecoveryObservationKind.NO_EFFECT_CONFIRMED,
                    RecoveryObservationKind.COMPENSATION_CONFIRMED,
                }
            ):
                return TaskRecoveryMutationOutcome.SOURCE_CHANGED
            current_step = self._steps.get(
                (task.task_id, resolution.source_step_key)
            )
            if current_step is None:
                return TaskRecoveryMutationOutcome.MISSING
            try:
                updated_step = apply_recovery_step_resolution(
                    current_step,
                    resolution,
                )
            except ValueError:
                return TaskRecoveryMutationOutcome.SOURCE_CHANGED

        projection = decision.terminal_projection
        if projection is not None:
            source_step = self._steps.get(
                (task.task_id, projection.source_step_key)
            )
            if (
                source_step is None
                or source_step.current_step_attempt_no
                != projection.source_step_attempt_no
                or source_step.checkpoint is None
                or source_step.checkpoint.code != projection.checkpoint_code
                or source_step.checkpoint.result_digest
                != projection.checkpoint_digest
            ):
                return TaskRecoveryMutationOutcome.SOURCE_CHANGED
        try:
            updated_task, updated_case = apply_recovery_decision(
                task,
                case,
                decision,
            )
        except ValueError:
            return TaskRecoveryMutationOutcome.SOURCE_CHANGED

        if updated_step is not None:
            self._steps[(task.task_id, updated_step.step_key)] = updated_step
        self._tasks[task.task_id] = updated_task
        self._recovery_cases[case.case_id] = updated_case
        self._recovery_decisions[decision.decision_id] = decision
        # KEEP 同样释放本轮观察权；后续到点后由 awaiting_evidence 再领取并产生新 fencing。
        self._recovery_authorities.pop(case.case_id, None)
        return TaskRecoveryMutationOutcome.APPLIED

    def list_observations(
        self,
        case_id: str,
    ) -> tuple[TaskRecoveryObservation, ...]:
        return tuple(
            item
            for item in self._recovery_observations.values()
            if item.case_id == case_id
        )

    def list_operations(self, case_id: str) -> tuple[TaskRecoveryOperation, ...]:
        return tuple(
            item
            for item in self._recovery_operations.values()
            if item.case_id == case_id
        )

    def update_progress(self, command: TaskProgressCommand) -> TaskExecutionMutationOutcome:
        outcome, task, attempt = self._authority_outcome(command.authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task is not None and attempt is not None
        if attempt.state is not TaskAttemptState.RUNNING or task.state is not TaskState.RUNNING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        if self._latest.get(task.business_ref) != task.task_id:
            return TaskExecutionMutationOutcome.STALE_LATEST
        return TaskExecutionMutationOutcome.APPLIED

    def finish(self, command: TaskTerminalCommand) -> TaskExecutionMutationOutcome:
        outcome, task, attempt = self._authority_outcome(command.authority)
        if outcome is not TaskExecutionMutationOutcome.APPLIED:
            return outcome
        assert task is not None and attempt is not None
        if task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.STALE}:
            return TaskExecutionMutationOutcome.DUPLICATE_TERMINAL
        if attempt.state is not TaskAttemptState.RUNNING:
            return TaskExecutionMutationOutcome.INVALID_STATE
        if self._latest.get(task.business_ref) != task.task_id:
            return TaskExecutionMutationOutcome.STALE_LATEST
        next_state = transition_task_state(task.state, command.transition)
        attempt_state = (
            TaskAttemptState.SUCCEEDED
            if next_state is TaskState.SUCCEEDED
            else TaskAttemptState.FAILED
        )
        self._tasks[task.task_id] = replace(
            task,
            state=next_state,
            row_version=task.row_version + 1,
        )
        self._attempts[task.task_id] = replace(
            attempt,
            state=attempt_state,
            completed_at=command.completed_at,
        )
        return TaskExecutionMutationOutcome.APPLIED


__all__ = ["FakeClock", "StrictTaskControlFake"]
