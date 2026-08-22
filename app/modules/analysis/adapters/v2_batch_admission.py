"""Analysis v5 批量输入到 Task Control v2 Admission 的原子适配器。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from uuid import uuid4

from app.modules.analysis.adapters.task_codec import (
    AnalysisTaskInputCodecError,
    AnalysisV5TaskCommandCodec,
)
from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisExecutionRef,
)
from app.modules.tasks.adapters.sqlite import SQLiteBusyError
from app.modules.tasks.domain import TaskBatchRef, TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ClockPort,
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
    TaskAdmissionUnitOfWorkFactory,
)


logger = logging.getLogger(__name__)


def _new_task_id() -> TaskId:
    return TaskId(uuid4().hex)


def _new_batch_id() -> str:
    return uuid4().hex


class AnalysisV2BatchAdmissionError(RuntimeError):
    """批量受理依赖返回值破坏内部契约。"""


class SQLiteAnalysisV2BatchAdmissionAdapter:
    """一次 ``BEGIN IMMEDIATE`` 提交 1～32 个 v5 execution。

    所有 TaskId、Profile 快照与 Codec 往返都在事务外准备；事务内只做冲突分类和
    Task/latest/event 写入。任一成员冲突时不提交任何成员，成功后由上层仅发送一次
    可丢唤醒提示。
    """

    def __init__(
        self,
        *,
        admission_uow_factory: TaskAdmissionUnitOfWorkFactory,
        codec: AnalysisV5TaskCommandCodec,
        clock: ClockPort,
        task_id_factory: Callable[[], TaskId] = _new_task_id,
        batch_id_factory: Callable[[], str] = _new_batch_id,
    ) -> None:
        if not callable(admission_uow_factory):
            raise TypeError("admission_uow_factory 必须可调用")
        if not isinstance(codec, AnalysisV5TaskCommandCodec):
            raise TypeError("codec 必须是 AnalysisV5TaskCommandCodec")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not callable(task_id_factory) or not callable(batch_id_factory):
            raise TypeError("Task/Batch 身份工厂必须可调用")
        self._admission_uow_factory = admission_uow_factory
        self._codec = codec
        self._clock = clock
        self._task_id_factory = task_id_factory
        self._batch_id_factory = batch_id_factory

    def create_batch_if_allowed(
        self,
        command: AnalysisBatchCommand,
    ) -> AnalysisBatchAdmission:
        if not isinstance(command, AnalysisBatchCommand):
            raise TypeError("command 必须是 AnalysisBatchCommand")
        batch_id = self._batch_id_factory()
        if not isinstance(batch_id, str):
            raise TypeError("batch_id_factory 必须返回 str")
        # 使用既有 Analysis 值对象校验 32 位小写十六进制身份，禁止适配器放宽合同。
        validation_ref = AnalysisExecutionRef(
            TaskId("analysis-batch-validation"),
            "analysis-batch-validation",
            batch_id,
            1,
        )
        batch_id = validation_ref.batch_id
        accepted_at = self._clock.now_utc()
        requests: list[TaskAdmissionRequest] = []
        executions: list[AnalysisExecutionRef] = []
        seen_task_ids: set[TaskId] = set()
        for sequence, submission in enumerate(command.submissions, start=1):
            task_id = self._task_id_factory()
            if not isinstance(task_id, TaskId):
                raise TypeError("task_id_factory 必须返回 TaskId")
            if task_id in seen_task_ids:
                raise AnalysisV2BatchAdmissionError("task_id_factory 返回重复任务身份")
            seen_task_ids.add(task_id)
            try:
                encoded = self._codec.encode_batch_item(
                    submission,
                    task_id=task_id,
                    batch_id=batch_id,
                    batch_sequence=sequence,
                    accepted_at=accepted_at,
                    trace_id=command.trace_id,
                )
            except AnalysisTaskInputCodecError as exc:
                raise AnalysisV2BatchAdmissionError(
                    "Analysis v5 输入在受理前无法严格往返"
                ) from exc
            business_ref = TaskBusinessRef("file", submission.file_name)
            requests.append(
                TaskAdmissionRequest(
                    task_id=task_id,
                    task_type="file",
                    business_ref=business_ref,
                    input_schema_version=self._codec.write_schema_version,
                    input_snapshot=encoded.input_snapshot,
                    input_payload=encoded.input_payload,
                    public_request_payload=encoded.projection_request_payload,
                    initial_public_status=encoded.initial_public_status,
                    trace_id=command.trace_id,
                    accepted_at=accepted_at,
                    batch=TaskBatchRef(batch_id, sequence),
                )
            )
            executions.append(
                AnalysisExecutionRef(task_id, submission.file_name, batch_id, sequence)
            )

        try:
            with self._admission_uow_factory() as unit_of_work:
                results = unit_of_work.admission.admit_many(tuple(requests))
                if all(
                    item.outcome is TaskAdmissionOutcome.ACCEPTED for item in results
                ):
                    self._validate_accepted_results(tuple(requests), results)
                    unit_of_work.commit()
        except SQLiteBusyError:
            logger.warning(
                "Analysis v2 批量受理数据库繁忙: batch_id=%s item_count=%d",
                batch_id,
                len(requests),
            )
            return AnalysisBatchAdmission(AnalysisBatchAdmissionOutcome.BUSY)

        outcome = self._map_outcome(results)
        if outcome is not AnalysisBatchAdmissionOutcome.ACCEPTED:
            logger.info(
                "Analysis v2 批量受理被拒绝: batch_id=%s item_count=%d outcome=%s",
                batch_id,
                len(requests),
                outcome.value,
            )
            return AnalysisBatchAdmission(outcome)
        logger.info(
            "Analysis v2 批量受理事实已提交: batch_id=%s item_count=%d",
            batch_id,
            len(requests),
        )
        return AnalysisBatchAdmission(outcome, tuple(executions))

    @staticmethod
    def _validate_accepted_results(requests, results) -> None:
        if len(results) != len(requests):
            raise AnalysisV2BatchAdmissionError("Admission 返回数量与请求不一致")
        for request, result in zip(requests, results, strict=True):
            if (
                result.task_id != request.task_id
                or result.business_ref != request.business_ref
                or result.task is None
                or result.task.task_id != request.task_id
                or result.task.business_ref != request.business_ref
                or result.task.state.value != "accepted"
            ):
                raise AnalysisV2BatchAdmissionError(
                    "Admission accepted 回显与预分配身份不一致"
                )

    @staticmethod
    def _map_outcome(results) -> AnalysisBatchAdmissionOutcome:
        outcomes = {item.outcome for item in results}
        if outcomes == {TaskAdmissionOutcome.ACCEPTED}:
            return AnalysisBatchAdmissionOutcome.ACCEPTED
        callback_conflicts = {
            TaskAdmissionOutcome.CALLBACK_SENDING,
            TaskAdmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
        }
        if outcomes & callback_conflicts:
            return AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING
        if TaskAdmissionOutcome.ACTIVE_TASK_CONFLICT in outcomes:
            return AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE
        if outcomes == {TaskAdmissionOutcome.BATCH_REJECTED}:
            # 理论上只有同批另一项冲突才会产生 BATCH_REJECTED；没有根因说明 Store
            # 返回集合损坏，不能擅自把未知情况映射成公开冲突。
            raise AnalysisV2BatchAdmissionError("Admission 批量拒绝缺少根因")
        if TaskAdmissionOutcome.BATCH_REJECTED in outcomes:
            raise AnalysisV2BatchAdmissionError("Admission 返回未知批量拒绝组合")
        raise AnalysisV2BatchAdmissionError("Admission 返回未知受理结果")


__all__ = [
    "AnalysisV2BatchAdmissionError",
    "SQLiteAnalysisV2BatchAdmissionAdapter",
]
