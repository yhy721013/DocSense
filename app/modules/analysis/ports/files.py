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

    def resolve(self, execution: AnalysisExecutionRef) -> AnalysisTaskWorkspace:
        """只读解析已存在任务目录；缺失时失败，不能在恢复门禁前补建。"""
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
class AnalysisSourceAcquisitionRequest:
    """只描述受控 Source 获取；不得隐式启动 DocumentProcessing。"""

    execution: AnalysisExecutionRef
    source_url: str
    task_root: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(self, "source_url", _required_text(self.source_url, name="source_url"))
        object.__setattr__(self, "task_root", _required_text(self.task_root, name="task_root"))


@dataclass(frozen=True)
class AcquiredAnalysisSource:
    """已获取 Source 的任务内临时引用；持久快照只能保存 basename 与摘要。"""

    execution: AnalysisExecutionRef
    source_path: str
    source_basename: str
    source_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(self, "source_path", _required_text(self.source_path, name="source_path"))
        basename = _required_text(self.source_basename, name="source_basename")
        if basename.replace("\\", "/").rsplit("/", 1)[-1] != basename:
            raise ValueError("source_basename 必须是 basename")
        object.__setattr__(self, "source_basename", basename)
        digest = _required_text(self.source_sha256, name="source_sha256").lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("source_sha256 必须是 SHA-256")
        object.__setattr__(self, "source_sha256", digest)


@dataclass(frozen=True)
class AnalysisSourceResolutionRequest:
    """根据续跑快照恢复已存在 Source，不访问 Source URL。"""

    execution: AnalysisExecutionRef
    task_root: str
    source_basename: str
    source_sha256: str

    def __post_init__(self) -> None:
        # 复用 Acquired DTO 的严格 basename/摘要校验，避免两套规则漂移。
        probe = AcquiredAnalysisSource(
            execution=self.execution,
            source_path=self.source_basename,
            source_basename=self.source_basename,
            source_sha256=self.source_sha256,
        )
        object.__setattr__(self, "task_root", _required_text(self.task_root, name="task_root"))
        object.__setattr__(self, "source_basename", probe.source_basename)
        object.__setattr__(self, "source_sha256", probe.source_sha256)


@dataclass(frozen=True)
class AnalysisDocumentPreparationRequest:
    """只允许处理一个已经取得并校验过的 Source。"""

    execution: AnalysisExecutionRef
    task_root: str
    source: AcquiredAnalysisSource
    document_processing_policy: AnalysisDocumentProcessingPolicySnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.source, AcquiredAnalysisSource):
            raise TypeError("source 必须是 AcquiredAnalysisSource")
        if self.source.execution != self.execution:
            raise ValueError("source 不属于当前 execution")
        object.__setattr__(self, "task_root", _required_text(self.task_root, name="task_root"))
        if not isinstance(
            self.document_processing_policy,
            AnalysisDocumentProcessingPolicySnapshot,
        ):
            raise TypeError("document_processing_policy 类型错误")


@dataclass(frozen=True)
class PreparedAnalysisDocument:
    """准备完成后的任务级文档引用。

    ``prepared_artifact`` 是正文、结果映射和全文翻译使用的 canonical Artifact；
    ``rag_upload_artifact`` 是允许送入 RAG Provider 的最终内容。二者必须显式分开，
    避免为了检索移除 Base64 图片时反向污染业务正文与翻译结果。
    """

    execution: AnalysisExecutionRef
    source_path: str
    upload_path: str
    original_text: str
    processing_path: str = ""
    internal_prepared_basename: str = ""
    prepared_artifact: ArtifactRef | None = None
    rag_upload_artifact: ArtifactRef | None = None
    rag_projection_profile_id: str = ""
    # v2 Workflow 用该摘要形成 ``source.download`` checkpoint。历史 Fake/夹具可暂不
    # 提供；生产 v2 文件 Adapter 必须返回真实下载字节的 SHA-256。
    source_sha256: str = ""

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
        if self.rag_upload_artifact is not None:
            if not isinstance(self.rag_upload_artifact, ArtifactRef):
                raise TypeError("rag_upload_artifact 必须是 ArtifactRef 或 None")
            if self.rag_upload_artifact.task_id != self.execution.task_id:
                raise ValueError("rag_upload_artifact 不属于当前 analysis task")
        if not isinstance(self.rag_projection_profile_id, str):
            raise TypeError("rag_projection_profile_id 必须是 str")
        profile_id = self.rag_projection_profile_id.strip().lower()
        if profile_id and (
            len(profile_id) != 64
            or any(character not in "0123456789abcdef" for character in profile_id)
        ):
            raise ValueError("rag_projection_profile_id 必须是 SHA-256")
        if profile_id and self.rag_upload_artifact is None:
            raise ValueError("存在 RAG 投影 Profile 时必须提供 rag_upload_artifact")
        object.__setattr__(self, "rag_projection_profile_id", profile_id)
        if not isinstance(self.source_sha256, str):
            raise TypeError("source_sha256 必须是 str")
        source_sha256 = self.source_sha256.strip().lower()
        if source_sha256 and (
            len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
        ):
            raise ValueError("source_sha256 必须为空或 SHA-256")
        object.__setattr__(self, "source_sha256", source_sha256)


@runtime_checkable
class FilePreparationPort(Protocol):
    """下载、规范化和正文读取的任务级文件能力。"""

    def prepare(
        self,
        request: AnalysisFilePreparationRequest,
    ) -> PreparedAnalysisDocument:
        ...


@runtime_checkable
class AnalysisSourceAcquisitionPort(Protocol):
    """只执行 Source 获取，不调用文档处理器。"""

    def acquire_source(
        self,
        request: AnalysisSourceAcquisitionRequest,
    ) -> AcquiredAnalysisSource: ...

    def resolve_source(
        self,
        request: AnalysisSourceResolutionRequest,
    ) -> AcquiredAnalysisSource: ...


@runtime_checkable
class AnalysisDocumentPreparationPort(Protocol):
    """只处理已取得 Source，不访问原始 Source URL。"""

    def prepare_document(
        self,
        request: AnalysisDocumentPreparationRequest,
    ) -> PreparedAnalysisDocument: ...


__all__ = (
    "AcquiredAnalysisSource",
    "AnalysisDocumentPreparationPort",
    "AnalysisDocumentPreparationRequest",
    "AnalysisFilePreparationRequest",
    "AnalysisSourceAcquisitionPort",
    "AnalysisSourceAcquisitionRequest",
    "AnalysisSourceResolutionRequest",
    "AnalysisTaskWorkspace",
    "AnalysisTaskWorkspacePort",
    "FilePreparationPort",
    "PreparedAnalysisDocument",
)
