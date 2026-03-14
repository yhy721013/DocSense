from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from app.services.hy_mt_translator import DocumentTranslator, HYMTTranslator


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
            self._translator = HYMTTranslator()
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
                print(f"[LLMTranslationService] 进度回调失败：{e}")

    def translate_document(
            self,
            file_path: str,
            target_lang: str = "Chinese",
            translate_all: int = 0,
    ) -> Tuple[str, str]:
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
            output_html = base_path.parent / f"{base_path.stem}_bilingual.html"

            # 【新增】在翻译前通知进度（0% 起点）
            self._notify_progress(0.0, "开始翻译文档...")

            # 翻译文档（生成 TXT）
            translated_txt_path = self._document_translator.process_file(
                file_path=str(file_path),
                output_path=str(output_txt),
                target_lang=target_lang,
                translate_all=translate_all,
            )

            # 【新增】读取翻译后的文本时通知进度（50%）
            self._notify_progress(0.5, "正在读取翻译结果...")

            # 读取翻译后的文本
            translated_text = ""
            if os.path.exists(translated_txt_path):
                translated_text = Path(translated_txt_path).read_text(encoding="utf-8", errors="ignore")

            # 生成双语 HTML
            bilingual_html_path = self._document_translator.convert_to_html(
                file_path=str(file_path),
                output_dir=str(base_path.parent),
                target_lang=target_lang,
                show_bilingual=True,
                translate_all=translate_all,
            )

            # 【新增】读取 HTML 内容时通知进度（80%）
            self._notify_progress(0.8, "正在生成双语对照 HTML...")

            # 读取 HTML 内容
            html_content = ""
            if os.path.exists(bilingual_html_path):
                html_content = Path(bilingual_html_path).read_text(encoding="utf-8", errors="ignore")

            # 【新增】完成时通知进度（100%）
            self._notify_progress(1.0, "翻译完成")

            return translated_text, html_content

        except Exception as e:
            print(f"[LLMTranslationService] 翻译失败：{e}")
            self._notify_progress(0.0, f"翻译失败：{e}")
            return "", ""

    def translate_text_only(
            self,
            text: str,
            target_lang: str = "Chinese",
    ) -> str:
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
            return self._translator.translate_text(text, target_lang)
        except Exception as e:
            print(f"[LLMTranslationService] 文本翻译失败：{e}")
            return ""


# 全局单例（可选）
_translation_service_instance: Optional[LLMTranslationService] = None


def get_translation_service() -> LLMTranslationService:
    """获取翻译服务单例"""
    global _translation_service_instance
    if _translation_service_instance is None:
        _translation_service_instance = LLMTranslationService()
    return _translation_service_instance

