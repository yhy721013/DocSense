"""独立 Translation 模块。

本模块只拥有语言转换、翻译进度与结果渲染；原始格式识别、OCR、MinerU、MHTML、
LibreOffice 和 Artifact 物化均属于 DocumentProcessing。
"""

from .application import TranslatePreparedDocument, build_translation_profile
from .domain import (
    RenderedTranslation,
    TranslationError,
    TranslationFailurePolicy,
    TranslationMode,
    TranslationProfile,
    TranslationRequest,
    TranslationResult,
    TranslationUnit,
)
from .ports import (
    PreparedArtifactReaderPort,
    TranslationEnginePort,
    TranslationProgressPort,
    TranslationRendererPort,
)

__all__ = [
    "PreparedArtifactReaderPort",
    "RenderedTranslation",
    "TranslatePreparedDocument",
    "TranslationEnginePort",
    "TranslationError",
    "TranslationFailurePolicy",
    "TranslationMode",
    "TranslationProfile",
    "TranslationProgressPort",
    "TranslationRendererPort",
    "TranslationRequest",
    "TranslationResult",
    "TranslationUnit",
    "build_translation_profile",
]
