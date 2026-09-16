"""报告可靠受理用例；由 Web Adapter 通过应用组合根调用。"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from app.modules.tasks.domain import (
    ProgressKey,
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
)
from app.modules.tasks.ports import (
    ProgressPublication,
    ProgressPublisherPort,
    TaskCommandPort,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
)

from app.modules.report.domain import (
    REPORT_INPUT_SCHEMA_VERSION,
    ReportInputSnapshot,
    ReportPortContractError,
    ReportSubmission,
    ReportTaskConflictError,
)
from app.modules.report.ports import (
    ReportTaskDispatcherPort,
)


logger = logging.getLogger(__name__)

REPORT_TASK_TYPE = "report"
REPORT_PUBLIC_PROCESSING_STATUS = "0"


@dataclass(frozen=True)
class SubmitReportResult:
    """仅供内部组合根使用的受理结果；Presenter 不得公开 ``task_id``。"""

    task_id: TaskId
    progress_notified: bool
    dispatcher_notified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.progress_notified, bool):
            raise TypeError("progress_notified 必须是 bool")
        if not isinstance(self.dispatcher_notified, bool):
            raise TypeError("dispatcher_notified 必须是 bool")


class SubmitReportTask:
    """原子持久化受理事实，并以可丢唤醒通知本地执行器。

    create-if-allowed 成功后，Progress 或 Dispatcher 通知失败都不能撤销已提交任务；
    周期扫描必须能够从持久化 accepted 事实恢复。这一语义避免把内存通知误作可靠队列。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[
            ReportSubmission,
            ReportInputSnapshot,
            object,
        ],
        progress_publisher: ProgressPublisherPort,
        dispatcher: ReportTaskDispatcherPort,
    ) -> None:
        self._task_commands = task_commands
        self._progress_publisher = progress_publisher
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> ReportTaskDispatcherPort:
        """暴露只读依赖身份，供组合根证明受理与生命周期使用同一实例。"""

        return self._dispatcher

    def execute(self, submission: ReportSubmission) -> SubmitReportResult:
        """受理一个已由 Web Adapter 严格解析的不可变报告命令。"""

        if not isinstance(submission, ReportSubmission):
            raise TypeError("submission 必须是 ReportSubmission")
        business_ref = TaskBusinessRef(
            REPORT_TASK_TYPE,
            submission.report_id.business_key,
        )
        command = TaskSubmissionCommand(
            task_type=REPORT_TASK_TYPE,
            business_ref=business_ref,
            input_schema_version=REPORT_INPUT_SCHEMA_VERSION,
            submission=submission,
            trace_id=submission.trace_id,
        )
        # CALLBACK_SENDING 与 CALLBACK_OUTCOME_UNKNOWN 均按已确认契约立即映射为 409。
        # Web 请求线程不轮询等待旧回调，也不在同一请求中重试受理；调用方稍后重试时，
        # Repository 会再次执行同一个原子判断。
        result = self._task_commands.create_if_allowed(command)
        execution = self._accepted_execution(result, submission, business_ref)

        progress_notified = self._publish_initial_progress(execution)
        dispatcher_notified = self._notify_dispatcher(execution.task_id)
        logger.info(
            "报告任务受理事实已提交: task_id=%s report_id=%s "
            "progress_notified=%s dispatcher_notified=%s",
            execution.task_id,
            submission.report_id.public_value,
            progress_notified,
            dispatcher_notified,
        )
        return SubmitReportResult(
            task_id=execution.task_id,
            progress_notified=progress_notified,
            dispatcher_notified=dispatcher_notified,
        )

    @staticmethod
    def _accepted_execution(
        result: object,
        submission: ReportSubmission,
        business_ref: TaskBusinessRef,
    ) -> TaskExecutionSnapshot[ReportInputSnapshot]:
        """校验 Adapter 返回值，防止跨任务输入被静默受理。"""

        if not isinstance(result, TaskSubmissionResult):
            raise ReportPortContractError(
                "TaskCommandPort.create_if_allowed 必须返回 TaskSubmissionResult"
            )
        if result.outcome is not TaskSubmissionOutcome.ACCEPTED:
            logger.info(
                "报告任务受理被内部冲突规则拒绝: report_id=%s outcome=%s",
                submission.report_id.public_value,
                result.outcome.value,
            )
            # ACTIVE、CALLBACK_SENDING 和 OUTCOME_UNKNOWN 对外均映射为已批准的同一 409；
            # 这里不携带 HTTP 细节，避免应用层依赖 Web 框架。
            raise ReportTaskConflictError("任务正在处理中")

        execution = result.execution
        if not isinstance(execution, TaskExecutionSnapshot):
            raise ReportPortContractError("accepted 结果缺少执行快照")
        snapshot = execution.input_snapshot
        if not isinstance(snapshot, ReportInputSnapshot):
            raise ReportPortContractError("报告执行输入必须是 ReportInputSnapshot")
        expected = (
            execution.task_type == REPORT_TASK_TYPE
            and execution.business_ref == business_ref
            and execution.execution_state == "accepted"
            and execution.public_status == REPORT_PUBLIC_PROCESSING_STATUS
            and execution.progress == 0.0
            and execution.task_id.value == snapshot.task_id
            and snapshot.report_id == submission.report_id
            and snapshot.schema_version == REPORT_INPUT_SCHEMA_VERSION
            and snapshot.source_urls == submission.source_urls
            and snapshot.template_outline_url == submission.template_outline_url
            and snapshot.template_desc == submission.template_desc
            and snapshot.requirement == submission.requirement
            and snapshot.accepted_at == execution.accepted_at
            and snapshot.trace_id == submission.trace_id
            and execution.trace_id == submission.trace_id
        )
        if not expected:
            raise ReportPortContractError("受理结果与报告命令身份不一致")
        return execution

    def _publish_initial_progress(
        self,
        execution: TaskExecutionSnapshot[ReportInputSnapshot],
    ) -> bool:
        publication = ProgressPublication(
            key=ProgressKey(
                REPORT_TASK_TYPE,
                execution.business_ref.business_key,
            ),
            expected_task_id=execution.task_id,
            progress=0.0,
            message="",
            internal_state="accepted",
        )
        try:
            self._progress_publisher.publish(publication)
        except Exception:
            logger.exception(
                "报告任务初始Progress通知失败，持久化受理事实仍保留: task_id=%s",
                execution.task_id,
            )
            return False
        return True

    def _notify_dispatcher(self, task_id: TaskId) -> bool:
        try:
            self._dispatcher.dispatch(task_id)
        except Exception:
            logger.exception(
                "报告任务唤醒失败，等待持久化扫描恢复: task_id=%s",
                task_id,
            )
            return False
        return True


__all__ = [
    "REPORT_PUBLIC_PROCESSING_STATUS",
    "REPORT_TASK_TYPE",
    "SubmitReportResult",
    "SubmitReportTask",
]
