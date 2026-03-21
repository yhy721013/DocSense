import os
from typing import Optional
from .core import HYMTTranslator
from .docx_handler import DocxHandler
from .pdf_handler import PDFHandler
from .txt_handler import TXTHandler


class DocumentTranslator:
    """文档翻译器 - 统一接口"""

    def __init__(self, translator: HYMTTranslator):
        """
        初始化文档翻译器
        :param translator: HYMTTranslator 实例
        """
        self.translator = translator
        self.docx_handler = DocxHandler(translator)
        self.pdf_handler = PDFHandler(translator)
        self.txt_handler = TXTHandler(translator)

    def process_file(
            self,
            file_path: str,
            output_path: Optional[str] = None,
            target_lang: str = "Chinese",
            translate_all: int = 0
    ) -> str:
        """
        根据文件后缀自动选择处理方式
        :param file_path: 输入文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页/段落
        :return: 输出文件路径
        """
        if not output_path:
            base, ext = os.path.splitext(file_path)
            output_path = f"{base}_translated{ext}"

        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        # 重置进度追踪器
        self.translator.get_progress_tracker().reset()

        if ext == '.pdf':
            return self.pdf_handler.process(file_path, output_path, target_lang, translate_all)
        elif ext in ['.docx', '.doc']:
            return self.docx_handler.process(file_path, output_path, target_lang, translate_all)
        elif ext == '.txt':
            return self.txt_handler.process(file_path, output_path, target_lang, translate_all)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    def convert_to_html(
            self,
            file_path: str,
            output_dir: str = "./output",
            target_lang: str = "Chinese",
            show_bilingual: bool = True,
            translate_all: int = 0
    ) -> str:
        """
        将文档转换为翻译后的 HTML（中英对照）
        支持：PDF, DOCX, TXT
        :param file_path: 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param show_bilingual: 是否显示中英对照
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页/段落
        :return: 输出的 HTML 文件路径
        """
        os.makedirs(output_dir, exist_ok=True)

        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        # 重置进度追踪器
        self.translator.get_progress_tracker().reset()

        if ext == '.pdf':
            return self.pdf_handler.convert_to_html_translated(
                file_path,
                output_dir,
                target_lang,
                show_bilingual,
                translate_all
            )
        elif ext in ['.docx', '.doc']:
            return self.docx_handler.convert_to_html(
                file_path,
                output_dir,
                target_lang,
                show_bilingual,
                translate_all
            )
        elif ext == '.txt':
            return self.txt_handler.convert_to_html(
                file_path,
                output_dir,
                target_lang,
                show_bilingual,
                translate_all
            )
        else:
            raise ValueError(f"不支持的文件格式：{ext}。支持的格式：PDF, DOCX, TXT")

    def get_progress(self) -> dict:
        """
        获取当前处理进度
        :return: 包含进度信息的字典
        """
        tracker = self.translator.get_progress_tracker()
        return tracker.get_progress()
