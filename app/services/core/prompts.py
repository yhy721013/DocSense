"""Prompt 兼容导出与非 Analysis 的共享 Prompt。

阶段 1F-1 已把文件分析 Prompt 移至 Analysis Domain。本模块保留旧导入路径，并继续
承载报告 Prompt。Chat 标题 Prompt 已归入 ``app/modules/chat/application``，不得再从共享
服务反向依赖 Chat 用例。
"""

from __future__ import annotations

from app.modules.analysis.domain.prompts import *  # noqa: F401,F403


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
