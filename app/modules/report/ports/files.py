"""报告源文件、规范化、上传准备和 Word 模板提取端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .artifacts import ReportArtifactRef, ReportArtifactScope


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    if not value.strip():
        raise ValueError(f"{name} 不能为空")
    return value


@dataclass(frozen=True)
class ReportSourceDownload:
    """按原请求顺序下载一个源文件的命令。"""

    scope: ReportArtifactScope
    source_url: str
    sequence_no: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ReportArtifactScope):
            raise TypeError("scope 必须是 ReportArtifactScope")
        object.__setattr__(
            self,
            "source_url",
            _required_text(self.source_url, name="source_url"),
        )
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no <= 0
        ):
            raise ValueError("sequence_no 必须是正整数")


@dataclass(frozen=True)
class ReportTemplateDownload:
    """下载一次报告模板的命令。"""

    scope: ReportArtifactScope
    template_url: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ReportArtifactScope):
            raise TypeError("scope 必须是 ReportArtifactScope")
        object.__setattr__(
            self,
            "template_url",
            _required_text(self.template_url, name="template_url"),
        )


@runtime_checkable
class ReportFilePort(Protocol):
    """把文件系统和下载器细节隔离在 Application 之外。"""

    def download_source(self, command: ReportSourceDownload) -> ReportArtifactRef:
        ...

    def normalize_source(self, source: ReportArtifactRef) -> ReportArtifactRef:
        """成功返回同 task/sequence 的 ``normalized_source``；显式失败供兼容回退。"""
        ...

    def prepare_upload_files(
        self,
        source: ReportArtifactRef,
    ) -> tuple[ReportArtifactRef, ...]:
        """返回同 task/sequence 的有序 ``rag_input``；允许一个源文件展开为多项。"""
        ...

    def download_template(
        self,
        command: ReportTemplateDownload,
    ) -> ReportArtifactRef:
        ...

    def extract_template_text(self, template: ReportArtifactRef) -> str:
        ...


__all__ = [
    "ReportFilePort",
    "ReportSourceDownload",
    "ReportTemplateDownload",
]
