"""武器谱可靠受理用例；只提交任务事实并发送可丢唤醒通知。"""

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
from app.modules.weaponry.domain import (
    WEAPONRY_BUSINESS_TYPE,
    WEAPONRY_INPUT_SCHEMA_VERSION,
    WeaponryInputSnapshot,
    WeaponrySubmission,
)
from app.modules.weaponry.ports import WeaponryTaskDispatcherPort

from .errors import WeaponryPortContractError, WeaponryTaskConflictError


logger = logging.getLogger(__name__)

WEAPONRY_TASK_TYPE = WEAPONRY_BUSINESS_TYPE
WEAPONRY_PUBLIC_PROCESSING_STATUS = "1"


@dataclass(frozen=True)
class SubmitWeaponryResult:
    """内部受理结果；后续 Presenter 不得把 ``task_id`` 放入公开响应。"""

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


class SubmitWeaponryTask:
    """短事务受理武器谱任务，并在提交后尝试 Progress/Dispatcher 通知。

    ``create_if_allowed`` 是 202 语义的唯一权威提交点。后续两个通知都只是可恢复的进程内
    信号：失败不能撤销 accepted 事实，也不能让 Web 层把已提交任务伪装成未受理。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[
            WeaponrySubmission,
            WeaponryInputSnapshot,
            object,
        ],
        progress_publisher: ProgressPublisherPort,
        dispatcher: WeaponryTaskDispatcherPort,
    ) -> None:
        self._task_commands = task_commands
        self._progress_publisher = progress_publisher
        self._dispatcher = dispatcher

    @property
    def dispatcher(self) -> WeaponryTaskDispatcherPort:
        """暴露只读依赖身份，供 1D-5 组合根证明实例链一致。"""

        return self._dispatcher

    @property
    def task_commands(
        self,
    ) -> TaskCommandPort[WeaponrySubmission, WeaponryInputSnapshot, object]:
        """只读暴露任务事实端口，供组合根核对单一实例链。"""

        return self._task_commands

    @property
    def progress_publisher(self) -> ProgressPublisherPort:
        return self._progress_publisher

    def execute(self, submission: WeaponrySubmission) -> SubmitWeaponryResult:
        """受理一个已经完成 Web 校验、文档冻结和策略注入的命令。"""

        if not isinstance(submission, WeaponrySubmission):
            raise TypeError("submission 必须是 WeaponrySubmission")
        business_ref = TaskBusinessRef(
            WEAPONRY_TASK_TYPE,
            submission.business_key,
        )
        command = TaskSubmissionCommand(
            task_type=WEAPONRY_TASK_TYPE,
            business_ref=business_ref,
            input_schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
            submission=submission,
            trace_id=submission.trace_id,
        )
        result = self._task_commands.create_if_allowed(command)
        execution = self._accepted_execution(
            result,
            submission=submission,
            business_ref=business_ref,
        )

        progress_notified = self._publish_initial_progress(execution)
        dispatcher_notified = self._notify_dispatcher(execution.task_id)
        logger.info(
            "武器谱任务受理事实已提交: task_id=%s architecture_id=%s "
            "document_count=%d field_count=%d progress_notified=%s "
            "dispatcher_notified=%s",
            execution.task_id.value,
            submission.architecture_id,
            len(submission.document_scope.documents),
            len(submission.fields),
            progress_notified,
            dispatcher_notified,
        )
        return SubmitWeaponryResult(
            task_id=execution.task_id,
            progress_notified=progress_notified,
            dispatcher_notified=dispatcher_notified,
        )

    @staticmethod
    def _accepted_execution(
        result: object,
        *,
        submission: WeaponrySubmission,
        business_ref: TaskBusinessRef,
    ) -> TaskExecutionSnapshot[WeaponryInputSnapshot]:
        """严格核对受理返回，防止 Adapter 把其他任务快照静默交给调用方。"""

        if not isinstance(result, TaskSubmissionResult):
            raise WeaponryPortContractError(
                "TaskCommandPort.create_if_allowed 必须返回 TaskSubmissionResult"
            )
        if result.outcome is not TaskSubmissionOutcome.ACCEPTED:
            logger.info(
                "武器谱任务受理被冲突规则拒绝: architecture_id=%s outcome=%s",
                submission.architecture_id,
                result.outcome.value,
            )
            # ACTIVE、CALLBACK_SENDING 和 OUTCOME_UNKNOWN 已批准统一映射既有 409。
            raise WeaponryTaskConflictError("任务正在处理中")

        execution = result.execution
        if not isinstance(execution, TaskExecutionSnapshot):
            raise WeaponryPortContractError("accepted 结果缺少执行快照")
        snapshot = execution.input_snapshot
        if not isinstance(snapshot, WeaponryInputSnapshot):
            raise WeaponryPortContractError("武器谱执行输入必须是 WeaponryInputSnapshot")
        expected = (
            execution.task_type == WEAPONRY_TASK_TYPE
            and execution.business_ref == business_ref
            and execution.execution_state == "accepted"
            and execution.public_status == WEAPONRY_PUBLIC_PROCESSING_STATUS
            and execution.progress == 0.0
            and execution.task_id.value == snapshot.task_id
            and snapshot.schema_version == WEAPONRY_INPUT_SCHEMA_VERSION
            and snapshot.architecture_id == submission.architecture_id
            and snapshot.fields == submission.fields
            and snapshot.document_scope == submission.document_scope
            and snapshot.evidence_selection_policy
            == submission.evidence_selection_policy
            and snapshot.execution_policy == submission.execution_policy
            and snapshot.auxiliary_guidance_policy
            == submission.auxiliary_guidance_policy
            and snapshot.accepted_at == execution.accepted_at
            and snapshot.trace_id == submission.trace_id
            and execution.trace_id == submission.trace_id
        )
        if not expected:
            raise WeaponryPortContractError("受理结果与武器谱命令身份不一致")
        return execution

    def _publish_initial_progress(
        self,
        execution: TaskExecutionSnapshot[WeaponryInputSnapshot],
    ) -> bool:
        try:
            self._progress_publisher.publish(
                ProgressPublication(
                    key=ProgressKey(
                        WEAPONRY_TASK_TYPE,
                        execution.business_ref.business_key,
                    ),
                    expected_task_id=execution.task_id,
                    progress=0.0,
                    message="",
                    internal_state="accepted",
                )
            )
        except Exception:
            logger.exception(
                "武器谱初始 Progress 通知失败，accepted 事实仍保留: task_id=%s",
                execution.task_id.value,
            )
            return False
        return True

    def _notify_dispatcher(self, task_id: TaskId) -> bool:
        try:
            self._dispatcher.dispatch(task_id)
        except Exception:
            logger.exception(
                "武器谱任务唤醒失败，等待持久化扫描恢复: task_id=%s",
                task_id.value,
            )
            return False
        return True


__all__ = [
    "SubmitWeaponryResult",
    "SubmitWeaponryTask",
    "WEAPONRY_PUBLIC_PROCESSING_STATUS",
    "WEAPONRY_TASK_TYPE",
]
