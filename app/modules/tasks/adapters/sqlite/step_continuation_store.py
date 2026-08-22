"""业务组件续跑快照 SQLite Store 的严格公共实现骨架。"""

from __future__ import annotations

import json
import sqlite3

from app.modules.tasks.domain import TaskExecutionAuthority, TaskId
from app.modules.tasks.ports import (
    TaskStepContinuationDraft,
    TaskStepContinuationSnapshot,
    require_persisted_utc,
)


_ALLOWED_TABLES = frozenset(
    {
        "report_step_continuation_snapshots",
        "weaponry_step_continuation_snapshots",
        "analysis_step_continuation_snapshots",
    }
)


class SQLiteTaskStepContinuationStore:
    """只向一个构造期白名单表追加快照，不提供更新或删除入口。"""

    def __init__(self, connection: sqlite3.Connection, *, table_name: str) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection 必须是 sqlite3.Connection")
        if table_name not in _ALLOWED_TABLES:
            raise ValueError("续跑快照表未登记")
        self._connection = connection
        self._table_name = table_name

    def save(
        self,
        *,
        authority: TaskExecutionAuthority,
        step_key: str,
        step_attempt_no: int,
        source_step_attempt_no: int,
        draft: TaskStepContinuationDraft,
        created_at: str,
    ) -> TaskStepContinuationSnapshot:
        if not isinstance(authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if not isinstance(step_key, str) or not step_key.strip():
            raise ValueError("step_key 不能为空")
        if type(step_attempt_no) is not int or step_attempt_no <= 0:
            raise ValueError("step_attempt_no 必须是正整数")
        if type(source_step_attempt_no) is not int or source_step_attempt_no < 0:
            raise ValueError("source_step_attempt_no 必须是非负整数")
        if not isinstance(draft, TaskStepContinuationDraft):
            raise TypeError("draft 必须是 TaskStepContinuationDraft")
        timestamp = require_persisted_utc(created_at, name="created_at")
        normalized_key = step_key.strip()

        # 快照必须和同一事务刚刚建立的 running Step Attempt、当前 Task Authority 完整绑定。
        # 即使未来误用 Store，也不能给旧 fencing 或未开始的 Step 补造续跑依据。
        row = self._connection.execute(
            """
            SELECT 1
              FROM llm_task_executions AS task
              JOIN task_attempts AS attempt
                ON attempt.task_id = task.execution_id
               AND attempt.attempt_no = task.current_attempt_no
              JOIN task_steps AS step
                ON step.task_id = task.execution_id
               AND step.step_key = ?
              JOIN task_step_attempts AS step_attempt
                ON step_attempt.task_id = step.task_id
               AND step_attempt.step_key = step.step_key
               AND step_attempt.step_attempt_no = step.current_step_attempt_no
             WHERE task.execution_id = ?
               AND task.execution_state = 'running'
               AND task.current_attempt_no = ?
               AND task.fencing_token = ?
               AND attempt.lease_token = ?
               AND attempt.state = 'running'
               AND step.state = 'running'
               AND step.current_step_attempt_no = ?
               AND step_attempt.state = 'running'
            """,
            (
                normalized_key,
                authority.task_id.value,
                authority.attempt_no,
                authority.fencing_token,
                authority.lease_token,
                step_attempt_no,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeError("续跑快照写入时 Task/Step Authority 已失效")

        payload_json = draft.payload_json
        try:
            self._connection.execute(
                f"""
                INSERT INTO {self._table_name} (
                    task_id, step_key, step_attempt_no,
                    task_attempt_no, task_fencing_token,
                    source_step_attempt_no, snapshot_schema_version,
                    input_payload_fingerprint, execution_profile_fingerprint,
                    predecessor_checkpoint_digest, payload_json,
                    payload_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    authority.task_id.value,
                    normalized_key,
                    step_attempt_no,
                    authority.attempt_no,
                    authority.fencing_token,
                    source_step_attempt_no,
                    draft.schema_version,
                    draft.input_payload_fingerprint,
                    draft.execution_profile_fingerprint,
                    draft.predecessor_checkpoint_digest,
                    payload_json,
                    draft.payload_digest,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("续跑快照已存在或身份约束不成立") from exc
        return TaskStepContinuationSnapshot(
            task_id=authority.task_id,
            step_key=normalized_key,
            step_attempt_no=step_attempt_no,
            task_attempt_no=authority.attempt_no,
            task_fencing_token=authority.fencing_token,
            source_step_attempt_no=source_step_attempt_no,
            draft=draft,
            payload_digest=draft.payload_digest,
            created_at=timestamp,
        )

    def get(
        self,
        task_id: TaskId,
        step_key: str,
        step_attempt_no: int,
    ) -> TaskStepContinuationSnapshot | None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(step_key, str) or not step_key.strip():
            raise ValueError("step_key 不能为空")
        if type(step_attempt_no) is not int or step_attempt_no <= 0:
            raise ValueError("step_attempt_no 必须是正整数")
        row = self._connection.execute(
            f"""
            SELECT task_id, step_key, step_attempt_no,
                   task_attempt_no, task_fencing_token,
                   source_step_attempt_no, snapshot_schema_version,
                   input_payload_fingerprint, execution_profile_fingerprint,
                   predecessor_checkpoint_digest, payload_json,
                   payload_digest, created_at
              FROM {self._table_name}
             WHERE task_id = ? AND step_key = ? AND step_attempt_no = ?
            """,
            (task_id.value, step_key.strip(), step_attempt_no),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[10]))
        if not isinstance(payload, dict):  # pragma: no cover - Schema 与编码器双重门禁。
            raise RuntimeError("续跑快照 payload 不是 JSON object")
        draft = TaskStepContinuationDraft(
            schema_version=int(row[6]),
            input_payload_fingerprint=str(row[7]),
            execution_profile_fingerprint=str(row[8]),
            predecessor_checkpoint_digest=str(row[9]),
            payload=payload,
        )
        return TaskStepContinuationSnapshot(
            task_id=TaskId(str(row[0])),
            step_key=str(row[1]),
            step_attempt_no=int(row[2]),
            task_attempt_no=int(row[3]),
            task_fencing_token=int(row[4]),
            source_step_attempt_no=int(row[5]),
            draft=draft,
            payload_digest=str(row[11]),
            created_at=str(row[12]),
        )


__all__ = ["SQLiteTaskStepContinuationStore"]
