"""Chat 应用用例使用的确定性 Prompt 构造规则。"""

from __future__ import annotations

import json
from typing import Mapping, Sequence


def build_chat_title_prompt(
    messages: Sequence[Mapping[str, str]],
    *,
    max_title_chars: int = 20,
) -> str:
    """构建文件对话标题生成 Prompt，不执行网络或文件 I/O。"""

    if (
        isinstance(max_title_chars, bool)
        or not isinstance(max_title_chars, int)
        or max_title_chars < 1
    ):
        raise ValueError("max_title_chars 必须是正整数")

    normalized_messages: list[dict[str, str]] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise TypeError("messages 只能包含 Mapping")
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        normalized_messages.append({"role": role, "content": content})

    if not normalized_messages:
        raise ValueError("messages 不能为空")

    history_json = json.dumps(
        normalized_messages,
        ensure_ascii=False,
        indent=2,
    )
    return (
        "你是文件对话标题生成器。请仅根据给定的对话历史生成一个简短中文标题。\n"
        "要求：\n"
        f"1. 标题最多 {max_title_chars} 个字符，超出也必须自行压缩。\n"
        "2. 只输出标题正文，不要输出引号、书名号、Markdown、序号、解释或多余标点。\n"
        "3. 标题应概括用户问题和助手回答的核心主题，避免使用“对话”“总结”等泛化词。\n"
        "4. 不得使用对话历史之外的信息，不得编造文件中不存在的主题。\n"
        "【对话历史(JSON)】\n"
        f"{history_json}\n"
        "【输出】"
    )


__all__ = ["build_chat_title_prompt"]
