"""Task Service 测试造数与当前 Analysis 受理辅助函数。

阶段 1G-5 删除了 ``LLMTaskService`` 上仅供旧调用方使用的文件任务创建方法。大量
Task/Callback 测试仍需要构造“只有 ``llm_tasks`` 公开投影、没有新版 execution”的
历史记录，以验证迁移兼容和失败恢复。这里把这种行为明确限制在测试目录，避免为了测试
造数而在生产 Service 上永久保留一个可被误用的兼容 API。

凡是验证当前并发受理、整批回滚或 Callback Guard 的测试，必须使用
``admit_analysis_tasks``，由现行 ``create_analysis_batch_if_allowed`` 事务入口写入；不能
使用 ``seed_legacy_file_task`` 证明当前受理能力。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.modules.analysis.adapters import (
    SQLiteAnalysisBatchCommandAdapter,
    SQLiteAnalysisCallbackAdapter,
    SQLiteAnalysisCallbackRecoverySource,
)
from app.modules.analysis.application import (
    AnalysisTaskCompletion,
    RecoverAnalysisCallbackSynchronously,
)
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisBatchCommand,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
)
from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import ExpectedTaskCompletion
from app.services.llm_service.task_service import (
    AnalysisBatchTaskAdmission,
    LLMTaskService,
    TaskAlreadyProcessingError,
)


def seed_legacy_file_task(
    service: LLMTaskService,
    file_name: str,
    request_payload: Mapping[str, Any],
    status: str = "1",
) -> dict[str, Any]:
    """仅为测试写入旧式文件任务公开投影。

    该辅助函数有意调用 Service 的通用内部投影写入原语，使历史兼容测试仍能覆盖
    “数据库中没有新版 execution”的真实升级场景。它不提供并发受理保证，也不得被
    App、脚本或生产代码导入。
    """

    if not isinstance(service, LLMTaskService):
        raise TypeError("service 必须是 LLMTaskService")
    normalized_name = str(file_name or "").strip()
    if not normalized_name:
        raise ValueError("file_name 不能为空")
    if not isinstance(request_payload, Mapping):
        raise TypeError("request_payload 必须是 Mapping")
    normalized_status = str(status)
    if normalized_status not in {"0", "1"}:
        raise ValueError("历史文件任务初始状态只能是 0 或 1")
    return service._upsert_task(  # noqa: SLF001 - 测试专用历史数据构造边界
        "file",
        normalized_name,
        dict(request_payload),
        status=normalized_status,
    )


def admit_analysis_tasks(
    service: LLMTaskService,
    file_tasks: Sequence[tuple[str, Mapping[str, Any], str]],
) -> list[dict[str, Any]]:
    """通过现行 Analysis 批量事务入口受理测试任务。

    返回值继续采用 ``llm_tasks`` 公开投影形状，便于原有断言聚焦受理结果；内部同时写入
    execution、批次序号和不可变输入身份。冲突结果映射为既有领域异常，只用于减少测试
    改造噪声，生产 Application 仍按有限 outcome 枚举完成 HTTP 映射。
    """

    if not isinstance(service, LLMTaskService):
        raise TypeError("service 必须是 LLMTaskService")
    normalized_tasks = tuple(file_tasks)
    if not normalized_tasks:
        raise ValueError("file_tasks 不能为空")

    batch_id = uuid4().hex
    accepted_at = datetime.now(timezone.utc).isoformat()
    trace_id = f"test-{uuid4().hex}"
    admissions: list[AnalysisBatchTaskAdmission] = []
    for sequence, (file_name, request_payload, status) in enumerate(
        normalized_tasks,
        start=1,
    ):
        normalized_name = str(file_name or "").strip()
        if not normalized_name:
            raise ValueError("file_name 不能为空")
        if not isinstance(request_payload, Mapping):
            raise TypeError("request_payload 必须是 Mapping")
        projection = dict(request_payload)
        projection["businessType"] = "file"
        params = projection.get("params")
        if not (
            isinstance(params, list)
            and len(params) == 1
            and isinstance(params[0], Mapping)
        ):
            projection["params"] = [{"fileName": normalized_name}]
        else:
            normalized_param = dict(params[0])
            normalized_param["fileName"] = normalized_name
            projection["params"] = [normalized_param]

        execution_id = uuid4().hex
        admissions.append(
            AnalysisBatchTaskAdmission(
                execution_id=execution_id,
                business_key=normalized_name,
                input_schema_version=4,
                input_payload={
                    "task_id": execution_id,
                    "batch_id": batch_id,
                    "batch_sequence": sequence,
                    "file_name": normalized_name,
                    "accepted_at": accepted_at,
                    "trace_id": trace_id,
                },
                projection_request_payload=projection,
                initial_public_status=str(status),
                trace_id=trace_id,
            )
        )

    result = service.create_analysis_batch_if_allowed(
        batch_id=batch_id,
        admissions=tuple(admissions),
        accepted_at=accepted_at,
    )
    outcome = str(result.get("outcome") or "")
    if outcome != "accepted":
        first_name = admissions[0].business_key
        current = service.get_task("file", first_name) or {}
        reason_is_callback = outcome in {
            "callback_pending",
            "callback_sending",
            "callback_outcome_unknown",
        }
        raise TaskAlreadyProcessingError(
            first_name,
            str(current.get("status") or ""),
            "pending" if reason_is_callback else str(
                current.get("callback_status") or ""
            ),
            callback_delivery_in_flight=outcome == "callback_sending",
        )

    snapshots: list[dict[str, Any]] = []
    for admission in admissions:
        snapshot = service.get_task("file", admission.business_key)
        if snapshot is None:
            raise AssertionError("现行 Analysis 受理成功后缺少公开任务投影")
        snapshots.append(snapshot)
    return snapshots


def admit_analysis_task(
    service: LLMTaskService,
    file_name: str,
    request_payload: Mapping[str, Any],
    status: str = "1",
) -> dict[str, Any]:
    """通过现行批量入口受理一个 Analysis 测试任务。"""

    return admit_analysis_tasks(
        service,
        ((file_name, request_payload, status),),
    )[0]


def build_analysis_callback_recovery(
    service: LLMTaskService,
    *,
    callback_url: str,
    transport: Callable[
        [AnalysisCallbackDeliveryRequest], AnalysisCallbackDelivery
    ]
    | None = None,
) -> RecoverAnalysisCallbackSynchronously:
    """为测试构造与生产同构的 Analysis 同步回调恢复链。

    默认 Transport 返回严格成功，且禁用非权威回调历史落盘，保证离线测试不访问网络、
    不写运行目录。需要验证并发阻塞或失败分类时，调用方可注入强类型 Transport。
    """

    if transport is None:
        def delivered(
            request: AnalysisCallbackDeliveryRequest,
        ) -> AnalysisCallbackDelivery:
            return AnalysisCallbackDelivery(
                execution=request.lease.execution,
                lease_token=request.lease.lease_token,
                lease_version=request.lease.lease_version,
                outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
            )

        effective_transport = delivered
    else:
        effective_transport = transport

    callbacks = SQLiteAnalysisCallbackAdapter(
        service,
        callback_timeout=5.0,
        lease_seconds=5.0,
        transport=effective_transport,
        history_writer=lambda _payload, *, callback_context: None,
    )
    return RecoverAnalysisCallbackSynchronously(
        source=SQLiteAnalysisCallbackRecoverySource(service),
        callbacks=callbacks,
        callback_url=callback_url,
    )


def create_terminal_analysis_task(
    service: LLMTaskService,
    file_name: str,
    *,
    result_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """经现行 Adapter 完整执行受理、领取和终态提交，构造可恢复任务。

    Callback Guard 只允许已提交终态的 latest execution 获取发送权；直接更新公开投影
    无法满足这个不变量。因此需要验证同步恢复的测试必须使用本函数，不能通过 SQL 或
    ``mark_business_result`` 伪造终态。
    """

    normalized_name = str(file_name or "").strip()
    if not normalized_name:
        raise ValueError("file_name 不能为空")
    raw_params = {
        "fileName": normalized_name,
        "filePath": f"https://example.invalid/{normalized_name}",
    }
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    command = AnalysisBatchCommand(
        request_projection=FrozenJsonObject.from_mapping(
            {"businessType": "file", "params": [raw_params]},
            name="analysis_callback_fixture_request",
        ),
        submissions=(submission,),
        trace_id=f"test-callback-{uuid4().hex}",
    )
    adapter = SQLiteAnalysisBatchCommandAdapter(service)
    admission = adapter.create_batch_if_allowed(command)
    execution = admission.executions[0]
    claimed = adapter.claim(execution.task_id)
    if claimed.execution is None:
        raise AssertionError("Analysis 测试任务未能领取")

    public_data = {
        "fileName": normalized_name,
        "status": "2",
        **dict(result_data or {}),
    }
    # fileName/status 是公开身份与终态，调用方附加字段不得覆盖这两个固定值。
    public_data["fileName"] = normalized_name
    public_data["status"] = "2"
    callback_payload = FrozenJsonObject.from_mapping(
        {
            "businessType": "file",
            "data": public_data,
            "msg": "解析完成",
        },
        name="analysis_callback_fixture_payload",
    )
    completed = adapter.finish_if_current(
        ExpectedTaskCompletion(
            expected_task_id=execution.task_id,
            business_ref=TaskBusinessRef("file", normalized_name),
            execution_state="succeeded",
            public_status="2",
            message="解析完成",
            result=AnalysisTaskCompletion(
                callback_payload=callback_payload,
                succeeded=True,
                mapped_result=FrozenJsonObject.from_mapping(
                    {"architectureId": 1},
                    name="analysis_callback_fixture_result",
                ),
            ),
        )
    )
    if not completed:
        raise AssertionError("Analysis 测试任务未能提交终态")
    task = service.get_task("file", normalized_name)
    if task is None:
        raise AssertionError("Analysis 测试任务缺少公开投影")
    return task


__all__ = (
    "admit_analysis_task",
    "admit_analysis_tasks",
    "build_analysis_callback_recovery",
    "create_terminal_analysis_task",
    "seed_legacy_file_task",
)
