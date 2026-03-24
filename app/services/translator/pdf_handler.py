import os
import re
import pdfplumber
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from typing import Optional, List, Dict
from .core import HYMTTranslator
from pdf2docx import Converter
import shutil



class PDFHandler:
    """PDF 文档处理器 - 先转为 DOCX 再处理"""

    def __init__(self, translator: HYMTTranslator):
        self.translator = translator
        self._register_chinese_font()

    def _register_chinese_font(self):
        """注册中文字体"""
        try:
            font_path = os.path.join(os.environ['WINDIR'], 'Fonts', 'simsun.ttc')
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont('SimSun', font_path))
                self.font_name = 'SimSun'
            else:
                self.font_name = 'Helvetica'
        except:
            self.font_name = 'Helvetica'

    def _convert_pdf_to_docx(self, pdf_path: str, docx_path: str) -> bool:
        """
        将 PDF 转换为 DOCX
        :param pdf_path: PDF 文件路径
        :param docx_path: DOCX 输出路径
        :return: 是否转换成功
        """
        try:
            print(f"正在将 PDF 转换为 DOCX: {os.path.basename(pdf_path)}")
            cv = Converter(pdf_path)
            cv.convert(docx_path)
            cv.close()
            print(f"PDF 转 DOCX 完成：{os.path.basename(docx_path)}")
            return True
        except Exception as e:
            print(f"PDF 转 DOCX 失败：{e}")
            return False

    def _extract_text_with_position(self, path: str) -> List[Dict]:
        """
        提取带位置信息的文本（保留原有方法，用于 process 方法）
        :param path: PDF 文件路径
        :return: 包含文本和位置信息的列表
        """
        paragraphs = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                chars = page.chars
                if chars:
                    current_line = []
                    current_y = None

                    for char in chars:
                        y_pos = char['top']

                        if current_y is None or abs(y_pos - current_y) < 3:
                            current_line.append(char)
                            current_y = y_pos
                        else:
                            if current_line:
                                line_text = "".join([c['text'] for c in current_line])
                                if line_text.strip():
                                    paragraphs.append({
                                        'text': self._clean_text(line_text),
                                        'x': min([c['x0'] for c in current_line]),
                                        'y': current_y,
                                        'font_size': sum([c.get('size', 10) for c in current_line]) / len(current_line)
                                    })
                            current_line = [char]
                            current_y = y_pos

                    if current_line:
                        line_text = "".join([c['text'] for c in current_line])
                        if line_text.strip():
                            paragraphs.append({
                                'text': self._clean_text(line_text),
                                'x': min([c['x0'] for c in current_line]),
                                'y': current_y,
                                'font_size': sum([c.get('size', 10) for c in current_line]) / len(current_line)
                            })
        return paragraphs

    def _clean_text(self, text: str) -> str:
        """清理文本"""
        text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]', '', text)
        text = re.sub(r'[■▐█▄▀▌░▓]', '', text)
        return text.strip()

    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&#39;')
        return text

    def process(
            self,
            input_path: str,
            output_path: Optional[str] = None,
            target_lang: str = "Chinese",
            translate_all: int = 0
    ) -> str:
        """
        处理 PDF 文档翻译（保留位置信息）
        :param input_path: 输入文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页
        :return: 输出文件路径
        """
        if not output_path:
            base, _ = os.path.splitext(input_path)
            output_path = f"{base}_translated.pdf"

        print(f"Processing PDF: {input_path}")
        text_blocks = self._extract_text_with_position(input_path)

        doc = SimpleDocTemplate(output_path, pagesize=A4)
        styles = getSampleStyleSheet()

        story = []
        current_y = 0

        tracker = self.translator.get_progress_tracker()
        tracker.set_file_info(os.path.basename(input_path), len(text_blocks), "paragraph")

        for idx, block in enumerate(text_blocks):
            if translate_all > 0 and idx >= translate_all:
                break

            if block['font_size'] < 6:
                continue

            orig_text = block['text']
            translated = self.translator.translate_text(orig_text, target_lang)

            style = ParagraphStyle(
                'Body',
                parent=styles['Normal'],
                fontName=self.font_name,
                fontSize=block['font_size'],
                leading=block['font_size'] * 1.2,
                spaceAfter=6
            )

            story.append(Paragraph(orig_text, style))
            story.append(Paragraph(translated, style))
            story.append(Spacer(1, 4))

            tracker.update_paragraph(idx + 1)

        doc.build(story)
        tracker.mark_completed()
        print(f"PDF saved to: {output_path}")
        return output_path

    def convert_to_html_translated(
            self,
            input_path: str,
            output_dir: str,
            target_lang: str = "Chinese",
            show_bilingual: bool = True,
            translate_all: int = 0
    ) -> str:
        """
        将 PDF 转换为 HTML 并翻译（中英对照）
        新逻辑：PDF -> DOCX -> HTML
        :param input_path: PDF 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param show_bilingual: 是否显示中英对照
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 页
        :return: 输出的 HTML 文件路径
        """
        os.makedirs(output_dir, exist_ok=True)

        # 创建临时文件夹
        temp_folder = os.path.join(output_dir, "temp_translate")
        os.makedirs(temp_folder, exist_ok=True)

        try:
            # 1. 将 PDF 转换为 DOCX
            base_name = os.path.basename(input_path)
            name_without_ext = os.path.splitext(base_name)[0]
            temp_docx_path = os.path.join(temp_folder, f"{name_without_ext}.docx")

            print(f"\n{'=' * 50}")
            print(f"步骤 1: 将 PDF 转换为 DOCX")
            print(f"{'=' * 50}")
            success = self._convert_pdf_to_docx(input_path, temp_docx_path)

            if not success:
                raise RuntimeError("PDF 转 DOCX 失败")

            # 2. 调用 DocxHandler 处理 DOCX 生成 HTML
            print(f"\n{'=' * 50}")
            print(f"步骤 2: 将 DOCX 转换为 HTML 并翻译")
            print(f"{'=' * 50}")

            from .docx_handler import DocxHandler
            docx_handler = DocxHandler(self.translator)

            # 注意：translate_all 参数传递给 DOCX  handler
            # 由于 PDF 的 translate_all 是页数，DOCX 是段落数，这里直接传入
            html_output_path = docx_handler.convert_to_html(
                input_path=temp_docx_path,
                output_dir=output_dir,
                target_lang=target_lang,
                show_bilingual=show_bilingual,
                translate_all=translate_all * 5 if translate_all > 0 else 0
            )

            print(f"\n{'=' * 50}")
            print(f"PDF 转 HTML 完成！最终输出：{html_output_path}")
            print(f"{'=' * 50}")

            return html_output_path

        except Exception as e:
            print(f"\n处理 PDF 时出错：{e}")
            raise
        finally:
            # 3. 清理临时文件夹
            if os.path.exists(temp_folder):
                print(f"\n清理临时文件夹：{temp_folder}")
                try:
                    shutil.rmtree(temp_folder)
                    print("临时文件夹已删除")
                except Exception as e:
                    print(f"清理临时文件夹失败：{e}")


def is_point_in_rect(px, py, rect):
    """判断点 (px, py) 是否在矩形 rect (x0, y0, x1, y1) 内"""
    return rect[0] <= px <= rect[2] and rect[1] <= py <= rect[3]


def is_block_in_table(block_bbox, table_bbox):
    """
    判断一个文本块 (block_bbox) 是否主要位于表格 (table_bbox) 内部。
    采用中心点判断法。
    """
    if not block_bbox or not table_bbox:
        return False
    center_x = (block_bbox[0] + block_bbox[2]) / 2
    center_y = (block_bbox[1] + block_bbox[3]) / 2
    return is_point_in_rect(center_x, center_y, table_bbox)
