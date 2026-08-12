"""Report DTO 与通用 TaskCommand SQLite JSON 之间的严格 Codec。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.modules.tasks.ports import EncodedTaskResult, EncodedTaskSubmission
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import TaskSubmissionCommand

from app.modules.report.application import ReportTaskCompletion
from app.modules.report.domain import (
    REPORT_INPUT_SCHEMA_VERSION,
    ReportId,
    ReportInputSnapshot,
    ReportSubmission,
)


_REPORT_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "report_id_key",
        "public_report_id",
        "source_urls",
        "template_outline_url",
        "template_desc",
        "requirement",
        "accepted_at",
        "trace_id",
    }
)


class ReportTaskCommandCodec:
    """保存完整不可变报告输入，并生成兼容 ``llm_tasks`` 的请求投影。"""

    task_type = "report"

    def encode_submission(
        self,
        command: TaskSubmissionCommand[ReportSubmission],
        *,
        task_id: TaskId,
        accepted_at: str,
    ) -> EncodedTaskSubmission[ReportInputSnapshot]:
        if not isinstance(command, TaskSubmissionCommand):
            raise TypeError("command 必须是 TaskSubmissionCommand")
        if not isinstance(command.submission, ReportSubmission):
            raise TypeError("报告 Codec 只接受 ReportSubmission")
        if command.task_type != self.task_type:
            raise ValueError("报告命令 task_type 必须是 report")
        submission = command.submission
        if (
            command.business_ref.business_type != self.task_type
            or command.business_ref.business_key
            != submission.report_id.business_key
            or command.input_schema_version != REPORT_INPUT_SCHEMA_VERSION
            or command.trace_id != submission.trace_id
        ):
            raise ValueError("报告命令身份、Schema 或 trace 不一致")
        snapshot = ReportInputSnapshot.from_submission(
            submission,
            task_id=task_id.value,
            accepted_at=accepted_at,
            schema_version=command.input_schema_version,
        )
        input_payload = {
            "schema_version": snapshot.schema_version,
            "task_id": snapshot.task_id,
            "report_id_key": snapshot.report_id.business_key,
            "public_report_id": snapshot.report_id.public_value,
            "source_urls": list(snapshot.source_urls),
            "template_outline_url": snapshot.template_outline_url,
            "template_desc": snapshot.template_desc,
            "requirement": snapshot.requirement,
            "accepted_at": snapshot.accepted_at,
            "trace_id": snapshot.trace_id,
        }
        # llm_tasks 仍是旧查询/Progress 的最新投影，因此保留既有公开请求形状；该 JSON
        # 不作为 Worker 输入，Worker 只解码上面的 execution input_payload。
        projection_request = {
            "businessType": "report",
            "params": [
                {
                    "reportId": snapshot.report_id.public_value,
                    "filePathList": list(snapshot.source_urls),
                    "templateOutline": snapshot.template_outline_url,
                    "templateDesc": snapshot.template_desc,
                    "requirement": snapshot.requirement,
                }
            ],
        }
        return EncodedTaskSubmission(
            input_snapshot=snapshot,
            input_payload=input_payload,
            projection_request_payload=projection_request,
            initial_public_status="0",
            active_public_statuses=("0",),
        )

    def decode_input(
        self,
        *,
        schema_version: int,
        payload: Mapping[str, Any],
    ) -> ReportInputSnapshot:
        if schema_version != REPORT_INPUT_SCHEMA_VERSION:
            raise ValueError("不支持的报告输入 Schema 版本")
        if not isinstance(payload, Mapping):
            raise TypeError("payload 必须是 Mapping")
        if frozenset(payload.keys()) != _REPORT_INPUT_KEYS:
            raise ValueError("报告 input_payload 字段集合不完整或包含未知字段")
        payload_schema = payload.get("schema_version")
        if payload_schema != schema_version:
            raise ValueError("报告 input_payload Schema 与 execution 行不一致")
        public_report_id = payload.get("public_report_id")
        report_id_key = payload.get("report_id_key")
        report_id = ReportId(
            public_value=public_report_id,  # type: ignore[arg-type]
            business_key=report_id_key,  # type: ignore[arg-type]
        )
        return ReportInputSnapshot(
            schema_version=schema_version,
            task_id=payload.get("task_id"),  # type: ignore[arg-type]
            report_id=report_id,
            source_urls=payload.get("source_urls"),  # type: ignore[arg-type]
            template_outline_url=payload.get("template_outline_url"),  # type: ignore[arg-type]
            template_desc=payload.get("template_desc"),  # type: ignore[arg-type]
            requirement=payload.get("requirement"),  # type: ignore[arg-type]
            accepted_at=payload.get("accepted_at"),  # type: ignore[arg-type]
            trace_id=payload.get("trace_id"),  # type: ignore[arg-type]
        )

    def encode_result(self, result: ReportTaskCompletion) -> EncodedTaskResult:
        if not isinstance(result, ReportTaskCompletion):
            raise TypeError("报告 Codec 只接受 ReportTaskCompletion")
        report_artifact: dict[str, Any] | None = None
        if result.report_artifact is not None:
            report_artifact = {
                "task_id": result.report_artifact.task_id.value,
                "artifact_id": result.report_artifact.artifact_id,
                "category": result.report_artifact.category.value,
                "sequence_no": result.report_artifact.sequence_no,
                "size_bytes": result.report_artifact.size_bytes,
                "checksum": result.report_artifact.checksum,
            }
        # execution 只保存恢复执行所需的最小事实。完整模型响应已经由 RAG 审计持久化，
        # HTML 正文由最终 Artifact 持有；旧 llm_tasks 则只保存甲方原有 Callback 载荷。
        # 这样 check-task 恢复不会误发内部 Schema，也避免同一大型 HTML 在两张表重复。
        execution_result = {
            "schema_version": 1,
            "report_id": result.callback_payload.report_id.public_value,
            "status": result.callback_payload.status,
            "empty_rag_result": (
                result.report_result.empty_rag_result
                if result.report_result is not None
                else False
            ),
            "report_artifact": report_artifact,
        }
        return EncodedTaskResult(
            execution_result_payload=execution_result,
            projection_result_payload=result.callback_payload.to_public_dict(),
        )


__all__ = ["ReportTaskCommandCodec"]
