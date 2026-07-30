"""MHTML 浏览器优先、确定失败后 Markdown 降级的应用编排。"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.document_processing.domain import (
    DocumentProcessingRequest,
    DocumentProcessingResult,
    ProcessingOutcome,
)

from .prepare_document import PrepareDocument


@dataclass(frozen=True, slots=True)
class PrepareMHTMLRequest:
    browser_request: DocumentProcessingRequest
    fallback_request: DocumentProcessingRequest

    def __post_init__(self) -> None:
        if (
            self.browser_request.task_id != self.fallback_request.task_id
            or self.browser_request.source_artifact
            != self.fallback_request.source_artifact
        ):
            raise ValueError("MHTML 主流程与降级流程必须共享 task/source Artifact")


class PrepareMHTMLDocument:
    """只在浏览器结果已确认失败时执行 Markdown 降级。"""

    def __init__(
        self,
        *,
        browser: PrepareDocument,
        fallback: PrepareDocument,
    ) -> None:
        self._browser = browser
        self._fallback = fallback

    def execute(self, command: PrepareMHTMLRequest) -> DocumentProcessingResult:
        if not isinstance(command, PrepareMHTMLRequest):
            raise TypeError("command 必须是 PrepareMHTMLRequest")
        primary = self._browser.execute(command.browser_request)
        if primary.outcome is ProcessingOutcome.FAILED:
            return self._fallback.execute(command.fallback_request)
        # succeeded、running/skipped 与 outcome_unknown 均不能启动第二条处理链。
        return primary


__all__ = ["PrepareMHTMLDocument", "PrepareMHTMLRequest"]
