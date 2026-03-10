from __future__ import annotations

import json
from typing import Any, Iterable


def _format_options(title: str, items: Iterable[Any]) -> str:
    return f"{title}: {json.dumps(list(items), ensure_ascii=False)}\n"


def build_file_analysis_prompt(request_params: dict) -> str:
    schema = {
        "country": "",
        "channel": "",
        "maturity": "",
        "format": "",
        "architectureId": 0,
        "fileDataItem": {
            "fileName": request_params.get("fileName", ""),
            "dataTime": "",
            "keyword": "",
            "summary": "",
            "score": 0.0,
            "fileNo": "",
            "source": "",
            "originalLink": "",
            "language": "",
            "dataFormat": "",
            "associatedEquipment": "",
            "relatedTechnology": "",
            "equipmentModel": "",
            "documentOverview": "",
            "originalText": "",
            "documentTranslationOne": "",
            "documentTranslationTwo": "",
        },
    }
    return (
        "请仅基于文档内容进行字段抽取，并输出严格合法 JSON。\n"
        "不要直接原样返回候选对象、候选数组、key/value 对象或中文键名。\n"
        "country、channel、maturity、format 只能输出候选项中的 value 字符串；"
        "architectureId 只能输出候选项中的 id 数字。\n"
        "如果文档证据不足，对应字符串字段输出空字符串，architectureId 输出 0，score 输出 0.0。\n"
        "documentTranslationOne 和 documentTranslationTwo 当前固定输出空字符串。\n"
        "输出 JSON 必须严格匹配以下结构：\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        + _format_options("领域体系候选", request_params.get("architectureList", []))
        + _format_options("国家候选", request_params.get("country", []))
        + _format_options("渠道候选", request_params.get("channel", []))
        + _format_options("成熟度候选", request_params.get("maturity", []))
        + _format_options("格式候选", request_params.get("format", []))
        + "请优先抽取：资料年代、关键词、摘要、文件编号、资料来源、原文链接、语种、资料格式、所属装备、所属技术、装备型号、文件概述、文件原文。\n"
    )


def build_report_prompt(request_params: dict) -> str:
    return (
        "请基于提供的全部文件内容生成 HTML 报告片段。\n"
        f"模板说明: {request_params.get('templateDesc', '')}\n"
        f"模板大纲: {request_params.get('templateOutline', '')}\n"
        f"业务需求: {request_params.get('requirement', '')}\n"
        "输出必须可直接嵌入页面，不要附加 Markdown 代码块。\n"
    )
