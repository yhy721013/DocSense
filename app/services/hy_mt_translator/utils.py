import re
from typing import Optional


def build_prompt(source_text: str, target_lang: str = "Chinese") -> str:
    """
    构建针对 tencent-hy-mt:1.8b 优化的提示词。
    增强点：
    1. 增加领域上下文（军事/技术），减少术语错误。
    2. 增加强力的负面约束，防止幻觉（如Apple ID、重复段落）。
    3. 强制要求纯净输出。
    """
    # 简单的语言检测，默认假设源文本为英文，目标为中文
    is_zh_target = target_lang.lower() in ["chinese", "zh", "中文", "cn"]

    if is_zh_target:
        system_instruction = (
            "你是一名专业的军事与技术文档翻译专家。"
            "请将以下文本翻译成中文。"
            "要求：\n"
            "1. 保持专业术语准确（例如：'Class'在舰船语境下译为'级'，'Ohio'译为'俄亥俄级'而非'俄亥俄州'）。\n"
            "2. 严禁添加原文中不存在的内容（如'Apple ID'、'password'、无关的免责声明）。\n"
            "3. 严禁重复输出相同的段落或句子。\n"
            "4. 直接输出翻译结果，不要包含'翻译如下'、'注意'等任何解释性文字。\n"
            "5. 如果原文是表格数据或乱码，请尽量保持原样或标记为[数据]，不要编造内容。"
        )
        prompt = f"{system_instruction}\n\n待翻译文本:\n{source_text}\n\n翻译结果:"
    else:
        system_instruction = (
            "You are a professional military and technical document translator. "
            f"Translate the following text into {target_lang}. "
            "Requirements:\n"
            "1. Maintain accurate terminology.\n"
            "2. Do NOT add content not present in the source (e.g., 'Apple ID', 'password').\n"
            "3. Do NOT repeat paragraphs.\n"
            "4. Output ONLY the translation, no explanations.\n"
            "5. If the source is garbled data, keep it as is or mark as [Data], do not hallucinate."
        )
        prompt = f"{system_instruction}\n\nSource Text:\n{source_text}\n\nTranslation:"

    return prompt


def clean_output(output_text: str, prompt: str) -> str:
    """
    清理模型输出，针对 tencent-hy-mt:1.8b 的常见问题进行修复。
    修复点：
    1. 去除Prompt残留。
    2. 检测并去除"Apple ID"等已知幻觉内容。
    3. 检测并去除连续重复段落。
    4. 截断过长的异常输出。
    """
    if not output_text:
        return ""

    # 1. 去除Prompt残留 (原有逻辑增强)
    if prompt in output_text:
        output_text = output_text.split(prompt)[-1]

    # 去除常见的引导词
    patterns_to_remove = [
        r"^(翻译结果|Translation|翻译如下|以下是翻译):?\s*",
        r"将以下文本翻译为 [\u4e2d\u82f1\u6587ZhongEnglish]+，？注意只需要输出翻译后的结果，？不要额外解释 [:：]?\s*",
        r"Translate the following segment into [\w\s]+,?\s*without additional explanation\.?\s*",
    ]
    for pattern in patterns_to_remove:
        output_text = re.sub(pattern, "", output_text, flags=re.IGNORECASE)

    # 2. 【关键修复】检测并移除已知的幻觉内容 (Apple ID, Password等)
    # 如果整段都是关于Apple ID的，直接清空，因为原文是军事文档，不可能包含此内容
    hallucination_keywords = ["Apple ID", "password", "generated based on the provided"]
    if any(keyword in output_text for keyword in hallucination_keywords):
        # 检查是否大部分内容都是幻觉
        lines = output_text.split('\n')
        clean_lines = []
        for line in lines:
            if not any(keyword in line for keyword in hallucination_keywords):
                clean_lines.append(line)
        output_text = '\n'.join(clean_lines)

        # 如果清理后为空，说明整段都是幻觉，返回空或原文标记
        if not output_text.strip():
            return "[内容过滤：检测到模型幻觉]"

    # 3. 【关键修复】检测连续重复段落
    # 将文本按行分割，移除连续重复的行
    lines = output_text.split('\n')
    unique_lines = []
    prev_line = None
    repeat_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped == prev_line:
            repeat_count += 1
            # 如果同一行重复超过2次，跳过后续重复
            if repeat_count > 2:
                continue
        else:
            repeat_count = 0
            prev_line = stripped

        unique_lines.append(line)

    output_text = '\n'.join(unique_lines)

    # 4. 长度异常检测 (防止无限生成)
    # 如果译文长度超过原文长度的4倍，极大概率是重复幻觉，强制截断到第一个合理的结束点
    # 这里简单处理：如果太长，只保留前2000字符（可根据实际情况调整）
    # 注意：需要先获取原文长度，但此函数未传入原文，故仅做绝对长度限制或基于比例的启发式
    # 此处做一个简单的截断保护，防止单个段落爆炸
    if len(output_text) > 3000:
        # 尝试在最后一个句号处截断
        last_period = output_text.rfind('。', 0, 2000)
        if last_period != -1:
            output_text = output_text[:last_period + 1] + "\n[警告：输出过长已截断]"
        else:
            output_text = output_text[:2000] + "\n[警告：输出过长已截断]"

    # 5. 去除首尾空白和多余的冒号
    output_text = output_text.strip().lstrip(':：').strip()

    return output_text


class ProgressTracker:
    """进度追踪器 - 用于跟踪文档翻译进度"""

    def __init__(self):
        self.total_pages = 0
        self.current_page = 0
        self.total_paragraphs = 0
        self.current_paragraph = 0
        self.file_name = ""
        self.status = "idle"
        self.errors = []

    def set_file_info(self, file_name: str, total_items: int, item_type: str = "page"):
        """设置文件信息"""
        self.file_name = file_name
        if item_type == "page":
            self.total_pages = total_items
            self.current_page = 0
        else:
            self.total_paragraphs = total_items
            self.current_paragraph = 0
        self.status = "processing"

    def update_page(self, page_num: int):
        """更新当前页码"""
        self.current_page = page_num

    def update_paragraph(self, para_num: int):
        """更新当前段落"""
        self.current_paragraph = para_num

    def get_progress(self) -> dict:
        """获取当前进度"""
        if self.total_pages > 0:
            percentage = (self.current_page / self.total_pages * 100) if self.total_pages > 0 else 0
            return {
                "file": self.file_name,
                "status": self.status,
                "current": self.current_page,
                "total": self.total_pages,
                "percentage": round(percentage, 2),
                "type": "page"
            }
        elif self.total_paragraphs > 0:
            percentage = (self.current_paragraph / self.total_paragraphs * 100) if self.total_paragraphs > 0 else 0
            return {
                "file": self.file_name,
                "status": self.status,
                "current": self.current_paragraph,
                "total": self.total_paragraphs,
                "percentage": round(percentage, 2),
                "type": "paragraph"
            }
        else:
            return {
                "file": self.file_name,
                "status": self.status,
                "current": 0,
                "total": 0,
                "percentage": 0,
                "type": "unknown"
            }

    def mark_completed(self):
        """标记为完成"""
        self.status = "completed"
        if self.total_pages > 0:
            self.current_page = self.total_pages
        if self.total_paragraphs > 0:
            self.current_paragraph = self.total_paragraphs

    def mark_error(self, error_msg: str):
        """标记错误"""
        self.status = "error"
        self.errors.append(error_msg)

    def reset(self):
        """重置进度"""
        self.__init__()