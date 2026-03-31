import logging
logger = logging.getLogger(__name__)
import os
import re
from typing import Optional
from .core import HYMTTranslator
from .chunk_processor import ChunkProcessor

class TXTHandler:
    """文本文件 (.txt) 处理器"""

    def __init__(self, translator: HYMTTranslator):
        self.translator = translator
        self.chunk_processor = ChunkProcessor(translator.model_name)

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
            translate_all: int = 0,
            fast_translate: bool = True,
    ) -> str:
        """
        处理文本文件翻译（批量翻译优化版）
        :param input_path: 输入文件路径
        :param output_path: 输出文件路径（可选）
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: 输出文件路径
        """
        if not output_path:
            base, _ = os.path.splitext(input_path)
            output_path = f"{base}_translated.txt"

        logger.info(f"Processing TXT: {input_path}")

        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()

        paragraphs = content.split('\n\n')

        tracker = self.translator.get_progress_tracker()

        # 确定需要处理的段落范围
        paras_to_process = len(paragraphs) if translate_all == 0 else min(translate_all, len(paragraphs))
        tracker.set_file_info(os.path.basename(input_path), paras_to_process, "paragraph")

        # 【关键修改】根据是否启用快速翻译选择翻译策略
        if fast_translate:
            # 快速翻译模式：逐段翻译
            logger.info(f"[TXT 处理] 使用快速翻译模式（ArgoTranslate），共 {paras_to_process} 个段落...")
            translated_paragraphs = self._translate_paragraphs_one_by_one(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=True
            )
        else:
            # 大模型翻译模式：批量翻译
            logger.info(f"[TXT 处理] 使用大模型批量翻译模式，共 {paras_to_process} 个段落...")
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
        logger.info(f"TXT saved to: {output_path}")
        return output_path

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
                logger.info(f"  [空段落] 段落 {idx + 1}")
            elif self._is_chinese_text(para):
                logger.info(f"[跳过] 段落 {idx + 1} 为中文，已跳过翻译")
                translated_paragraphs.append(para)
            else:
                try:
                    translated_para = self.translator.translate_text(
                        para,
                        target_lang,
                        fast_translate=fast_translate
                    )
                    translated_paragraphs.append(translated_para)
                    logger.info(f"  ✓ 段落 {idx + 1}: {len(translated_para)} 字")
                    tracker.update_paragraph(idx + 1)
                except Exception as e:
                    fallback_text = f"[翻译失败：{str(e)}]"
                    translated_paragraphs.append(fallback_text)
                    logger.info(f"  ✗ 段落 {idx + 1}: {fallback_text}")
                    tracker.update_paragraph(idx + 1)

        logger.info("")  # 换行
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
                logger.info(f"[跳过] 段落 {idx + 1} 为中文，已跳过翻译")
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

        logger.info(f"[批量翻译] 共 {len(translation_needed)} 段需要翻译，分为 {len(chunks)} 个批次")

        # 3. 逐批翻译
        translated_idx = 0
        for chunk_idx, chunk in enumerate(chunks):
            # 显示进度条
            current_progress = (chunk_idx + 1) / len(chunks) * 100
            progress_bar = self._create_progress_bar(current_progress, width=30)
            logger.info(f"[{progress_bar}] {current_progress:.1f}% | 批次 {chunk_idx + 1}/{len(chunks)}")

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

                # 【关键修复】将翻译结果精确映射回原位置
                for para_local_idx, global_para_idx in enumerate(chunk["paragraph_indices"]):
                    # global_para_idx 是在 translation_needed 列表中的索引
                    # 需要找到它在 processed_paragraphs 中的真实位置
                    original_idx, _ = translation_needed[global_para_idx]

                    if para_local_idx < len(translated_paras):
                        translated_para = translated_paras[para_local_idx]
                        processed_paragraphs[original_idx] = translated_para
                        logger.info(f"  ✓ 段落 {original_idx + 1}: {len(translated_para)} 字")
                    else:
                        # 段落数量确实不匹配时的容错
                        fallback_text = f"[部分翻译失败：期望{len(chunk['paragraph_indices'])}段，实际{len(translated_paras)}段]"
                        processed_paragraphs[original_idx] = fallback_text
                        logger.info(f"  ✗ 段落 {original_idx + 1}: {fallback_text}")

                    translated_idx += 1
                    tracker.update_paragraph(translated_idx)

            except Exception as e:
                logger.error(f"[错误] 批次 {chunk_idx + 1} 翻译失败：{e}")
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
                        logger.info(f"  ✓ 段落 {original_idx + 1} (回退): {len(translated_para)} 字")
                    except Exception as e2:
                        fallback_text = f"[翻译失败：{str(e2)}]"
                        processed_paragraphs[original_idx] = fallback_text
                        logger.info(f"  ✗ 段落 {original_idx + 1} (回退): {fallback_text}")

                    translated_idx += 1
                    tracker.update_paragraph(translated_idx)

        logger.info("")  # 换行
        return processed_paragraphs

    def convert_to_html(
            self,
            input_path: str,
            output_dir: str,
            target_lang: str = "Chinese",
            translate_all: int = 0,
            fast_translate: bool = True,
    ) -> tuple[str, str]:
        """
        将 TXT 文件转换为 HTML 并翻译（批量翻译优化版）
        :param input_path: TXT 文件路径
        :param output_dir: 输出目录
        :param target_lang: 目标语言
        :param translate_all: 是否翻译全文，0=全文，>0 表示翻译前 N 个段落
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :return: (双语 HTML 路径，单语 HTML 路径)
        """
        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"Processing TXT to HTML: {input_path}")

        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()

        paragraphs = content.split('\n\n')

        tracker = self.translator.get_progress_tracker()
        valid_paragraphs = [p for p in paragraphs if p.strip()]
        paras_to_process = len(valid_paragraphs) if translate_all == 0 else min(translate_all,
                                                                                len(valid_paragraphs))
        tracker.set_file_info(os.path.basename(input_path), paras_to_process, "paragraph")

        # 【关键修改】同时生成双语和单语 HTML 内容
        bilingual_html_content = []
        monolingual_html_content = []

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
        bilingual_html_content.append(html_header)

        # 单语 HTML header（隐藏 original-text 样式）
        monolingual_header = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>Text Translated - Monolingual</title>
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
                .translated-text {
                    color: #0066cc;
                    font-weight: bold;
                    padding-top: 10px;
                    border-top: 1px dashed #e0e0e0;
                    white-space: pre-wrap;
                }
                .original-text {
                    display: none;
                }
            </style>
        </head>
        <body>
            <div class="document-container">
        """
        monolingual_html_content.append(monolingual_header)

        # 【关键修改】根据是否启用快速翻译选择翻译策略
        if fast_translate:
            # 快速翻译模式：逐段翻译
            logger.info(f"[HTML 转换] 使用快速翻译模式（ArgoTranslate），共 {paras_to_process} 个段落...")
            translated_paragraphs = self._translate_paragraphs_one_by_one(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=True
            )
        else:
            # 大模型翻译模式：批量翻译
            logger.info(f"[HTML 转换] 使用大模型批量翻译模式，共 {paras_to_process} 个段落...")
            translated_paragraphs = self._batch_translate_paragraphs(
                paragraphs[:paras_to_process],
                target_lang,
                tracker,
                fast_translate=False
            )

        # 未翻译的段落保持原样
        final_paragraphs = translated_paragraphs + paragraphs[paras_to_process:]

        processed_count = 0
        total_paras = paras_to_process

        for idx, para in enumerate(paragraphs):
            if not para.strip():
                continue

            if translate_all > 0 and processed_count >= translate_all:
                break

            orig = para.strip()

            # 从批量翻译结果中获取译文
            trans = final_paragraphs[idx] if idx < len(final_paragraphs) else ""

            # 【新增】显示进度条
            current_progress = (processed_count + 1) / total_paras * 100 if total_paras > 0 else 0
            progress_bar = self._create_progress_bar(current_progress, width=30)
            logger.info(f"[{progress_bar}] {current_progress:.1f}% | 段落 {processed_count + 1}/{total_paras}")

            # 检测是否为中文，如果是则跳过翻译
            if self._is_chinese_text(orig):
                # 中文段落不翻译，原文和译文相同
                # 双语版本
                paragraph_html_bilingual = (
                    f'<div class="paragraph">'
                    f'<div class="original-text">{self._escape_html(orig)}</div>'
                    f'</div>'
                )
                bilingual_html_content.append(paragraph_html_bilingual)

                # 单语版本（只显示原文，因为原文=译文）
                paragraph_html_monolingual = (
                    f'<div class="paragraph">'
                    f'<div class="original-text">{self._escape_html(orig)}</div>'
                    f'</div>'
                )
                monolingual_html_content.append(paragraph_html_monolingual)
            else:
                # 需要翻译的段落 - 同时生成双语和单语版本
                # 双语版本
                paragraph_html_bilingual = (
                    f'<div class="paragraph">'
                    f'<div class="original-text">{self._escape_html(orig)}</div>'
                    f'<div class="translated-text">{self._escape_html(trans)}</div>'
                    f'</div>'
                )
                bilingual_html_content.append(paragraph_html_bilingual)

                # 单语版本（只显示译文）
                paragraph_html_monolingual = (
                    f'<div class="paragraph">'
                    f'<div class="translated-text">{self._escape_html(trans)}</div>'
                    f'</div>'
                )
                monolingual_html_content.append(paragraph_html_monolingual)

            processed_count += 1
            tracker.update_paragraph(processed_count)

        # 【新增】完成提示
        logger.info("")  # 换行，避免进度条覆盖后续输出
        html_footer = """
            </div>
        </body>
        </html>
        """
        bilingual_html_content.append(html_footer)
        monolingual_html_content.append(html_footer)

        base_name = os.path.basename(input_path)
        name_without_ext = os.path.splitext(base_name)[0]

        # 保存双语 HTML 文件
        bilingual_output_path = os.path.join(output_dir, f"{name_without_ext}_bilingual.html")
        with open(bilingual_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(bilingual_html_content))

        # 保存单语 HTML 文件
        monolingual_output_path = os.path.join(output_dir, f"{name_without_ext}_monolingual.html")
        with open(monolingual_output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(monolingual_html_content))

        tracker.mark_completed()
        print(f"双语 HTML 已保存至：{bilingual_output_path}")
        print(f"单语 HTML 已保存至：{monolingual_output_path}")
        return bilingual_output_path, monolingual_output_path

    def _create_progress_bar(self, percentage: float, width: int = 30) -> str:
        """创建进度条字符串"""
        percentage = max(0, min(100, percentage))
        filled_length = int(width * percentage / 100)
        bar = '█' * filled_length + '░' * (width - filled_length)
        return bar

    def _create_progress_bar(self, percentage: float, width: int = 30) -> str:
        """
        创建进度条字符串
        :param percentage: 进度百分比 (0-100)
        :param width: 进度条宽度
        :return: 进度条字符串
        """
        percentage = max(0, min(100, percentage))
        filled_length = int(width * percentage / 100)
        bar = '█' * filled_length + '░' * (width - filled_length)
        return bar

    def _escape_html(self, text: str) -> str:
        """转义 HTML 特殊字符"""
        text = text.replace('&', '&amp;')
        text = text.replace('<', '&lt;')
        text = text.replace('>', '&gt;')
        text = text.replace('"', '&quot;')
        text = text.replace("'", '&#39;')
        return text
