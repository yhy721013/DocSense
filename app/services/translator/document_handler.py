import os
import logging
from typing import Optional
from .core import HYMTTranslator
from .txt_handler import TXTHandler
from .MarkdownHandler import MarkdownHandler
from .mhtml_handler import MHTMLHandler


logger = logging.getLogger(__name__)


class DocumentTranslator:
    """文档翻译器 - 统一接口"""

    def __init__(self, translator: HYMTTranslator):
        """
        初始化文档翻译器
        :param translator: HYMTTranslator 实例
        """
        self.translator = translator
        self.txt_handler = TXTHandler(translator)
        self.markdown_handler = MarkdownHandler(translator)
        self.mhtml_handler = MHTMLHandler(translator)

    def process_file(
            self,
            file_path: str,
            output_path: Optional[str] = None,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True
    ) -> str:
        """
        根据文件后缀自动选择处理方式，均生成 txt 双语翻译文本
        :param file_path: 输入文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页/段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: 输出文件路径
        """
        if not output_path:
            base, ext = os.path.splitext(file_path)
            output_path = f"{base}_translated{ext}"

        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        # 重置进度追踪器
        self.translator.get_progress_tracker().reset()

        # DOCX、PDF、PPTX、XLSX 统一使用 MinerU 模式
        if ext in ['.pdf', '.docx', '.pptx', '.xlsx']:
            logger.info("使用 MinerU 解析文档: file_name=%s", os.path.basename(file_path))

            # 【修复】确定输出目录（与输出文件同目录）
            output_dir = os.path.dirname(os.path.abspath(output_path))
            
            # 1. 先将文档转换为 Markdown
            markdown_path = self.markdown_handler.convert_to_markdown(
                input_path=file_path,
                use_ocr=False,  # 可根据需要调整
                lang="ch",
                extract_images=True,
                formula_enable=True,
                table_enable=True,
                output_dir=output_dir,  # 【新增】指定输出目录
            )

            # 2. 翻译 Markdown
            return self.markdown_handler.process(
                markdown_path=markdown_path,
                output_path=output_path,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate,
            )
        
        # MHTML 特殊处理
        elif ext in ['.mhtml', '.mht']:
            return self.mhtml_handler.process(
                mhtml_path=file_path,
                output_path=output_path,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate,
            )
        
        # TXT 直接处理
        elif ext == '.txt':
            return self.txt_handler.process(
                input_path=file_path,
                output_path=output_path,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate)
        
        # MD 直接处理
        elif ext == '.md':
            return self.markdown_handler.process(
                markdown_path=file_path,
                output_path=output_path,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate,
            )
        else:
            raise ValueError(f"Unsupported file format: {ext}。支持的格式：PDF, DOCX, PPTX, XLSX, MHTML/MHT, TXT, MD")

    def convert_to_html(
            self,
            file_path: str,
            output_dir: str = "./output",
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True
    ) -> tuple[str, str]:
        """
        将文档转换为翻译后的 HTML（中英对照）
        支持：PDF, DOCX, PPTX, XLSX, MHTML/MHT, TXT, MD
        :param file_path: 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页/段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: (双语 HTML 路径，单语 HTML 路径)
        """
        os.makedirs(output_dir, exist_ok=True)

        _, ext = os.path.splitext(file_path)
        ext = ext.lower()

        # 重置进度追踪器
        self.translator.get_progress_tracker().reset()

        # DOCX、PDF、PPTX、XLSX 统一使用 MinerU 模式
        if ext in ['.pdf', '.docx', '.pptx', '.xlsx']:
            logger.info("使用 MinerU 解析 HTML 转换源文件: file_name=%s", os.path.basename(file_path))

            # 1. 先将文档转换为 Markdown
            markdown_path = self.markdown_handler.convert_to_markdown(
                input_path=file_path,
                use_ocr=False,  # 可根据需要调整
                lang="ch",
                extract_images=True,
                formula_enable=True,
                table_enable=True,
                output_dir=output_dir,  # 【新增】指定输出目录
            )

            # 2. 将 Markdown 转换为 HTML 并翻译（返回双语和单语两个路径）
            return self.markdown_handler.convert_to_html(
                markdown_path=markdown_path,
                output_dir=output_dir,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate,
            )
        
        # MHTML 特殊处理
        elif ext in ['.mhtml', '.mht']:
            return self.mhtml_handler.convert_to_html(
                mhtml_path=file_path,
                output_dir=output_dir,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate,
            )
        
        # TXT 直接处理
        elif ext == '.txt':
            bilingual_path, monolingual_path = self.txt_handler.convert_to_html(
                file_path,
                output_dir,
                target_lang,
                translate_all,
                fast_translate
            )
            return bilingual_path, monolingual_path
        
        # MD 直接处理
        elif ext == '.md':
            bilingual_path, monolingual_path = self.markdown_handler.convert_to_html(
                markdown_path=file_path,
                output_dir=output_dir,
                target_lang=target_lang,
                translate_all=translate_all,
                fast_translate=fast_translate,
            )
            return bilingual_path, monolingual_path
        else:
            raise ValueError(f"不支持的文件格式：{ext}。支持的格式：PDF, DOCX, PPTX, XLSX, MHTML/MHT, TXT, MD")

    def get_progress(self) -> dict:
        """
        获取当前处理进度
        :return: 包含进度信息的字典
        """
        tracker = self.translator.get_progress_tracker()
        return tracker.get_progress()
