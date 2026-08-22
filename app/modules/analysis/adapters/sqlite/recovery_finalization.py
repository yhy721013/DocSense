"""Analysis/file Recovery 终态的同事务结果快照预检。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.modules.tasks.domain import RecoveryDecisionKind, TaskRecoveryDecision, TaskState
from app.modules.tasks.ports import TaskRecoverySnapshot


class SQLiteAnalysisRecoveryFinalizationPreflight:
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
            snapshot.task.task_type != "file"
            or decision.kind is not RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT
            or projection is None
            or projection.source_step_key != "result.snapshot"
            or projection.checkpoint_code != "analysis_result_snapshot_v1"
        ):
            return False
        source = next(
            (item for item in snapshot.steps if item.step_key == "result.snapshot"),
            None,
        )
        row = self._connection.execute(
            """
            SELECT business_key, result_schema_version, callback_payload_json,
                   result_digest
            FROM analysis_result_snapshots WHERE task_id = ?
            """,
            (decision.task_id.value,),
        ).fetchone()
        if source is None or source.checkpoint is None or row is None:
            return False
        serialized = str(row["callback_payload_json"])
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        try:
            payload = json.loads(serialized)
            status = payload["data"]["status"]
        except (TypeError, ValueError, KeyError):
            return False
        expected_state = (
            TaskState.SUCCEEDED if status == "2" else TaskState.FAILED if status == "3" else None
        )
        expected_message = "解析完成" if status == "2" else None
        expected_ref = f"analysis-result:v1:{digest}"
        return bool(
            int(row["result_schema_version"]) == 1
            and row["business_key"] == snapshot.task.business_ref.business_key
            and row["result_digest"] == digest == projection.checkpoint_digest
            and source.checkpoint.result_ref == expected_ref
            and projection.result_ref == expected_ref
            and decision.terminal_state is expected_state
            and projection.public_status == status
            and (
                projection.message == expected_message
                if expected_message is not None
                else bool(projection.message)
            )
        )


__all__ = ["SQLiteAnalysisRecoveryFinalizationPreflight"]
