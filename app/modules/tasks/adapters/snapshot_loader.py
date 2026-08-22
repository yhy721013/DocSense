"""从只读 Task Control Query 解码冻结业务输入的通用 Adapter。"""

from __future__ import annotations

import hashlib
import json

from app.modules.tasks.domain import TaskExecutionSnapshot, TaskId
from app.modules.tasks.ports import (
    LoadedTaskExecutionInput,
    TaskCommandCodec,
    TaskControlQueryUnitOfWorkFactory,
)


class CodecTaskExecutionSnapshotLoader:
    """把持久 JSON 交给指定业务 Codec，禁止用当前环境默认值补字段。"""

    def __init__(
        self,
        *,
        query_uow_factory: TaskControlQueryUnitOfWorkFactory,
        codec: TaskCommandCodec[object, object, object],
    ) -> None:
        if not callable(query_uow_factory):
            raise TypeError("query_uow_factory 必须可调用")
        if not callable(getattr(codec, "decode_input", None)):
            raise TypeError("codec 必须实现 TaskCommandCodec")
        task_type = getattr(codec, "task_type", None)
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("codec.task_type 必须是非空 str")
        self._query_uow_factory = query_uow_factory
        self._codec = codec
        self._task_type = task_type.strip()

    def load(self, task_id: TaskId) -> LoadedTaskExecutionInput:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        with self._query_uow_factory() as unit_of_work:
            persisted = unit_of_work.queries.load_execution_input(task_id)
            recovery_steps = unit_of_work.queries.list_steps(task_id)
        if persisted is None:
            raise LookupError("Task 冻结输入不存在")
        if persisted.task_type != self._task_type:
            raise ValueError("Task 类型与 Snapshot Loader Codec 不一致")

        # Store 已保证值域是 JSON 对象；这里用与持久层一致的 canonical 规则计算输入身份。
        canonical = json.dumps(
            dict(persisted.input_payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        decoded = self._codec.decode_input(
            schema_version=persisted.input_schema_version,
            payload=persisted.input_payload,
        )
        snapshot = TaskExecutionSnapshot(
            task_id=persisted.task_id,
            task_type=persisted.task_type,
            business_ref=persisted.business_ref,
            execution_state=persisted.execution_state,
            public_status=persisted.public_status,
            progress=persisted.progress,
            message=persisted.message,
            input_snapshot=decoded,
            accepted_at=persisted.accepted_at,
            trace_id=persisted.trace_id,
        )
        return LoadedTaskExecutionInput(
            snapshot=snapshot,
            input_schema_version=persisted.input_schema_version,
            input_payload_fingerprint=hashlib.sha256(canonical).hexdigest(),
            retry_from_step_key=persisted.retry_from_step_key,
            recovery_steps=(
                recovery_steps if persisted.retry_from_step_key else ()
            ),
        )


__all__ = ["CodecTaskExecutionSnapshotLoader"]
