"""Translation 的纯文本规则。"""

from __future__ import annotations

import re


_CHINESE_CHARACTER = re.compile(r"[\u4e00-\u9fff]")


def split_translation_units(text: str) -> tuple[str, ...]:
    """按旧处理器的空行语义切分，并剔除纯空白单元。"""

    if not isinstance(text, str):
        raise TypeError("text 必须是 str")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return tuple(
        item.strip()
        for item in re.split(r"\n[ \t]*\n", normalized)
        if item.strip()
    )


def is_mostly_chinese(text: str, *, threshold: float = 0.8) -> bool:
    """保持旧 Handler 的 80% 中文字符跳过规则。"""

    if not text:
        return False
    return len(_CHINESE_CHARACTER.findall(text)) / len(text) >= threshold


__all__ = ["is_mostly_chinese", "split_translation_units"]
