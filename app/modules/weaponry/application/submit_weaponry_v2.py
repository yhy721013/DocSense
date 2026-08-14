"""Weaponry Input v2 到统一 Task Admission/文档快照 UoW 的受理用例。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Protocol, runtime_checkable
from uuid import uuid4

from app.modules.tasks.domain import ProgressKey, TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ClockPort,
    EncodedTaskSubmission,
    ProgressPublication,
    ProgressPublisherPort,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskSubmissionCommand,
)
from app.modules.weaponry.domain import (
    WEAPONRY_INPUT_SCHEMA_VERSION,
    WeaponryInputSnapshot,
    WeaponrySubmission,
)
from app.modules.weaponry.ports import WeaponryTaskDispatcherPort

from .errors import WeaponryPortContractError, WeaponryTaskConflictError
from .execution_uow import WeaponryAdmissionUnitOfWorkFactory
from .submit_weaponry import (
    SubmitWeaponryResult,
    SubmitWeaponryTask,
    WEAPONRY_PUBLIC_PROCESSING_STATUS,
    WEAPONRY_TASK_TYPE,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class _WeaponryAdmissionCodec(Protocol):
    @property
    def write_schema_version(self) -> int: ...

    def encode_submission(
        self,
        command: TaskSubmissionCommand[WeaponrySubmission],
        *,
        task_id: TaskId,
        accepted_at: str,
    ) -> EncodedTaskSubmission[WeaponryInputSnapshot]: ...


def _new_task_id() -> TaskId:
    return TaskId(str(uuid4()))


class SubmitWeaponryV2Task(SubmitWeaponryTask):
    """原子提交 v2 Task 与文档快照，再发送两个可丢进程内提示。

    继承旧用例仅为保持现有 Request 编排器的内部类型契约；本类不会构造或调用旧
    ``TaskCommandPort``，生产写入者只有阶段 2 Admission UoW。
    """

    def __init__(
        self,
        *,
        admission_uow_factory: WeaponryAdmissionUnitOfWorkFactory,
        codec: _WeaponryAdmissionCodec,
        clock: ClockPort,
        progress_publisher: ProgressPublisherPort,
        dispatcher: WeaponryTaskDispatcherPort,
        task_id_factory: Callable[[], TaskId] = _new_task_id,
    ) -> None:
        if not isinstance(admission_uow_factory, WeaponryAdmissionUnitOfWorkFactory):
            raise TypeError("admission_uow_factory 必须实现 Weaponry Admission UoW Factory")
        if not isinstance(codec, _WeaponryAdmissionCodec):
            raise TypeError("codec 必须实现 Weaponry v2 受理编码契约")
        if codec.write_schema_version != WEAPONRY_INPUT_SCHEMA_VERSION:
            raise ValueError("Weaponry v2 受理必须装配 Input v2 Codec")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not isinstance(progress_publisher, ProgressPublisherPort):
            raise TypeError("progress_publisher 必须实现 ProgressPublisherPort")
        if not isinstance(dispatcher, WeaponryTaskDispatcherPort):
            raise TypeError("dispatcher 必须实现 WeaponryTaskDispatcherPort")
        if not callable(task_id_factory):
            raise TypeError("task_id_factory 必须可调用")
        self._admission_uow_factory = admission_uow_factory
        self._codec = codec
        self._clock = clock
        self._progress_publisher = progress_publisher
        self._dispatcher = dispatcher
        self._task_id_factory = task_id_factory

    @property
    def dispatcher(self) -> WeaponryTaskDispatcherPort:
        return self._dispatcher

    @property
    def progress_publisher(self) -> ProgressPublisherPort:
        return self._progress_publisher

    def execute(self, submission: WeaponrySubmission) -> SubmitWeaponryResult:
        if not isinstance(submission, WeaponrySubmission):
            raise TypeError("submission 必须是 WeaponrySubmission")
        task_id = self._task_id_factory()
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id_factory 必须返回 TaskId")
        accepted_at = self._clock.now_utc()
        business_ref = TaskBusinessRef(WEAPONRY_TASK_TYPE, submission.business_key)
        encoded = self._codec.encode_submission(
            TaskSubmissionCommand(
                task_type=WEAPONRY_TASK_TYPE,
                business_ref=business_ref,
                input_schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
                submission=submission,
                trace_id=submission.trace_id,
            ),
            task_id=task_id,
            accepted_at=accepted_at,
        )
        snapshot = encoded.input_snapshot
        if (
            not isinstance(snapshot, WeaponryInputSnapshot)
            or snapshot.task_id != task_id.value
            or snapshot.architecture_id != submission.architecture_id
            or snapshot.schema_version != WEAPONRY_INPUT_SCHEMA_VERSION
            or snapshot.accepted_at != accepted_at
            or snapshot.trace_id != submission.trace_id
        ):
            raise WeaponryPortContractError("Weaponry v2 Codec 返回的受理快照身份不一致")

        request = TaskAdmissionRequest(
            task_id=task_id,
            task_type=WEAPONRY_TASK_TYPE,
            business_ref=business_ref,
            input_schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
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
                unit_of_work.document_snapshots.replace_for_task(
                    task_id=task_id,
                    business_ref=business_ref,
                    documents=snapshot.document_scope.documents,
                )
                unit_of_work.commit()

        if result.outcome is not TaskAdmissionOutcome.ACCEPTED:
            logger.info(
                "Weaponry v2 受理被控制面拒绝: architecture_id=%s outcome=%s",
                submission.architecture_id,
                result.outcome.value,
            )
            raise WeaponryTaskConflictError("任务正在处理中")
        if (
            result.task is None
            or result.task.task_id != task_id
            or result.task.business_ref != business_ref
            or result.task.state.value != "accepted"
        ):
            raise WeaponryPortContractError("Task Admission 返回的 accepted 身份不一致")

        progress_notified = self._publish_initial(task_id, business_ref)
        dispatcher_notified = self._wake(task_id)
        logger.info(
            "Weaponry v2 受理事实已提交: task_id=%s architecture_id=%s "
            "document_count=%d field_count=%d progress_notified=%s "
            "dispatcher_notified=%s",
            task_id,
            submission.architecture_id,
            len(snapshot.document_scope.documents),
            len(snapshot.fields),
            progress_notified,
            dispatcher_notified,
        )
        return SubmitWeaponryResult(task_id, progress_notified, dispatcher_notified)

    def _publish_initial(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        try:
            self._progress_publisher.publish(
                ProgressPublication(
                    key=ProgressKey(WEAPONRY_TASK_TYPE, business_ref.business_key),
                    expected_task_id=task_id,
                    progress=0.0,
                    message="",
                    internal_state="accepted",
                )
            )
        except Exception:
            logger.exception(
                "Weaponry v2 初始 Progress 通知失败，accepted 事实仍保留: task_id=%s",
                task_id,
            )
            return False
        return True

    def _wake(self, task_id: TaskId) -> bool:
        try:
            self._dispatcher.dispatch(task_id)
        except Exception:
            logger.exception(
                "Weaponry v2 Executor 唤醒失败，等待持久扫描恢复: task_id=%s",
                task_id,
            )
            return False
        return True


__all__ = ["SubmitWeaponryV2Task"]
