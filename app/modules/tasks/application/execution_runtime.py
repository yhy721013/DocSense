"""阶段 2 v2 单 Task Execution Runtime。

Runtime 只编排 claim、start、heartbeat 和 v2 Workflow，不理解 Report、Weaponry、
Analysis 的输入、步骤、Progress、终态或 Callback。所有控制写均使用阶段 2-2 的窄
Execution UoW，且不会回退到旧 Task Service。
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from app.modules.tasks.domain import (
    TaskExecutionAuthority,
    TaskId,
    TaskLeaseRuntimeSettings,
    TaskOwnerIdentity,
    add_persisted_utc_seconds,
)
from app.modules.tasks.ports import (
    ClockAnomalyError,
    ClockPort,
    LeaseHeartbeatSupervisorPort,
    LeaseSupervisorOutcome,
    TaskClaimRequest,
    TaskExecutionMutationOutcome,
    TaskExecutionRuntimeOutcome,
    TaskExecutionRuntimeResult,
    TaskExecutionStopRequested,
    TaskExecutionUnitOfWorkFactory,
    TaskLeaseTokenFactoryPort,
    TaskWorkflowRunnerPort,
)

from .authority_session import TaskExecutionAuthoritySession


logger = logging.getLogger(__name__)


class TaskExecutionRuntime:
    """把一次 Task 从 accepted 领取到 v2 Workflow，并监督其 Authority。"""

    def __init__(
        self,
        *,
        task_type: str,
        owner: TaskOwnerIdentity,
        clock: ClockPort,
        execution_uow_factory: TaskExecutionUnitOfWorkFactory,
        lease_token_factory: TaskLeaseTokenFactoryPort,
        heartbeat_supervisor_factory: Callable[[], LeaseHeartbeatSupervisorPort],
        workflow_runner: TaskWorkflowRunnerPort,
        lease_settings: TaskLeaseRuntimeSettings,
    ) -> None:
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type 必须是非空 str")
        if not isinstance(owner, TaskOwnerIdentity):
            raise TypeError("owner 必须是 TaskOwnerIdentity")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not callable(execution_uow_factory):
            raise TypeError("execution_uow_factory 必须可调用")
        if not isinstance(lease_token_factory, TaskLeaseTokenFactoryPort):
            raise TypeError("lease_token_factory 必须实现 TaskLeaseTokenFactoryPort")
        if not callable(heartbeat_supervisor_factory):
            raise TypeError("heartbeat_supervisor_factory 必须可调用")
        if not isinstance(workflow_runner, TaskWorkflowRunnerPort):
            raise TypeError("workflow_runner 必须实现 TaskWorkflowRunnerPort")
        if not isinstance(lease_settings, TaskLeaseRuntimeSettings):
            raise TypeError("lease_settings 必须是 TaskLeaseRuntimeSettings")

        self._task_type = task_type.strip()
        self._owner = owner
        self._clock = clock
        self._execution_uow_factory = execution_uow_factory
        self._lease_token_factory = lease_token_factory
        self._heartbeat_supervisor_factory = heartbeat_supervisor_factory
        self._workflow_runner = workflow_runner
        self._lease_settings = lease_settings

    def run(self, task_id: TaskId) -> TaskExecutionRuntimeResult:
        """执行一次可领取 Task；有限竞争结果不会启动业务 Workflow。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        try:
            authority = self._claim(task_id)
        except ClockAnomalyError:
            logger.critical(
                "Task Runtime 因时钟异常拒绝 claim: task_id=%s task_type=%s "
                "reason_code=task_clock_unsafe",
                task_id,
                self._task_type,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.CLOCK_UNSAFE,
            )
        except Exception as exc:
            logger.error(
                "Task Runtime claim 基础设施失败: task_id=%s task_type=%s "
                "reason_code=task_claim_infrastructure_error error_type=%s",
                task_id,
                self._task_type,
                type(exc).__name__,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR,
            )

        if isinstance(authority, TaskExecutionMutationOutcome):
            logger.debug(
                "Task Runtime claim 未取得执行权: task_id=%s task_type=%s outcome=%s",
                task_id,
                self._task_type,
                authority.value,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.CLAIM_REJECTED,
                authority,
            )

        try:
            start_outcome = self._start(authority)
        except ClockAnomalyError:
            logger.critical(
                "Task Runtime 因时钟异常拒绝 start: task_id=%s task_type=%s "
                "attempt_no=%d fencing=%d reason_code=task_clock_unsafe",
                task_id,
                self._task_type,
                authority.attempt_no,
                authority.fencing_token,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.CLOCK_UNSAFE,
            )
        except Exception as exc:
            logger.error(
                "Task Runtime start 基础设施失败: task_id=%s task_type=%s "
                "attempt_no=%d fencing=%d reason_code=task_start_infrastructure_error "
                "error_type=%s",
                task_id,
                self._task_type,
                authority.attempt_no,
                authority.fencing_token,
                type(exc).__name__,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR,
            )
        if start_outcome is not TaskExecutionMutationOutcome.APPLIED:
            logger.warning(
                "Task Runtime start 条件写被拒绝: task_id=%s task_type=%s "
                "attempt_no=%d fencing=%d outcome=%s",
                task_id,
                self._task_type,
                authority.attempt_no,
                authority.fencing_token,
                start_outcome.value,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.START_REJECTED,
                start_outcome,
            )

        session = TaskExecutionAuthoritySession(authority)
        try:
            supervisor = self._heartbeat_supervisor_factory()
            if not isinstance(supervisor, LeaseHeartbeatSupervisorPort):
                raise TypeError("heartbeat_supervisor_factory 返回值未实现 Port")
            supervisor.start(session)
        except Exception as exc:
            logger.error(
                "Task heartbeat 启动失败: task_id=%s task_type=%s attempt_no=%d "
                "fencing=%d reason_code=heartbeat_start_error error_type=%s",
                task_id,
                self._task_type,
                authority.attempt_no,
                authority.fencing_token,
                type(exc).__name__,
            )
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR,
            )

        workflow_failed = False
        supervisor_stop_failed = False
        try:
            self._workflow_runner.run(session)
        except TaskExecutionStopRequested:
            # 失权是预期协作停止路径；最终分类以 supervisor/session 的稳定结果为准。
            pass
        except Exception as exc:
            workflow_failed = True
            logger.error(
                "Task Runtime Workflow 异常退出: task_id=%s task_type=%s "
                "attempt_no=%d fencing=%d reason_code=task_workflow_error error_type=%s",
                task_id,
                self._task_type,
                authority.attempt_no,
                authority.fencing_token,
                type(exc).__name__,
            )
        finally:
            try:
                supervisor_result = supervisor.stop()
            except Exception as exc:
                supervisor_stop_failed = True
                supervisor_result = None
                logger.error(
                    "Task heartbeat 停止失败: task_id=%s task_type=%s "
                    "attempt_no=%d fencing=%d reason_code=heartbeat_stop_error "
                    "error_type=%s",
                    task_id,
                    self._task_type,
                    authority.attempt_no,
                    authority.fencing_token,
                    type(exc).__name__,
                )

        if supervisor_stop_failed:
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR,
            )

        stop_result = session.stop_result() or supervisor_result
        if stop_result is not None and stop_result.outcome is not LeaseSupervisorOutcome.STOPPED:
            mapped = {
                LeaseSupervisorOutcome.AUTHORITY_LOST: TaskExecutionRuntimeOutcome.AUTHORITY_LOST,
                LeaseSupervisorOutcome.CLOCK_UNSAFE: TaskExecutionRuntimeOutcome.CLOCK_UNSAFE,
                LeaseSupervisorOutcome.INFRASTRUCTURE_ERROR: (
                    TaskExecutionRuntimeOutcome.INFRASTRUCTURE_ERROR
                ),
            }[stop_result.outcome]
            return TaskExecutionRuntimeResult(
                task_id,
                mapped,
                stop_result.last_mutation_outcome,
            )
        if workflow_failed:
            return TaskExecutionRuntimeResult(
                task_id,
                TaskExecutionRuntimeOutcome.WORKFLOW_ERROR,
            )
        return TaskExecutionRuntimeResult(
            task_id,
            TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED,
        )

    def _claim(
        self,
        task_id: TaskId,
    ) -> TaskExecutionAuthority | TaskExecutionMutationOutcome:
        claimed_at = self._clock.now_utc()
        request = TaskClaimRequest(
            task_id=task_id,
            task_type=self._task_type,
            owner=self._owner,
            lease_token=self._lease_token_factory.new_token(),
            claimed_at=claimed_at,
            lease_expires_at=add_persisted_utc_seconds(
                claimed_at,
                seconds=self._lease_settings.lease_duration_seconds,
            ),
        )
        with self._execution_uow_factory() as unit_of_work:
            result = unit_of_work.execution.claim(request)
            if result.outcome is not TaskExecutionMutationOutcome.APPLIED:
                return result.outcome
            assert result.attempt is not None
            authority = result.attempt.authority
            unit_of_work.commit()
            logger.info(
                "Task Runtime 已提交 claim: task_id=%s task_type=%s "
                "attempt_no=%d fencing=%d",
                task_id,
                self._task_type,
                authority.attempt_no,
                authority.fencing_token,
            )
            return authority

    def _start(
        self,
        authority: TaskExecutionAuthority,
    ) -> TaskExecutionMutationOutcome:
        started_at = self._clock.now_utc()
        with self._execution_uow_factory() as unit_of_work:
            outcome = unit_of_work.execution.start(authority, started_at=started_at)
            if outcome is TaskExecutionMutationOutcome.APPLIED:
                unit_of_work.commit()
            return outcome


__all__ = ["TaskExecutionRuntime"]
