"""Analysis 完整 Callback 结果快照 Port。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.tasks.domain import TaskBusinessRef, TaskId


@dataclass(frozen=True, slots=True)
class AnalysisResultSnapshot:
    """可在重启后无损恢复既有公开 Callback 的完整持久事实。"""

    task_id: TaskId
    business_ref: TaskBusinessRef
    payload: FrozenJsonObject
    result_digest: str
    created_at: str
    batch_id: str
    batch_sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if (
            not isinstance(self.business_ref, TaskBusinessRef)
            or self.business_ref.business_type != "file"
        ):
            raise TypeError("business_ref 必须是 file TaskBusinessRef")
        if not isinstance(self.payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        digest = str(self.result_digest or "").strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("result_digest 必须是 SHA-256 小写十六进制摘要")
        object.__setattr__(self, "result_digest", digest)
        batch_id = str(self.batch_id or "").strip()
        if len(batch_id) != 32 or any(character not in "0123456789abcdef" for character in batch_id):
            raise ValueError("batch_id 必须是 32 位小写十六进制字符串")
        if (
            isinstance(self.batch_sequence, bool)
            or not isinstance(self.batch_sequence, int)
            or not 1 <= self.batch_sequence <= 32
        ):
            raise ValueError("batch_sequence 必须是 1..32 的整数")
        object.__setattr__(self, "batch_id", batch_id)


@runtime_checkable
class AnalysisResultSnapshotStorePort(Protocol):
    def save(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        payload: FrozenJsonObject,
        created_at: str,
    ) -> AnalysisResultSnapshot: ...

    def get(self, task_id: TaskId) -> AnalysisResultSnapshot | None: ...


__all__ = ["AnalysisResultSnapshot", "AnalysisResultSnapshotStorePort"]
