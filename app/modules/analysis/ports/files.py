"""文件准备 Port；冻结 raw、processing、upload 三类任务内路径与处理策略。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.document_processing.domain import ArtifactRef
from .common import AnalysisExecutionRef
from app.modules.analysis.domain.task_inputs import (
    AnalysisDocumentProcessingPolicySnapshot,
)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空 str")
    return value.strip()


@dataclass(frozen=True)
class AnalysisTaskWorkspace:
    """一个 execution 专属的本地工作目录。

    Application 只持有已经由基础设施 Adapter 创建并校验过的目录引用，不能自行拼接
    文件系统路径。这样下载、规范化、OCR 与翻译中间产物始终被限制在同一任务范围内，
    后续资源恢复也可以只依据这一明确事实处理。
    """

    execution: AnalysisExecutionRef
    root_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(
            self,
            "root_path",
            _required_text(self.root_path, name="root_path"),
        )


@runtime_checkable
class AnalysisTaskWorkspacePort(Protocol):
    """创建并返回当前 execution 唯一允许使用的任务目录。"""

    def create(self, execution: AnalysisExecutionRef) -> AnalysisTaskWorkspace:
        ...


@dataclass(frozen=True)
class AnalysisFilePreparationRequest:
    """文件 Adapter 必须使用 execution 专属目录，不能从文件名推导全局目录。"""

    execution: AnalysisExecutionRef
    source_url: str
    task_root: str
    document_processing_policy: AnalysisDocumentProcessingPolicySnapshot | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(self, "source_url", _required_text(self.source_url, name="source_url"))
        object.__setattr__(self, "task_root", _required_text(self.task_root, name="task_root"))
        policy = self.document_processing_policy
        if policy is None:
            policy = AnalysisDocumentProcessingPolicySnapshot.for_source(
                self.source_url,
                business_file_name=self.execution.file_name,
            )
            object.__setattr__(self, "document_processing_policy", policy)
        if not isinstance(policy, AnalysisDocumentProcessingPolicySnapshot):
            raise TypeError(
                "document_processing_policy 必须是 AnalysisDocumentProcessingPolicySnapshot"
            )


@dataclass(frozen=True)
class PreparedAnalysisDocument:
    """准备完成后的任务级文档引用；正文由 Adapter 管理，不在 Port 中复制可变文件对象。"""

    execution: AnalysisExecutionRef
    source_path: str
    upload_path: str
    original_text: str
    processing_path: str = ""
    internal_prepared_basename: str = ""
    prepared_artifact: ArtifactRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        for field_name in ("source_path", "upload_path"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), name=field_name),
            )
        processing_path = self.processing_path or self.upload_path
        object.__setattr__(
            self,
            "processing_path",
            _required_text(processing_path, name="processing_path"),
        )
        if not isinstance(self.original_text, str):
            raise TypeError("original_text 必须是 str")
        if not isinstance(self.internal_prepared_basename, str):
            raise TypeError("internal_prepared_basename 必须是 str")
        basename = self.internal_prepared_basename.strip()
        if basename and basename.replace("\\", "/").rsplit("/", 1)[-1] != basename:
            raise ValueError("internal_prepared_basename 必须是 basename")
        object.__setattr__(self, "internal_prepared_basename", basename)
        if self.prepared_artifact is not None:
            if not isinstance(self.prepared_artifact, ArtifactRef):
                raise TypeError("prepared_artifact 必须是 ArtifactRef 或 None")
            if self.prepared_artifact.task_id != self.execution.task_id:
                raise ValueError("prepared_artifact 不属于当前 analysis task")


@runtime_checkable
class FilePreparationPort(Protocol):
    """下载、规范化和正文读取的任务级文件能力。"""

    def prepare(
        self,
        request: AnalysisFilePreparationRequest,
    ) -> PreparedAnalysisDocument:
        ...


__all__ = (
    "AnalysisFilePreparationRequest",
    "AnalysisTaskWorkspace",
    "AnalysisTaskWorkspacePort",
    "FilePreparationPort",
    "PreparedAnalysisDocument",
)
