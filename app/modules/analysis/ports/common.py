"""Analysis Ports 共用的执行身份值对象。

Ports 可以复用通用 ``TaskId``，但不能依赖 tasks 的 Application 或 Adapter；这样 Analysis
未来切换 SQLite、可靠队列或多实例 Worker 时，外部能力接口仍只围绕稳定任务身份协作。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.tasks.domain import TaskId


_MAX_BATCH_SEQUENCE = 32


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空 str")
    return value.strip()


@dataclass(frozen=True)
class AnalysisExecutionRef:
    """一个已受理文件 execution 的内部身份，不得写入任何公开响应。"""

    task_id: TaskId
    file_name: str
    batch_id: str
    batch_sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "file_name", _required_text(self.file_name, name="file_name"))
        batch_id = _required_text(self.batch_id, name="batch_id")
        if len(batch_id) != 32 or any(
            character not in "0123456789abcdef" for character in batch_id
        ):
            raise ValueError("batch_id 必须是 32 位小写十六进制字符串")
        if (
            isinstance(self.batch_sequence, bool)
            or not isinstance(self.batch_sequence, int)
            or self.batch_sequence < 1
            or self.batch_sequence > _MAX_BATCH_SEQUENCE
        ):
            raise ValueError(f"batch_sequence 必须是 1..{_MAX_BATCH_SEQUENCE} 的整数")
        object.__setattr__(self, "batch_id", batch_id)


__all__ = ("AnalysisExecutionRef",)
