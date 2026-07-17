"""从兼容 SQLite latest 投影恢复报告同步回调候选。"""

from __future__ import annotations

from collections.abc import Mapping
import logging

from app.modules.report.domain import (
    REPORT_TERMINAL_STATUSES,
    ReportCallbackPayload,
    ReportId,
)
from app.modules.report.ports import (
    ReportCallbackRecoveryCandidate,
    ReportCallbackRecoverySourcePort,
)
from app.modules.tasks.domain import TaskId
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)
_REPORT_BUSINESS_TYPE = "report"
_RECOVERABLE_CALLBACK_STATUSES = frozenset({"pending", "failed"})


class SQLiteReportCallbackRecoverySource(ReportCallbackRecoverySourcePort):
    """只读取 latest 公共投影，不把 execution 内部结果误发给甲方。"""

    def __init__(self, task_service: LLMTaskService) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        self._task_service = task_service

    def load_recoverable(
        self,
        report_id: ReportId,
    ) -> ReportCallbackRecoveryCandidate | None:
        if not isinstance(report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        task = self._task_service.get_task(
            _REPORT_BUSINESS_TYPE,
            report_id.business_key,
        )
        if task is None:
            return None
        if task.get("status") not in REPORT_TERMINAL_STATUSES:
            return None
        if task.get("callback_status") not in _RECOVERABLE_CALLBACK_STATUSES:
            return None

        execution_id = task.get("execution_id")
        payload = task.get("result_payload")
        if not isinstance(payload, Mapping):
            raise RuntimeError("可恢复报告缺少公开回调载荷")
        if payload.get("businessType") != _REPORT_BUSINESS_TYPE:
            raise RuntimeError("报告公开回调载荷 businessType 无效")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise RuntimeError("报告公开回调载荷 data 无效")
        payload_report_id = ReportId.from_public_value(data.get("reportId"))  # type: ignore[arg-type]
        if payload_report_id != report_id:
            raise RuntimeError("报告公开回调载荷与 latest 业务键不一致")
        callback_payload = ReportCallbackPayload(
            report_id=payload_report_id,
            status=data.get("status"),  # type: ignore[arg-type]
            details=data.get("details"),  # type: ignore[arg-type]
            message=payload.get("msg"),  # type: ignore[arg-type]
        )
        candidate = ReportCallbackRecoveryCandidate(
            task_id=TaskId(execution_id),  # type: ignore[arg-type]
            report_id=report_id,
            payload=callback_payload,
        )
        logger.debug(
            "已加载报告同步回调恢复候选: task_id=%s report_id=%s callback_status=%s",
            candidate.task_id,
            report_id.public_value,
            task.get("callback_status"),
        )
        return candidate


__all__ = ["SQLiteReportCallbackRecoverySource"]
