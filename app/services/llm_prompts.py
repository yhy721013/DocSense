from __future__ import annotations

from typing import Any, Iterable


def _format_options(title: str, items: Iterable[Any]) -> str:
    formatted = []
    for item in items:
        if isinstance(item, dict):
            formatted.append(str(item))
        else:
            formatted.append(repr(item))
    return f"{title}: {formatted}\n"


def build_file_analysis_prompt(request_params: dict) -> str:
    return (
        "请仅基于文档内容输出严格 JSON。\n"
        + _format_options("领域体系候选", request_params.get("architectureList", []))
        + _format_options("国家候选", request_params.get("country", []))
        + _format_options("渠道候选", request_params.get("channel", []))
        + _format_options("成熟度候选", request_params.get("maturity", []))
        + _format_options("格式候选", request_params.get("format", []))
        + "必须保留文件翻译字段，但当前返回空字符串。\n"
    )
