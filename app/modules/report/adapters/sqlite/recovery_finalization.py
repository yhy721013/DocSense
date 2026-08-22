"""Report Recovery 终态的同事务业务结果预检。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from app.modules.report.application.artifact_identity import report_artifact_result_ref
from app.modules.report.domain import REPORT_STATUS_SUCCEEDED
from app.modules.tasks.domain import RecoveryDecisionKind, TaskRecoveryDecision, TaskState
from app.modules.tasks.ports import TaskRecoverySnapshot


class SQLiteReportRecoveryFinalizationPreflight:
    """仅核验 SQLite 资源记录；最终 Artifact 文件完整性必须先形成 Observation。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        self._connection = connection
        self._connection.row_factory = sqlite3.Row

    def verify(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool:
        projection = decision.terminal_projection
        if (
            snapshot.task.task_type != "report"
            or decision.kind is not RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT
            or decision.terminal_state is not TaskState.SUCCEEDED
            or projection is None
            or projection.source_step_key != "artifact.publish"
            or projection.checkpoint_code != "artifact_published_v1"
            or projection.public_status != REPORT_STATUS_SUCCEEDED
            or projection.message != "报告生成完成"
        ):
            return False
        source = next(
            (item for item in snapshot.steps if item.step_key == "artifact.publish"),
            None,
        )
        row = self._connection.execute(
            """
            SELECT business_type, business_key, record_payload
            FROM report_resource_records WHERE execution_id = ?
            """,
            (decision.task_id.value,),
        ).fetchone()
        if source is None or source.checkpoint is None or row is None:
            return False
        try:
            payload = json.loads(str(row["record_payload"]))
        except (TypeError, ValueError):
            return False
        artifact = payload.get("final_artifact") if isinstance(payload, dict) else None
        if not isinstance(artifact, Mapping):
            return False
        checksum = str(artifact.get("checksum") or "").strip().lower()
        return bool(
            row["business_type"] == "report"
            and row["business_key"] == snapshot.task.business_ref.business_key
            and artifact.get("task_id") == decision.task_id.value
            and artifact.get("category") == "report_html"
            and isinstance(artifact.get("size_bytes"), int)
            and artifact.get("size_bytes") >= 0
            and len(checksum) == 64
            and checksum == projection.checkpoint_digest
            and source.checkpoint.result_ref == artifact.get("artifact_id")
            and projection.result_ref == report_artifact_result_ref(artifact)
        )


__all__ = ["SQLiteReportRecoveryFinalizationPreflight"]
