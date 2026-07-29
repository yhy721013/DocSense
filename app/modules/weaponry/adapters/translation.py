"""现有纯文本翻译能力到 WeaponryTranslationPort 的兼容 Adapter。"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.modules.translation.domain import TranslationMode
from app.modules.translation.ports import TranslationEnginePort
from app.modules.weaponry.ports import (
    WeaponryTranslationOutcome,
    WeaponryTranslationRequest,
    WeaponryTranslationResult,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class WeaponryTextTranslatorProtocol(Protocol):
    def translate_text_only(
        self,
        text: str,
        target_lang: str = "Chinese",
        fast_translate: bool | None = None,
        as_html: bool = True,
    ) -> str:
        ...


class LLMTranslationServiceWeaponryAdapter:
    """不保存跨任务正文缓存；翻译失败按既有兼容语义返回空文本。"""

    def __init__(self, translator: WeaponryTextTranslatorProtocol) -> None:
        if not isinstance(translator, WeaponryTextTranslatorProtocol):
            raise TypeError("translator 必须实现纯文本翻译契约")
        self._translator = translator

    def translate(
        self,
        request: WeaponryTranslationRequest,
    ) -> WeaponryTranslationResult:
        if not isinstance(request, WeaponryTranslationRequest):
            raise TypeError("request 必须是 WeaponryTranslationRequest")
        try:
            translated = self._translator.translate_text_only(
                request.text,
                target_lang=request.target_language,
                fast_translate=True,
                as_html=False,
            )
        except Exception as exc:
            logger.warning(
                "武器谱来源翻译调用失败: task_id=%s call_id=%s error_type=%s",
                request.call.task_id.value,
                request.call.call_id,
                type(exc).__name__,
            )
            return WeaponryTranslationResult(
                call=request.call,
                text="",
                outcome=WeaponryTranslationOutcome.FAILED,
                error_code="translation_exception",
            )
        normalized = translated.strip() if isinstance(translated, str) else ""
        if not normalized:
            logger.warning(
                "武器谱来源翻译返回空结果: task_id=%s call_id=%s",
                request.call.task_id.value,
                request.call.call_id,
            )
            return WeaponryTranslationResult(
                call=request.call,
                text="",
                outcome=WeaponryTranslationOutcome.FAILED,
                error_code="translation_empty",
            )
        logger.info(
            "武器谱来源翻译完成: task_id=%s call_id=%s input_chars=%d output_chars=%d",
            request.call.task_id.value,
            request.call.call_id,
            len(request.text),
            len(normalized),
        )
        return WeaponryTranslationResult(
            call=request.call,
            text=normalized,
            outcome=WeaponryTranslationOutcome.SUCCEEDED,
        )


class TranslationEngineWeaponryAdapter:
    """Weaponry 直接消费独立 TranslationEngine，不再经过文档翻译服务。"""

    def __init__(self, engine: TranslationEnginePort) -> None:
        if not isinstance(engine, TranslationEnginePort):
            raise TypeError("engine 必须实现 TranslationEnginePort")
        self._engine = engine

    def translate(
        self,
        request: WeaponryTranslationRequest,
    ) -> WeaponryTranslationResult:
        if not isinstance(request, WeaponryTranslationRequest):
            raise TypeError("request 必须是 WeaponryTranslationRequest")
        try:
            translated = self._engine.translate(
                request.text,
                target_language=request.target_language,
                mode=TranslationMode.MACHINE,
            )
        except Exception as exc:
            logger.warning(
                "武器谱 TranslationEngine 调用失败: task_id=%s "
                "call_id=%s error_type=%s",
                request.call.task_id.value,
                request.call.call_id,
                type(exc).__name__,
            )
            return WeaponryTranslationResult(
                call=request.call,
                text="",
                outcome=WeaponryTranslationOutcome.FAILED,
                error_code="translation_exception",
            )
        normalized = translated.strip() if isinstance(translated, str) else ""
        if not normalized:
            return WeaponryTranslationResult(
                call=request.call,
                text="",
                outcome=WeaponryTranslationOutcome.FAILED,
                error_code="translation_empty",
            )
        return WeaponryTranslationResult(
            call=request.call,
            text=normalized,
            outcome=WeaponryTranslationOutcome.SUCCEEDED,
        )


__all__ = [
    "LLMTranslationServiceWeaponryAdapter",
    "TranslationEngineWeaponryAdapter",
    "WeaponryTextTranslatorProtocol",
]
