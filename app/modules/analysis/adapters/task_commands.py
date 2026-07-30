"""文件分析批量受理与通用 TaskCommand 的 SQLite 适配器。

该模块把 1F-2 的不可变 ``AnalysisBatchCommand`` 与 1F-3 的 ``RunAnalysisTask`` 接到
同一份追加 execution 事实。它不读取 Flask Request、不发布 Progress、不创建线程，也不
进行文件、RAG、模型或 Callback I/O；这些职责分别留在 Web、Application 和后续 Dispatcher
边界，避免把“已受理”重新绑定到某个进程内队列。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

from app.modules.analysis.application.workflow_models import AnalysisTaskCompletion
from app.modules.analysis.domain.callback_payloads import build_file_callback_payload
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV1,
    AnalysisTaskInputV3,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisBatchCommandPort,
    AnalysisExecutionRef,
    AnalysisTaskClaim,
    AnalysisTaskClaimOutcome,
)
from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
)
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskCommandPort,
    TaskQueueSnapshot,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
)
from app.services.llm_service.task_service import (
    AnalysisBatchTaskAdmission,
    LLMTaskService,
    TaskAdmissionBusyError,
)

from .task_codec import AnalysisTaskInputCodec, AnalysisTaskInputCodecError


logger = logging.getLogger(__name__)

_ANALYSIS_BUSINESS_TYPE = "file"
_MAX_BATCH_SEQUENCE = 32


def _utc_now_iso() -> str:
    """返回带时区 UTC 时间，供一次批次的所有子任务共享受理时刻。"""

    return datetime.now(timezone.utc).isoformat()


def _aware_utc_iso(value: object, *, name: str) -> str:
    """规范化可比较的内部时钟值，拒绝无时区时间和隐式字符串化。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空 str")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 ISO 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必须包含时区")
    return parsed.astimezone(timezone.utc).isoformat()


def _new_task_id() -> TaskId:
    """生成仅在内部 SQLite execution 中使用的任务身份。"""

    return TaskId(uuid4().hex)


def _new_batch_id() -> str:
    """生成 128 位随机批次身份；绝不投影到 HTTP、Progress 或 Callback。"""

    return uuid4().hex


def _required_text(value: object, *, name: str) -> str:
    """严格处理内部标识，避免把任意对象误写入 SQLite 或日志。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class AnalysisTaskCommandAdapterError(RuntimeError):
    """SQLite 返回值、Codec 或跨层身份不符合 Analysis 内部合同。"""


class AnalysisTaskSnapshotCorruptedError(AnalysisTaskCommandAdapterError):
    """持久化 execution 或输入快照已损坏，普通重试无法恢复。"""


class SQLiteAnalysisBatchCommandAdapter:
    """复用 ``LLMTaskService`` 的批量原子受理和 TaskId 条件写。

    同一个实例同时实现 ``AnalysisBatchCommandPort`` 与临时复用的通用
    ``TaskCommandPort``：前者是新任务唯一受理入口，后者只服务 1F-3 已冻结的
    ``RunAnalysisTask(TaskId)``。二者始终读取同一 execution 行，不能分别维护内存
    队列、批次表或可变快照。
    """

    def __init__(
        self,
        task_service: LLMTaskService,
        *,
        task_id_factory: Callable[[], TaskId] = _new_task_id,
        batch_id_factory: Callable[[], str] = _new_batch_id,
        clock: Callable[[], str] = _utc_now_iso,
    ) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        if not callable(task_id_factory):
            raise TypeError("task_id_factory 必须可调用")
        if not callable(batch_id_factory):
            raise TypeError("batch_id_factory 必须可调用")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._task_service = task_service
        self._task_id_factory = task_id_factory
        self._batch_id_factory = batch_id_factory
        self._clock = clock

    def create_batch_if_allowed(
        self,
        command: AnalysisBatchCommand,
    ) -> AnalysisBatchAdmission:
        """构造全部不可变输入后，一次调用 SQLite 批量事务。

        任务 ID、批次 ID、Codec 往返和 JSON 可序列化都在事务外完成。真正的活动任务和
        Callback Guard 冲突判断只能由 ``create_analysis_batch_if_allowed`` 在 ``BEGIN
        IMMEDIATE`` 内完成，因此本方法绝不调用 ``get_task`` 做事务外预查。
        """

        if not isinstance(command, AnalysisBatchCommand):
            raise TypeError("command 必须是 AnalysisBatchCommand")
        batch_id = self._new_valid_batch_id()
        accepted_at = _aware_utc_iso(self._clock(), name="accepted_at")
        prepared_admissions: list[AnalysisBatchTaskAdmission] = []
        expected_executions: list[AnalysisExecutionRef] = []
        seen_task_ids: set[TaskId] = set()

        for sequence, submission in enumerate(command.submissions, start=1):
            task_id = self._task_id_factory()
            if not isinstance(task_id, TaskId):
                raise TypeError("task_id_factory 必须返回 TaskId")
            if task_id in seen_task_ids:
                raise AnalysisTaskCommandAdapterError("task_id_factory 返回了重复任务身份")
            seen_task_ids.add(task_id)
            task_input = AnalysisTaskInputV3.from_submission(
                submission,
                task_id=task_id.value,
                batch_id=batch_id,
                batch_sequence=sequence,
                accepted_at=accepted_at,
                trace_id=command.trace_id,
            )
            payload = AnalysisTaskInputCodec.encode(task_input)
            try:
                roundtrip = AnalysisTaskInputCodec.decode(
                    payload,
                    expected_task_id=task_id.value,
                    expected_business_key=submission.file_name,
                    expected_batch_id=batch_id,
                )
            except AnalysisTaskInputCodecError as exc:
                raise AnalysisTaskCommandAdapterError(
                    "Analysis任务输入在受理前无法往返解码"
                ) from exc
            if roundtrip != task_input:
                raise AnalysisTaskCommandAdapterError(
                    "Analysis任务输入受理前往返解码不一致"
                )
            expected_executions.append(
                AnalysisExecutionRef(
                    task_id=task_id,
                    file_name=submission.file_name,
                    batch_id=batch_id,
                    batch_sequence=sequence,
                )
            )
            # 旧 ``llm_tasks`` 仍是 file Progress/check-task 的公开最新投影。每项只
            # 保存自身 params，严格复刻旧路由的批量受理形状；完整请求和内部批次身份只
            # 留在 execution 输入，不能进入公开结果。
            projection_request = {
                "businessType": _ANALYSIS_BUSINESS_TYPE,
                "params": [submission.raw_params.to_dict()],
            }
            prepared_admissions.append(
                AnalysisBatchTaskAdmission(
                    execution_id=task_id.value,
                    business_key=submission.file_name,
                    input_schema_version=ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
                    input_payload=payload,
                    projection_request_payload=projection_request,
                    initial_public_status="1" if sequence == 1 else "0",
                    trace_id=command.trace_id,
                )
            )

        try:
            raw_result = self._task_service.create_analysis_batch_if_allowed(
                batch_id=batch_id,
                admissions=tuple(prepared_admissions),
                accepted_at=accepted_at,
            )
        except TaskAdmissionBusyError:
            logger.warning(
                "文件分析批次受理返回繁忙: batch_id=%s item_count=%d",
                batch_id,
                len(expected_executions),
            )
            return AnalysisBatchAdmission(AnalysisBatchAdmissionOutcome.BUSY)
        if not isinstance(raw_result, Mapping):
            raise AnalysisTaskCommandAdapterError("Analysis批量受理返回值必须是 Mapping")

        outcome = self._admission_outcome(raw_result.get("outcome"))
        if outcome is not AnalysisBatchAdmissionOutcome.ACCEPTED:
            if raw_result.get("executions") is not None:
                raise AnalysisTaskCommandAdapterError("未受理批次不得携带 execution")
            logger.info(
                "文件分析批次已在事务内拒绝: batch_id=%s item_count=%d outcome=%s",
                batch_id,
                len(expected_executions),
                outcome.value,
            )
            return AnalysisBatchAdmission(outcome)

        # SQLite Service 已在提交前读回并核对 execution/投影；这里的返回映射只是诊断
        # 回显。若回显损坏，受理事实仍已提交，不能把成功伪装为 500 或漏掉后续唤醒。
        try:
            self._validate_committed_echo(
                raw_result.get("executions"),
                expected_executions=tuple(expected_executions),
            )
        except Exception:
            logger.critical(
                "文件分析批次已提交但Repository回显不一致；按已验证受理身份继续唤醒: "
                "batch_id=%s item_count=%d",
                batch_id,
                len(expected_executions),
                exc_info=True,
            )
        logger.info(
            "文件分析批次受理适配完成: batch_id=%s item_count=%d",
            batch_id,
            len(expected_executions),
        )
        return AnalysisBatchAdmission(
            AnalysisBatchAdmissionOutcome.ACCEPTED,
            tuple(expected_executions),
        )

    def create_if_allowed(
        self,
        command: TaskSubmissionCommand[AnalysisSubmissionSnapshot],
    ) -> TaskSubmissionResult[AnalysisTaskInputV1]:
        """为通用 TaskCommand 兼容入口保留单项批量语义。

        1F-4 之后任何 Analysis execution 都必须拥有 batch_id/sequence，因而不能调用
        通用单项 SQLite 写入口。该兼容方法把一项受理也转换为一个长度为 1 的批次；生产
        路由和新 Application 仍应直接使用 ``create_batch_if_allowed``，避免退化成逐项事务。
        """

        if not isinstance(command, TaskSubmissionCommand):
            raise TypeError("command 必须是 TaskSubmissionCommand")
        if not isinstance(command.submission, AnalysisSubmissionSnapshot):
            raise TypeError("Analysis单项命令只接受AnalysisSubmissionSnapshot")
        submission = command.submission
        if (
            command.task_type != _ANALYSIS_BUSINESS_TYPE
            or command.business_ref
            != TaskBusinessRef(_ANALYSIS_BUSINESS_TYPE, submission.file_name)
            or command.input_schema_version != ANALYSIS_TASK_INPUT_SCHEMA_VERSION
        ):
            raise ValueError("Analysis单项命令身份或Schema不一致")
        batch_command = AnalysisBatchCommand(
            request_projection=FrozenJsonObject.from_mapping(
                {
                    "businessType": _ANALYSIS_BUSINESS_TYPE,
                    "params": [submission.raw_params.to_dict()],
                },
                name="analysis_single_submission",
            ),
            submissions=(submission,),
            trace_id=command.trace_id,
        )
        admission = self.create_batch_if_allowed(batch_command)
        if admission.outcome is AnalysisBatchAdmissionOutcome.BUSY:
            raise TaskAdmissionBusyError("任务库繁忙，请稍后重试")
        if admission.outcome is AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE:
            return TaskSubmissionResult(TaskSubmissionOutcome.ACTIVE_CONFLICT)
        if admission.outcome is AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING:
            return TaskSubmissionResult(TaskSubmissionOutcome.CALLBACK_SENDING)
        if admission.outcome is not AnalysisBatchAdmissionOutcome.ACCEPTED:
            raise AnalysisTaskCommandAdapterError("Analysis单项受理出现未知结果")
        execution = self.get_execution(admission.executions[0].task_id)
        if execution is None:
            raise AnalysisTaskCommandAdapterError("已受理Analysis任务无法重新读取")
        return TaskSubmissionResult(TaskSubmissionOutcome.ACCEPTED, execution)

    def load_input(self, task_id: TaskId) -> AnalysisTaskInputV1 | None:
        """按内部 TaskId 读取严格解码后的冻结输入，不回退到业务键查询。"""

        execution = self.get_execution(task_id)
        return execution.input_snapshot if execution is not None else None

    def claim_if_accepted(self, task_id: TaskId) -> AnalysisTaskClaim:
        """把通用条件领取结果映射为 Analysis Port 的最小有限分类。"""

        claim = self.claim(task_id)
        if claim.outcome is TaskClaimOutcome.MISSING:
            return AnalysisTaskClaim(AnalysisTaskClaimOutcome.MISSING)
        if claim.outcome is TaskClaimOutcome.STALE:
            return AnalysisTaskClaim(AnalysisTaskClaimOutcome.STALE)
        if claim.outcome is not TaskClaimOutcome.CLAIMED:
            return AnalysisTaskClaim(AnalysisTaskClaimOutcome.NOT_ACCEPTED)
        if not isinstance(claim.execution, TaskExecutionSnapshot):
            raise AnalysisTaskCommandAdapterError("已领取Analysis任务缺少执行快照")
        return AnalysisTaskClaim(
            AnalysisTaskClaimOutcome.CLAIMED,
            self._to_execution_ref(claim.execution),
        )

    def get_execution(
        self,
        task_id: TaskId,
    ) -> TaskExecutionSnapshot[AnalysisTaskInputV1] | None:
        """读取并严格核对 task、业务键、批次和 Codec 输入的同一性。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        raw = self._task_service.get_task_execution(task_id.value)
        return self._decode_execution(raw) if raw is not None else None

    def claim(
        self,
        task_id: TaskId,
    ) -> TaskClaimResult[AnalysisTaskInputV1]:
        """先证明 execution 属于新 Analysis 批次，再执行原子领取。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        existing_ref = self._load_analysis_control_ref(task_id)
        if existing_ref is None:
            return TaskClaimResult(TaskClaimOutcome.MISSING)
        raw_result = self._task_service.claim_task_execution(task_id.value)
        if not isinstance(raw_result, Mapping):
            raise AnalysisTaskCommandAdapterError("Analysis领取返回值必须是 Mapping")
        outcome = self._claim_outcome(raw_result.get("outcome"))
        if outcome is TaskClaimOutcome.MISSING:
            if raw_result.get("execution") is not None:
                raise AnalysisTaskCommandAdapterError("missing领取结果不得携带execution")
            return TaskClaimResult(outcome)
        execution = self._decode_execution(raw_result.get("execution"))
        if execution.task_id != task_id:
            raise AnalysisTaskCommandAdapterError("Analysis领取结果task_id不一致")
        if self._to_execution_ref(execution) != existing_ref:
            raise AnalysisTaskCommandAdapterError("Analysis领取结果业务键不一致")
        return TaskClaimResult(outcome, execution)

    def update_progress_if_current(self, update: ExpectedProgressUpdate) -> bool:
        """执行 expected TaskId Progress 条件写，拒绝跨业务或旧兼容 execution。"""

        if not isinstance(update, ExpectedProgressUpdate):
            raise TypeError("update 必须是 ExpectedProgressUpdate")
        self._require_current_analysis_execution(
            update.expected_task_id,
            update.business_ref,
        )
        return self._task_service.update_task_execution_progress_if_current(
            expected_execution_id=update.expected_task_id.value,
            business_type=update.business_ref.business_type,
            business_key=update.business_ref.business_key,
            progress=update.progress,
            message=update.message,
            execution_state=update.execution_state,
            public_status=update.public_status,
        )

    def finish_if_current(
        self,
        completion: ExpectedTaskCompletion[AnalysisTaskCompletion],
    ) -> bool:
        """执行 expected TaskId 单终态写，并隔离 execution 与公开 Callback 投影。"""

        if not isinstance(completion, ExpectedTaskCompletion):
            raise TypeError("completion 必须是 ExpectedTaskCompletion")
        if not isinstance(completion.result, AnalysisTaskCompletion):
            raise TypeError("Analysis终态结果必须是AnalysisTaskCompletion")
        self._require_current_analysis_execution(
            completion.expected_task_id,
            completion.business_ref,
        )
        result = completion.result
        execution_result = {
            "schema_version": 1,
            "succeeded": result.succeeded,
            "mapped_result": (
                result.mapped_result.to_dict()
                if result.mapped_result is not None
                else None
            ),
        }
        return self._task_service.finish_task_execution_if_current(
            expected_execution_id=completion.expected_task_id.value,
            business_type=completion.business_ref.business_type,
            business_key=completion.business_ref.business_key,
            execution_state=completion.execution_state,
            public_status=completion.public_status,
            message=completion.message,
            execution_result_payload=execution_result,
            projection_result_payload=result.callback_payload.to_dict(),
        )

    def is_latest(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        """在外部副作用前复核当前 owner，不把 stale 当作可重试错误。"""

        self._require_current_analysis_execution(task_id, business_ref)
        return self._task_service.is_task_execution_latest(
            execution_id=task_id.value,
            business_type=business_ref.business_type,
            business_key=business_ref.business_key,
        )

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        """只扫描带 batch 身份的新 Analysis 积压，避免误领旧 file 兼容链。"""

        if _required_text(task_type, name="task_type") != _ANALYSIS_BUSINESS_TYPE:
            raise ValueError("task_type 必须是 file")
        return tuple(
            TaskId(execution_id)
            for execution_id in self._task_service.list_accepted_analysis_task_execution_ids(
                limit=limit,
            )
        )

    def inspect_queue(
        self,
        task_type: str,
        *,
        running_sample_limit: int,
    ) -> TaskQueueSnapshot:
        """返回仅包含新批次 Analysis execution 的只读队列诊断。

        旧 file 兼容链在 1F-5B 前仍由旧路由管理。若把它混入新 Dispatcher 的队列快照，
        会造成“新 Worker 有积压”的假象，并可能诱发对历史 ``running`` 的错误处理。
        """

        if _required_text(task_type, name="task_type") != _ANALYSIS_BUSINESS_TYPE:
            raise ValueError("task_type 必须是 file")
        raw = self._task_service.inspect_analysis_task_execution_queue(
            running_sample_limit=running_sample_limit,
        )
        if not isinstance(raw, Mapping):
            raise AnalysisTaskCommandAdapterError("Analysis任务队列汇总必须是Mapping")
        raw_running_ids = raw.get("running_execution_ids")
        if isinstance(raw_running_ids, (str, bytes, bytearray)) or not isinstance(
            raw_running_ids,
            (list, tuple),
        ):
            raise AnalysisTaskCommandAdapterError(
                "Analysis任务队列running_execution_ids必须是序列"
            )
        try:
            return TaskQueueSnapshot(
                task_type=_required_text(
                    raw.get("business_type"),
                    name="business_type",
                ),
                accepted_count=raw.get("accepted_count"),  # type: ignore[arg-type]
                running_count=raw.get("running_count"),  # type: ignore[arg-type]
                oldest_accepted_at=raw.get("oldest_accepted_at"),
                oldest_running_at=raw.get("oldest_running_at"),
                running_task_ids=tuple(
                    TaskId(_required_text(item, name="running_execution_id"))
                    for item in raw_running_ids
                ),
            )
        except (TypeError, ValueError) as exc:
            raise AnalysisTaskCommandAdapterError(
                "Analysis任务队列汇总数据无效"
            ) from exc

    def defer_accepted(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        """持久化领取前的有限退避，不允许把 running/终态重置为 accepted。"""

        existing_ref = self._load_analysis_control_ref(task_id)
        if existing_ref is None:
            return False
        normalized_retry_at = _aware_utc_iso(retry_at, name="retry_at")
        normalized_reason = _required_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason 长度不能超过256")
        return self._task_service.defer_accepted_task_execution(
            task_id.value,
            retry_at=normalized_retry_at,
            reason=normalized_reason,
        )

    def defer_accepted_with_backoff(
        self,
        task_id: TaskId,
        *,
        retry_base_seconds: float,
        retry_max_seconds: float,
        reason: str,
    ) -> bool:
        """按持久失败次数计算指数退避，避免重启后重新从固定等待开始。

        ``LLMTaskService`` 在同一 SQLite 写事务内读取计数、计算下一次时间并条件写入。
        这里先验证控制面身份，确保此 Adapter 不会把同业务类型的旧 file 兼容 execution
        当成新 Analysis 任务冷却；无需解码输入 payload 的毒快照则由专用终态路径处理。
        """

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if self._load_analysis_control_ref(task_id) is None:
            return False
        normalized_reason = _required_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason 长度不能超过256")
        return self._task_service.defer_accepted_task_execution_with_backoff(
            task_id.value,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            reason=normalized_reason,
            now=_aware_utc_iso(self._clock(), name="clock"),
        )

    def fail_poisoned_accepted(
        self,
        task_id: TaskId,
        *,
        reason: str,
    ) -> AnalysisExecutionRef | None:
        """不解码坏快照，按 latest owner 条件把新 Analysis accepted 收敛为失败。

        ``reason`` 只进入内部 execution 诊断列；公开消息和回调继续使用既有固定失败
        合同，避免把异常类型、数据库细节或内部 TaskId 泄露给前端。
        """

        execution_ref = self._load_analysis_control_ref(
            task_id,
            require_supported_schema=False,
        )
        if execution_ref is None:
            return False
        normalized_reason = _required_text(reason, name="reason")
        if len(normalized_reason) > 256:
            raise ValueError("reason 长度不能超过256")
        public_message = "解析失败（task_snapshot）：analysis_task_snapshot_invalid"
        finished = self._task_service.fail_accepted_analysis_task_execution_if_current(
            expected_execution_id=task_id.value,
            expected_business_key=execution_ref.file_name,
            message=public_message,
            reason=normalized_reason,
            execution_result_payload={
                "schema_version": 1,
                "succeeded": False,
                "mapped_result": None,
            },
            projection_result_payload=build_file_callback_payload(
                execution_ref.file_name,
                {},
                status="3",
            ),
        )
        return execution_ref if finished else None

    def _new_valid_batch_id(self) -> str:
        """用现有值对象统一校验可注入工厂的批次身份格式。"""

        raw_batch_id = self._batch_id_factory()
        if not isinstance(raw_batch_id, str):
            raise TypeError("batch_id_factory 必须返回 str")
        # ``AnalysisExecutionRef`` 是批次身份的唯一业务值对象；使用一个临时合法任务
        # 身份只做格式校验，不会写库或泄露到任何响应。
        return AnalysisExecutionRef(
            task_id=TaskId("analysis-batch-id-validation"),
            file_name="analysis-batch-id-validation",
            batch_id=raw_batch_id,
            batch_sequence=1,
        ).batch_id

    @staticmethod
    def _admission_outcome(value: object) -> AnalysisBatchAdmissionOutcome:
        """把兼容服务稳定字符串映射为 Application 可见的有限 outcome。"""

        if not isinstance(value, str):
            raise AnalysisTaskCommandAdapterError("Analysis受理outcome必须是str")
        mapping = {
            "accepted": AnalysisBatchAdmissionOutcome.ACCEPTED,
            "active_conflict": AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE,
            "callback_pending": AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING,
            "callback_sending": AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING,
            "callback_outcome_unknown": AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING,
        }
        try:
            return mapping[value]
        except KeyError as exc:
            raise AnalysisTaskCommandAdapterError("Analysis受理outcome未知") from exc

    @staticmethod
    def _claim_outcome(value: object) -> TaskClaimOutcome:
        if not isinstance(value, str):
            raise AnalysisTaskCommandAdapterError("Analysis领取outcome必须是str")
        try:
            return TaskClaimOutcome(value)
        except ValueError as exc:
            raise AnalysisTaskCommandAdapterError("Analysis领取outcome未知") from exc

    @staticmethod
    def _validate_committed_echo(
        raw_executions: object,
        *,
        expected_executions: tuple[AnalysisExecutionRef, ...],
    ) -> None:
        """验证服务读回映射仅作诊断，不能取代提交前的事务内校验。"""

        if not isinstance(raw_executions, (tuple, list)):
            raise AnalysisTaskCommandAdapterError("accepted批次必须返回execution序列")
        if len(raw_executions) != len(expected_executions):
            raise AnalysisTaskCommandAdapterError("accepted批次execution数量不一致")
        for raw, expected in zip(raw_executions, expected_executions):
            if not isinstance(raw, Mapping):
                raise AnalysisTaskCommandAdapterError("accepted批次execution必须是Mapping")
            if (
                raw.get("execution_id") != expected.task_id.value
                or raw.get("business_type") != _ANALYSIS_BUSINESS_TYPE
                or raw.get("business_key") != expected.file_name
                or raw.get("batch_id") != expected.batch_id
                or raw.get("batch_sequence") != expected.batch_sequence
            ):
                raise AnalysisTaskCommandAdapterError("accepted批次execution顺序或身份不一致")

    def _decode_execution(
        self,
        raw: object,
    ) -> TaskExecutionSnapshot[AnalysisTaskInputV1]:
        """从 SQLite 行严格恢复 Worker 所需快照，拒绝历史无批次 file execution。"""

        if not isinstance(raw, Mapping):
            raise AnalysisTaskSnapshotCorruptedError(
                "Analysis execution必须是Mapping"
            )
        try:
            task_id = TaskId(_required_text(raw.get("execution_id"), name="execution_id"))
            business_type = _required_text(
                raw.get("business_type"),
                name="business_type",
            )
            business_key = _required_text(raw.get("business_key"), name="business_key")
            if business_type != _ANALYSIS_BUSINESS_TYPE:
                raise AnalysisTaskCommandAdapterError("execution不属于文件分析业务")
            batch_id = _required_text(raw.get("batch_id"), name="batch_id")
            batch_sequence = raw.get("batch_sequence")
            dispatch_sequence = raw.get("dispatch_sequence")
            if (
                isinstance(dispatch_sequence, bool)
                or not isinstance(dispatch_sequence, int)
                or dispatch_sequence < 1
            ):
                raise AnalysisTaskCommandAdapterError("Analysis execution缺少有效dispatch_sequence")
            execution_ref = AnalysisExecutionRef(
                task_id=task_id,
                file_name=business_key,
                batch_id=batch_id,
                batch_sequence=batch_sequence,  # type: ignore[arg-type]
            )
            schema_version = raw.get("input_schema_version")
            if schema_version not in {
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1,
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
            }:
                raise AnalysisTaskCommandAdapterError("Analysis execution输入Schema不受支持")
            payload = raw.get("input_payload")
            if not isinstance(payload, Mapping):
                raise AnalysisTaskCommandAdapterError("Analysis execution输入必须是Mapping")
            task_input = AnalysisTaskInputCodec.decode(
                payload,
                expected_task_id=task_id.value,
                expected_business_key=business_key,
                expected_batch_id=execution_ref.batch_id,
            )
            if task_input.batch_sequence != execution_ref.batch_sequence:
                raise AnalysisTaskCommandAdapterError("Analysis输入与execution批内序号不一致")
            accepted_at = _required_text(raw.get("created_at"), name="created_at")
            trace_id = _required_text(raw.get("trace_id"), name="trace_id")
            if (
                task_input.accepted_at != accepted_at
                or task_input.trace_id != trace_id
            ):
                raise AnalysisTaskCommandAdapterError("Analysis输入与execution受理事实不一致")
            return TaskExecutionSnapshot(
                task_id=task_id,
                task_type=_ANALYSIS_BUSINESS_TYPE,
                business_ref=TaskBusinessRef(business_type, business_key),
                execution_state=_required_text(
                    raw.get("execution_state"),
                    name="execution_state",
                ),
                public_status=_required_text(
                    raw.get("public_status"),
                    name="public_status",
                ),
                progress=raw.get("progress"),
                message=raw.get("message"),
                input_snapshot=task_input,
                accepted_at=accepted_at,
                trace_id=trace_id,
            )
        except (
            AnalysisTaskCommandAdapterError,
            AnalysisTaskInputCodecError,
            TypeError,
            ValueError,
        ) as exc:
            raise AnalysisTaskSnapshotCorruptedError(
                "Analysis execution数据无效"
            ) from exc

    @staticmethod
    def _to_execution_ref(
        execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
    ) -> AnalysisExecutionRef:
        """从已验证 execution 快照投影最小内部身份。"""

        if not isinstance(execution.input_snapshot, AnalysisTaskInputV1):
            raise AnalysisTaskCommandAdapterError("Analysis execution缺少V1输入快照")
        snapshot = execution.input_snapshot
        return AnalysisExecutionRef(
            task_id=execution.task_id,
            file_name=snapshot.file_name,
            batch_id=snapshot.batch_id,
            batch_sequence=snapshot.batch_sequence,
        )

    def _require_current_analysis_execution(
        self,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
    ) -> AnalysisExecutionRef:
        """以窄控制面身份守住 expected 写，不重复解码完整领域树快照。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if business_ref.business_type != _ANALYSIS_BUSINESS_TYPE:
            raise ValueError("business_ref.business_type 必须是 file")
        execution_ref = self._load_analysis_control_ref(task_id)
        if execution_ref is None:
            raise AnalysisTaskCommandAdapterError("Analysis execution不存在")
        if (
            execution_ref.file_name != business_ref.business_key
            or business_ref.business_type != _ANALYSIS_BUSINESS_TYPE
        ):
            raise AnalysisTaskCommandAdapterError("Analysis execution业务键不一致")
        return execution_ref

    def _load_analysis_control_ref(
        self,
        task_id: TaskId,
        *,
        require_supported_schema: bool = True,
    ) -> AnalysisExecutionRef | None:
        """读取不含 input_payload 的最小批次身份，并拒绝旧 file 兼容记录。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        raw = self._task_service.get_analysis_task_execution_control_record(
            task_id.value
        )
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise AnalysisTaskSnapshotCorruptedError(
                "Analysis控制面记录必须是Mapping"
            )
        try:
            if (
                _required_text(raw.get("execution_id"), name="execution_id")
                != task_id.value
            ):
                raise AnalysisTaskCommandAdapterError(
                    "Analysis控制面任务身份不一致"
                )
            business_type = _required_text(
                raw.get("business_type"),
                name="business_type",
            )
            if business_type != _ANALYSIS_BUSINESS_TYPE:
                raise AnalysisTaskCommandAdapterError(
                    "控制面记录不属于文件分析业务"
                )
            if (
                require_supported_schema
                and raw.get("input_schema_version")
                not in {
                    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1,
                    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
                    ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
                }
            ):
                raise AnalysisTaskCommandAdapterError(
                    "Analysis控制面输入Schema不受支持"
                )
            dispatch_sequence = raw.get("dispatch_sequence")
            if (
                isinstance(dispatch_sequence, bool)
                or not isinstance(dispatch_sequence, int)
                or dispatch_sequence < 1
            ):
                raise AnalysisTaskCommandAdapterError(
                    "Analysis控制面缺少有效dispatch_sequence"
                )
            _required_text(
                raw.get("execution_state"),
                name="execution_state",
            )
            return AnalysisExecutionRef(
                task_id=task_id,
                file_name=_required_text(
                    raw.get("business_key"),
                    name="business_key",
                ),
                batch_id=_required_text(raw.get("batch_id"), name="batch_id"),
                batch_sequence=raw.get("batch_sequence"),  # type: ignore[arg-type]
            )
        except (
            AnalysisTaskCommandAdapterError,
            TypeError,
            ValueError,
        ) as exc:
            raise AnalysisTaskSnapshotCorruptedError(
                "Analysis控制面身份数据无效"
            ) from exc


__all__ = (
    "AnalysisTaskCommandAdapterError",
    "AnalysisTaskSnapshotCorruptedError",
    "SQLiteAnalysisBatchCommandAdapter",
)
