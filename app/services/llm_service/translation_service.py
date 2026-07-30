from __future__ import annotations

import os
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from app.services.translator import DocumentTranslator, HYMTTranslator


logger = logging.getLogger(__name__)


_MACHINE_TRANSLATION_MODES = {"machine", "fast", "argos", "argostranslate"}
_LLM_TRANSLATION_MODES = {"llm", "model", "ollama"}


class LLMTranslationService:
    """文档翻译服务 - 为 LLM 分析流程提供翻译能力"""

    def __init__(self):
        """初始化翻译服务"""
        self._translator: Optional[HYMTTranslator] = None
        self._document_translator: Optional[DocumentTranslator] = None
        self._init_lock = threading.RLock()
        # DocumentTranslator 会短暂修改 MinerU 的共享输出目录；当前实现尚不能证明
        # 该对象可重入。因此在单进程内串行化真实翻译 I/O，不能只依赖初始化锁。
        self._execution_lock = threading.RLock()

    def _ensure_translator(self) -> None:
        """确保基础翻译器已初始化（懒加载）"""
        if self._translator is not None:
            return

        with self._init_lock:
            if self._translator is None:
                model_name = os.getenv("DOCSENSE_TRANSLATION_MODEL", "Qwen3-4B-Instruct-2507-Q4_K_M")
                self._translator = HYMTTranslator(model_name=model_name, check_ollama=False)

    def _ensure_document_translator(self) -> None:
        """确保文档翻译器已初始化，并修复可能存在的半初始化状态。"""
        if self._translator is not None and self._document_translator is not None:
            return

        with self._init_lock:
            if self._translator is None:
                model_name = os.getenv("DOCSENSE_TRANSLATION_MODEL", "Qwen3-4B-Instruct-2507-Q4_K_M")
                self._translator = HYMTTranslator(model_name=model_name, check_ollama=False)
            if self._document_translator is None:
                self._document_translator = DocumentTranslator(self._translator)

    def _default_fast_translate(self) -> bool:
        """
        根据环境变量决定默认翻译模式。

        DOCSENSE_TRANSLATION_MODE=machine 使用 argostranslate 机器翻译；
        DOCSENSE_TRANSLATION_MODE=llm 使用本地大模型翻译。
        """
        mode = os.getenv("DOCSENSE_TRANSLATION_MODE", "machine").strip().lower()
        if mode in _MACHINE_TRANSLATION_MODES:
            return True
        if mode in _LLM_TRANSLATION_MODES:
            return False

        logger.warning(
            "环境变量 DOCSENSE_TRANSLATION_MODE 配置无效，默认使用机器翻译: mode=%s",
            mode,
        )
        return True

    def _resolve_fast_translate(self, fast_translate: Optional[bool]) -> bool:
        if fast_translate is not None:
            return fast_translate
        return self._default_fast_translate()

    def translate_document(
            self,
            file_path: str,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: Optional[bool] = None,
            use_minerU: bool = True
    ) -> tuple[str, str]:
        """
        翻译文档并返回双语结果

        :param file_path: 待翻译文件路径
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页/段落
        :param fast_translate: 是否启用快速翻译；不传时读取 DOCSENSE_TRANSLATION_MODE
        :return: (双语 HTML 内容，单语 HTML 内容)
        """
        self._ensure_document_translator()

        if not os.path.exists(file_path):
            return "", ""

        wait_started_at = time.perf_counter()
        try:
            with self._execution_lock:
                logger.debug(
                    "文档翻译进入共享执行区: file_name=%s wait_ms=%d",
                    Path(file_path).name,
                    round((time.perf_counter() - wait_started_at) * 1000),
                )
                # 生成输出路径。路径设置、MinerU 转换和 HTML 读取都必须在同一临界区，
                # 否则另一任务可能在本任务读取前覆盖共享输出目录。
                base_path = Path(file_path)
                output_htmls = base_path.parent / f"{base_path.stem}"

                # 翻译文档（生成双语和单语 HTML，只翻译一次）
                document_translator = self._document_translator
                if document_translator is None:
                    raise RuntimeError("文档翻译器未初始化")

                resolved_fast_translate = self._resolve_fast_translate(fast_translate)
                bilingual_html_path, monolingual_html_path = document_translator.convert_to_html(
                    file_path=str(file_path),
                    output_dir=str(output_htmls),
                    target_lang=target_lang,
                    translate_all=translate_all,
                    fast_translate=resolved_fast_translate,
                )

                # 读取双语 HTML 内容
                bilingual_html_content = ""
                if os.path.exists(bilingual_html_path):
                    bilingual_html_content = Path(bilingual_html_path).read_text(encoding="utf-8", errors="ignore")

                # 读取单语 HTML 内容
                monolingual_html_content = ""
                if os.path.exists(monolingual_html_path):
                    monolingual_html_content = Path(monolingual_html_path).read_text(encoding="utf-8", errors="ignore")

                return bilingual_html_content, monolingual_html_content
        except Exception as e:
            logger.exception(
                "文档翻译失败: file_name=%s error_type=%s",
                Path(file_path).name,
                type(e).__name__,
            )
            return "", ""

    def translate_text_only(
            self,
            text: str,
            target_lang: str = "Chinese",
            fast_translate: Optional[bool] = None,
            as_html: bool = True,
    ) -> str:
        """
        仅翻译纯文本（适用于短文本或摘要）

        :param text: 待翻译文本
        :param target_lang: 目标语言
        :param fast_translate: 是否使用快速翻译；不传时读取 DOCSENSE_TRANSLATION_MODE
        :param as_html: 是否返回带 HTML 标记的格式（用于前端页面展示）
        :return: 翻译后的文本
        """
        self._ensure_translator()

        if not text.strip():
            return ""

        wait_started_at = time.perf_counter()
        try:
            with self._execution_lock:
                logger.debug(
                    "文本翻译进入共享执行区: text_chars=%d wait_ms=%d",
                    len(text),
                    round((time.perf_counter() - wait_started_at) * 1000),
                )
                resolved_fast_translate = self._resolve_fast_translate(fast_translate)
                translator = self._translator
                if translator is None:
                    raise RuntimeError("基础翻译器未初始化")
                translated = translator.translate_text(
                    text,
                    target_lang,
                    fast_translate=resolved_fast_translate,
                )
                if as_html:
                    # 返回HTML格式
                    return f'<div class="translated-text">{self._escape_html(translated)}</div>'
                return translated
        except Exception as e:
            logger.error("文本翻译失败: error_type=%s", type(e).__name__)
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
_translation_service_lock = threading.RLock()


def get_translation_service() -> LLMTranslationService:
    """获取翻译服务单例"""
    global _translation_service_instance
    if _translation_service_instance is None:
        with _translation_service_lock:
            if _translation_service_instance is None:
                _translation_service_instance = LLMTranslationService()
    return _translation_service_instance
