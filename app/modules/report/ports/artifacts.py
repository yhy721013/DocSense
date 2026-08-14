"""报告任务级 Artifact 命名空间与不透明引用端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class ReportArtifactCategory(str, Enum):
    """业务可识别的 Artifact 类别，不包含本地路径或 MinIO bucket。"""

    SOURCE = "source"
    NORMALIZED_SOURCE = "normalized_source"
    RAG_INPUT = "rag_input"
    TEMPLATE = "template"
    REPORT_HTML = "report_html"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class ReportArtifactRef:
    """Application 可安全持有的 Artifact 不透明引用。"""

    task_id: TaskId
    artifact_id: str
    category: ReportArtifactCategory
    sequence_no: int | None = None
    size_bytes: int | None = None
    checksum: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(
            self,
            "artifact_id",
            _required_text(self.artifact_id, name="artifact_id"),
        )
        if not isinstance(self.category, ReportArtifactCategory):
            raise TypeError("category 必须是 ReportArtifactCategory")
        if self.sequence_no is not None and (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no <= 0
        ):
            raise ValueError("sequence_no 必须是正整数或 None")
        if self.size_bytes is not None and (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes 必须是非负整数或 None")
        if not isinstance(self.checksum, str):
            raise TypeError("checksum 必须是 str")
        if self.category is ReportArtifactCategory.REPORT_HTML:
            # 最终报告是终态事实的一部分，必须能够通过大小和摘要校验其不可变内容；临时
            # 下载物仍允许由具体 Adapter 在持久化前补齐这些信息。
            if self.size_bytes is None:
                raise ValueError("report_html Artifact 必须包含 size_bytes")
            if not self.checksum.strip():
                raise ValueError("report_html Artifact 必须包含 checksum")


@dataclass(frozen=True)
class ReportArtifactScope:
    """一次执行独占的任务目录/对象前缀。"""

    task_id: TaskId
    namespace: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(
            self,
            "namespace",
            _required_text(self.namespace, name="namespace"),
        )


@dataclass(frozen=True)
class ReportArtifactCleanupResult:
    """清理结果；pending 也包含缺失或校验失败的保留产物，必须继续恢复/告警。"""

    cleaned: tuple[ReportArtifactRef, ...] = ()
    pending: tuple[ReportArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        cleaned = tuple(self.cleaned)
        pending = tuple(self.pending)
        if any(not isinstance(item, ReportArtifactRef) for item in cleaned + pending):
            raise TypeError("cleaned/pending 只能包含 ReportArtifactRef")
        cleaned_ids = tuple((item.task_id, item.artifact_id) for item in cleaned)
        pending_ids = tuple((item.task_id, item.artifact_id) for item in pending)
        if len(set(cleaned_ids)) != len(cleaned_ids):
            raise ValueError("cleaned 不得包含重复 Artifact")
        if len(set(pending_ids)) != len(pending_ids):
            raise ValueError("pending 不得包含重复 Artifact")
        if set(cleaned_ids).intersection(pending_ids):
            raise ValueError("同一 Artifact 不得同时标记为 cleaned 和 pending")
        object.__setattr__(self, "cleaned", cleaned)
        object.__setattr__(self, "pending", pending)


@runtime_checkable
class ReportArtifactPort(Protocol):
    """分配隔离命名空间、持久化最终报告并清理未获所有权的对象。

    ``begin`` 必须是无外部副作用的前缀/作用域计算；资源记录提交后，首次实际写入才可
    创建本地目录或 MinIO 对象。这样 Store 失败不会产生无法恢复的孤儿命名空间。
    """

    def begin(self, task_id: TaskId) -> ReportArtifactScope:
        ...

    def persist_report_html(
        self,
        scope: ReportArtifactScope,
        html_details: str,
    ) -> ReportArtifactRef:
        ...

    def load_report_html(self, artifact: ReportArtifactRef) -> str:
        """读取已持久化最终报告，并严格复核引用的大小与摘要。"""
        ...

    def cleanup_unretained(
        self,
        scope: ReportArtifactScope,
        *,
        retain: tuple[ReportArtifactRef, ...],
    ) -> ReportArtifactCleanupResult:
        """删除未保留产物，并复核终态持有的 final Artifact 完整性。"""
        ...


__all__ = [
    "ReportArtifactCategory",
    "ReportArtifactCleanupResult",
    "ReportArtifactPort",
    "ReportArtifactRef",
    "ReportArtifactScope",
]
