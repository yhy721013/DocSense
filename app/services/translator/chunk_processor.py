"""
智能分块处理器 - 基于 Token 的批量翻译
核心功能：
1. 根据模型上下文窗口自动分块
2. 保持段落结构映射
3. 最大化翻译效率
"""
import math
from typing import List, Dict, Tuple
import re


class ChunkProcessor:
    """基于 Token 的智能分块处理器"""

    # 不同模型的上下文窗口限制（token 数）
    MODEL_CONTEXT_LIMITS = {
        "qwen3.5:4b": 256000,  # 256k
        "qwen3.5:4b-instruct": 256000,
        "qwen3.5:4b-q4_K_M": 256000,
        "tencent-hy-mt:1.8b-q4": 8192,  # 8k
        "default": 32000,  # 默认 32k
    }

    # 安全边界（保留一部分给提示词和输出）
    SAFETY_MARGIN = 0.2  # 保留 20%

    def __init__(self, model_name: str = "qwen3.5:4b"):
        """
        初始化分块处理器
        :param model_name: 模型名称
        """
        self.model_name = model_name
        self.context_limit = self._get_context_limit(model_name)
        self.max_input_tokens = int(self.context_limit * (1 - self.SAFETY_MARGIN))

    def _get_context_limit(self, model_name: str) -> int:
        """获取模型的上下文窗口限制"""
        for key, limit in self.MODEL_CONTEXT_LIMITS.items():
            if key in model_name.lower():
                return limit
        return self.MODEL_CONTEXT_LIMITS["default"]

    def _estimate_tokens(self, text: str) -> int:
        """
        估算文本的 token 数量
        简化算法：中文按字符计，英文按单词计
        """
        # 分离中英文
        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        english_text = re.sub(r'[\u4e00-\u9fff]', '', text)
        english_words = len(english_text.split())

        # 中文 1 字≈1.5 token，英文 1 词≈1.3 token
        estimated = int(chinese_chars * 1.5 + english_words * 1.3)
        return max(estimated, 1)

    def create_chunks_with_mapping(
            self,
            paragraphs: List[str],
            target_lang: str = "Chinese"
    ) -> List[Dict]:
        """
        创建带段落映射的分块
        :param paragraphs: 原始段落列表
        :param target_lang: 目标语言
        :return: 分块列表，每个分块包含段落索引映射
        """
        chunks = []
        current_chunk = {
            "text": "",
            "paragraph_indices": [],  # 记录此 chunk 包含的段落索引
            "token_count": 0
        }

        for idx, para in enumerate(paragraphs):
            para_tokens = self._estimate_tokens(para)

            # 检查是否需要开始新 chunk
            if (current_chunk["token_count"] + para_tokens > self.max_input_tokens and
                    current_chunk["text"].strip()):
                # 保存当前 chunk
                chunks.append(current_chunk)
                # 创建新 chunk
                current_chunk = {
                    "text": "",
                    "paragraph_indices": [],
                    "token_count": 0
                }

            # 添加段落到当前 chunk
            if current_chunk["text"].strip():
                current_chunk["text"] += "\n\n" + para
            else:
                current_chunk["text"] = para

            current_chunk["paragraph_indices"].append(idx)
            current_chunk["token_count"] += para_tokens

        # 添加最后一个 chunk
        if current_chunk["text"].strip():
            chunks.append(current_chunk)

        return chunks

    def build_batch_translation_prompt(
            self,
            chunk_text: str,
            target_lang: str = "Chinese"
    ) -> str:
        """
        构建批量翻译提示词
        :param chunk_text: 待翻译的文本块
        :param target_lang: 目标语言
        :return: 优化后的提示词
        """
        # 【关键增强】更强力的指令，强调每个段落都要翻译
        prompt = (
            f"You are a professional translator. Translate the following English text to Chinese.\n"
            f"CRITICAL REQUIREMENTS:\n"
            f"1. Output ONLY the Chinese translation\n"
            f"2. DO NOT repeat the original English text\n"
            f"3. You MUST translate EVERY paragraph, including:\n"
            f"   - Short paragraphs (even single sentences)\n"
            f"   - URLs and links (keep URLs as-is, but translate surrounding text)\n"
            f"   - Titles and headings\n"
            f"4. Keep the EXACT same paragraph structure (use '\\n\\n' to separate paragraphs)\n"
            f"5. The number of output paragraphs MUST equal the number of input paragraphs\n"
            f"6. Do not add any explanations, notes, or comments\n"
            f"\n"
            f"Original English Text:\n"
            f"{'='*50}\n"
            f"{chunk_text}\n"
            f"{'='*50}\n"
            f"\n"
            f"Chinese Translation (output only the translation, maintain paragraph count):"
        )
        return prompt

    def parse_translated_chunks(
            self,
            translated_text: str,
            original_paragraph_count: int
    ) -> List[str]:
        """
        解析翻译后的文本，还原为段落列表
        :param translated_text: 翻译后的完整文本
        :param original_paragraph_count: 原始段落数量
        :return: 翻译后的段落列表
        """
        # 【关键改进】先清理翻译结果中的格式标记
        cleaned_text = self._clean_translated_text(translated_text)

        # 按双换行符分割段落
        raw_paragraphs = re.split(r'\n\n+', cleaned_text.strip())

        # 清理每个段落
        cleaned_paragraphs = [p.strip() for p in raw_paragraphs if p.strip()]

        # 如果段落数量不匹配，进行适配
        if len(cleaned_paragraphs) != original_paragraph_count:
            print(f"[警告] 段落数量不匹配：期望 {original_paragraph_count}, 实际 {len(cleaned_paragraphs)}")
            # 尝试智能调整
            cleaned_paragraphs = self._adapt_paragraph_count(
                cleaned_paragraphs,
                original_paragraph_count
            )

        return cleaned_paragraphs

    def _clean_translated_text(self, text: str) -> str:
        """
        清理翻译结果中的格式标记和不需要的内容
        :param text: 原始翻译结果
        :return: 清理后的文本
        """
        # 1. 移除可能的 Markdown 格式残留
        text = re.sub(r'\*\*', '', text)  # 移除加粗
        text = re.sub(r'^[\*\-\+]\s+', '', text, flags=re.MULTILINE)  # 移除列表标记

        # 2. 移除模型可能添加的解释性文字（如 "Note:", "翻译：" 等）
        text = re.sub(r'^(Note:|注意：|翻译：|Translation:)\s*', '', text, flags=re.IGNORECASE | re.MULTILINE)

        # 3. 移除单独的特殊符号行
        lines = text.split('\n')
        lines = [line for line in lines if line.strip() not in ['*', '**', '---']]
        text = '\n'.join(lines)

        return text

    def _adapt_paragraph_count(
            self,
            paragraphs: List[str],
            target_count: int
    ) -> List[str]:
        """
        自适应调整段落数量以匹配原文
        :param paragraphs: 当前段落列表
        :param target_count: 目标段落数量
        :return: 调整后的段落列表
        """
        current_count = len(paragraphs)

        # 【关键修复】如果差异太大（>30%），说明模型严重漏译，不要强行适配
        if abs(current_count - target_count) / max(target_count, 1) > 0.3:
            print(f"[警告] 段落数量差异过大 ({current_count} vs {target_count})，返回原始结果")
            return paragraphs

        # 如果段落太多，合并相邻段落
        if current_count > target_count:
            ratio = current_count / target_count
            adapted = []
            i = 0
            while i < current_count:
                # 计算需要合并的段落数
                merge_count = max(1, int(ratio))
                merged_para = " ".join(paragraphs[i:min(i + merge_count, current_count)])
                adapted.append(merged_para)
                i += merge_count
            return adapted[:target_count]

        # 如果段落太少，尝试拆分长段落或在末尾补充空段落
        elif current_count < target_count:
            adapted = []
            deficit = target_count - current_count

            for idx, para in enumerate(paragraphs):
                # 【优化】降低拆分阈值，更容易拆分
                if len(para) > 100 and deficit > 0:
                    # 尝试在句号处拆分
                    sentences = re.split(r'([.!?.])', para)
                    mid = len(sentences) // 2
                    if mid > 0 and mid < len(sentences):
                        part1 = "".join(sentences[:mid + 1]).strip()
                        part2 = "".join(sentences[mid + 1:]).strip()
                        if part1 and part2:
                            adapted.append(part1)
                            adapted.append(part2)
                            deficit -= 1
                            continue

                adapted.append(para)

            # 【新增】如果仍然不足，在末尾添加占位符（表示这些段落模型未翻译）
            while len(adapted) < target_count:
                adapted.append("[模型未翻译此段落]")

            return adapted

        return paragraphs

    def get_optimal_batch_size(self, avg_paragraph_tokens: int) -> int:
        """
        计算最优批量大小
        :param avg_paragraph_tokens: 平均每个段落的 token 数
        :return: 每批建议处理的段落数量
        """
        if avg_paragraph_tokens <= 0:
            return 10

        optimal = self.max_input_tokens // avg_paragraph_tokens
        return max(1, min(optimal, 100))  # 限制在 1-100 之间
