from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from app.services.translator import DocumentTranslator, HYMTTranslator


logger = logging.getLogger(__name__)


class LLMTranslationService:
    """文档翻译服务 - 为 LLM 分析流程提供翻译能力"""

    def __init__(self):
        """初始化翻译服务"""
        self._translator: Optional[HYMTTranslator] = None
        self._document_translator: Optional[DocumentTranslator] = None
        self._progress_callback: Optional[Callable[[float, str], None]] = None

    def _ensure_translator(self) -> None:
        """确保翻译器已初始化（懒加载）"""
        if self._translator is None:
            model_name = os.getenv("DOCSENSE_TRANSLATION_MODEL", "Qwen3-4B-Instruct-2507-Q4_K_M")
            self._translator = HYMTTranslator(model_name=model_name)
            self._document_translator = DocumentTranslator(self._translator)

    def set_progress_callback(self, callback: Callable[[float, str], None]) -> None:
        """
        设置进度回调函数

        :param callback: 回调函数，接收 (progress: float, message: str)
        """
        self._progress_callback = callback

    def _notify_progress(self, progress: float, message: str) -> None:
        """内部进度通知方法"""
        if self._progress_callback:
            try:
                self._progress_callback(progress, message)
            except Exception as e:
                logger.error("进度回调失败: %s", e)

    def translate_document(
            self,
            file_path: str,
            target_lang: str = "Chinese",
            translate_all: int = 0,
    ) -> tuple[str, str] | tuple[str, str]:
        """
        翻译文档并返回双语结果

        :param file_path: 待翻译文件路径
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页/段落
        :return: (翻译后文本，双语对照 HTML)
        """
        self._ensure_translator()

        if not os.path.exists(file_path):
            return "", ""

        try:
            # 生成输出路径
            base_path = Path(file_path)
            output_txt = base_path.parent / f"{base_path.stem}_translated.txt"
            output_bilingual_html = base_path.parent / f"{base_path.stem}"/"_bilingual_html"
            output_monolingual_html = base_path.parent / f"{base_path.stem}"/"_monolingual_html"

            # 翻译文档（生成 TXT）
            # translated_txt_path = self._document_translator.process_file(
            #     file_path=str(file_path),
            #     output_path=str(output_txt),
            #     target_lang=target_lang,
            #     translate_all=translate_all,
            # )
            #
            # # 读取翻译后的文本
            # translated_text = ""
            # if os.path.exists(translated_txt_path):
            #     translated_text = Path(translated_txt_path).read_text(encoding="utf-8", errors="ignore")

            # 生成双语 HTML
            bilingual_html_path = self._document_translator.convert_to_html(
                file_path=str(file_path),
                output_dir=str(output_bilingual_html),
                target_lang=target_lang,
                show_bilingual=True,
                translate_all=translate_all,
            )
            # 读取 HTML 内容
            bilingual_html_content = ""
            if os.path.exists(bilingual_html_path):
                bilingual_html_content = Path(bilingual_html_path).read_text(encoding="utf-8", errors="ignore")

            # 生成单语 HTML
            monolingual_html_path = self._document_translator.convert_to_html(
                file_path=str(file_path),
                output_dir=str(output_monolingual_html),
                target_lang=target_lang,
                show_bilingual=False,
                translate_all=translate_all,
            )
            monolingual_html_content = ""
            if os.path.exists(monolingual_html_path):
                monolingual_html_content = Path(monolingual_html_path).read_text(encoding="utf-8", errors="ignore")

            return bilingual_html_content, monolingual_html_content
        except Exception as e:
            logger.exception("文档翻译失败: %s, error=%s", file_path, e)
            self._notify_progress(0.0, f"翻译失败：{e}")
            return "", ""

    def translate_text_only(self, text: str, target_lang: str = "Chinese") -> str:
        """
        仅翻译纯文本（适用于短文本或摘要）

        :param text: 待翻译文本
        :param target_lang: 目标语言
        :return: 翻译后的文本
        """
        self._ensure_translator()

        if not text.strip():
            return ""

        try:
            translated = self._translator.translate_text(text, target_lang)
            # 返回HTML格式
            return f'<div class="translated-text">{self._escape_html(translated)}</div>'
        except Exception as e:
            logger.error("文本翻译失败: %s", e)
            return ""


    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&#39;')
        return text


# 全局单例（可选）
_translation_service_instance: Optional[LLMTranslationService] = None


def get_translation_service() -> LLMTranslationService:
    """获取翻译服务单例"""
    global _translation_service_instance
    if _translation_service_instance is None:
        _translation_service_instance = LLMTranslationService()
    return _translation_service_instance

