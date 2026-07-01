import os
import logging
from typing import Optional
from pathlib import Path

from .mhtml2pdf import MHTMLToPDFConverter
from .MarkdownHandler import MarkdownHandler
from .core import HYMTTranslator


logger = logging.getLogger(__name__)


class MHTMLHandler:
    """MHTML 文档处理器 - 先转 PDF，再经 MinerU 转 Markdown，后续复用 MarkdownHandler 翻译流程"""

    def __init__(self, translator: HYMTTranslator):
        """
        初始化 MHTML 处理器
        :param translator: HYMTTranslator 实例
        """
        self.translator = translator
        self.pdf_converter = MHTMLToPDFConverter()
        self.markdown_handler = MarkdownHandler(translator)

    def _convert_mhtml_to_markdown(self, mhtml_path: str, output_dir: str) -> str:
        """
        MHTML → PDF → Markdown
        :param mhtml_path: MHTML 文件路径
        :param output_dir: 输出目录（Markdown 及其中间文件保存位置）
        :return: Markdown 文件路径
        """
        base_name = Path(mhtml_path).stem
        short_name = base_name[:10] + "_mhtml2pdf" if len(base_name) > 10 else f"{base_name}_mhtml2pdf"

        # 步骤 1: MHTML → PDF (short name to avoid Windows MAX_PATH)
        pdf_path = os.path.join(output_dir, f"{short_name}.pdf")
        self.pdf_converter.convert(mhtml_path, pdf_path)

        # 步骤 2: PDF → Markdown (MinerU)
        md_path = self.markdown_handler.convert_to_markdown(
            input_path=pdf_path,
            use_ocr=False,
            lang="ch",
            extract_images=True,
            formula_enable=True,
            table_enable=True,
            output_dir=output_dir,
        )

        logger.info("MHTML Markdown saved: %s", md_path)
        return md_path

    def process(
            self,
            mhtml_path: str,
            output_path: Optional[str] = None,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True,
    ) -> str:
        """
        处理 MHTML 文档翻译（生成 TXT 双语对照）
        流程: MHTML → PDF → Markdown → 翻译 TXT
        :param mhtml_path: MHTML 文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: 输出文件路径
        """
        if not output_path:
            base, _ = os.path.splitext(mhtml_path)
            output_path = f"{base}_translated.txt"

        self.translator.get_progress_tracker().reset()

        # MHTML → Markdown（中间文件保存在 MHTML 所在目录）
        input_dir = os.path.dirname(os.path.abspath(mhtml_path))
        md_path = self._convert_mhtml_to_markdown(mhtml_path, input_dir)

        # 复用 MarkdownHandler 翻译流程
        return self.markdown_handler.process(
            markdown_path=md_path,
            output_path=output_path,
            target_lang=target_lang,
            translate_all=translate_all,
            fast_translate=fast_translate,
        )

    def convert_to_html(
            self,
            mhtml_path: str,
            output_dir: str,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True,
    ) -> tuple[str, str]:
        """
        将 MHTML 转换为翻译后的 HTML（中英对照）
        流程: MHTML → PDF → Markdown → 翻译 HTML
        :param mhtml_path: MHTML 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: (双语 HTML 路径，单语 HTML 路径)
        """
        os.makedirs(output_dir, exist_ok=True)
        self.translator.get_progress_tracker().reset()

        # MHTML → Markdown（中间文件保存在输出目录）
        md_path = self._convert_mhtml_to_markdown(mhtml_path, output_dir)

        # 复用 MarkdownHandler 转换 HTML 流程
        return self.markdown_handler.convert_to_html(
            markdown_path=md_path,
            output_dir=output_dir,
            target_lang=target_lang,
            translate_all=translate_all,
            fast_translate=fast_translate,
        )
