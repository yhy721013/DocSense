"""Translation 的显式组合函数；不在模块导入时加载模型或读取环境。"""

from __future__ import annotations

from app.modules.translation.adapters import (
    HYMTTranslationEngineAdapter,
    SafeHTMLTranslationRendererAdapter,
)
from app.modules.translation.application import TranslatePreparedDocument
from app.modules.translation.ports import (
    PreparedArtifactReaderPort,
    TranslationProgressPort,
)


def build_hymt_translation_application(
    *,
    reader: PreparedArtifactReaderPort,
    hymt_engine,
    engine_fingerprint: str,
    progress: TranslationProgressPort | None = None,
) -> TranslatePreparedDocument:
    """用既有 HYMT 运行时装配独立 Artifact 翻译用例。"""

    return TranslatePreparedDocument(
        reader=reader,
        engine=HYMTTranslationEngineAdapter(
            hymt_engine,
            engine_fingerprint=engine_fingerprint,
        ),
        renderer=SafeHTMLTranslationRendererAdapter(),
        progress=progress,
    )


__all__ = ["build_hymt_translation_application"]
