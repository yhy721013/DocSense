"""业务组件续跑快照的 SQLite 一致性预检骨架。"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import sqlite3

from app.modules.tasks.domain import RecoveryDecisionKind, TaskRecoveryDecision
from app.modules.tasks.ports import TaskRecoverySnapshot
from app.modules.tasks.ports.step_continuation import canonical_continuation_json


_ALLOWED_TABLES = frozenset(
    {
        "report_step_continuation_snapshots",
        "weaponry_step_continuation_snapshots",
        "analysis_step_continuation_snapshots",
    }
)


class SQLiteTaskRecoveryResumePreflight:
    """核验快照、source Authority 与根输入摘要；不执行文件或网络 I/O。"""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        table_name: str,
        supports_step: Callable[[str], bool],
    ) -> None:
        if table_name not in _ALLOWED_TABLES:
            raise ValueError("续跑预检表未登记")
        if not callable(supports_step):
            raise TypeError("supports_step 必须可调用")
        self._connection = connection
        self._table_name = table_name
        self._supports_step = supports_step

    def verify(
        self,
        snapshot: TaskRecoverySnapshot,
        decision: TaskRecoveryDecision,
    ) -> bool:
        if decision.kind is not RecoveryDecisionKind.RETRY_AUTHORIZED:
            return False
        resolution = decision.step_resolution
        if resolution is None or not self._supports_step(resolution.source_step_key):
            return False
        row = self._connection.execute(
            f"""
            SELECT task_attempt_no, task_fencing_token,
                   input_payload_fingerprint, payload_json, payload_digest
              FROM {self._table_name}
             WHERE task_id = ? AND step_key = ? AND step_attempt_no = ?
            """,
            (
                decision.task_id.value,
                resolution.source_step_key,
                resolution.source_step_attempt_no,
            ),
        ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(str(row[3]))
            canonical = canonical_continuation_json(payload)
            payload_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        input_row = self._connection.execute(
            "SELECT input_payload FROM llm_task_executions WHERE execution_id = ?",
            (decision.task_id.value,),
        ).fetchone()
        if input_row is None:
            return False
        try:
            input_payload = json.loads(str(input_row[0]))
            input_canonical = json.dumps(
                input_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            input_digest = hashlib.sha256(input_canonical.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            int(row[0]) == decision.source_attempt_no
            and int(row[1]) == decision.source_fencing_token
            and str(row[2]) == input_digest
            and str(row[4]) == payload_digest
            and snapshot.task.task_id == decision.task_id
        )


__all__ = ["SQLiteTaskRecoveryResumePreflight"]
