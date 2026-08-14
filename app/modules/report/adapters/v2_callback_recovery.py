"""从 Task Control v2 与 Report Artifact 重建同步 Callback 候选。"""

from __future__ import annotations

import logging

from app.modules.report.domain import (
    REPORT_STATUS_FAILED,
    REPORT_STATUS_SUCCEEDED,
    REPORT_TERMINAL_STATUSES,
    ReportId,
    build_report_callback,
)
from app.modules.report.ports import (
    ReportArtifactPort,
    ReportCallbackRecoveryCandidate,
    ReportResourceStorePort,
)
from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import TaskReadPort


logger = logging.getLogger(__name__)
_RECOVERABLE_CALLBACK_STATUSES = frozenset(
    {"pending", "failed", "outcome_unknown"}
)


class SQLiteReportV2CallbackRecoverySource:
    """只读取 v2 latest；成功正文从经校验的终态 Artifact 精确恢复。

    根 Task 仅保存稳定 result_ref，不复制大型 HTML。这里按资源记录持有的完整 Artifact
    元数据读取正文；缺失、篡改或非法 UTF-8 均失败关闭，绝不发送内部结果 Schema。
    """

    def __init__(
        self,
        *,
        task_reader: TaskReadPort,
        resources: ReportResourceStorePort,
        artifacts: ReportArtifactPort,
    ) -> None:
        if not isinstance(task_reader, TaskReadPort):
            raise TypeError("task_reader 必须实现 TaskReadPort")
        if not isinstance(resources, ReportResourceStorePort):
            raise TypeError("resources 必须实现 ReportResourceStorePort")
        if not isinstance(artifacts, ReportArtifactPort):
            raise TypeError("artifacts 必须实现 ReportArtifactPort")
        self._tasks = task_reader
        self._resources = resources
        self._artifacts = artifacts

    def load_recoverable(
        self,
        report_id: ReportId,
    ) -> ReportCallbackRecoveryCandidate | None:
        if not isinstance(report_id, ReportId):
            raise TypeError("report_id 必须是 ReportId")
        snapshot = self._tasks.get_latest(
            TaskBusinessRef("report", report_id.business_key)
        )
        if snapshot is None:
            return None
        if snapshot.public_status not in REPORT_TERMINAL_STATUSES:
            return None
        if snapshot.callback_status not in _RECOVERABLE_CALLBACK_STATUSES:
            return None
        if snapshot.public_status == REPORT_STATUS_SUCCEEDED:
            resource = self._resources.get(snapshot.task_id)
            if resource is None or resource.final_artifact is None:
                raise RuntimeError("成功 Report 缺少最终 Artifact 资源事实")
            details = self._artifacts.load_report_html(resource.final_artifact)
        elif snapshot.public_status == REPORT_STATUS_FAILED:
            details = ""
        else:  # pragma: no cover - REPORT_TERMINAL_STATUSES 已封闭值域
            raise RuntimeError("Report 公开终态值域无效")
        payload = build_report_callback(
            report_id,
            details,
            status=snapshot.public_status,
        )
        logger.debug(
            "已从 v2 控制面加载 Report Callback 恢复候选: task_id=%s "
            "report_id=%s callback_status=%s",
            snapshot.task_id,
            report_id.public_value,
            snapshot.callback_status,
        )
        return ReportCallbackRecoveryCandidate(
            task_id=snapshot.task_id,
            report_id=report_id,
            payload=payload,
            callback_attempts=snapshot.callback_attempts,
        )


__all__ = ["SQLiteReportV2CallbackRecoverySource"]
