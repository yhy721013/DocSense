import re


def build_prompt(source_text: str, target_lang: str = "Chinese") -> str:
    """
    通用大模型优化提示词。
    结合了对复杂排版（段落数量、URL匹配）的控制，以及对专业术语、防幻觉的强化指令。
    """
    prompt = (
        f"You are a professional military and technical document translator.\n"
        f"Translate the following English text to {target_lang}.\n\n"
        f"CRITICAL REQUIREMENTS:\n"
        f"1. Output ONLY the translation (no explanations, no 'Here is the translation').\n"
        f"2. Maintain accurate terminology (e.g., 'Class' for ships is '级').\n"
        f"3. Do NOT add content not present in the source (e.g., 'Apple ID', passwords, disclaimers).\n"
        f"4. DO NOT repeat the original English text or duplicate your own paragraphs.\n"
        f"5. You MUST translate EVERY paragraph, including short paragraphs and titles.\n"
        f"6. Keep URLs EXACTLY as-is (do NOT translate or remove them). If a paragraph is ONLY a URL, output that same URL.\n"
        f"7. Keep the EXACT same paragraph structure (use '\\n\\n' to separate paragraphs).\n"
        f"8. IMPORTANT: Your output MUST have EXACTLY the same number of paragraphs as the input.\n"
        f"9. Do NOT copy decorative separators from the original text (such as '=====', '-----', '*****').\n"
        f"\n"
        f"Input Paragraph Count: {source_text.count(chr(10)+chr(10)) + 1}\n"
        f"Expected Output Paragraph Count: {source_text.count(chr(10)+chr(10)) + 1}\n"
        f"\n"
        f"Source Text:\n"
        f"{'=' * 50}\n"
        f"{source_text}\n"
        f"{'=' * 50}\n"
        f"\n"
        f"Translation (output only the translation, MUST have {source_text.count(chr(10)+chr(10)) + 1} paragraphs, KEEP URLs unchanged):"
    )
    return prompt


def clean_output(output_text: str, prompt: str) -> str:
    """
    通用的大模型输出清理流水线。
    包含了 token IDs 防护、复杂分隔符清理、固定词去除、防幻觉和无限生成截断。
    """
    if not output_text:
        return ""

    output_text = output_text.strip()

    # 1. 检测 token IDs 特征
    if re.match(r'^[\d\s,\[\]]+$', output_text):
        print(f"[警告] 检测到输出为 token IDs 数组，非正常文本")
        return ""

    # 2. 去除Prompt残留
    if prompt in output_text:
        output_text = output_text.split(prompt)[-1]

    # 3. 移除装饰性分隔符（先内部后首尾）
    output_text = re.sub(r'([^\n])\n+[=]{3,}', r'\1', output_text)
    output_text = re.sub(r'([^\n])\n+[-]{3,}', r'\1', output_text)
    output_text = re.sub(r'([^\n])\n+\*{3,}', r'\1', output_text)
    
    output_text = re.sub(r'^[=]{3,}\n*', '', output_text, flags=re.MULTILINE)
    output_text = re.sub(r'^[-]{3,}\n*', '', output_text, flags=re.MULTILINE)
    output_text = re.sub(r'^\*{3,}\n*', '', output_text, flags=re.MULTILINE)
    
    output_text = re.sub(r'\n+[=]{3,}$', '', output_text, flags=re.MULTILINE)
    output_text = re.sub(r'\n+[-]{3,}$', '', output_text, flags=re.MULTILINE)
    output_text = re.sub(r'\n+\*{3,}$', '', output_text, flags=re.MULTILINE)
    
    output_text = re.sub(r'\n+[=]{3,}\n*', '\n', output_text)
    output_text = re.sub(r'\n+[-]{3,}\n*', '\n', output_text)
    output_text = re.sub(r'\n+\*{3,}\n*', '\n', output_text)
    
    output_text = re.sub(r'\n{3,}', '\n\n', output_text)

    # 4. 去除多余的引导词
    patterns_to_remove = [
        r"^(翻译结果|Translation|翻译如下|以下是翻译|译文):?\s*",
        r"^Original English Text:.*?(?=\n)",
        r"将以下文本翻译为 [\u4e2d\u82f1\u6587ZhongEnglish]+，？注意只需要输出翻译后的结果，？不要额外解释 [:：]?\s*",
        r"Translate the following segment into [\w\s]+,?\s*without additional explanation\.?\s*",
    ]
    for pattern in patterns_to_remove:
        output_text = re.sub(pattern, "", output_text, flags=re.IGNORECASE | re.DOTALL)

    # 5. 检测并移除已知幻觉关键词
    hallucination_keywords = ["Apple ID", "password", "generated based on the provided"]
    if any(keyword in output_text for keyword in hallucination_keywords):
        lines = output_text.split('\n')
        clean_lines = []
        for line in lines:
            if not any(keyword in line for keyword in hallucination_keywords):
                clean_lines.append(line)
        output_text = '\n'.join(clean_lines)

        if not output_text.strip():
            return "[内容过滤：检测到模型幻觉]"

    # 6. 去重处理 (防止相同段落连续出现)
    lines = output_text.split('\n')
    unique_lines = []
    prev_line = None
    repeat_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped == prev_line:
            repeat_count += 1
            if repeat_count > 2:
                continue
        else:
            repeat_count = 0
            prev_line = stripped
        unique_lines.append(line)

    output_text = '\n'.join(unique_lines).strip()

    # 7. 长度异常检测 (截断超长幻觉输出)
    if len(output_text) > 3000:
        last_period = output_text.rfind('。', 0, 2000)
        if last_period != -1:
            output_text = output_text[:last_period + 1] + "\n[警告：输出过长已截断]"
        else:
            output_text = output_text[:2000] + "\n[警告：输出过长已截断]"

    # 8. 最后清理多余符号
    output_text = output_text.lstrip(':：').strip()

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