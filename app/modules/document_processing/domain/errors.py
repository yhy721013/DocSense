"""共享文档处理内核的稳定错误分类。

错误码用于模块内部持久化、日志与恢复判断，不会直接成为 HTTP、SSE、WebSocket 或
Callback 字段。具体业务 Presenter 仍负责把结果投影为既有公开契约。
"""

from __future__ import annotations


class DocumentProcessingError(RuntimeError):
    """带稳定内部错误码的文档处理异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code).strip()
        self.outcome_unknown = bool(outcome_unknown)
        if not self.code:
            raise ValueError("DocumentProcessingError.code 不能为空")


class ArtifactError(DocumentProcessingError):
    """Artifact 发布、读取、校验或清理失败。"""


class ArtifactConflictError(ArtifactError):
    """确定性 Artifact 身份已存在，但内容与预期不一致。"""

    def __init__(self, message: str = "Artifact 内容冲突") -> None:
        super().__init__("artifact_conflict", message)


class ArtifactIntegrityError(ArtifactError):
    """Artifact 缺失或元数据校验不通过。"""

    def __init__(self, message: str = "Artifact 完整性校验失败") -> None:
        super().__init__("artifact_integrity_failed", message)


class ProcessingRecordError(DocumentProcessingError):
    """Processing Record 无法完成原子状态转换。"""


class ProcessingRecordConflictError(ProcessingRecordError):
    """记录的 claim/state 与调用方预期不一致。"""

    def __init__(self, message: str = "Processing Record 状态冲突") -> None:
        super().__init__("processing_record_conflict", message)


class RagProjectionError(DocumentProcessingError):
    """RAG-only 文本投影无法确定性生成。"""


__all__ = [
    "ArtifactConflictError",
    "ArtifactError",
    "ArtifactIntegrityError",
    "DocumentProcessingError",
    "ProcessingRecordConflictError",
    "ProcessingRecordError",
    "RagProjectionError",
]
