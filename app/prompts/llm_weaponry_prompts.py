from __future__ import annotations


def build_input_field_prompt(field_name: str, field_description: str = "") -> str:
    """构建 INPUT 类型字段的 RAG 查询 Prompt。"""
    desc_part = ""
    if field_description:
        desc_part = f"\n字段说明: {field_description}"
    return (
        f"请从文档中提取以下信息: {field_name}。{desc_part}\n"
        "要求:\n"
        "1. 只需回答该字段的具体值，不要添加额外解释\n"
        '2. 如果文档中找不到相关信息，请只回答"未找到"\n'
        "3. 请基于文档原文提取，不要推测或编造"
    )


def build_table_column_prompt(
    field_name: str,
    field_description: str = "",
    table_context: str = "",
) -> str:
    """构建 TABLE 列字段的 RAG 查询 Prompt。

    要求 LLM 返回该列字段在不同来源中的所有值，并标注来源。
    """
    desc_part = ""
    if field_description:
        desc_part = f"\n字段说明: {field_description}"
    ctx_part = ""
    if table_context:
        ctx_part = f"\n表格上下文: {table_context}"
    return (
        f'请从文档中提取关于"{field_name}"的所有数据。{desc_part}{ctx_part}\n'
        "要求:\n"
        f'1. 如果有多个不同来源或多条记录提到了不同的"{field_name}"值，请逐条列出\n'
        "2. 对每条值，请标注来自哪份文献或哪段原文\n"
        "3. 格式示例:\n"
        "   值1: XXX (来源: 文献A)\n"
        "   值2: YYY (来源: 文献B)\n"
        '4. 如果文档中找不到相关信息，请只回答"未找到"'
    )
