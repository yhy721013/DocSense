"""Prompt 兼容导出与非 Analysis Prompt。

阶段 1F-1 已把文件分析 Prompt 移至 Analysis Domain。本模块保留旧导入路径，同时继续
承载文件对话标题和报告 Prompt，避免把无关业务规则混入 Analysis Domain。
"""

from __future__ import annotations

import json
from typing import Mapping, Sequence

from app.modules.analysis.domain.prompts import *  # noqa: F401,F403


def build_chat_title_prompt(
    messages: Sequence[Mapping[str, str]],
    *,
    max_title_chars: int = 20,
) -> str:
    """构建文件对话标题生成 Prompt。"""

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


def build_report_prompt(request_params: dict) -> str:
    """构建报告生成 Prompt，保持既有调用位置与文本完全兼容。"""

    return (
        "请基于提供的全部文件内容生成 HTML 报告片段。\n"
        f"模板说明：{request_params.get('templateDesc', '')}\n"
        f"模板大纲：{request_params.get('templateOutline', '')}\n"
        f"业务需求：{request_params.get('requirement', '')}\n"
        "输出必须可直接嵌入页面，不要附加 Markdown 代码块。\n"
    )


__all__ = tuple(name for name in globals() if not name.startswith("__"))
