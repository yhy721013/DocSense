"""文件分析翻译能力的任务级 Port。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.document_processing.domain import ArtifactRef
from .common import AnalysisExecutionRef


class AnalysisTranslationKind(str, Enum):
    DOCUMENT = "document"
    SUMMARY = "summary"


class AnalysisTranslationOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AnalysisTranslationRequest:
    """一次翻译只绑定一个 execution，禁止通过全局 callback 回传任务进度。"""

    execution: AnalysisExecutionRef
    kind: AnalysisTranslationKind
    target_language: str = "Chinese"
    source_path: str = ""
    prepared_artifact: ArtifactRef | None = None
    text: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.kind, AnalysisTranslationKind):
            raise TypeError("kind 必须是 AnalysisTranslationKind")
        if not isinstance(self.target_language, str) or not self.target_language.strip():
            raise ValueError("target_language 必须是非空 str")
        if not isinstance(self.source_path, str) or not isinstance(self.text, str):
            raise TypeError("source_path 与 text 必须是 str")
        if self.kind is AnalysisTranslationKind.DOCUMENT:
            if self.text:
                raise ValueError("document 翻译不得携带 text")
            if self.prepared_artifact is not None and not isinstance(
                self.prepared_artifact,
                ArtifactRef,
            ):
                raise TypeError("prepared_artifact 必须是 ArtifactRef 或 None")
            if not self.source_path.strip() and self.prepared_artifact is None:
                raise ValueError("document 翻译必须携带受控 Artifact 或兼容 source_path")
            if (
                self.prepared_artifact is not None
                and self.prepared_artifact.task_id != self.execution.task_id
            ):
                raise ValueError("prepared_artifact 不属于当前 analysis task")
        elif not self.text:
            raise ValueError("summary 翻译必须携带非空 text")
        object.__setattr__(self, "target_language", self.target_language.strip())


@dataclass(frozen=True)
class AnalysisTranslationResult:
    execution: AnalysisExecutionRef
    kind: AnalysisTranslationKind
    outcome: AnalysisTranslationOutcome
    document_translation_one: str = ""
    document_translation_two: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.kind, AnalysisTranslationKind):
            raise TypeError("kind 必须是 AnalysisTranslationKind")
        if not isinstance(self.outcome, AnalysisTranslationOutcome):
            raise TypeError("outcome 必须是 AnalysisTranslationOutcome")
        for field_name in (
            "document_translation_one",
            "document_translation_two",
            "error_code",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"{field_name} 必须是 str")
        if self.outcome is AnalysisTranslationOutcome.SUCCEEDED and self.error_code:
            raise ValueError("成功翻译不得携带 error_code")
        if self.outcome is AnalysisTranslationOutcome.SUCCEEDED and (
            not self.document_translation_one or not self.document_translation_two
        ):
            raise ValueError("成功翻译必须携带两种非空展示结果")
        if self.outcome is AnalysisTranslationOutcome.FAILED and not self.error_code:
            raise ValueError("失败翻译必须携带 error_code")
        if self.outcome is AnalysisTranslationOutcome.FAILED and (
            self.document_translation_one or self.document_translation_two
        ):
            raise ValueError("失败翻译不得伪装为部分成功结果")


@runtime_checkable
class AnalysisTranslationPort(Protocol):
    """Adapter 必须提供任务级调用，不得保存或覆盖跨任务可变回调。"""

    def translate(
        self,
        request: AnalysisTranslationRequest,
    ) -> AnalysisTranslationResult:
        ...


__all__ = (
    "AnalysisTranslationKind",
    "AnalysisTranslationOutcome",
    "AnalysisTranslationPort",
    "AnalysisTranslationRequest",
    "AnalysisTranslationResult",
)
