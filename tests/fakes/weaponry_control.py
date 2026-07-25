"""武器谱 Callback、Resource 与 Dispatcher 严格 Fake。"""

from __future__ import annotations

from dataclasses import replace
from itertools import count
from threading import RLock

from app.modules.tasks.domain import TaskBusinessRef, TaskExecutionSnapshot, TaskId
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    ProgressPublication,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskQueueSnapshot,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
)
from app.modules.weaponry.domain import (
    WeaponryCallbackPayload,
    WeaponryInputSnapshot,
    WeaponryResult,
    WeaponrySubmission,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCleanupLease,
    AcquireWeaponryCallback,
    CleanupWeaponryExternalResource,
    CompleteWeaponryResourceCleanup,
    DeliverWeaponryCallback,
    IdempotentOperationResult,
    PrepareWeaponryResourceCleanup,
    QuarantineWeaponryResources,
    RegisterWeaponryResource,
    ReleaseWeaponryCleanupLease,
    ReleaseUnknownWeaponryCallback,
    WaitForWeaponryCallbackRelease,
    WeaponryCallbackAcquireOutcome,
    WeaponryCallbackAcquireReason,
    WeaponryCallbackAcquireResult,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCallbackGuardLease,
    WeaponryCallbackGuardSweepResult,
    WeaponryCallbackRecoveryCandidate,
    WeaponryCallbackReleaseOutcome,
    WeaponryCallbackReleaseResult,
    WeaponryCallbackWaitOutcome,
    WeaponryCallbackWaitResult,
    WeaponryCleanupLease,
    WeaponryCleanupLeaseAcquireOutcome,
    WeaponryCleanupLeaseAcquireResult,
    WeaponryExternalResourceCleanupResult,
    WeaponryPortStateError,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponryTrackedResourceState,
)

from .weaponry_support import WeaponryInvocationRecorder


class FakeWeaponryTaskCommandPort:
    """线程安全表达原子受理、领取和 latest 条件写的任务控制面 Fake。

    Fake 只保存不可变领域快照，不连接 SQLite，也不会替 Application 自动发送通知或
    回调。测试可以逐次注入条件写结果，从而精确复现 stale、持久化异常和错误返回类型。
    """

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self._sequence = count(1)
        self._lock = RLock()
        self.executions: dict[
            TaskId,
            TaskExecutionSnapshot[WeaponryInputSnapshot],
        ] = {}
        self.latest: dict[TaskBusinessRef, TaskId] = {}
        self.submission_outcomes: dict[TaskBusinessRef, TaskSubmissionOutcome] = {}
        self.submission_outcome_sequence: list[TaskSubmissionOutcome] = []
        self.claim_outcomes: dict[TaskId, TaskClaimOutcome] = {}
        self.errors: dict[str, BaseException] = {}
        self.progress_results: list[object] = []
        self.finish_results: list[object] = []
        self.latest_results: list[object] = []
        self.forced_create_result: object | None = None
        self.forced_get_result: object | None = None
        self.forced_claim_result: object | None = None
        self.submission_calls: list[TaskSubmissionCommand[WeaponrySubmission]] = []
        self.get_calls: list[TaskId] = []
        self.claim_calls: list[TaskId] = []
        self.progress_calls: list[ExpectedProgressUpdate] = []
        self.completion_calls: list[ExpectedTaskCompletion[WeaponryResult]] = []
        self.latest_calls: list[tuple[TaskId, TaskBusinessRef]] = []
        self.list_calls: list[tuple[str, int]] = []
        self.defer_calls: list[tuple[TaskId, str, str]] = []

    def create_if_allowed(
        self,
        command: TaskSubmissionCommand[WeaponrySubmission],
    ) -> TaskSubmissionResult[WeaponryInputSnapshot]:
        if not isinstance(command, TaskSubmissionCommand):
            raise TypeError("command 必须是 TaskSubmissionCommand")
        if not isinstance(command.submission, WeaponrySubmission):
            raise TypeError("Fake 只接受 WeaponrySubmission")
        self.recorder.record("task.create")
        with self._lock:
            self.submission_calls.append(command)
            self._raise_if_configured("create")
            if self.forced_create_result is not None:
                return self.forced_create_result  # type: ignore[return-value]
            if self.submission_outcome_sequence:
                outcome = self.submission_outcome_sequence.pop(0)
            elif command.business_ref in self.submission_outcomes:
                outcome = self.submission_outcomes[command.business_ref]
            else:
                # 未显式注入故障时，Fake 必须复现 Repository 的活动任务冲突，不能
                # 为同一业务键凭空创建两个 accepted owner。Callback Guard 的 sending/
                # unknown 分类仍通过 submission_outcomes 精确注入。
                latest_task_id = self.latest.get(command.business_ref)
                latest_execution = (
                    self.executions.get(latest_task_id)
                    if latest_task_id is not None
                    else None
                )
                outcome = (
                    TaskSubmissionOutcome.ACTIVE_CONFLICT
                    if latest_execution is not None
                    and latest_execution.execution_state in {"accepted", "running"}
                    else TaskSubmissionOutcome.ACCEPTED
                )
            if outcome is not TaskSubmissionOutcome.ACCEPTED:
                return TaskSubmissionResult(outcome)

            number = next(self._sequence)
            task_id = TaskId(f"weaponry-task-{number:04d}")
            accepted_at = f"2026-07-18T00:00:{number:02d}+08:00"
            snapshot = WeaponryInputSnapshot.from_submission(
                command.submission,
                task_id=task_id.value,
                accepted_at=accepted_at,
                schema_version=command.input_schema_version,
            )
            execution = TaskExecutionSnapshot(
                task_id=task_id,
                task_type=command.task_type,
                business_ref=command.business_ref,
                execution_state="accepted",
                public_status="1",
                progress=0.0,
                message="",
                input_snapshot=snapshot,
                accepted_at=accepted_at,
                trace_id=command.trace_id,
            )
            self.executions[task_id] = execution
            self.latest[command.business_ref] = task_id
            return TaskSubmissionResult(TaskSubmissionOutcome.ACCEPTED, execution)

    def get_execution(
        self,
        task_id: TaskId,
    ) -> TaskExecutionSnapshot[WeaponryInputSnapshot] | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        self.recorder.record("task.get", task_id=task_id.value)
        with self._lock:
            self.get_calls.append(task_id)
            self._raise_if_configured("get")
            if self.forced_get_result is not None:
                return self.forced_get_result  # type: ignore[return-value]
            return self.executions.get(task_id)

    def claim(self, task_id: TaskId) -> TaskClaimResult[WeaponryInputSnapshot]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        self.recorder.record("task.claim", task_id=task_id.value)
        with self._lock:
            self.claim_calls.append(task_id)
            self._raise_if_configured("claim")
            if self.forced_claim_result is not None:
                return self.forced_claim_result  # type: ignore[return-value]
            execution = self.executions.get(task_id)
            if execution is None:
                return TaskClaimResult(TaskClaimOutcome.MISSING)
            if task_id in self.claim_outcomes:
                outcome = self.claim_outcomes[task_id]
            elif execution.execution_state == "accepted":
                outcome = (
                    TaskClaimOutcome.CLAIMED
                    if self.latest.get(execution.business_ref) == task_id
                    else TaskClaimOutcome.STALE
                )
            elif execution.execution_state == "running":
                outcome = (
                    TaskClaimOutcome.ALREADY_RUNNING
                    if self.latest.get(execution.business_ref) == task_id
                    else TaskClaimOutcome.STALE
                )
            elif execution.execution_state in {"succeeded", "failed"}:
                outcome = TaskClaimOutcome.TERMINAL
            elif execution.execution_state == "stale":
                outcome = TaskClaimOutcome.STALE
            else:  # pragma: no cover - TaskExecutionSnapshot 已冻结允许状态
                raise AssertionError("Fake execution 存在未知状态")
            if outcome is TaskClaimOutcome.CLAIMED:
                execution = replace(execution, execution_state="running")
                self.executions[task_id] = execution
            elif outcome is TaskClaimOutcome.STALE:
                execution = replace(execution, execution_state="stale")
                self.executions[task_id] = execution
            return TaskClaimResult(outcome, execution)

    def update_progress_if_current(self, update: ExpectedProgressUpdate) -> bool:
        if not isinstance(update, ExpectedProgressUpdate):
            raise TypeError("update 必须是 ExpectedProgressUpdate")
        self.recorder.record(
            "task.progress",
            task_id=update.expected_task_id.value,
            call_id=f"{update.progress:.4f}",
        )
        with self._lock:
            self.progress_calls.append(update)
            self._raise_if_configured("progress")
            result = self.progress_results.pop(0) if self.progress_results else True
            if not isinstance(result, bool):
                return result  # type: ignore[return-value]
            execution = self.executions.get(update.expected_task_id)
            if not result:
                if execution is not None:
                    self.executions[update.expected_task_id] = replace(
                        execution,
                        execution_state="stale",
                    )
                return False
            if (
                execution is None
                or self.latest.get(update.business_ref) != update.expected_task_id
            ):
                return False
            self.executions[update.expected_task_id] = replace(
                execution,
                progress=update.progress,
                message=update.message,
                execution_state=update.execution_state,
                public_status=update.public_status,
            )
            return True

    def finish_if_current(
        self,
        completion: ExpectedTaskCompletion[WeaponryResult],
    ) -> bool:
        if not isinstance(completion, ExpectedTaskCompletion):
            raise TypeError("completion 必须是 ExpectedTaskCompletion")
        if not isinstance(completion.result, WeaponryResult):
            raise TypeError("Fake 只接受 WeaponryResult 终态")
        self.recorder.record(
            "task.finish",
            task_id=completion.expected_task_id.value,
            call_id=completion.public_status,
        )
        with self._lock:
            self.completion_calls.append(completion)
            self._raise_if_configured("finish")
            result = self.finish_results.pop(0) if self.finish_results else True
            if not isinstance(result, bool):
                return result  # type: ignore[return-value]
            execution = self.executions.get(completion.expected_task_id)
            if not result:
                if execution is not None:
                    self.executions[completion.expected_task_id] = replace(
                        execution,
                        execution_state="stale",
                    )
                return False
            if (
                execution is None
                or self.latest.get(completion.business_ref)
                != completion.expected_task_id
            ):
                return False
            self.executions[completion.expected_task_id] = replace(
                execution,
                progress=1.0,
                message=completion.message,
                execution_state=completion.execution_state,
                public_status=completion.public_status,
            )
            return True

    def is_latest(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        self.recorder.record("task.is_latest", task_id=task_id.value)
        with self._lock:
            self.latest_calls.append((task_id, business_ref))
            self._raise_if_configured("latest")
            if self.latest_results:
                return self.latest_results.pop(0)  # type: ignore[return-value]
            return self.latest.get(business_ref) == task_id

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type 必须是非空 str")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        self.recorder.record("task.list_accepted")
        with self._lock:
            self.list_calls.append((task_type, limit))
            return tuple(
                execution.task_id
                for execution in self.executions.values()
                if execution.task_type == task_type
                and execution.execution_state == "accepted"
            )[:limit]

    def defer_accepted(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(retry_at, str) or not retry_at.strip():
            raise ValueError("retry_at 必须是非空 str")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空 str")
        self.recorder.record("task.defer_accepted", task_id=task_id.value)
        with self._lock:
            self.defer_calls.append((task_id, retry_at, reason))
            execution = self.executions.get(task_id)
            return execution is not None and execution.execution_state == "accepted"

    def inspect_queue(
        self,
        task_type: str,
        *,
        running_sample_limit: int,
    ) -> TaskQueueSnapshot:
        if isinstance(running_sample_limit, bool) or not isinstance(
            running_sample_limit,
            int,
        ) or running_sample_limit < 1:
            raise ValueError("running_sample_limit 必须是正整数")
        self.recorder.record("task.inspect_queue")
        with self._lock:
            matching = tuple(
                execution
                for execution in self.executions.values()
                if execution.task_type == task_type
            )
            accepted = tuple(
                item for item in matching if item.execution_state == "accepted"
            )
            running = tuple(
                item for item in matching if item.execution_state == "running"
            )
            return TaskQueueSnapshot(
                task_type=task_type,
                accepted_count=len(accepted),
                running_count=len(running),
                oldest_accepted_at=(
                    min(item.accepted_at for item in accepted) if accepted else None
                ),
                oldest_running_at=(
                    min(item.accepted_at for item in running) if running else None
                ),
                running_task_ids=tuple(
                    item.task_id for item in running[:running_sample_limit]
                ),
            )

    def _raise_if_configured(self, operation: str) -> None:
        error = self.errors.get(operation)
        if error is not None:
            raise error


class FakeWeaponryProgressPublisherPort:
    """仅记录无敏感 Progress 投影的通知 Fake。"""

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.publications: list[ProgressPublication] = []
        self.error: BaseException | None = None
        self._lock = RLock()

    def publish(self, publication: ProgressPublication) -> None:
        if not isinstance(publication, ProgressPublication):
            raise TypeError("publication 必须是 ProgressPublication")
        self.recorder.record(
            "progress.publish",
            task_id=publication.expected_task_id.value,
            call_id=f"{publication.progress:.4f}",
        )
        with self._lock:
            if self.error is not None:
                raise self.error
            self.publications.append(publication)


class FakeWeaponryCallbackPort:
    """线程安全 latest/Guard Fake，精确区分 failed 与 outcome unknown。"""

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.delivery_results: dict[TaskId, WeaponryCallbackDeliveryResult] = {}
        self.acquire_errors: dict[TaskId, BaseException] = {}
        self.delivery_errors: dict[TaskId, BaseException] = {}
        self.recovery_candidates: dict[int, WeaponryCallbackRecoveryCandidate] = {}
        self._lock = RLock()
        self._latest: dict[int, TaskId] = {}
        self._active: dict[int, WeaponryCallbackGuardLease] = {}
        self._lease_reasons: dict[str, WeaponryCallbackAcquireReason] = {}
        self._fencing: dict[int, int] = {}
        self._completed: set[int] = set()
        self._failed: set[int] = set()
        self._unknown: set[int] = set()

    def set_latest(self, task_id: TaskId, architecture_id: int) -> None:
        """测试装配 latest owner；新 owner 不会自动解除旧 unknown。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or architecture_id < 1
        ):
            raise ValueError("architecture_id 必须是正整数")
        with self._lock:
            self._latest[architecture_id] = task_id

    def acquire(
        self,
        command: AcquireWeaponryCallback,
    ) -> WeaponryCallbackAcquireResult:
        if not isinstance(command, AcquireWeaponryCallback):
            raise TypeError("command 必须是 AcquireWeaponryCallback")
        self.recorder.record("callback.acquire", task_id=command.task_id.value)
        with self._lock:
            error = self.acquire_errors.get(command.task_id)
            if error is not None:
                raise error
            architecture_id = command.architecture_id
            if self._latest.get(architecture_id) != command.task_id:
                return WeaponryCallbackAcquireResult(
                    WeaponryCallbackAcquireOutcome.STALE
                )
            if architecture_id in self._unknown:
                return WeaponryCallbackAcquireResult(
                    WeaponryCallbackAcquireOutcome.OUTCOME_UNKNOWN
                )
            if architecture_id in self._completed:
                return WeaponryCallbackAcquireResult(
                    WeaponryCallbackAcquireOutcome.ALREADY_COMPLETED
                )
            if architecture_id in self._failed:
                if (
                    command.reason
                    is not WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
                ):
                    return WeaponryCallbackAcquireResult(
                        WeaponryCallbackAcquireOutcome.ALREADY_COMPLETED
                    )
                self._failed.remove(architecture_id)
            if architecture_id in self._active:
                return WeaponryCallbackAcquireResult(
                    WeaponryCallbackAcquireOutcome.BUSY
                )
            fencing = self._fencing.get(architecture_id, 0) + 1
            self._fencing[architecture_id] = fencing
            lease = WeaponryCallbackGuardLease(
                task_id=command.task_id,
                architecture_id=architecture_id,
                token=f"fake-callback-lease:{architecture_id}:{fencing}",
                fencing_token=fencing,
                deadline_at="2099-01-01T00:00:00+00:00",
            )
            self._active[architecture_id] = lease
            self._lease_reasons[lease.token] = command.reason
            return WeaponryCallbackAcquireResult(
                WeaponryCallbackAcquireOutcome.ACQUIRED,
                lease,
            )

    def wait_until_released(
        self,
        command: WaitForWeaponryCallbackRelease,
    ) -> WeaponryCallbackWaitResult:
        if not isinstance(command, WaitForWeaponryCallbackRelease):
            raise TypeError("command 必须是 WaitForWeaponryCallbackRelease")
        self.recorder.record("callback.wait")
        with self._lock:
            if command.architecture_id in self._unknown:
                outcome = WeaponryCallbackWaitOutcome.OUTCOME_UNKNOWN
            elif command.architecture_id in self._active:
                outcome = WeaponryCallbackWaitOutcome.TIMED_OUT
            else:
                outcome = WeaponryCallbackWaitOutcome.RELEASED
            return WeaponryCallbackWaitResult(outcome)

    def deliver(
        self,
        command: DeliverWeaponryCallback,
    ) -> WeaponryCallbackDeliveryResult:
        if not isinstance(command, DeliverWeaponryCallback):
            raise TypeError("command 必须是 DeliverWeaponryCallback")
        lease = command.lease
        self.recorder.record("callback.deliver", task_id=lease.task_id.value)
        with self._lock:
            if self._active.get(lease.architecture_id) != lease:
                raise WeaponryPortStateError(
                    "callback_lease_not_active",
                    "回调发送租约不存在或已经失效",
                )
            if self._latest.get(lease.architecture_id) != lease.task_id:
                return WeaponryCallbackDeliveryResult(
                    WeaponryCallbackDeliveryOutcome.STALE
                )
            error = self.delivery_errors.get(lease.task_id)
            if error is not None:
                raise error
            result = self.delivery_results.get(lease.task_id)
            if result is None:
                raise AssertionError(
                    "FakeWeaponryCallbackPort 收到未配置投递: "
                    f"task_id={lease.task_id.value}"
                )
            return result

    def complete(
        self,
        lease: WeaponryCallbackGuardLease,
        result: WeaponryCallbackDeliveryResult,
        payload: WeaponryCallbackPayload,
    ) -> bool:
        if not isinstance(lease, WeaponryCallbackGuardLease):
            raise TypeError("lease 必须是 WeaponryCallbackGuardLease")
        if not isinstance(result, WeaponryCallbackDeliveryResult):
            raise TypeError("result 必须是 WeaponryCallbackDeliveryResult")
        if not isinstance(payload, WeaponryCallbackPayload):
            raise TypeError("payload 必须是 WeaponryCallbackPayload")
        if payload.architecture_id != lease.architecture_id:
            raise ValueError("payload 与 lease architecture_id 不一致")
        self.recorder.record("callback.complete", task_id=lease.task_id.value)
        with self._lock:
            if (
                self._active.get(lease.architecture_id) != lease
                or self._latest.get(lease.architecture_id) != lease.task_id
            ):
                return False
            self._active.pop(lease.architecture_id, None)
            self._lease_reasons.pop(lease.token, None)
            if result.outcome is WeaponryCallbackDeliveryOutcome.SUCCESS:
                self._completed.add(lease.architecture_id)
            elif result.outcome is WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN:
                self._unknown.add(lease.architecture_id)
            elif result.outcome in {
                WeaponryCallbackDeliveryOutcome.REJECTED,
                WeaponryCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
            }:
                self._failed.add(lease.architecture_id)
            return True

    def freeze_expired(self, *, limit: int) -> WeaponryCallbackGuardSweepResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        self.recorder.record("callback.freeze_expired")
        with self._lock:
            leases = tuple(self._active.values())[:limit]
            for lease in leases:
                self._active.pop(lease.architecture_id, None)
                self._lease_reasons.pop(lease.token, None)
                self._unknown.add(lease.architecture_id)
            return WeaponryCallbackGuardSweepResult(
                scanned_count=len(leases),
                frozen_count=len(leases),
            )

    def release_unknown(
        self,
        command: ReleaseUnknownWeaponryCallback,
    ) -> WeaponryCallbackReleaseResult:
        if not isinstance(command, ReleaseUnknownWeaponryCallback):
            raise TypeError("command 必须是 ReleaseUnknownWeaponryCallback")
        self.recorder.record("callback.release_unknown")
        with self._lock:
            if command.architecture_id not in self._unknown:
                return WeaponryCallbackReleaseResult(
                    WeaponryCallbackReleaseOutcome.NOT_FROZEN
                )
            self._unknown.remove(command.architecture_id)
            self._failed.add(command.architecture_id)
            return WeaponryCallbackReleaseResult(
                WeaponryCallbackReleaseOutcome.RELEASED
            )

    def load_recoverable(
        self,
        architecture_id: int,
    ) -> WeaponryCallbackRecoveryCandidate | None:
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or architecture_id < 1
        ):
            raise ValueError("architecture_id 必须是正整数")
        self.recorder.record("callback.load_recoverable")
        with self._lock:
            candidate = self.recovery_candidates.get(architecture_id)
            if candidate is None:
                return None
            if self._latest.get(architecture_id) != candidate.task_id:
                raise AssertionError("Fake 回调恢复候选不是 latest owner")
            return candidate


class FakeWeaponryResourceStorePort:
    """支持 CAS、幂等登记和 shared 禁止清理的资源事实替身。"""

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.failures: dict[str, BaseException] = {}
        self._lock = RLock()
        self._records: dict[TaskId, WeaponryResourceRecord] = {}

    @property
    def records(self) -> tuple[WeaponryResourceRecord, ...]:
        with self._lock:
            return tuple(self._records.values())

    def _raise(self, operation: str) -> None:
        error = self.failures.get(operation)
        if error is not None:
            raise error

    def create(self, record: WeaponryResourceRecord) -> WeaponryResourceRecord:
        if not isinstance(record, WeaponryResourceRecord):
            raise TypeError("record 必须是 WeaponryResourceRecord")
        self.recorder.record("resource.create", task_id=record.task_id.value)
        with self._lock:
            self._raise("create")
            existing = self._records.get(record.task_id)
            if existing is not None:
                if existing == record:
                    return existing
                raise WeaponryPortStateError(
                    "resource_record_exists",
                    "task_id 已存在不同资源记录",
                )
            if record.version != 0 or record.state is not WeaponryResourceRecordState.TRACKING:
                raise ValueError("新资源记录必须从 tracking/version=0 开始")
            self._records[record.task_id] = record
            return record

    def get(self, task_id: TaskId) -> WeaponryResourceRecord | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        self.recorder.record("resource.get", task_id=task_id.value)
        with self._lock:
            return self._records.get(task_id)

    def register(
        self,
        command: RegisterWeaponryResource,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, RegisterWeaponryResource):
            raise TypeError("command 必须是 RegisterWeaponryResource")
        with self._lock:
            self._raise("register")
            record = self._require(command.task_id)
            if command.resource.call_id and not command.resource.call_id.startswith(
                f"weaponry:{command.task_id.value}:"
            ):
                raise WeaponryPortStateError(
                    "resource_call_identity_mismatch",
                    "资源 call_id 不属于当前 task_id",
                )
            by_id = {item.resource_id: item for item in record.resources}
            by_key = {item.idempotency_key: item for item in record.resources}
            existing = by_id.get(command.resource.resource_id) or by_key.get(
                command.resource.idempotency_key
            )
            if existing is not None:
                if existing == command.resource:
                    self.recorder.record(
                        "resource.register",
                        task_id=command.task_id.value,
                        call_id=command.resource.kind.value,
                    )
                    return record
                raise WeaponryPortStateError(
                    "resource_registration_conflict",
                    "资源 ID 或幂等键已绑定不同事实",
                )
            self._require_version(record, command.expected_version)
            if record.state is not WeaponryResourceRecordState.TRACKING:
                raise WeaponryPortStateError(
                    "resource_record_not_tracking",
                    "清理开始后不得登记新资源",
                )
            updated = replace(
                record,
                resources=record.resources + (command.resource,),
                version=record.version + 1,
            )
            self._records[record.task_id] = updated
            self.recorder.record(
                "resource.register",
                task_id=command.task_id.value,
                call_id=command.resource.kind.value,
            )
            return updated

    def prepare_cleanup(
        self,
        command: PrepareWeaponryResourceCleanup,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, PrepareWeaponryResourceCleanup):
            raise TypeError("command 必须是 PrepareWeaponryResourceCleanup")
        self.recorder.record("resource.prepare_cleanup", task_id=command.task_id.value)
        with self._lock:
            self._raise("prepare_cleanup")
            record = self._require(command.task_id)
            if record.state in {
                WeaponryResourceRecordState.CLEANUP_PENDING,
                WeaponryResourceRecordState.CLEANED,
            }:
                return record
            if record.state is WeaponryResourceRecordState.QUARANTINED:
                raise WeaponryPortStateError(
                    "resource_record_quarantined",
                    "隔离资源不得自动重新进入清理",
                )
            self._require_version(record, command.expected_version)
            resources = tuple(
                replace(item, state=WeaponryTrackedResourceState.CLEANUP_PENDING)
                if item.ownership is WeaponryResourceOwnership.OWNED
                and item.state is WeaponryTrackedResourceState.ACTIVE
                else item
                for item in record.resources
            )
            all_owned_cleaned = all(
                item.ownership is WeaponryResourceOwnership.SHARED
                or item.state is WeaponryTrackedResourceState.CLEANED
                for item in resources
            )
            updated = replace(
                record,
                resources=resources,
                state=(
                    WeaponryResourceRecordState.CLEANED
                    if all_owned_cleaned
                    else WeaponryResourceRecordState.CLEANUP_PENDING
                ),
                version=record.version + 1,
            )
            self._records[record.task_id] = updated
            return updated

    def acquire_cleanup(
        self,
        command: AcquireWeaponryCleanupLease,
    ) -> WeaponryCleanupLeaseAcquireResult:
        if not isinstance(command, AcquireWeaponryCleanupLease):
            raise TypeError("command 必须是 AcquireWeaponryCleanupLease")
        self.recorder.record("resource.acquire_cleanup", task_id=command.task_id.value)
        with self._lock:
            self._raise("acquire_cleanup")
            record = self._require(command.task_id)
            if record.state is not WeaponryResourceRecordState.CLEANUP_PENDING:
                return WeaponryCleanupLeaseAcquireResult(
                    WeaponryCleanupLeaseAcquireOutcome.NOT_READY
                )
            if record.cleanup_lease is not None:
                return WeaponryCleanupLeaseAcquireResult(
                    WeaponryCleanupLeaseAcquireOutcome.BUSY
                )
            self._require_version(record, command.expected_version)
            fencing = record.cleanup_fencing_token + 1
            lease = WeaponryCleanupLease(
                task_id=record.task_id,
                token=f"fake-resource-lease:{record.task_id.value}:{fencing}",
                fencing_token=fencing,
                deadline_at="2099-01-01T00:00:00+00:00",
            )
            updated = replace(
                record,
                cleanup_lease=lease,
                cleanup_fencing_token=fencing,
                version=record.version + 1,
            )
            self._records[record.task_id] = updated
            return WeaponryCleanupLeaseAcquireResult(
                WeaponryCleanupLeaseAcquireOutcome.ACQUIRED,
                lease,
            )

    def complete_cleanup(
        self,
        command: CompleteWeaponryResourceCleanup,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, CompleteWeaponryResourceCleanup):
            raise TypeError("command 必须是 CompleteWeaponryResourceCleanup")
        self.recorder.record("resource.complete_cleanup", task_id=command.task_id.value)
        with self._lock:
            self._raise("complete_cleanup")
            record = self._require(command.task_id)
            resource = next(
                (
                    item
                    for item in record.resources
                    if item.resource_id == command.resource_id
                ),
                None,
            )
            if resource is None:
                raise WeaponryPortStateError(
                    "resource_not_found",
                    "待清理资源不存在",
                )
            if resource.ownership is WeaponryResourceOwnership.SHARED:
                raise WeaponryPortStateError(
                    "shared_resource_cleanup_forbidden",
                    "shared 资源禁止由任务清理",
                )
            if (
                resource.state is WeaponryTrackedResourceState.CLEANED
                and command.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
            ):
                return record
            self._require_version(record, command.expected_version)
            if record.state is not WeaponryResourceRecordState.CLEANUP_PENDING:
                raise WeaponryPortStateError(
                    "resource_cleanup_not_prepared",
                    "资源记录尚未进入 cleanup_pending",
                )
            if record.cleanup_lease != command.lease:
                raise WeaponryPortStateError(
                    "resource_cleanup_lease_mismatch",
                    "资源清理租约不存在或已经失权",
                )
            if resource.state is WeaponryTrackedResourceState.CLEANUP_UNKNOWN:
                raise WeaponryPortStateError(
                    "resource_cleanup_outcome_unknown",
                    "结果未知资源必须先对账或隔离，禁止直接重试",
                )
            target_state = {
                WeaponryResourceCleanupOutcome.SUCCEEDED: WeaponryTrackedResourceState.CLEANED,
                WeaponryResourceCleanupOutcome.FAILED: (
                    WeaponryTrackedResourceState.CLEANUP_PENDING
                ),
                WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN: (
                    WeaponryTrackedResourceState.CLEANUP_UNKNOWN
                ),
            }[command.outcome]
            resources = tuple(
                replace(item, state=target_state)
                if item.resource_id == command.resource_id
                else item
                for item in record.resources
            )
            all_owned_cleaned = all(
                item.ownership is WeaponryResourceOwnership.SHARED
                or item.state is WeaponryTrackedResourceState.CLEANED
                for item in resources
            )
            updated = replace(
                record,
                resources=resources,
                state=(
                    WeaponryResourceRecordState.CLEANED
                    if all_owned_cleaned
                    else WeaponryResourceRecordState.CLEANUP_PENDING
                ),
                cleanup_lease=(None if all_owned_cleaned else record.cleanup_lease),
                retry_count=(
                    record.retry_count
                    if command.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
                    else record.retry_count + 1
                ),
                last_error_code=(
                    ""
                    if command.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
                    else command.error_code
                ),
                last_error_message=(
                    ""
                    if command.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED
                    else "清理未成功，等待恢复处理"
                ),
                version=record.version + 1,
            )
            self._records[record.task_id] = updated
            return updated

    def release_cleanup(
        self,
        command: ReleaseWeaponryCleanupLease,
    ) -> IdempotentOperationResult:
        if not isinstance(command, ReleaseWeaponryCleanupLease):
            raise TypeError("command 必须是 ReleaseWeaponryCleanupLease")
        task_id = command.lease.task_id
        self.recorder.record("resource.release_cleanup", task_id=task_id.value)
        with self._lock:
            self._raise("release_cleanup")
            record = self._require(task_id)
            if record.cleanup_lease is None:
                return IdempotentOperationResult(success=True, already_applied=True)
            if record.cleanup_lease != command.lease:
                raise WeaponryPortStateError(
                    "resource_cleanup_lease_mismatch",
                    "不能释放其他 Worker 的资源清理租约",
                )
            self._require_version(record, command.expected_version)
            updated = replace(
                record,
                cleanup_lease=None,
                version=record.version + 1,
            )
            self._records[record.task_id] = updated
            return IdempotentOperationResult(success=True)

    def quarantine(
        self,
        command: QuarantineWeaponryResources,
    ) -> WeaponryResourceRecord:
        if not isinstance(command, QuarantineWeaponryResources):
            raise TypeError("command 必须是 QuarantineWeaponryResources")
        self.recorder.record("resource.quarantine", task_id=command.task_id.value)
        with self._lock:
            self._raise("quarantine")
            record = self._require(command.task_id)
            if record.state is WeaponryResourceRecordState.QUARANTINED:
                if (
                    record.last_error_code == command.error_code
                    and record.last_error_message == command.reason
                ):
                    return record
                raise WeaponryPortStateError(
                    "resource_quarantine_conflict",
                    "资源已按不同原因隔离",
                )
            self._require_version(record, command.expected_version)
            updated = replace(
                record,
                state=WeaponryResourceRecordState.QUARANTINED,
                cleanup_lease=None,
                last_error_code=command.error_code,
                last_error_message=command.reason,
                version=record.version + 1,
            )
            self._records[record.task_id] = updated
            return updated

    def list_recoverable(self, *, limit: int) -> tuple[TaskId, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        self.recorder.record("resource.list_recoverable")
        with self._lock:
            candidates = tuple(
                record.task_id
                for record in self._records.values()
                if record.state is WeaponryResourceRecordState.CLEANUP_PENDING
            )
            return tuple(sorted(candidates, key=lambda item: item.value)[:limit])

    def _require(self, task_id: TaskId) -> WeaponryResourceRecord:
        record = self._records.get(task_id)
        if record is None:
            raise WeaponryPortStateError(
                "resource_record_not_found",
                "资源记录不存在",
            )
        return record

    @staticmethod
    def _require_version(record: WeaponryResourceRecord, expected: int) -> None:
        if record.version != expected:
            raise WeaponryPortStateError(
                "resource_version_conflict",
                "资源记录版本不一致",
            )


class FakeWeaponryExternalResourceCleanupPort:
    """只执行显式配置结果的线程安全外部清理替身。"""

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.results: dict[str, WeaponryExternalResourceCleanupResult] = {}
        self.errors: dict[str, BaseException] = {}
        self.calls: list[CleanupWeaponryExternalResource] = []
        self._lock = RLock()

    def cleanup(
        self,
        command: CleanupWeaponryExternalResource,
    ) -> WeaponryExternalResourceCleanupResult:
        if not isinstance(command, CleanupWeaponryExternalResource):
            raise TypeError("command 必须是 CleanupWeaponryExternalResource")
        resource_id = command.resource.resource_id
        self.recorder.record(
            "resource.external_cleanup",
            task_id=command.task_id.value,
            call_id=resource_id,
        )
        with self._lock:
            self.calls.append(command)
            error = self.errors.get(resource_id)
            if error is not None:
                raise error
            result = self.results.get(resource_id)
            if result is None:
                raise AssertionError(
                    "FakeWeaponryExternalResourceCleanupPort 收到未配置调用: "
                    f"resource_id={resource_id}"
                )
            return result


class FakeWeaponryDispatcherPort:
    """可观测、可故障注入且具有显式生命周期的调度替身。"""

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.dispatch_errors: dict[TaskId, BaseException] = {}
        self.dispatched: list[TaskId] = []
        self.started = False
        self.closed = False
        self.stop_result = True
        self._lock = RLock()

    def dispatch(self, task_id: TaskId) -> None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        self.recorder.record("dispatcher.dispatch", task_id=task_id.value)
        with self._lock:
            if self.closed:
                raise WeaponryPortStateError(
                    "dispatcher_closed",
                    "Dispatcher 已关闭",
                )
            error = self.dispatch_errors.get(task_id)
            if error is not None:
                raise error
            self.dispatched.append(task_id)

    def start(self) -> None:
        self.recorder.record("dispatcher.start")
        with self._lock:
            if self.closed:
                raise WeaponryPortStateError(
                    "dispatcher_closed",
                    "已关闭 Dispatcher 不能重新启动",
                )
            self.started = True

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds,
                (int, float),
            ):
                raise TypeError("timeout_seconds 必须是数字或 None")
            if float(timeout_seconds) <= 0.0:
                raise ValueError("timeout_seconds 必须是正数")
        self.recorder.record("dispatcher.stop")
        with self._lock:
            if self.stop_result:
                self.started = False
            return self.stop_result

    def close(self) -> None:
        self.recorder.record("dispatcher.close")
        with self._lock:
            self.started = False
            self.closed = True


__all__ = [
    "FakeWeaponryCallbackPort",
    "FakeWeaponryDispatcherPort",
    "FakeWeaponryExternalResourceCleanupPort",
    "FakeWeaponryProgressPublisherPort",
    "FakeWeaponryResourceStorePort",
    "FakeWeaponryTaskCommandPort",
]
