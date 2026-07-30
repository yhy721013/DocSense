import os
import re
import logging
from typing import Optional, List, Dict, Tuple
from .core import HYMTTranslator
from .chunk_processor import ChunkProcessor
from app.modules.document_processing.adapters.mineru import MinerUConverter
import markdown
import shutil
from pathlib import Path


logger = logging.getLogger(__name__)

class MarkdownHandler:
    """Markdown 文档处理器 - 翻译 Markdown 并转为 HTML"""

    def __init__(self, translator: HYMTTranslator):
        """
        初始化 Markdown 处理器
        :param translator: HYMTTranslator 实例
        """
        self.translator = translator
        self.chunk_processor = ChunkProcessor(translator.model_name)
        self.mineru_converter = MinerUConverter()

    def convert_to_markdown(
            self,
            input_path: str,
            use_ocr: bool = False,
            lang: str = "ch",
            extract_images: bool = True,
            formula_enable: bool = True,
            table_enable: bool = True,
            output_dir: Optional[str] = None,
    ) -> str:
        """
        使用 MinerU 将任意格式文档转换为 Markdown
        :param input_path: 输入文件路径
        :param use_ocr: 是否使用 OCR
        :param lang: 语言 zh/en
        :param extract_images: 是否提取图片
        :param formula_enable: 是否启用公式识别
        :param table_enable: 是否启用表格识别
        :param output_dir: 输出根目录（可选，默认使用 MinerUConverter 的默认目录）
        :return: Markdown 文件路径
        """
        logger.info("开始使用 MinerU 将文档转换为 Markdown")

        if output_dir:
            original_output_dir = self.mineru_converter.output_dir
            self.mineru_converter.output_dir = Path(output_dir).expanduser().resolve()
            self.mineru_converter.output_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.mineru_converter.convert_to_markdown(
                input_path=input_path,
                use_ocr=use_ocr,
                lang=lang,
                extract_images=extract_images,
                formula_enable=formula_enable,
                table_enable=table_enable,
            )
        finally:
            if output_dir:
                self.mineru_converter.output_dir = original_output_dir

        target_dir = Path(output_dir) if output_dir else self.mineru_converter.output_dir
        md_files = list(target_dir.rglob("*.md"))
        if not md_files:
            raise FileNotFoundError(f"No .md file found under: {target_dir}")
        
        # 【关键修改】找到第一个 MD 文件后，将其复制到 output_dir 根目录
        original_md_path = md_files[0]
        if output_dir:
            output_dir_path = Path(output_dir).expanduser().resolve()
            
            # 如果 MD 文件不在 output_dir 根目录，则复制过去
            if original_md_path.parent != output_dir_path:
                new_md_path = output_dir_path / original_md_path.name
                
                # 读取原 MD 文件内容
                with open(original_md_path, 'r', encoding='utf-8') as f:
                    md_content = f.read()
                
                # 写入到 output_dir 根目录
                with open(new_md_path, 'w', encoding='utf-8') as f:
                    f.write(md_content)
                
                logger.info("MinerU Markdown 文件已复制到输出目录: file_name=%s", new_md_path.name)
                md_path = str(new_md_path)
            else:
                md_path = str(original_md_path)
        else:
            md_path = str(original_md_path)

        logger.info("MinerU 文档转换完成: markdown_file=%s", Path(md_path).name)
        return md_path

    def process(
            self,
            markdown_path: str,
            output_path: Optional[str] = None,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True,
    ) -> str:
        """
        处理 Markdown 文档翻译（生成 TXT 双语对照）
        :param markdown_path: Markdown 文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: 输出文件路径
        """
        if not output_path:
            base, _ = os.path.splitext(markdown_path)
            output_path = f"{base}_translated.txt"

        logger.info("开始翻译 Markdown 文档: file_name=%s", os.path.basename(markdown_path))

        # 读取 Markdown 文件
        with open(markdown_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 解析 Markdown 段落（按空行分割）
        paragraphs = self._parse_markdown_paragraphs(content)

        tracker = self.translator.get_progress_tracker()
        paras_to_process = len(paragraphs) if translate_all == 0 else min(translate_all, len(paragraphs))
        tracker.set_file_info(os.path.basename(markdown_path), paras_to_process, "paragraph")

        # 根据是否启用快速翻译选择翻译策略
        if fast_translate:
            logger.info("Markdown 使用快速翻译模式（Argos Translate），共 %d 个段落", paras_to_process)
            translated_paragraphs = self._translate_paragraphs_one_by_one(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=True
            )
        else:
            logger.info("Markdown 使用大模型批量翻译模式，共 %d 个段落", paras_to_process)
            translated_paragraphs = self._batch_translate_paragraphs(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=False
            )

        # 未翻译的段落保持原样
        final_paragraphs = translated_paragraphs + paragraphs[paras_to_process:]

        # 生成输出（保持原有格式）
        results = []
        for idx, (orig, trans) in enumerate(zip(paragraphs, final_paragraphs)):
            if orig.strip():
                results.append(f"{orig}\n\n{trans}\n\n{'-' * 30}\n\n")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("".join(results))

        tracker.mark_completed()
        logger.info("Markdown 翻译结果已保存为 TXT: output_file=%s", os.path.basename(output_path))
        return output_path

    def convert_to_html(
            self,
            markdown_path: str,
            output_dir: str,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True,
    ) -> tuple[str, str]:
        """
        将 Markdown 转换为 HTML 并翻译（中英对照）
        :param markdown_path: Markdown 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: (双语 HTML 路径，单语 HTML 路径)
        """
        os.makedirs(output_dir, exist_ok=True)

        logger.info("开始将 Markdown 转换为 HTML 并翻译: file_name=%s", os.path.basename(markdown_path))

        # 读取 Markdown 文件
        with open(markdown_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 【关键修改】获取 Markdown 文件所在目录（用于处理图片相对路径）
        markdown_dir = os.path.dirname(markdown_path)

        tracker = self.translator.get_progress_tracker()

        # 【关键修改】只翻译一次，同时生成双语和单语 HTML
        html_content = self._convert_markdown_to_html_with_translation(
            markdown_content=content,
            markdown_dir=markdown_dir,
            output_dir=output_dir,
            target_lang=target_lang,
            show_bilingual=True,  # 始终生成双语版本
            translate_all=translate_all,
            fast_translate=fast_translate,
            tracker=tracker,
        )

        # 保存双语 HTML 文件
        base_name = os.path.basename(markdown_path)
        name_without_ext = os.path.splitext(base_name)[0]
        bilingual_output_path = os.path.join(output_dir, f"{name_without_ext}_bilingual.html")

        with open(bilingual_output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        # 【新增】从双语 HTML 生成单语 HTML（只保留译文）
        monolingual_html_content = self._convert_bilingual_to_monolingual(html_content)
        monolingual_output_path = os.path.join(output_dir, f"{name_without_ext}_monolingual.html")

        with open(monolingual_output_path, "w", encoding="utf-8") as f:
            f.write(monolingual_html_content)

        tracker.mark_completed()
        logger.info("双语 HTML 已保存: output_file=%s", os.path.basename(bilingual_output_path))
        logger.info("单语 HTML 已保存: output_file=%s", os.path.basename(monolingual_output_path))
        return bilingual_output_path, monolingual_output_path

    def _convert_bilingual_to_monolingual(self, bilingual_html: str) -> str:
        """
        将双语 HTML 转换为单语 HTML
        - 中文段落：保持 original-text 样式（黑色）
        - 英文段落：移除原文，只保留 translated-text 样式（蓝色译文）
        :param bilingual_html: 双语 HTML 内容
        :return: 单语 HTML 内容
        """
        import re

        # 复制一份双语 HTML
        monolingual_html = bilingual_html

        # 【关键修复1】span 级别的双语结构：移除原文，只保留译文（英文段落）
        # 【重要】使用 [^<]* 替代 .*? 避免跨标签匹配
        span_pattern = r'<span class="original-text">[^<]*</span>\s*<span class="translated-text">([^<]*)</span>'
        span_monolingual_pattern = r'<span class="translated-text">\1</span>'
        monolingual_html = re.sub(span_pattern, span_monolingual_pattern, monolingual_html, flags=re.DOTALL)

        # 【关键修复2】div 级别的双语结构：移除原文，只保留译文（英文段落）
        div_pattern = r'<div class="paragraph">\s*<div class="original-text">[^<]*</div>\s*<div class="translated-text">([^<]*)</div>\s*</div>'
        div_monolingual_pattern = r'<div class="paragraph"><div class="translated-text">\1</div></div>'
        monolingual_html = re.sub(div_pattern, div_monolingual_pattern, monolingual_html, flags=re.DOTALL)

        # 【关键】只有 original-text 的中文段落保持不变（已经是黑色样式）
        # 不需要做任何转换，保持原样即可

        return monolingual_html

    def _convert_markdown_to_html_with_translation(
            self,
            markdown_content: str,
            markdown_dir: str,
            output_dir: str,
            target_lang: str,
            show_bilingual: bool,
            translate_all: int,
            fast_translate: bool,
            tracker
    ) -> str:
        """
        将 Markdown 转换为 HTML 并翻译（保留格式）
        :param markdown_content: Markdown 内容
        :param markdown_dir: Markdown 文件所在目录
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param show_bilingual: 是否双语对照
        :param translate_all: 翻译范围
        :param fast_translate: 是否快速翻译
        :param tracker: 进度追踪器
        :return: HTML 字符串
        """
        # 【关键修改】第一步：使用 markdown 库将 Markdown 转为 HTML（保留 HTML 标签）
        md = markdown.Markdown(
            extensions=[
                'extra',
                'codehilite',
                'nl2br',
                'sane_lists',
            ],
            output_format='html5'
        )

        html_body = md.convert(markdown_content)

        # 【关键修改】第二步：翻译 HTML 中的文本内容（保留 HTML 标签）
        translated_html = self._translate_html_content(
            html_content=html_body,
            target_lang=target_lang,
            show_bilingual=show_bilingual,
            translate_all=translate_all,
            fast_translate=fast_translate,
            tracker=tracker,
        )

        # 【关键修改】第三步：处理图片路径（相对路径转绝对路径）
        translated_html = self._process_image_paths(
            html_content=translated_html,
            markdown_dir=markdown_dir,
            output_dir=output_dir,
        )

        # 构建完整 HTML 文档
        full_html = self._get_html_header()
        full_html += translated_html
        full_html += self._get_html_footer()

        return full_html

    def _translate_html_content(
            self,
            html_content: str,
            target_lang: str,
            show_bilingual: bool,
            translate_all: int,
            fast_translate: bool,
            tracker
    ) -> str:
        """
        翻译 HTML 中的文本内容（保留 HTML 标签）- 生成双语对照
        :param html_content: HTML 内容
        :param target_lang: 目标语言
        :param show_bilingual: 是否双语对照（始终为 True）
        :param translate_all: 翻译范围
        :param fast_translate: 是否快速翻译
        :param tracker: 进度追踪器
        :return: 翻译后的 HTML（包含双语对照）
        """
        # 【关键修改】使用正则表达式提取 HTML 中的文本节点并构建双语对照

        # 匹配 HTML 标签和文本节点
        tag_pattern = r'<[^>]+>'

        # 分割为标签和文本片段
        fragments = re.split(tag_pattern, html_content)
        tags = re.findall(tag_pattern, html_content)

        # 翻译文本片段并构建双语结构
        translated_fragments = []
        total_frags = len([f for f in fragments if f.strip() and not f.strip().startswith('<')])
        processed = 0

        tracker.set_file_info("Markdown HTML", total_frags, "paragraph")

        logger.info("HTML 翻译开始，共 %d 个文本片段", total_frags)

        for idx, fragment in enumerate(fragments):
            # 跳过空片段和纯空白
            if not fragment.strip():
                translated_fragments.append(fragment)
                continue

            # 检查是否是 HTML 标签（不应该被翻译）
            if fragment.strip().startswith('<'):
                translated_fragments.append(fragment)
                continue

            # 【新增】计算并显示进度条
            current_progress = (processed + 1) / total_frags * 100 if total_frags > 0 else 0
            progress_bar = self._create_progress_bar(current_progress, width=30)

            # 翻译文本片段
            try:
                original_text = fragment.strip()

                # 检测是否为中文，如果是则不翻译
                if self._is_chinese_text(original_text):
                    # 【修复】中文文本：双语模式下只显示原文（不需要重复显示）
                    if show_bilingual:
                        bilingual_fragment = f'<span class="original-text">{self._escape_html(original_text)}</span>'
                    else:
                        # 单语模式：也显示原文（因为原文=译文）
                        bilingual_fragment = f'<span class="translated-text">{self._escape_html(original_text)}</span>'
                    translated_fragments.append(bilingual_fragment)
                    logger.debug(
                        "HTML 翻译进度更新，中文片段无需翻译: progress_percent=%.1f "
                        "fragment=%d/%d",
                        current_progress,
                        processed + 1,
                        total_frags,
                    )
                else:
                    # 非中文：需要翻译
                    translated_text = self.translator.translate_text(
                        original_text,
                        target_lang,
                        fast_translate=fast_translate
                    )

                    # 【关键修改】生成双语对照结构
                    if show_bilingual:
                        bilingual_fragment = f'''<span class="original-text">{self._escape_html(original_text)}</span>
<span class="translated-text">{self._escape_html(translated_text)}</span>'''
                    else:
                        bilingual_fragment = f'<span class="translated-text">{self._escape_html(translated_text)}</span>'

                    translated_fragments.append(bilingual_fragment)
                    logger.debug(
                        "HTML 翻译进度更新: progress_percent=%.1f fragment=%d/%d "
                        "translated_chars=%d",
                        current_progress,
                        processed + 1,
                        total_frags,
                        len(translated_text),
                    )

                processed += 1
                tracker.update_paragraph(processed)

            except Exception as e:
                logger.warning(
                    "HTML 文本片段翻译失败，保留原文: fragment_index=%d error_type=%s",
                    idx,
                    type(e).__name__,
                )
                # 失败时保留原文
                translated_fragments.append(f'<span class="original-text">{self._escape_html(fragment)}</span>')
                processed += 1
                tracker.update_paragraph(processed)

        # 【新增】换行，避免覆盖最后的进度信息
        logger.info("HTML 内容翻译完毕，共处理 %d 个片段", processed)

        # 重新组合 HTML
        result_html = ""
        for i in range(len(translated_fragments)):
            result_html += translated_fragments[i]
            if i < len(tags):
                result_html += tags[i]

        return result_html

    def _process_image_paths(
            self,
            html_content: str,
            markdown_dir: str,
            output_dir: str,
    ) -> str:
        """
        处理 HTML 中的图片路径（将图片转换为 Base64 编码嵌入 HTML）
        :param html_content: HTML 内容
        :param markdown_dir: Markdown 文件所在目录
        :param output_dir: 输出目录
        :return: 处理后的 HTML
        """
        import base64

        # 【关键修改】找到所有图片引用
        img_pattern = r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>'

        # 【新增】统计图片数量
        img_matches = re.findall(img_pattern, html_content)
        if img_matches:
            logger.info("发现 %d 张图片，开始转换为 Base64 编码", len(img_matches))

        def replace_img_with_base64(match):
            img_tag = match.group(0)
            img_src = match.group(1)

            # 如果是相对路径或本地绝对路径
            if not img_src.startswith(('http://', 'https://', 'data:')):
                # 构建源图片路径
                src_img_path = os.path.join(markdown_dir, img_src)

                # 如果图片存在，转换为 Base64 编码
                if os.path.exists(src_img_path):
                    try:
                        # 读取图片二进制数据
                        with open(src_img_path, 'rb') as img_file:
                            img_data = img_file.read()

                        # 转换为 Base64 编码
                        img_base64 = base64.b64encode(img_data).decode('utf-8')

                        # 检测图片格式（根据文件扩展名）
                        img_ext = os.path.splitext(img_src)[1].lower()
                        mime_type_map = {
                            '.jpg': 'image/jpeg',
                            '.jpeg': 'image/jpeg',
                            '.png': 'image/png',
                            '.gif': 'image/gif',
                            '.webp': 'image/webp',
                            '.svg': 'image/svg+xml',
                            '.bmp': 'image/bmp',
                        }
                        mime_type = mime_type_map.get(img_ext, 'image/png')

                        # 构建 data URI
                        data_uri = f'data:{mime_type};base64,{img_base64}'

                        # 更新 img 标签的 src 属性
                        new_img_tag = img_tag.replace(f'src="{img_src}"', f'src="{data_uri}"')
                        new_img_tag = new_img_tag.replace(f"src='{img_src}'", f'src="{data_uri}"')

                        img_name = os.path.basename(img_src)
                        img_size_kb = len(img_data) / 1024
                        logger.debug(
                            "图片已转换为 Base64 编码: image_name=%s size_kb=%.2f",
                            img_name,
                            img_size_kb,
                        )

                        return new_img_tag

                    except Exception as e:
                        logger.warning(
                            "图片转换为 Base64 编码失败，保留原始引用: "
                            "image_name=%s error_type=%s",
                            os.path.basename(img_src),
                            type(e).__name__,
                        )
                        return img_tag

            # 对于网络图片或已经是 data URI 的图片，保持不变
            return img_tag

        # 替换所有图片路径为 Base64 编码
        processed_html = re.sub(img_pattern, replace_img_with_base64, html_content)

        if img_matches:
            logger.info("图片 Base64 编码处理完毕")

        return processed_html

    def _parse_markdown_paragraphs(self, content: str) -> List[str]:
        """
        解析 Markdown 段落
        :param content: Markdown 内容
        :return: 段落列表
        """
        # 按两个或更多换行符分割段落
        paragraphs = re.split(r'\n\s*\n', content)
        return paragraphs

    def _is_chinese_text(self, text: str) -> bool:
        """
        检测文本是否主要为中文（包括阿拉伯数字和常见标点）
        :param text: 待检测文本
        :return: True 表示主要是中文、数字或标点，False 表示非中文
        """
        import re
        # 统计中文字符、阿拉伯数字和中文标点的数量
        chinese_chars = re.findall(r'[\u4e00-\u9fff]', text)
        arabic_numerals = re.findall(r'[0-9]', text)
        # 中文标点符号范围：包括常用标点如，。！？；：""''（）【】《》等
        chinese_punctuation = re.findall(r'[\u3000-\u303f\uff00-\uffef]', text)

        # 【关键修复】如果中文字符、阿拉伯数字或中文标点占比超过 50%，则认为是中文文本
        # 【说明】降低阈值以兼容包含项目符号、空格等非中文字符的中文文本（如目录项 "▪ 动力系统"）
        if len(text) > 0:
            chinese_or_digit_count = len(chinese_chars) + len(arabic_numerals) + len(chinese_punctuation)
            ratio = chinese_or_digit_count / len(text)
            return ratio >= 0.5
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
        :param paragraphs: 段落列表
        :param target_lang: 目标语言
        :param tracker: 进度追踪器
        :param fast_translate: 是否启用快速翻译
        :return: 翻译后的段落列表
        """
        if not paragraphs:
            return []

        translated_paragraphs = []

        for idx, para in enumerate(paragraphs):
            if not para.strip():
                translated_paragraphs.append("")
                logger.debug("跳过空段落: paragraph_index=%d", idx + 1)
            elif self._is_chinese_text(para):
                logger.debug("段落为中文，跳过翻译: paragraph_index=%d", idx + 1)
                translated_paragraphs.append(para)
            else:
                try:
                    translated_para = self.translator.translate_text(
                        para,
                        target_lang,
                        fast_translate=fast_translate
                    )
                    translated_paragraphs.append(translated_para)
                    logger.debug(
                        "段落翻译完成: paragraph_index=%d translated_chars=%d",
                        idx + 1,
                        len(translated_para),
                    )
                    tracker.update_paragraph(idx + 1)
                except Exception as e:
                    fallback_text = f"[翻译失败：{str(e)}]"
                    translated_paragraphs.append(fallback_text)
                    logger.warning(
                        "段落翻译失败，已写入失败占位文本: paragraph_index=%d error_type=%s",
                        idx + 1,
                        type(e).__name__,
                    )
                    tracker.update_paragraph(idx + 1)

        return translated_paragraphs

    def _batch_translate_paragraphs(
            self,
            paragraphs: list,
            target_lang: str,
            tracker,
            fast_translate: bool = False,
    ) -> list:
        """
        批量翻译段落（利用大模型上下文窗口，仅用于大模型翻译模式）
        :param paragraphs: 段落列表
        :param target_lang: 目标语言
        :param tracker: 进度追踪器
        :param fast_translate: 是否启用快速翻译（此参数在批量翻译中不使用）
        :return: 翻译后的段落列表
        """
        if not paragraphs:
            return []

        # 1. 过滤掉中文段落（不翻译）
        processed_paragraphs = []
        translation_needed = []

        for idx, para in enumerate(paragraphs):
            if not para.strip():
                processed_paragraphs.append("")
            elif self._is_chinese_text(para):
                logger.debug("段落为中文，跳过翻译: paragraph_index=%d", idx + 1)
                processed_paragraphs.append(para)
            else:
                processed_paragraphs.append(None)  # 占位，稍后填充
                translation_needed.append((idx, para))

        if not translation_needed:
            return processed_paragraphs

        # 2. 创建带映射的分块
        chunks = self.chunk_processor.create_chunks_with_mapping(
            [para for _, para in translation_needed],
            target_lang
        )

        logger.info(
            "开始批量翻译段落: paragraph_count=%d batch_count=%d",
            len(translation_needed),
            len(chunks),
        )

        # 3. 逐批翻译
        translated_idx = 0
        for chunk_idx, chunk in enumerate(chunks):
            # 显示进度条
            current_progress = (chunk_idx + 1) / len(chunks) * 100
            progress_bar = self._create_progress_bar(current_progress, width=30)
            logger.debug(
                "批量翻译进度更新: progress_percent=%.1f batch=%d/%d",
                current_progress,
                chunk_idx + 1,
                len(chunks),
            )

            try:
                # 调用翻译（一次性翻译整个 chunk）
                translated_chunk_text = self.translator.translate_text(
                    chunk["text"],
                    target_lang,
                    fast_translate=False,  # 批量翻译只在大模型模式下使用
                )

                # 解析翻译结果，还原为段落列表
                translated_paras = self.chunk_processor.parse_translated_chunks(
                    translated_chunk_text,
                    len(chunk["paragraph_indices"])
                )

                # 将翻译结果精确映射回原位置
                for para_local_idx, global_para_idx in enumerate(chunk["paragraph_indices"]):
                    original_idx, _ = translation_needed[global_para_idx]

                    if para_local_idx < len(translated_paras):
                        translated_para = translated_paras[para_local_idx]
                        processed_paragraphs[original_idx] = translated_para
                        logger.debug(
                            "批量段落翻译完成: paragraph_index=%d translated_chars=%d",
                            original_idx + 1,
                            len(translated_para),
                        )
                    else:
                        # 段落数量确实不匹配时的容错
                        fallback_text = f"[部分翻译失败：期望{len(chunk['paragraph_indices'])}段，实际{len(translated_paras)}段]"
                        processed_paragraphs[original_idx] = fallback_text
                        logger.warning(
                            "批量翻译结果缺少对应段落，已写入失败占位文本: "
                            "paragraph_index=%d expected_count=%d actual_count=%d",
                            original_idx + 1,
                            len(chunk["paragraph_indices"]),
                            len(translated_paras),
                        )

                    translated_idx += 1
                    tracker.update_paragraph(translated_idx)

            except Exception as e:
                logger.warning(
                    "批量翻译失败，改为逐段回退: batch_index=%d error_type=%s",
                    chunk_idx + 1,
                    type(e).__name__,
                )
                # 失败回退：逐段翻译
                for para_local_idx, global_para_idx in enumerate(chunk["paragraph_indices"]):
                    original_idx, _ = translation_needed[global_para_idx]

                    try:
                        translated_para = self.translator.translate_text(
                            translation_needed[global_para_idx][1],
                            target_lang,
                            fast_translate=False
                        )
                        processed_paragraphs[original_idx] = translated_para
                        logger.debug(
                            "逐段回退翻译完成: paragraph_index=%d translated_chars=%d",
                            original_idx + 1,
                            len(translated_para),
                        )
                    except Exception as e2:
                        fallback_text = f"[翻译失败：{str(e2)}]"
                        processed_paragraphs[original_idx] = fallback_text
                        logger.warning(
                            "逐段回退翻译失败，已写入失败占位文本: "
                            "paragraph_index=%d error_type=%s",
                            original_idx + 1,
                            type(e2).__name__,
                        )

                    translated_idx += 1
                    tracker.update_paragraph(translated_idx)

        return processed_paragraphs

    def _generate_paragraph_html(self, original: str, translated: str, show_bilingual: bool) -> str:
        """
        生成段落 HTML
        :param original: 原文
        :param translated: 译文
        :param show_bilingual: 是否显示双语对照
        :return: HTML 字符串
        """
        if not show_bilingual or not original:
            # 单语模式或无原文
            return f'<div class="paragraph"><div class="translated-text">{self._escape_html(translated)}</div></div>'

        if original == translated:
            # 中文段落，原文和译文相同
            return f'<div class="paragraph"><div class="original-text">{self._escape_html(original)}</div></div>'

        # 双语对照模式
        return f'''
        <div class="paragraph">
            <div class="original-text">{self._escape_html(original)}</div>
            <div class="translated-text">{self._escape_html(translated)}</div>
        </div>
        '''

    def _get_html_header(self) -> str:
        """生成 HTML 头部"""
        return """
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Translated Document</title>
            <style>
                body { 
                    margin: 0; 
                    padding: 20px; 
                    background: #f5f5f5; 
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
                    line-height: 1.6;
                }
                .document-container {
                    max-width: 900px;
                    margin: 0 auto;
                    background: white;
                    padding: 40px;
                    box-shadow: 0 0 10px rgba(0,0,0,0.1);
                }

                /* 表格样式 */
                table {
                    border-collapse: collapse;
                    width: 100%;
                    margin: 20px 0;
                    font-size: 14px;
                }
                th, td {
                    border: 1px solid #ddd;
                    padding: 12px 8px;
                    text-align: left;
                }
                th {
                    background-color: #f0f0f0;
                    font-weight: bold;
                }
                tr:nth-child(even) {
                    background-color: #f9f9f9;
                }
                tr:hover {
                    background-color: #f0f0f0;
                }

                /* 代码块样式 */
                pre {
                    background-color: #f6f8fa;
                    padding: 16px;
                    overflow: auto;
                    border-radius: 3px;
                    border: 1px solid #e1e4e8;
                }
                code {
                    font-family: SFMono-Regular, Consolas, Liberation Mono, Menlo, monospace;
                    font-size: 85%;
                }
                :not(pre) > code {
                    background-color: #f6f8fa;
                    padding: 0.2em 0.4em;
                    border-radius: 3px;
                }

                /* 标题样式 */
                h1, h2, h3, h4, h5, h6 {
                    margin-top: 24px;
                    margin-bottom: 16px;
                    font-weight: 600;
                    line-height: 1.25;
                }
                h1 { font-size: 2em; border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }
                h2 { font-size: 1.5em; border-bottom: 1px solid #eaecef; padding-bottom: 0.3em; }
                h3 { font-size: 1.25em; }

                /* 引用样式 */
                blockquote {
                    border-left: 4px solid #dfe2e5;
                    padding: 0 1em;
                    color: #6a737d;
                    margin: 16px 0;
                }

                /* 列表样式 */
                ul, ol {
                    padding-left: 2em;
                    margin: 16px 0;
                }

                /* 图片样式 */
                img {
                    max-width: 100%;
                    height: auto;
                    display: block;
                    margin: 20px auto;
                }

                /* 链接样式 */
                a {
                    color: #0366d6;
                    text-decoration: none;
                }
                a:hover {
                    text-decoration: underline;
                }

                /* 目录样式 */
                .toc {
                    background: #f6f8fa;
                    padding: 20px;
                    border-radius: 3px;
                    margin-bottom: 30px;
                }
                .toc ul {
                    list-style: none;
                }

                /* 【新增】双语翻译样式 */
                .original-text {
                    color: #333;
                    display: block;
                    margin-bottom: 8px;
                }
                .translated-text {
                    color: #0066cc;
                    display: block;
                    font-weight: 500;
                    border-top: 1px dashed #e0e0e0;
                    padding-top: 8px;
                    margin-top: 8px;
                }
            </style>
        </head>
        <body>
            <div class="document-container">
        """

    def _get_html_footer(self) -> str:
        """生成 HTML 尾部"""
        return """
            </div>
        </body>
        </html>
        """

    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&#39;')
        return text

    def _create_progress_bar(self, percentage: float, width: int = 30) -> str:
        """创建进度条字符串"""
        percentage = max(0, min(100, percentage))
        filled_length = int(width * percentage / 100)
        bar = '█' * filled_length + '░' * (width - filled_length)
        return bar
