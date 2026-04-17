from __future__ import annotations

import json
from typing import Any, Iterable


ARCHITECTURE_CLASSIFICATION_RULES = (
    "【领域分类判定规则】\n"
    "1. 军事基地：军事设施、基地建设、基地布局、军事要塞、港口码头、机场跑道、后勤保障设施、营房工程、防御工事。\n"
    "2. 体系运用：作战体系、系统集成、联合作战、协同配合、多域作战、体系对抗。\n"
    "3. 装备型号：武器装备、装备参数、技术指标、装备性能。若候选中存在二级节点，应优先判断为空中装备、水面装备或水下装备。\n"
    "4. 作战环境：战场环境、地理条件、气象水文、电磁环境、海洋环境。\n"
    "5. 作战指挥：指挥控制、决策流程、作战计划、战术战法。若候选中存在二级节点，应优先判断为条令条例或组织机构。\n"
    "6. 组织机构：机构编制、隶属关系、职责分工、司令部、部门设置、岗位任命、职能说明等内容更偏向该类。\n"
    "7. 条令条例：发布机构、编号、版本、规范、条令、条例、制度等内容更偏向该类。\n"
    "8. architectureId 必须来自候选 architectureList 中的 id。若候选中同时存在上级和下级节点，优先选择更具体的下级节点。\n"
    "9. 当文档与所有候选领域都明显无关时，architectureId 输出 1。\n"
    "10. 不要输出分类名称、候选列表或概率，只输出最终 architectureId 数字。\n"
)


def _format_options(title: str, items: Iterable[Any]) -> str:
    return f"{title}: {json.dumps(list(items), ensure_ascii=False)}\n"


def build_file_analysis_prompt(request_params: dict) -> str:
    from app.services.llm_service.analysis_service import build_effective_analysis_ranges

    ranges = build_effective_analysis_ranges(request_params)
    schema = {
        "country": "",
        "channel": "",
        "maturity": "",
        "format": "",
        "architectureId": 1,
        "fileDataItem": {
            "fileName": request_params.get("fileName", ""),
            "dataTime": "",
            "keyword": "",
            "summary": "",
            "score": 2.5,
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
        "你是结构化抽取器。请仅基于文档内容抽取字段，并且只输出一个严格合法 JSON 对象。\n"
        "【输出契约】\n"
        "1. 必须只输出 JSON，不要输出 Markdown、解释文本、候选列表或思考过程。\n"
        "2. 顶层键只能是: country, channel, maturity, format, architectureId, fileDataItem。\n"
        "3. 不要直接原样返回候选对象、候选数组、key/value 对象或中文键名。\n"
        "4. country/channel/maturity/format 只能输出候选项中的 value 字符串；不能输出 key，也不能输出对象。\n"
        "5. architectureId 只能输出候选 architectureList 中的 id 数字；无法匹配时输出 1。\n"
        "6. fileDataItem.fileName 必须与请求中的 fileName 一致。\n"
        "7. documentTranslationOne 和 documentTranslationTwo 固定输出空字符串。\n"
        "8. originalText 当前由服务端回填，输出空字符串即可，不要编造长段原文。\n"
        "9. fileDataItem中的summary,keyword,fileNo,dataFormat字段不允许留空，必须根据文档内容推断出具体值。score字段范围在0.0到5.0之间。\n"
        "【正反例】\n"
        "- 正确: \"country\": \"美国\"\n"
        "- 错误: \"country\": {\"key\": \"02\", \"value\": \"美国\"}\n"
        "- 正确: \"architectureId\": 10502\n"
        "- 错误: \"architectureId\": \"作战指挥/组织机构\"\n"
        + ARCHITECTURE_CLASSIFICATION_RULES
        + "输出 JSON 必须严格匹配以下结构：\n"
        + f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        + _format_options("领域体系候选", ranges["architectureList"])
        + _format_options("国家候选", ranges["country"])
        + _format_options("渠道候选", ranges["channel"])
        + _format_options("成熟度候选", ranges["maturity"])
        + _format_options("格式候选", ranges["format"])
        + "【抽取优先级】请优先抽取：资料年代、关键词、摘要、文件编号、资料来源、原文链接、语种、资料格式、所属装备、所属技术、装备型号、文件概述。\n"
        + "【抽取字段解释】keyword: 文档中提到的关键信息或主题（由两三个简短的词构成）; score: 该信息的重要性评分; fileNo: 文件编号; dataFormat: 资料格式，保持与\"dataFormat\"一致。\n"
        + "【输出前自检清单】\n"
        + "1. country/channel/maturity/format 是否都为候选 value 或空字符串。\n"
        + "2. architectureId 是否为候选 id 或 1。\n"
        + "3. 是否仅使用英文键名且 JSON 语法可解析。\n"
    )


def build_report_prompt(request_params: dict) -> str:
    return (
        "请基于提供的全部文件内容生成 HTML 报告片段。\n"
        f"模板说明: {request_params.get('templateDesc', '')}\n"
        f"模板大纲: {request_params.get('templateOutline', '')}\n"
        f"业务需求: {request_params.get('requirement', '')}\n"
        "输出必须可直接嵌入页面，不要附加 Markdown 代码块。\n"
    )


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
        "3. 请基于文档原文提取，不要推测或编造\n"
        "4. 若原文中存在多个并列的值（例如多个型号、多个编号、多个名称等），"
        '请使用英文逗号加空格 ", " 将所有值依次串联在一行内返回，'
        "不要用自然语言连句、不要添加数量描述、不要使用换行或项目符号\n"
        "5. 若只有单个值，直接返回该值即可，不要加任何分隔符\n"
        "格式示例:\n"
        "   单值: 052D\n"
        "   多值: no.1, no.2, no.100"
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
