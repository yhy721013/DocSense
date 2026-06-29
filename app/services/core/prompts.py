from __future__ import annotations

import json
from typing import Any, Iterable


ARCHITECTURE_CLASSIFICATION_RULES = (
    "【领域分类判定规则】\n"
    "1. architectureList 只包含 id, name, parentId, path, pathName, remark：id 是节点唯一标识，name 是节点名称，parentId 表示父节点 id，path 是从根到当前节点的 id 链，pathName 是从根到当前节点的名称链，remark 是节点名词概述，可用于理解节点含义。\n"
    "2. 当 architectureList 只有一个节点时，architectureId 必须直接输出这个唯一节点的 id，不要再判断文件所属领域分类；但仍需继续完成 fileDataItem 信息提取。\n"
    "3. 多节点候选中，【必须】分类到最底层的叶子节点。\n"
    "4. 如果叶子节点证据不足或者无法区分应该归类到哪一个叶子节点，则返回「战技指标」这一节点对应的 id。\n"
    "5. 如果候选的多个叶子节点证据相当，则输出证据最充分的节点 id。\n"
    "6. 当文档内容明确为 GJB、国军标、国家军用标准相关资料时，应归类到候选中的「数据标准」下的各个子节点，并返回叶子节点的 id，【禁止】返回「数据标准」对应的ID。\n"
    "7. 不要输出分类名称、候选列表或概率，只输出最终 architectureId 数字。\n"
    "8. 如果文档主要介绍一种武器装备，【必须】将文档分类为这种武器装备下描述某个方面的子类别，如基础数据、战技指标、运用数据、效能数据等"
)

SOURCE_SCORE_RULES = (
    "【score 评分规则】\n"
    "score 必须且只能输出以下 5 个整数值，且为必填项：\n"
    "1. 闭源渠道或权威机构公开发布，如军政官方机构网站或正式出版物，评分 95。\n"
    "2. 专业科研单位，如兰德、简氏等知名智库及装备研制单位等，评分 85。\n"
    "3. 专业信息网站，如海上舰艇交通网、美舰艇历史与部署网、海军学院新闻网、防务新闻网等，评分 75。\n"
    "4. 普通信息网站，如综合性新闻网、百度百科等，评分 65。\n"
    "5. 未明确数据来源的资料，评分 55。\n"
)


SOURCE_FIELD_RULES = (
    "【source 来源出处规则】\n"
    "source 必须输出文档内容中提到的具体数据来源出处，如发布机构、网站、刊物、报告名、原文出处或资料页来源。\n"
    "不要把 score 数字、评分档位或“权威机构公开发布/专业信息网站”等泛泛评分理由当作 source。\n"
    "文档未明确提到具体来源出处时，source 输出“未明确数据来源”；这种情况下 score 应按评分规则输出 55。\n"
)


def _format_options(title: str, items: Iterable[Any]) -> str:
    return f"{title}: {json.dumps(list(items), ensure_ascii=False)}\n"


def _format_architecture_options(items: Iterable[Any], title: str = "领域体系候选") -> str:
    fields = ("id", "name", "parentId", "path", "pathName", "remark")
    formatted_items = []
    for item in items:
        if not isinstance(item, dict):
            formatted_items.append(item)
            continue
        formatted_items.append({field: item.get(field) for field in fields if field in item})
    return _format_options(title, formatted_items)


def build_file_analysis_prompt(request_params: dict) -> str:
    from app.services.llm_service.analysis_service import build_effective_analysis_ranges

    ranges = build_effective_analysis_ranges(request_params)
    standard_ranges = ranges["architectureStandardList"]
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
            "score": 55,
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
    data_standard_contract = ""
    data_standard_options = ""
    data_standard_priority = ""
    data_standard_self_check = ""
    final_self_check_index = "4"
    if standard_ranges:
        schema["fileDataItem"].update(
            {
                "militaryName": "",
                "num": "",
                "startTime": "",
                "implTime": "",
                "approvalDept": "",
            }
        )
        data_standard_contract = (
            "11. architectureStandardList 表示数据标准额外解析范围；当最终 architectureId 命中该范围时，"
            "fileDataItem 必须额外抽取 militaryName、num、startTime、implTime、approvalDept。"
            "startTime 和 implTime 使用 yyyy-MM-dd；找不到则输出空字符串。\n"
        )
        data_standard_options = _format_architecture_options(standard_ranges, "数据标准额外解析范围")
        data_standard_priority = (
            "【数据标准额外字段】若文件属于 architectureStandardList 范围，请抽取："
            "militaryName=国军标名称，num=编号，startTime=发布时间，implTime=实施时间，approvalDept=批准部门。\n"
        )
        data_standard_self_check = "4. 若命中 architectureStandardList，startTime/implTime 是否为 yyyy-MM-dd 或空字符串。\n"
        final_self_check_index = "5"
    return (
        "你是结构化抽取器。请仅基于文档内容抽取字段，并且只输出一个严格合法 JSON 对象。\n"
        "【请求上下文】\n"
        f"fileName: {request_params.get('fileName', '')}\n"
        f"originalFileName: {request_params.get('originalFileName', '')}\n"
        "【输出契约】\n"
        "1. 必须只输出 JSON，不要输出 Markdown、解释文本、候选列表或思考过程。\n"
        "2. 顶层键只能是：country, channel, maturity, format, architectureId, fileDataItem。\n"
        "3. 不要直接原样返回候选对象、候选数组、key/value 对象或中文键名。\n"
        "4. country/channel/maturity/format 只能输出候选项中的 value 字符串；fileDataItem.dataFormat 必须与顶层 format 完全一致，也只能输出格式候选中的 value；不能输出 key，也不能输出对象。\n"
        "5. architectureId 只能输出候选 architectureList 中的叶子 id 数字；无法匹配时输出 1。\n"
        "6. fileDataItem.fileName 必须与请求中的 fileName 一致。\n"
        "7. documentTranslationOne 和 documentTranslationTwo 固定输出空字符串。\n"
        "8. originalText 当前由服务端回填，输出空字符串即可，不要编造长段原文。\n"
        "9. fileDataItem 中的 summary, keyword, score, source, fileNo, dataFormat 字段不允许留空，必须根据文档内容推断；source 必须是具体数据来源出处，找不到明确出处时输出“未明确数据来源”。score 必须按下方评分规则输出 95、85、75、65、55 之一。\n"
        "10. documentOverview 字段要求输出不少于 1000 字的描述，尽可能详细完整，突出文档核心内容和特点。\n"
        "11. fileDataItem.dataTime 必须输出文档中明确提到的资料年代，输出格式为 yyyy-MM-dd，找不到时输出空字符串。\n"
        + data_standard_contract
        + "【正反例】\n"
        "- 正确：\"country\": \"美国\"\n"
        "- 错误：\"country\": {\"key\": \"02\", \"value\": \"美国\"}\n"
        "- 正确：\"architectureId\": 10502\n"
        "- 错误：\"architectureId\": \"作战指挥/组织机构\"\n"
        + ARCHITECTURE_CLASSIFICATION_RULES
        + SOURCE_SCORE_RULES
        + SOURCE_FIELD_RULES
        + "输出 JSON 必须严格匹配以下结构：\n"
        + f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        + _format_architecture_options(ranges["architectureList"])
        + data_standard_options
        + _format_options("国家候选", ranges["country"])
        + _format_options("渠道候选", ranges["channel"])
        + _format_options("成熟度候选", ranges["maturity"])
        + _format_options("格式候选", ranges["format"])
        + "【抽取优先级】请优先抽取：资料年代、关键词、摘要、文件编号、资料来源、原文链接、语种、资料格式、所属装备、所属技术、装备型号、文件概述。\n"
        + data_standard_priority
        + "【抽取字段解释】keyword：文档中提到的关键信息或主题，由至少 10 个关键词构成，关键词需要涵盖文章中提到的内容，按照占比从高到低排列；score：资料来源权威性评分；source：文档中提到的具体数据来源出处，缺少明确出处时输出“未明确数据来源”；fileNo：文件编号；dataFormat：资料格式，必须与顶层 format 完全一致，并且只能使用格式候选中的 value。\n"
        + "【输出前自检清单】\n"
        + "1. country/channel/maturity/format 是否都为候选 value 或空字符串；fileDataItem.dataFormat 是否与顶层 format 完全一致。\n"
        + "2. architectureId 是否为候选叶子 id 或 1。\n"
        + "3. score 是否为 95、85、75、65、55 之一；source 是否为具体来源出处或“未明确数据来源”。\n"
        + "4. fileDataItem.dataTime 是否为 yyyy-MM-dd 或空字符串。\n"
        + "5. 当文件内容与数据标准相关时，architectureId 【禁止】输出「数据标准」对应的ID，而是输出其下的六个子类别之一的对应ID：建模与仿真标准，军用软件标准，目标特性标准，术语与定义标准，通用要求标准，元数据标准。\n"
        + data_standard_self_check
        + f"{final_self_check_index}. 是否仅使用英文键名且 JSON 语法可解析。\n"
    )


def build_report_prompt(request_params: dict) -> str:
    return (
        "请基于提供的全部文件内容生成 HTML 报告片段。\n"
        f"模板说明：{request_params.get('templateDesc', '')}\n"
        f"模板大纲：{request_params.get('templateOutline', '')}\n"
        f"业务需求：{request_params.get('requirement', '')}\n"
        "输出必须可直接嵌入页面，不要附加 Markdown 代码块。\n"
    )


def build_input_field_prompt(field_name: str, field_description: str = "") -> str:
    """构建 INPUT 类型字段的 RAG 查询 Prompt。"""
    desc_part = ""
    if field_description:
        desc_part = f"\n字段说明：{field_description}"
    return (
        f"请从文档中提取以下信息：{field_name}。{desc_part}\n"
        "要求：\n"
        "1. 只需回答该字段的具体值，不要添加额外解释\n"
        '2. 如果文档中找不到相关信息，请只回答"未找到"\n'
        "3. 请基于文档原文提取，不要推测或编造\n"
        "4. 若原文中存在多个并列的值（例如多个型号、多个编号、多个名称等），"
        '请使用英文逗号加空格 ", " 将所有值依次串联在一行内返回，'
        "不要用自然语言连句、不要添加数量描述、不要使用换行或项目符号\n"
        "5. 若只有单个值，直接返回该值即可，不要加任何分隔符\n"
        "格式示例：\n"
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
        desc_part = f"\n字段说明：{field_description}"
    ctx_part = ""
    if table_context:
        ctx_part = f"\n表格上下文：{table_context}"
    return (
        f'请从文档中提取关于"{field_name}"的所有数据。{desc_part}{ctx_part}\n'
        "要求：\n"
        f'1. 如果有多个不同来源或多条记录提到了不同的"{field_name}"值，请逐条列出\n'
        "2. 对每条值，请标注来自哪份文献或哪段原文\n"
        "3. 格式示例：\n"
        "   值1: XXX (来源: 文献A)\n"
        "   值2: YYY (来源: 文献B)\n"
        '4. 如果文档中找不到相关信息，请只回答"未找到"'
    )


def build_table_extraction_prompt(
    table_name: str,
    table_description: str,
    column_defs: list[dict],
    chunks: list[str],
    terms_rule_context: str = "",
) -> str:
    """构建 TABLE 类型字段的整表抽取 Prompt。"""
    desc_part = f"表格说明：{table_description}\n" if table_description else ""
    terms_part = _build_terms_rule_part(terms_rule_context)

    column_lines = []
    json_fields = [
        f'{json.dumps("__rowKey", ensure_ascii=False)}: '
        f'{json.dumps("用于识别并合并同一行实体的名称、型号或唯一标识", ensure_ascii=False)}'
    ]
    for index, column in enumerate(column_defs, 1):
        column_name = str(column.get("fieldName", "")).strip()
        if not column_name:
            continue
        column_desc = str(column.get("fieldDescription", "")).strip()
        if column_desc:
            column_lines.append(f"{index}. {column_name}: {column_desc}")
        else:
            column_lines.append(f"{index}. {column_name}")
        json_fields.append(f'{json.dumps(column_name, ensure_ascii=False)}: ""')

    chunks_text = ""
    for idx, chunk in enumerate(chunks, 1):
        chunks_text += f"第{idx}段Chunk是：\n{chunk}\n\n"

    example_json = "[\n  {\n    " + ",\n    ".join(json_fields) + "\n  }\n]"

    return (
        f"请基于以下提供的文本片段，抽取表格“{table_name}”的多行结构化数据。\n"
        f"{desc_part}"
        f"{terms_part}"
        "列定义：\n"
        f"{chr(10).join(column_lines)}\n\n"
        "要求：\n"
        "1. 必须且只能基于以下文本片段抽取，不得使用其他知识、常识或推测。\n"
        "2. 每一行必须表示一个独立对象、部件、型号或记录；例如同一艘航母上的多种雷达必须拆成多行。\n"
        "3. 不得把多个行实体用逗号、顿号、换行或项目符号合并到同一个单元格。\n"
        "4. 每个对象只填写列定义中要求的字段；某列没有明确依据时填空字符串。\n"
        "5. __rowKey 必须填写该行实体最稳定的名称、型号或唯一标识，用于服务端合并同一行。\n"
        '6. 如果没有任何可抽取的行，请只输出空数组 []。\n'
        "7. 只输出合法 JSON 数组，不要输出 Markdown、解释说明或额外文本。\n\n"
        "JSON 格式示例：\n"
        f"{example_json}\n\n"
        "【文本片段开始】\n"
        f"{chunks_text}"
        "【文本片段结束】"
    )


def _build_terms_rule_part(terms_rule_context: str = "") -> str:
    if not terms_rule_context:
        return ""
    return (
        "【术语规则参考开始】\n"
        f"{terms_rule_context}\n"
        "【术语规则参考结束】\n"
        "术语规则参考仅用于理解字段口径、别名和单位，不是目标装备资料；"
        "不得从术语规则中抽取 analyseData。\n\n"
    )


def build_chunk_based_field_prompt(
    field_name: str,
    chunk_text: str,
    field_description: str = "",
    terms_rule_context: str = "",
) -> str:
    """构建基于具体 Chunk 的 INPUT 类型字段查询 Prompt。"""
    desc_part = ""
    if field_description:
        desc_part = f"字段说明：{field_description}\n"
    terms_part = _build_terms_rule_part(terms_rule_context)
    return (
        f"请基于以下给定的文本片段，提取字段“{field_name}”的信息。\n"
        f"{desc_part}"
        f"{terms_part}"
        "要求：\n"
        "1. 必须且只能基于以下提供的文本片段进行回答，不得使用其他知识。\n"
        '2. 如果在文本片段中找不到相关信息，请只回答"未找到"，不要包含额外说明。\n'
        "3. 只需回答该字段的具体值，不要添加额外解释。\n"
        "4. 若原文中存在多个并列的值（例如多个型号、多个编号、多个名称等），"
        '请使用英文逗号加空格 ", " 将所有值依次串联在一行内返回，不要用自然语言连句或换行。\n'
        "5. 若只有单个值，直接返回该值即可，不要加任何分隔符。\n\n"
        "【文本片段开始】\n"
        f"{chunk_text}\n"
        "【文本片段结束】"
    )


def build_multi_chunk_based_field_prompt(
    field_name: str,
    chunks: list[str],
    field_description: str = "",
    terms_rule_context: str = "",
) -> str:
    """构建基于多个 Chunk 的 INPUT 类型字段查询 Prompt。"""
    desc_part = ""
    if field_description:
        desc_part = f"字段说明：{field_description}\n"
    terms_part = _build_terms_rule_part(terms_rule_context)
    
    chunks_text = ""
    for idx, chunk in enumerate(chunks, 1):
        chunks_text += f"第{idx}段Chunk是：\n{chunk}\n\n"

    return (
        f"请基于以下提供的文本片段，提取字段“{field_name}”的信息。\n"
        f"{desc_part}"
        f"{terms_part}"
        "要求：\n"
        "1. 存在多个相关的Chunk，必须且只能基于以下提供的所有文本片段进行综合判断和回答，不得使用其他知识。\n"
        '2. 如果在所有文本片段中都找不到相关信息，请只回答"未找到"，不要包含额外说明。\n'
        "3. 只需回答该字段的具体值，不要添加额外解释。\n"
        "4. 若原文中存在多个并列的值（例如多个型号、多个编号、多个名称等），"
        '请使用英文逗号加空格 ", " 将所有值依次串联在一行内返回，不要用自然语言连句或换行。\n'
        "5. 若只有单个值，直接返回该值即可，不要加任何分隔符。\n\n"
        "【文本片段开始】\n"
        f"{chunks_text}"
        "【文本片段结束】"
    )
