"""生成 RAG-only Markdown Artifact 的应用用例。"""

from __future__ import annotations

from app.modules.document_processing.application.prepare_document import PrepareDocument
from app.modules.document_processing.domain import (
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentProcessingResult,
    ProcessingProfile,
)

RAG_PROJECTION_STEP_ID = "rag-markdown-projection-v2"


class ProjectDocumentForRag:
    """把投影请求交给通用幂等处理协调器。

    本用例不接触 AnythingLLM，也不返回本地路径。任务、源 Artifact、算法 Profile 和
    逻辑步骤共同决定 step_key，使未来可靠队列接管后仍可复用同一处理事实。
    """

    def __init__(
        self,
        *,
        prepare_document: PrepareDocument,
        profile: ProcessingProfile,
    ) -> None:
        if not isinstance(prepare_document, PrepareDocument):
            raise TypeError("prepare_document 必须是 PrepareDocument")
        if not isinstance(profile, ProcessingProfile):
            raise TypeError("profile 必须是 ProcessingProfile")
        self._prepare_document = prepare_document
        self._profile = profile

    @property
    def profile_id(self) -> str:
        """供资源事实和发布门禁记录投影算法身份。"""

        return self._profile.profile_id

    def execute(
        self,
        source_artifact: ArtifactRef,
        *,
        trace_id: str,
    ) -> DocumentProcessingResult:
        if not isinstance(source_artifact, ArtifactRef):
            raise TypeError("source_artifact 必须是 ArtifactRef")
        request = DocumentProcessingRequest(
            task_id=source_artifact.task_id,
            step_id=RAG_PROJECTION_STEP_ID,
            source_artifact=source_artifact,
            profile=self._profile,
            trace_id=trace_id,
        )
        return self._prepare_document.execute(request)


__all__ = ["ProjectDocumentForRag", "RAG_PROJECTION_STEP_ID"]
