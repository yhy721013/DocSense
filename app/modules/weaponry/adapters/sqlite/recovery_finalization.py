"""Weaponry Recovery 终态的同事务结果快照预检。"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.modules.tasks.domain import RecoveryDecisionKind, TaskRecoveryDecision, TaskState
from app.modules.tasks.ports import TaskRecoverySnapshot
from app.modules.weaponry.domain import (
    WEAPONRY_FAILURE_MESSAGE,
    WEAPONRY_STATUS_FAILED,
    WEAPONRY_STATUS_SUCCEEDED,
)


class SQLiteWeaponryRecoveryFinalizationPreflight:
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
            snapshot.task.task_type != "weaponry"
            or decision.kind is not RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT
            or projection is None
            or projection.source_step_key != "result.map"
            or projection.checkpoint_code != "weaponry_result_mapped_v1"
        ):
            return False
        source = next((item for item in snapshot.steps if item.step_key == "result.map"), None)
        row = self._connection.execute(
            """
            SELECT business_key, result_schema_version, callback_payload_json,
                   result_digest
            FROM weaponry_result_snapshots WHERE task_id = ?
            """,
            (decision.task_id.value,),
        ).fetchone()
        if source is None or source.checkpoint is None or row is None:
            return False
        serialized = str(row["callback_payload_json"])
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        try:
            payload = json.loads(serialized)
            data = payload["data"]
            status = data["status"]
            message = payload["msg"]
        except (TypeError, ValueError, KeyError):
            return False
        expected_state = (
            TaskState.SUCCEEDED
            if status == WEAPONRY_STATUS_SUCCEEDED
            else TaskState.FAILED
            if status == WEAPONRY_STATUS_FAILED
            else None
        )
        expected_message = (
            "解析完成"
            if status == WEAPONRY_STATUS_SUCCEEDED
            else WEAPONRY_FAILURE_MESSAGE
        )
        expected_ref = f"weaponry-result:v1:{digest}"
        return bool(
            int(row["result_schema_version"]) == 1
            and row["business_key"] == snapshot.task.business_ref.business_key
            and row["result_digest"] == digest == projection.checkpoint_digest
            and source.checkpoint.result_ref == expected_ref
            and projection.result_ref == expected_ref
            and decision.terminal_state is expected_state
            and projection.public_status == status
            and projection.message == expected_message
            and isinstance(message, str)
        )


__all__ = ["SQLiteWeaponryRecoveryFinalizationPreflight"]
