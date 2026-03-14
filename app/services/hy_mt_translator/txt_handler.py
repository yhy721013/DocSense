import os
import re
from typing import Optional
from .core import HYMTTranslator


class TXTHandler:
    """文本文件 (.txt) 处理器"""

    def __init__(self, translator: HYMTTranslator):
        self.translator = translator

    def _is_chinese_text(self, text: str) -> bool:
        """
        检测文本是否主要为中文
        :param text: 待检测文本
        :return: True 表示主要是中文，False 表示非中文
        """
        # 统计中文字符数量
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)

        # 如果中文字符占比超过 80%，则认为是中文文本
        if len(text) > 0:
            chinese_ratio = len(chinese_chars) / len(text)
            return chinese_ratio >= 0.8
        return False

    def process(
            self,
            input_path: str,
            output_path: Optional[str] = None,
            target_lang: str = "Chinese",
            translate_all: int = 0
    ) -> str:
        """
        处理文本文件翻译
        :param input_path: 输入文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :return: 输出文件路径
        """
        if not output_path:
            base, _ = os.path.splitext(input_path)
            output_path = f"{base}_translated.txt"

        print(f"Processing TXT: {input_path}")

        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()

        paragraphs = content.split('\n\n')

        tracker = self.translator.get_progress_tracker()
        paras_to_process = len(paragraphs) if translate_all == 0 else min(translate_all, len(paragraphs))
        tracker.set_file_info(os.path.basename(input_path), paras_to_process, "paragraph")

        results = []
        for idx, para in enumerate(paragraphs):
            if translate_all > 0 and idx >= translate_all:
                break

            if not para.strip():
                continue

            orig = para.strip()

            # 检测是否为中文，如果是则跳过翻译
            if self._is_chinese_text(orig):
                # 中文段落不翻译
                trans = orig
                print(f"[跳过] 段落 {idx + 1} 为中文，已跳过翻译")
            else:
                trans = self.translator.translate_text(orig, target_lang)

            results.append(f"{orig}\n\n{trans}\n\n{'-' * 30}\n\n")

            tracker.update_paragraph(idx + 1)

        tracker.mark_completed()

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("".join(results))

        print(f"TXT saved to: {output_path}")
        return output_path

    def convert_to_html(
            self,
            input_path: str,
            output_dir: str,
            target_lang: str = "Chinese",
            show_bilingual: bool = True,
            translate_all: int = 0
    ) -> str:
        """
        将 TXT 文件转换为 HTML 并翻译
        :param input_path: TXT 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param show_bilingual: 是否显示中英对照
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :return: 输出的 HTML 文件路径
        """
        os.makedirs(output_dir, exist_ok=True)

        print(f"Processing TXT to HTML: {input_path}")

        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()

        paragraphs = content.split('\n\n')

        tracker = self.translator.get_progress_tracker()
        valid_paragraphs = [p for p in paragraphs if p.strip()]
        paras_to_process = len(valid_paragraphs) if translate_all == 0 else min(translate_all, len(valid_paragraphs))
        tracker.set_file_info(os.path.basename(input_path), paras_to_process, "paragraph")

        html_content = []

        html_header = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Text Translated - Bilingual</title>
            <style>
                body { 
                    margin: 0; 
                    padding: 20px; 
                    background: #f5f5f5; 
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                }
                .document-container {
                    max-width: 800px;
                    margin: 0 auto;
                    background: white;
                    padding: 40px;
                    box-shadow: 0 0 10px rgba(0,0,0,0.1);
                    line-height: 1.6;
                }
                .paragraph {
                    margin-bottom: 20px;
                    padding: 15px;
                    border-left: 3px solid #e0e0e0;
                }
                .original-text {
                    color: #000000;
                    margin-bottom: 10px;
                    white-space: pre-wrap;
                }
                .translated-text {
                    color: #0066cc;
                    font-weight: bold;
                    padding-top: 10px;
                    border-top: 1px dashed #e0e0e0;
                    white-space: pre-wrap;
                }
            </style>
        </head>
        <body>
            <div class="document-container">
        """
        html_content.append(html_header)

        processed_count = 0
        for para in paragraphs:
            if not para.strip():
                continue

            if translate_all > 0 and processed_count >= translate_all:
                break

            orig = para.strip()

            # 检测是否为中文，如果是则跳过翻译
            if self._is_chinese_text(orig):
                # 中文段落不翻译，原文和译文相同
                paragraph_html = (
                    f'<div class="paragraph">'
                    f'<div class="original-text">{self._escape_html(orig)}</div>'
                    f'<div class="translated-text">{self._escape_html(orig)}</div>'
                    f'</div>'
                )
                html_content.append(paragraph_html)
            elif show_bilingual:
                translated = self.translator.translate_text(orig, target_lang)

                paragraph_html = (
                    f'<div class="paragraph">'
                    f'<div class="original-text">{self._escape_html(orig)}</div>'
                    f'<div class="translated-text">{self._escape_html(translated)}</div>'
                    f'</div>'
                )
                html_content.append(paragraph_html)
            else:
                translated = self.translator.translate_text(orig, target_lang)
                paragraph_html = (
                    f'<div class="paragraph">'
                    f'<div class="translated-text">{self._escape_html(translated)}</div>'
                    f'</div>'
                )
                html_content.append(paragraph_html)

            processed_count += 1
            tracker.update_paragraph(processed_count)

        html_footer = """
            </div>
        </body>
        </html>
        """
        html_content.append(html_footer)

        base_name = os.path.basename(input_path)
        name_without_ext = os.path.splitext(base_name)[0]
        output_path = os.path.join(output_dir, f"{name_without_ext}_translated.html")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(html_content))

        tracker.mark_completed()
        print(f"翻译后的 HTML 已保存至：{output_path}")
        return output_path

    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&#39;')
        return text

