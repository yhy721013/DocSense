"""Translation Application 依赖的窄端口。"""

from __future__ import annotations

from typing import BinaryIO, ContextManager, Protocol, Sequence, runtime_checkable

from app.modules.document_processing.domain import ArtifactRef
from app.modules.translation.domain import (
    RenderedTranslation,
    TranslationMode,
    TranslationRequest,
    TranslationUnit,
)


@runtime_checkable
class PreparedArtifactReaderPort(Protocol):
    """只读取已准备 Artifact，不提供路径、删除或格式转换能力。"""

    def open_reader(
        self,
        artifact: ArtifactRef,
    ) -> ContextManager[BinaryIO]:
        ...


@runtime_checkable
class TranslationEnginePort(Protocol):
    """语言转换引擎；实现可以是 Argos、LLM 或测试 Fake。"""

    @property
    def engine_id(self) -> str:
        ...

    @property
    def engine_fingerprint(self) -> str:
        ...

    def translate(
        self,
        text: str,
        *,
        target_language: str,
        mode: TranslationMode,
    ) -> str:
        ...


@runtime_checkable
class TranslationRendererPort(Protocol):
    """从 prepared 文本提取安全单元，并渲染双语/单语 HTML。

    分段属于 Renderer 的一部分，因为 Markdown 必须先形成结构化 HTML，再只翻译
    文本节点；如果 Application 直接按空行拆 Markdown，标题、表格、代码块和图片
    都可能被语言模型破坏。
    """

    @property
    def renderer_id(self) -> str:
        ...

    @property
    def renderer_fingerprint(self) -> str:
        ...

    def extract_units(
        self,
        *,
        request: TranslationRequest,
        source_text: str,
    ) -> Sequence[str]:
        """返回有序可翻译文本节点；不得保存跨请求可变状态。"""

        ...

    def render(
        self,
        *,
        request: TranslationRequest,
        source_text: str,
        units: Sequence[TranslationUnit],
    ) -> RenderedTranslation:
        ...


@runtime_checkable
class TranslationProgressPort(Protocol):
    """任务级进度观察端口；不得保存全局可变回调。"""

    def started(self, request: TranslationRequest, *, total_items: int) -> None:
        ...

    def advanced(
        self,
        request: TranslationRequest,
        *,
        completed_items: int,
    ) -> None:
        ...

    def completed(self, request: TranslationRequest) -> None:
        ...

    def failed(self, request: TranslationRequest, *, error_code: str) -> None:
        ...


__all__ = [
    "PreparedArtifactReaderPort",
    "TranslationEnginePort",
    "TranslationProgressPort",
    "TranslationRendererPort",
]
