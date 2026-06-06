import os
import re
from email import message_from_string
from typing import Optional, List, Tuple
from bs4 import BeautifulSoup
from .core import HYMTTranslator
from .chunk_processor import ChunkProcessor


class MHTMLHandler:
    """MHTML 文档处理器 - 保留格式翻译"""

    def __init__(self, translator: HYMTTranslator):
        """
        初始化 MHTML 处理器
        :param translator: HYMTTranslator 实例
        """
        self.translator = translator
        self.chunk_processor = ChunkProcessor(translator.model_name)

    def _parse_mhtml(self, mhtml_path: str) -> tuple[str, dict]:
        """
        解析 MHTML 文件，提取 HTML 内容和元数据
        :param mhtml_path: MHTML 文件路径
        :return: (HTML 内容, 元数据字典)
        """
        print(f"\n{'=' * 60}")
        print(f"步骤 1: 解析 MHTML 文件")
        print(f"{'=' * 60}")

        with open(mhtml_path, 'r', encoding='utf-8', errors='ignore') as f:
            mhtml_content = f.read()

        # 使用 email 模块解析 MIME 结构
        msg = message_from_string(mhtml_content)

        html_content = ""
        metadata = {
            'subject': '',
            'date': '',
            'from': '',
        }

        # 提取邮件头信息
        if msg.get('Subject'):
            metadata['subject'] = msg.get('Subject')
        if msg.get('Date'):
            metadata['date'] = msg.get('Date')
        if msg.get('From'):
            metadata['from'] = msg.get('From')

        # 提取 HTML 部分
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == 'text/html':
                    charset = part.get_content_charset() or 'utf-8'
                    html_content = part.get_payload(decode=True).decode(charset, errors='ignore')
                    break
        else:
            # 非多部分 MHTML
            content_type = msg.get_content_type()
            if content_type == 'text/html':
                charset = msg.get_content_charset() or 'utf-8'
                html_content = msg.get_payload(decode=True).decode(charset, errors='ignore')

        if not html_content:
            raise ValueError("无法从 MHTML 文件中提取 HTML 内容")

        print(f"[MHTML 解析] ✓ 成功提取 HTML 内容（{len(html_content)} 字符）")
        return html_content, metadata

    def _extract_html_and_css(self, html_content: str) -> Tuple[str, str]:
        """
        从 HTML 中提取主体内容和 CSS 样式
        :param html_content: 完整 HTML 内容
        :return: (body HTML, CSS 样式)
        """
        print(f"\n{'=' * 60}")
        print(f"步骤 2: 提取 HTML 主体和 CSS 样式")
        print(f"{'=' * 60}")

        soup = BeautifulSoup(html_content, 'html.parser')

        # 提取所有 style 标签中的 CSS
        css_parts = []
        for style_tag in soup.find_all('style'):
            css_parts.append(style_tag.get_text())
        
        css_content = '\n'.join(css_parts)

        # 获取 body 内容（保留完整结构）
        body_tag = soup.find('body')
        if body_tag:
            body_html = str(body_tag)
        else:
            # 如果没有 body 标签，使用整个 HTML
            body_html = str(soup)

        print(f"[HTML 提取] ✓ 提取完成（Body: {len(body_html)} 字符, CSS: {len(css_content)} 字符）")
        return body_html, css_content

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

        print(f"\nProcessing MHTML: {mhtml_path}")

        # 步骤 1: 解析 MHTML
        html_content, metadata = self._parse_mhtml(mhtml_path)

        # 步骤 2: 提取 HTML 主体和 CSS
        body_html, css_content = self._extract_html_and_css(html_content)

        # 步骤 3: 将 HTML 转换为文本段落进行翻译
        paragraphs = self._extract_text_from_html(body_html)

        tracker = self.translator.get_progress_tracker()
        paras_to_process = len(paragraphs) if translate_all == 0 else min(translate_all, len(paragraphs))
        tracker.set_file_info(os.path.basename(mhtml_path), paras_to_process, "paragraph")

        # 步骤 4: 翻译段落
        if fast_translate:
            print(f"\n[MHTML 处理] 使用快速翻译模式（ArgoTranslate），共 {paras_to_process} 个段落...")
            translated_paragraphs = self._translate_paragraphs_one_by_one(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=True
            )
        else:
            print(f"\n[MHTML 处理] 使用大模型批量翻译模式，共 {paras_to_process} 个段落...")
            translated_paragraphs = self._batch_translate_paragraphs(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=False
            )

        # 未翻译的段落保持原样
        final_paragraphs = translated_paragraphs + paragraphs[paras_to_process:]

        # 步骤 5: 生成输出（保持原有格式）
        results = []
        for idx, (orig, trans) in enumerate(zip(paragraphs, final_paragraphs)):
            if orig.strip():
                results.append(f"{orig}\n\n{trans}\n\n{'-' * 30}\n\n")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("".join(results))

        tracker.mark_completed()
        print(f"\nTXT saved to: {output_path}")
        return output_path

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
        :param mhtml_path: MHTML 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: (双语 HTML 路径，单语 HTML 路径)
        """
        os.makedirs(output_dir, exist_ok=True)

        print(f"\n{'=' * 60}")
        print(f"MHTML 转 HTML 翻译：{mhtml_path}")
        print(f"{'=' * 60}")

        # 步骤 1: 解析 MHTML
        html_content, metadata = self._parse_mhtml(mhtml_path)

        # 步骤 2: 提取 HTML 主体和 CSS
        body_html, css_content = self._extract_html_and_css(html_content)

        # 步骤 3: 翻译 HTML 内容（保留格式）
        bilingual_html = self._translate_html_content(
            html_content=body_html,
            target_lang=target_lang,
            show_bilingual=True,
            translate_all=translate_all,
            fast_translate=fast_translate,
        )

        # 步骤 4: 生成单语 HTML（只保留译文）
        monolingual_html = self._convert_bilingual_to_monolingual(bilingual_html)

        # 步骤 5: 构建完整 HTML 文档
        base_name = os.path.basename(mhtml_path)
        name_without_ext = os.path.splitext(base_name)[0]

        bilingual_output_path = os.path.join(output_dir, f"{name_without_ext}_bilingual.html")
        monolingual_output_path = os.path.join(output_dir, f"{name_without_ext}_monolingual.html")

        # 保存双语 HTML
        full_bilingual_html = self._build_full_html(bilingual_html, css_content, metadata)
        with open(bilingual_output_path, 'w', encoding='utf-8') as f:
            f.write(full_bilingual_html)

        # 保存单语 HTML
        full_monolingual_html = self._build_full_html(monolingual_html, css_content, metadata)
        with open(monolingual_output_path, 'w', encoding='utf-8') as f:
            f.write(full_monolingual_html)

        print(f"双语 HTML 已保存至：{bilingual_output_path}")
        print(f"单语 HTML 已保存至：{monolingual_output_path}")
        return bilingual_output_path, monolingual_output_path

    def _extract_text_from_html(self, html_content: str) -> List[str]:
        """
        从 HTML 中提取文本段落
        :param html_content: HTML 内容
        :return: 文本段落列表
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 移除脚本和样式标签
        for script_or_style in soup(['script', 'style']):
            script_or_style.decompose()
        
        # 提取所有文本节点
        paragraphs = []
        for element in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'th']):
            text = element.get_text(strip=True)
            if text:
                paragraphs.append(text)
        
        # 如果没有找到结构化元素，使用整个文本
        if not paragraphs:
            text = soup.get_text(strip=True)
            if text:
                paragraphs = [text]
        
        return paragraphs

    def _translate_html_content(
            self,
            html_content: str,
            target_lang: str,
            show_bilingual: bool,
            translate_all: int,
            fast_translate: bool,
    ) -> str:
        """
        翻译 HTML 内容（保留 HTML 结构）
        :param html_content: HTML 内容
        :param target_lang: 目标语言
        :param show_bilingual: 是否显示双语对照
        :param translate_all: 翻译范围
        :param fast_translate: 是否快速翻译
        :return: 翻译后的 HTML
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 查找所有需要翻译的文本元素
        text_elements = soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'td', 'th'])
        
        tracker = self.translator.get_progress_tracker()
        elements_to_process = len(text_elements) if translate_all == 0 else min(translate_all, len(text_elements))
        tracker.set_file_info("MHTML HTML", elements_to_process, "element")
        
        print(f"\n[HTML 翻译] 开始翻译 HTML 内容，共 {elements_to_process} 个元素...\n")
        
        processed_count = 0
        for idx, element in enumerate(text_elements):
            if translate_all > 0 and idx >= translate_all:
                break
            
            original_text = element.get_text(strip=True)
            if not original_text:
                continue
            
            # 计算并显示进度条
            current_progress = (processed_count + 1) / elements_to_process * 100 if elements_to_process > 0 else 0
            progress_bar = self._create_progress_bar(current_progress, width=30)
            
            # 检测是否为中文
            if self._is_chinese_text(original_text):
                # 中文不翻译，添加标记
                if show_bilingual:
                    element.string = f'<span class="original-text">{original_text}</span>'
                print(f"\r[{progress_bar}] {current_progress:.1f}% | 元素 {processed_count + 1}/{elements_to_process} [中文跳过]", end="", flush=True)
            else:
                try:
                    translated_text = self.translator.translate_text(
                        original_text,
                        target_lang,
                        fast_translate=fast_translate
                    )
                    
                    # 生成双语对照结构
                    if show_bilingual:
                        bilingual_html = f'''<span class="original-text">{original_text}</span>
<span class="translated-text">{translated_text}</span>'''
                        element.string = ''
                        element.append(BeautifulSoup(bilingual_html, 'html.parser'))
                    else:
                        element.string = translated_text
                    
                    print(f"\r[{progress_bar}] {current_progress:.1f}% | 元素 {processed_count + 1}/{elements_to_process} ✓ {len(translated_text)}字", end="", flush=True)
                except Exception as e:
                    print(f"\r[{progress_bar}] {current_progress:.1f}% | 元素 {processed_count + 1}/{elements_to_process} ✗ 翻译失败: {e}", end="", flush=True)
                
                processed_count += 1
                tracker.update_paragraph(processed_count)
        
        print(f"\n[完成] HTML 内容翻译完毕，共处理 {processed_count} 个元素\n")
        return str(soup)

    def _convert_bilingual_to_monolingual(self, bilingual_html: str) -> str:
        """
        将双语 HTML 转换为单语 HTML（只保留译文）
        :param bilingual_html: 双语 HTML 内容
        :return: 单语 HTML 内容
        """
        soup = BeautifulSoup(bilingual_html, 'html.parser')
        
        # 移除所有 original-text span，只保留 translated-text
        for original_span in soup.find_all('span', class_='original-text'):
            translated_span = original_span.find_next_sibling('span', class_='translated-text')
            if translated_span:
                # 用译文替换原文
                original_span.replace_with(translated_span)
            else:
                # 如果没有译文，直接删除原文
                original_span.decompose()
        
        return str(soup)

    def _build_full_html(self, body_html: str, css_content: str, metadata: dict) -> str:
        """
        构建完整 HTML 文档
        :param body_html: Body HTML 内容
        :param css_content: CSS 样式
        :param metadata: 元数据
        :return: 完整 HTML 文档
        """
        subject = metadata.get('subject', 'Translated Document')
        
        html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{subject}</title>
    <style>
        body {{ 
            margin: 0; 
            padding: 20px; 
            background: #f5f5f5; 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            line-height: 1.6;
        }}
        .document-container {{
            max-width: 900px;
            margin: 0 auto;
            background: white;
            padding: 40px;
            box-shadow: 0 0 10px rgba(0,0,0,0.1);
        }}
        /* 双语翻译样式 */
        .original-text {{
            color: #333;
            display: block;
            margin-bottom: 8px;
        }}
        .translated-text {{
            color: #0066cc;
            display: block;
            font-weight: 500;
            border-top: 1px dashed #e0e0e0;
            padding-top: 8px;
            margin-top: 8px;
        }}
        /* 用户自定义 CSS */
        {css_content}
    </style>
</head>
<body>
    <div class="document-container">
        {body_html}
    </div>
</body>
</html>"""
        
        return html_template

    def _is_chinese_text(self, text: str) -> bool:
        """
        检测文本是否主要为中文
        :param text: 待检测文本
        :return: True 表示主要是中文
        """
        import re
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        arabic_numerals = re.findall(r'[0-9]', text)
        chinese_punctuation = re.findall(r'[\u3000-\u303f\uff00-\uffef]', text)

        if len(text) > 0:
            chinese_or_digit_count = len(chinese_chars) + len(arabic_numerals) + len(chinese_punctuation)
            ratio = chinese_or_digit_count / len(text)
            return ratio >= 0.7
        return False

    def _translate_paragraphs_one_by_one(
            self,
            paragraphs: list,
            target_lang: str,
            tracker,
            fast_translate: bool = True,
    ) -> list:
        """
        逐段翻译段落（用于快速翻译模式）
        """
        if not paragraphs:
            return []

        translated_paragraphs = []

        for idx, para in enumerate(paragraphs):
            if not para.strip():
                translated_paragraphs.append("")
                print(f"  [空段落] 段落 {idx + 1}")
            elif self._is_chinese_text(para):
                print(f"[跳过] 段落 {idx + 1} 为中文，已跳过翻译")
                translated_paragraphs.append(para)
            else:
                try:
                    translated_para = self.translator.translate_text(
                        para,
                        target_lang,
                        fast_translate=fast_translate
                    )
                    translated_paragraphs.append(translated_para)
                    print(f"  ✓ 段落 {idx + 1}: {len(translated_para)} 字")
                    tracker.update_paragraph(idx + 1)
                except Exception as e:
                    fallback_text = f"[翻译失败：{str(e)}]"
                    translated_paragraphs.append(fallback_text)
                    print(f"  ✗ 段落 {idx + 1}: {fallback_text}")
                    tracker.update_paragraph(idx + 1)

        print("\n")
        return translated_paragraphs

    def _batch_translate_paragraphs(
            self,
            paragraphs: list,
            target_lang: str,
            tracker,
            fast_translate: bool = False,
    ) -> list:
        """
        批量翻译段落（利用大模型上下文窗口）
        """
        if not paragraphs:
            return []

        # 过滤掉中文段落
        processed_paragraphs = []
        translation_needed = []

        for idx, para in enumerate(paragraphs):
            if not para.strip():
                processed_paragraphs.append("")
            elif self._is_chinese_text(para):
                print(f"[跳过] 段落 {idx + 1} 为中文，已跳过翻译")
                processed_paragraphs.append(para)
            else:
                processed_paragraphs.append(None)
                translation_needed.append((idx, para))

        if not translation_needed:
            return processed_paragraphs

        # 创建分块
        chunks = self.chunk_processor.create_chunks_with_mapping(
            [para for _, para in translation_needed],
            target_lang
        )

        print(f"\n[批量翻译] 共 {len(translation_needed)} 段需要翻译，分为 {len(chunks)} 个批次")

        # 逐批翻译
        translated_idx = 0
        for chunk_idx, chunk in enumerate(chunks):
            current_progress = (chunk_idx + 1) / len(chunks) * 100
            progress_bar = self._create_progress_bar(current_progress, width=30)
            print(f"\r[{progress_bar}] {current_progress:.1f}% | 批次 {chunk_idx + 1}/{len(chunks)}", end="",
                  flush=True)

            try:
                translated_chunk_text = self.translator.translate_text(
                    chunk["text"],
                    target_lang,
                    fast_translate=False,
                )

                translated_paras = self.chunk_processor.parse_translated_chunks(
                    translated_chunk_text,
                    len(chunk["paragraph_indices"])
                )

                for para_local_idx, global_para_idx in enumerate(chunk["paragraph_indices"]):
                    original_idx, _ = translation_needed[global_para_idx]

                    if para_local_idx < len(translated_paras):
                        translated_para = translated_paras[para_local_idx]
                        processed_paragraphs[original_idx] = translated_para
                        print(f"  ✓ 段落 {original_idx + 1}: {len(translated_para)} 字")
                    else:
                        fallback_text = f"[部分翻译失败]"
                        processed_paragraphs[original_idx] = fallback_text
                        print(f"  ✗ 段落 {original_idx + 1}: {fallback_text}")

                    translated_idx += 1
                    tracker.update_paragraph(translated_idx)

            except Exception as e:
                print(f"\n[错误] 批次 {chunk_idx + 1} 翻译失败：{e}")
                for para_local_idx, global_para_idx in enumerate(chunk["paragraph_indices"]):
                    original_idx, _ = translation_needed[global_para_idx]

                    try:
                        translated_para = self.translator.translate_text(
                            translation_needed[global_para_idx][1],
                            target_lang,
                            fast_translate=False
                        )
                        processed_paragraphs[original_idx] = translated_para
                        print(f"  ✓ 段落 {original_idx + 1} (回退): {len(translated_para)} 字")
                    except Exception as e2:
                        fallback_text = f"[翻译失败：{str(e2)}]"
                        processed_paragraphs[original_idx] = fallback_text
                        print(f"  ✗ 段落 {original_idx + 1} (回退): {fallback_text}")

                    translated_idx += 1
                    tracker.update_paragraph(translated_idx)

        print("\n")
        return processed_paragraphs

    def _create_progress_bar(self, percentage: float, width: int = 30) -> str:
        """创建进度条字符串"""
        percentage = max(0, min(100, percentage))
        filled_length = int(width * percentage / 100)
        bar = '█' * filled_length + '░' * (width - filled_length)
        return bar
