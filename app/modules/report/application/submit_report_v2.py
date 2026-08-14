"""Report Input v2 到统一 Task Admission UoW 的生产受理用例。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.modules.report.domain import (
    REPORT_INPUT_SCHEMA_VERSION_V2,
    ReportInputSnapshot,
    ReportPortContractError,
    ReportSubmission,
    ReportTaskConflictError,
)
from app.modules.report.ports import ReportTaskDispatcherPort
from app.modules.tasks.domain import ProgressKey, TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ClockPort,
    EncodedTaskSubmission,
    ProgressPublication,
    ProgressPublisherPort,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskAdmissionUnitOfWorkFactory,
    TaskSubmissionCommand,
)

from .submit_report import REPORT_TASK_TYPE, SubmitReportResult


logger = logging.getLogger(__name__)


@runtime_checkable
class _ReportAdmissionCodec(Protocol):
    """受理用例所需的最小编码结构契约。

    协议留在 Application 内部，避免用例识别 Adapter 具体类型，也避免 Report Port
    为描述统一 Task Admission DTO 而反向依赖另一个模块的 Port 层。
    """

    @property
    def write_schema_version(self) -> int: ...

    def encode_submission(
        self,
        command: TaskSubmissionCommand[ReportSubmission],
        *,
        task_id: TaskId,
        accepted_at: str,
    ) -> EncodedTaskSubmission[ReportInputSnapshot]: ...


def _new_task_id() -> TaskId:
    return TaskId(str(uuid4()))


class SubmitReportV2Task:
    """一次提交 v2 execution/latest/event，再发送可丢的进程内唤醒。

    Admission UoW 是唯一控制面写入口。Progress 与 Dispatcher 只在事务成功后通知；
    任一通知失败都不撤销 accepted 事实，周期扫描仍可恢复执行。
    """

    def __init__(
        self,
        *,
        admission_uow_factory: TaskAdmissionUnitOfWorkFactory,
        codec: _ReportAdmissionCodec,
        clock: ClockPort,
        progress_publisher: ProgressPublisherPort,
        dispatcher: ReportTaskDispatcherPort,
        task_id_factory: Callable[[], TaskId] = _new_task_id,
    ) -> None:
        if not callable(admission_uow_factory):
            raise TypeError("admission_uow_factory 必须可调用")
        if not isinstance(codec, _ReportAdmissionCodec):
            raise TypeError("codec 必须实现 Report 受理编码结构契约")
        if codec.write_schema_version != REPORT_INPUT_SCHEMA_VERSION_V2:
            raise ValueError("Report v2 受理必须装配 Input v2 Codec")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not isinstance(progress_publisher, ProgressPublisherPort):
            raise TypeError("progress_publisher 必须实现 ProgressPublisherPort")
        if not isinstance(dispatcher, ReportTaskDispatcherPort):
            raise TypeError("dispatcher 必须实现 ReportTaskDispatcherPort")
        if not callable(task_id_factory):
            raise TypeError("task_id_factory 必须可调用")
        self._admission_uow_factory = admission_uow_factory
        self._codec = codec
        self._clock = clock
        self._progress = progress_publisher
        self._dispatcher = dispatcher
        self._task_id_factory = task_id_factory

    @property
    def dispatcher(self) -> ReportTaskDispatcherPort:
        return self._dispatcher

    def execute(self, submission: ReportSubmission) -> SubmitReportResult:
        if not isinstance(submission, ReportSubmission):
            raise TypeError("submission 必须是 ReportSubmission")
        task_id = self._task_id_factory()
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id_factory 必须返回 TaskId")
        accepted_at = self._clock.now_utc()
        business_ref = TaskBusinessRef(
            REPORT_TASK_TYPE,
            submission.report_id.business_key,
        )
        encoded = self._codec.encode_submission(
            TaskSubmissionCommand(
                task_type=REPORT_TASK_TYPE,
                business_ref=business_ref,
                input_schema_version=REPORT_INPUT_SCHEMA_VERSION_V2,
                submission=submission,
                trace_id=submission.trace_id,
            ),
            task_id=task_id,
            accepted_at=accepted_at,
        )
        snapshot = encoded.input_snapshot
        if (
            not isinstance(snapshot, ReportInputSnapshot)
            or snapshot.task_id != task_id.value
            or snapshot.report_id != submission.report_id
            or snapshot.schema_version != REPORT_INPUT_SCHEMA_VERSION_V2
            or snapshot.execution_profile is None
            or snapshot.accepted_at != accepted_at
            or snapshot.trace_id != submission.trace_id
        ):
            raise ReportPortContractError("Report v2 Codec 返回的受理快照身份不一致")

        request = TaskAdmissionRequest(
            task_id=task_id,
            task_type=REPORT_TASK_TYPE,
            business_ref=business_ref,
            input_schema_version=REPORT_INPUT_SCHEMA_VERSION_V2,
            input_snapshot=snapshot,
            input_payload=encoded.input_payload,
            public_request_payload=encoded.projection_request_payload,
            initial_public_status=encoded.initial_public_status,
            trace_id=submission.trace_id,
            accepted_at=accepted_at,
        )
        with self._admission_uow_factory() as unit_of_work:
            result = unit_of_work.admission.admit_one(request)
            if result.outcome is TaskAdmissionOutcome.ACCEPTED:
                unit_of_work.commit()

        if result.outcome is not TaskAdmissionOutcome.ACCEPTED:
            logger.info(
                "Report v2 受理被控制面拒绝: report_id=%s outcome=%s",
                submission.report_id.public_value,
                result.outcome.value,
            )
            raise ReportTaskConflictError("任务正在处理中")
        if (
            result.task is None
            or result.task.task_id != task_id
            or result.task.business_ref != business_ref
            or result.task.state.value != "accepted"
        ):
            raise ReportPortContractError("Task Admission 返回的 accepted 身份不一致")

        progress_notified = self._publish_initial(task_id, business_ref)
        dispatcher_notified = self._wake(task_id)
        logger.info(
            "Report v2 受理事实已提交: task_id=%s report_id=%s "
            "progress_notified=%s dispatcher_notified=%s",
            task_id,
            submission.report_id.public_value,
            progress_notified,
            dispatcher_notified,
        )
        return SubmitReportResult(task_id, progress_notified, dispatcher_notified)

    def _publish_initial(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        try:
            self._progress.publish(
                ProgressPublication(
                    key=ProgressKey(REPORT_TASK_TYPE, business_ref.business_key),
                    expected_task_id=task_id,
                    progress=0.0,
                    message="",
                    internal_state="accepted",
                )
            )
        except Exception:
            logger.exception(
                "Report v2 初始 Progress 通知失败，accepted 事实仍保留: task_id=%s",
                task_id,
            )
            return False
        return True

    def _wake(self, task_id: TaskId) -> bool:
        try:
            self._dispatcher.dispatch(task_id)
        except Exception:
            logger.exception(
                "Report v2 Executor 唤醒失败，等待持久扫描恢复: task_id=%s",
                task_id,
            )
            return False
        return True


__all__ = ["SubmitReportV2Task"]
