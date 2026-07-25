from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence


MAX_REPAIR_CONTEXT_CHARS = 20_000
"""修复 Prompt 允许携带的模型原始结果最大字符数。

修复请求必须包含失败内容才能独立复核，但无限制复制异常回答会放大模型成本并可能超过
审计字段上限。该限制只截断修复上下文，不修改首次回答在 RagAttempt 中保存的原始证据。
"""

ANALYSIS_KEYWORD_MIN_COUNT = 5
"""文件分析 keyword 字段要求的最少关键词数量。"""

ANALYSIS_KEYWORD_MAX_COUNT = 10
"""文件分析 keyword 字段允许的最多关键词数量。"""

# 保留旧名称，避免既有调用方把原先的固定数量常量当作输出上限使用时失效。
ANALYSIS_KEYWORD_COUNT = ANALYSIS_KEYWORD_MAX_COUNT

ANALYSIS_KEYWORD_MAX_CHARS = 30
"""文件分析单个关键词允许的最大字符数。"""

ANALYSIS_ENUM_FIELD_MAX_ITEMS = 10
"""装备、技术和型号等枚举型字符串字段允许的最大去重项数。"""

ANALYSIS_ENUM_ITEM_MAX_CHARS = 50
"""装备、技术和型号等枚举型字符串字段单项允许的最大字符数。"""

ANALYSIS_SUMMARY_MAX_CHARS = 500
"""文件分析摘要允许的最大字符数。"""

ANALYSIS_RESPONSE_MAX_CHARS = 6_000
"""文件分析结构化 JSON 回答建议遵守的整体字符上限。"""

UNKNOWN_SOURCE_VALUE = "未明确数据来源"
"""文件内容未提供具体来源出处时使用的唯一 source 占位值。"""

ANALYSIS_SUMMARY_TYPE_CHAR_RANGES = {
    "【材料类型：新闻】": (100, 300),
    "【材料类型：报告】": (300, 500),
    "【材料类型：科普资讯文稿】": (100, 300),
}
"""已确认材料类型的 summary 字符数范围，固定前缀计入长度。"""

ANALYSIS_SUMMARY_RULES = (
    "【summary 摘要生成规则】\n"
    "1. 每份文件只生成一篇独立中文摘要，仅使用当前文档证据，不得混入其他材料内容；"
    "全程客观中立，不添加主观评价、褒贬、猜测或引申，过滤广告、推广和与正文无关的"
    "外链描述，但不得影响 originalLink、source 等其他字段的独立抽取。\n"
    "2. 先判断全文材料类型。仅当文档内容能够明确识别为新闻、报告或科普资讯文稿时，"
    "应用对应的类型专属规则；标准、规范、手册、目录、表格等不属于上述三类，不得添加"
    "任何材料类型前缀，只遵守通用的客观、非空和最多 500 字要求。不得根据文件名或局部"
    "词语强行判型，也不得创建新的材料类型标签。固定材料类型前缀计入下述字符数。\n"
    "3. 新闻：以“【材料类型：新闻】”开头，全文 100 至 300 个字符，使用单段连贯表述，"
    "按原文叙事顺序提炼原文明示的时间、地点、主体、事件、起因、进展和各方表态；原文"
    "缺少的要素不补造，区分事实与评论，不把评论当作核心事实，不放大冲突。\n"
    "4. 报告：以“【材料类型：报告】”开头，全文必须为 300 至 500 个字符，优先写到 "
    "350 至 450 个字符，避免贴近下限。先从全文整理至少 6 个互不重复的有效信息点，再按"
    "原文逻辑写成摘要。调研、政策或评估报告按原文明示内容提炼发布主体、调研范围、核心"
    "量化数据、现存问题、结论和对策；装备、工程或技术报告按原文明示内容提炼介绍对象、"
    "关键规格、系统组成、项目进度、现代化改进、运用情况、风险或估算信息。某类要素原文"
    "没有时不得补造，应改用其他有证据的信息点补足长度。不得篡改、简化或夸大数据，"
    "专业术语沿用原文，完整保留风险、短板和局限性，并明确标注预测、预估或估算内容。\n"
    "5. 科普资讯文稿：以“【材料类型：科普资讯文稿】”开头，全文 100 至 300 个字符，"
    "使用单段客观说明，按原文信息顺序提炼发布或编撰时间、介绍对象，以及原文明示的"
    "尺寸、性能和配套硬件参数；不得推测原文未提及的性能或背景，不评价介绍对象优劣。\n"
    "6. 写完 summary 后必须在输出 JSON 前进行一次字符数自检，汉字、字母、数字、标点和"
    "固定前缀均按字符计数，换行不作为补字手段。若低于当前材料类型下限，必须补入尚未"
    "覆盖的原文事实、数据、系统、进展或限制；若超过上限，必须合并重复信息并压缩表述；"
    "完成修正并重新检查后才能输出，不要把字符数或自检过程写入 JSON。\n"
    f"7. summary 在任何情况下都不得超过 {ANALYSIS_SUMMARY_MAX_CHARS} 个字符；不得照抄"
    "规则说明、示例占位内容或通过重复句子凑字数。\n"
)

ANALYSIS_KEYWORD_RULES = (
    "【keyword 关键词生成规则】\n"
    f"1. keyword 必须输出 {ANALYSIS_KEYWORD_MIN_COUNT} 至 {ANALYSIS_KEYWORD_MAX_COUNT} 个"
    "关键词，不得少于 5 个；保持一个字符串，使用英文逗号分隔，不加编号、分组标签或"
    "解释。分类路径关键词排在前面，内容关键词排在后面，同一组内按与文档的相关性从高"
    "到低排列。\n"
    "2. 分类路径关键词只能使用最终分类节点及其已验证 parentId 祖先链中各节点的 name 原值；"
    "pathName 是不透明的展示文本，不得按“/”或其他符号拆分、改写或反推树层级。两阶段抽取"
    "优先使用已确认领域分类中的 pathNodeNames，兼容模式根据最终 architectureId 和"
    "architectureList 的 parentId 链确定。优先选择 3 至 4 个与全文最相关的节点；当前有限树"
    "不足 3 个可见节点时如实使用全部已有节点，不得自行创造或补造分类词。将实际采用的"
    "分类路径关键词数量记为 C。\n"
    "3. 内容关键词数量不得少于 max(2, 5-C)，不得多于 min(7, 10-C)。必须先完成本次输出"
    "中的 summary，优先从 summary 明确出现的原词或原短语中选择；若与分类路径关键词合计"
    "仍不足 5 个，允许从正文明确出现的标准编号、装备名称、技术名称或其他核心名词短语中"
    "补足。不得进行没有正文证据的同义替换、拼接或编造。\n"
    "4. 优先选择不可替代的名词或名词短语，必要时可选个别核心动词；重点保留装备、系统、"
    "作战样式、关键要素和场景标识等专有名词。排除虚词、连接词、评价性形容词和“分析”、"
    "“研究”、“问题”等单独使用时过于宽泛的词。\n"
    "5. 分类路径关键词与内容关键词可以语义相关，但不得完全重复同一词项；不得循环输出、"
    "使用连续编号补造或添加与文档相关性不大的词凑数。"
    f"每个关键词不超过 {ANALYSIS_KEYWORD_MAX_CHARS} 个字符。\n"
    "6. 输出 JSON 前必须按英文逗号重新拆分并计数 keyword：最终总数少于 5 个时，继续从"
    "summary 或正文中补入有明确证据的核心词；超过 10 个时删除相关性最低的内容关键词。"
    "确认最终总数为 5 至 10 个后才能输出，不要把计数过程写入 JSON。\n"
)

ANALYSIS_RELATED_TECHNOLOGY_RULES = (
    "【relatedTechnology 所属技术生成规则】\n"
    "1. relatedTechnology 表示支撑文中核心装备、作战或保障活动的专业技术门类或能力域。"
    "必须输出中文规范名称。中文正文可使用正文技术原词或不改变含义的规范化名称；外文"
    "正文允许把原文技术术语准确翻译为中文，不要求中文名称在外文正文中逐字出现。\n"
    "2. 正文明示介绍、列举或用于说明核心装备性能、系统构成、作战能力或保障能力的技术"
    "可以入选；仅偶然出现、与文档主题无关或只能依靠常识推断的技术不选。\n"
    "3. 不得把装备名称或型号、部队番号或建制、地理名称、作战概念、单次战术动作、"
    "评价性形容词及过于宽泛的概括词当作所属技术。\n"
    "4. relatedTechnology 非空时，必须同时输出仅供服务端核验的 relatedTechnologyEvidence "
    "数组，并与技术名称按顺序一一对应。数组每项只能包含 nameZh 和 sourceTerm：nameZh "
    "必须与 relatedTechnology 中的中文名称完全一致；sourceTerm 必须复制正文中能够直接"
    "支持该中文名称的最短原文技术词或短语，保持原文语言，不得翻译、概括或填写整句。"
    "无法给出直接证据词的技术必须删除。没有合格技术时，relatedTechnology 输出空字符串，"
    "relatedTechnologyEvidence 输出空数组。\n"
    f"5. 最多输出 {ANALYSIS_ENUM_FIELD_MAX_ITEMS} 个互不重复的技术名称，每项不超过 "
    f"{ANALYSIS_ENUM_ITEM_MAX_CHARS} 个字符，按相关性从高到低排列，使用英文逗号分隔，"
    "不加编号或解释；没有合格技术时输出空字符串，不得凑数。\n"
)

ANALYSIS_COMPACT_OUTPUT_RULES = (
    ANALYSIS_SUMMARY_RULES
    + ANALYSIS_KEYWORD_RULES
    + ANALYSIS_RELATED_TECHNOLOGY_RULES
    + "【其他输出长度与去重约束】\n"
    "1. associatedEquipment、equipmentModel 使用英文逗号分隔；"
    f"每个字段最多 {ANALYSIS_ENUM_FIELD_MAX_ITEMS} 个互不重复的条目，每个条目不超过 "
    f"{ANALYSIS_ENUM_ITEM_MAX_CHARS} 个字符；只保留文档有明确证据的主体实体，禁止循环"
    "枚举或按编号规律补造实体。\n"
    "2. documentOverview 继续遵守不超过 1000 个字符的限制；任何字段都不得完全重复"
    "同一词项、实体、短语或句子。\n"
    f"3. 完整 JSON 对象不超过 {ANALYSIS_RESPONSE_MAX_CHARS} 个字符。接近上限时优先压缩"
    "自由文本和枚举字段，但必须保留完整 JSON 结构、闭合全部引号与括号，并在对象结束后"
    "立即停止输出。\n"
)


ARCHITECTURE_CLASSIFICATION_RULES = (
    "【领域分类判定规则】\n"
    "1. architectureList 只包含 id, name, parentId, path, pathName, remark：id 是节点唯一标识，name 是节点名称，parentId 表示父节点 id，path 是从根到当前节点的 id 链，pathName 是从根到当前节点的名称链，remark 是节点名词概述，可用于理解节点含义。\n"
    "2. 当 architectureList 只有一个节点时，architectureId 必须直接输出这个唯一节点的 id，不要再判断文件所属领域分类；但仍需继续完成 fileDataItem 信息提取。\n"
    "3. 多节点候选中，【必须】分类到最底层的叶子节点。\n"
    "4. 如果叶子节点证据不足或者无法区分，不得猜测、不得默认选择「战技指标」，应保持 architectureId 为空。\n"
    "5. 只有文档证据能够支持唯一候选时才输出该叶子节点 id。\n"
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
    f"5. source 为“{UNKNOWN_SOURCE_VALUE}”时，score 必须且只能输出 55。\n"
)


SOURCE_FIELD_RULES = (
    "【source 来源出处规则】\n"
    "source 必须输出文档内容中提到的具体数据来源出处，如发布机构、网站、刊物、报告名、原文出处或资料页来源。\n"
    "不要把 score 数字、评分档位或“权威机构公开发布/专业信息网站”等泛泛评分理由当作 source。\n"
    f"文档未明确提到具体来源出处时，source 必须且只能输出“{UNKNOWN_SOURCE_VALUE}”，"
    "且 score 必须且只能输出 55；禁止在这种情况下输出 95、85、75 或 65。\n"
    f"source 不允许留空，必须输出具体来源或“{UNKNOWN_SOURCE_VALUE}”。\n"
    "source 必须从文档内容中提取，不得凭常识、推测或编造。\n"
    "source 不允许与 channel（机构名称）、originalLink 等字段内容一致。\n"
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


MODEL_ARCHITECTURE_CANDIDATE_FIELDS = ("id", "pathName", "nodeType", "remark")
"""两阶段分类允许暴露给模型的最小候选字段集合。"""

MAX_ARCHITECTURE_REMARK_CHARS = 512
"""单个模型候选允许携带的 remark 最大字符数。"""


_CANDIDATE_FIELD_ALIASES = {
    "pathName": ("path_name", "semantic_path"),
    "nodeType": ("node_type",),
}

DATA_STANDARD_CANDIDATE_REMARKS = {
    "建模与仿真": (
        "标准主体涉及模型、仿真、HLA、VV&A、仿真互操作或模型数据。"
    ),
    "军用软件": (
        "标准主体涉及军用软件研制、测试、质量保证、生命周期、编码或软件文档。"
    ),
    "目标特性": (
        "标准主体涉及目标的雷达、红外、声学、散射、特征表征或特性数据。"
    ),
    "术语与定义": (
        "标准标题和范围的主体是术语、词汇、定义或概念体系；普通标准中的固定章节不算。"
    ),
    "通用要求": (
        "质量管理、通用技术、通用管理或综合要求，以及不属于其他五个专门主题的标准。"
    ),
    "元数据": (
        "标准主体涉及元数据、数据元素、数据字典、模式、目录、编码或交换描述。"
    ),
}


def _normalize_data_standard_kind(value: Any) -> str:
    text = str(value or "").strip()
    if "/" in text:
        text = text.rsplit("/", 1)[-1].strip()
    if text.endswith("标准"):
        text = text[:-2].strip()
    return text


def data_standard_candidate_remark(value: Any) -> str:
    """返回数据标准六类的服务端语义卡片，不依赖调用方是否填写 remark。"""

    return DATA_STANDARD_CANDIDATE_REMARKS.get(
        _normalize_data_standard_kind(value),
        "",
    )


def _candidate_field(item: Any, field: str) -> Any:
    """兼容协议字典和使用 snake_case/语义路径命名的不可变 DTO。"""
    names = (field, *_CANDIDATE_FIELD_ALIASES.get(field, ()))
    if isinstance(item, Mapping):
        for name in names:
            value = item.get(name)
            if value is not None:
                return value
        return None
    for name in names:
        value = getattr(item, name, None)
        if value is not None:
            return value
    return None


def _project_architecture_candidates(items: Iterable[Any]) -> list[dict[str, Any]]:
    """将内部候选收敛为分类模型可见的稳定投影。

    旧调用方可能仍传入只有 ``name/pathName`` 的完整树节点。为保证 legacy repair
    可继续运行，缺失的 ``pathName`` 会回退到 ``name``，缺失 ``nodeType`` 则显式
    标为 ``unknown``；不会把完整树的 parentId/path/name 等字段泄漏给两阶段 Prompt。
    """
    projected: list[dict[str, Any]] = []
    for item in items:
        candidate_id = _candidate_field(item, "id")
        if candidate_id is None:
            continue
        path_name = _candidate_field(item, "pathName")
        if path_name is None:
            path_name = _candidate_field(item, "name")
        node_type = _candidate_field(item, "nodeType") or "unknown"
        candidate = {
            "id": candidate_id,
            "pathName": str(path_name or ""),
            "nodeType": str(node_type),
        }
        remark = _candidate_field(item, "remark")
        if remark is not None and str(remark).strip():
            candidate["remark"] = str(remark).strip()[:MAX_ARCHITECTURE_REMARK_CHARS]
        projected.append(candidate)
    return projected


def build_architecture_classification_prompt(
    request_params: Mapping[str, Any],
    architecture_candidates: Iterable[Any],
    *,
    classification_context: Mapping[str, Any] | None = None,
) -> str:
    """构造两阶段流程的纯领域分类 Prompt。

    该 Prompt 只暴露召回模块已经裁剪过的模型候选，不包含完整领域树，也不要求模型
    同时完成字段抽取。返回值契约只有一个可为空的数字 ``architectureId``。
    """
    candidates = _project_architecture_candidates(architecture_candidates)
    if not candidates:
        raise ValueError("architecture_candidates 不能为空")
    context_payload = {
        key: classification_context[key]
        for key in (
            "title",
            "primaryIdentifier",
            "qualifier",
            "scopeKind",
            "highLevelBranchHint",
            "dominantDetailKind",
            "matchedScopeParentId",
            "treeGap",
        )
        if classification_context is not None
        and key in classification_context
        and classification_context[key] not in (None, "", ())
    }
    context_text = (
        "serverExtractedClassificationContext: "
        + json.dumps(
            context_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
        if context_payload
        else ""
    )
    if context_payload:
        classification_rules = (
            "1. 只能选择下方模型候选中的数字 id，不得输出候选外 ID、分类名称或默认值。\n"
            "2. 按全文主要对象和覆盖粒度分类；class/舰级资料应选择对应舰级或批次父节点，不能因首舰号而缩小成单舰资料。\n"
            "3. Fleetlist、Ship totals 或 Aircraft totals 中的成员型号只能辅助确认文档作用域，不能单独成为最终分类依据。\n"
            "4. 标题中的 Flight、Block、批次限定词优先于基础型号；存在对应作用域父节点时，应选择该父节点。\n"
            "5. 只有全文主要描述某个明细类别时，才选择该明细 leaf；serverExtractedClassificationContext 中 dominantDetailKind=technical_specifications 表示服务端已确认 Specifications 是短篇全文的主体标题而非 Contents 中的普通章节，此时必须选择兼容装备分支内的战技指标 leaf，不能退回装备父节点；没有该标记时，不得因综合资料中的局部 Specifications 章节，或 class 文档中的局部参数，把全文缩小到某一明细叶子。\n"
            "6. 叶子证据不足但能够可靠确定父级领域时，只能选择候选中已有的 nodeType=parent 节点，并选择与全文作用域一致的最深层父节点。\n"
            "7. 文档证据不足以支持任一候选时，architectureId 必须输出 null，不得猜测。\n"
            "8. architectureId 有值时必须是 JSON 数字，不能是字符串。\n"
            "9. 只输出严格 JSON 对象，唯一键为 architectureId；不要输出 Markdown、概率、解释、候选列表或思考过程。\n"
        )
    else:
        classification_rules = (
            "1. 只能选择下方模型候选中的数字 id，不得输出候选外 ID、分类名称或默认值。\n"
            "2. 文档证据足以支持叶子候选时，优先选择 nodeType=leaf 的最具体候选。\n"
            "3. 叶子证据不足但能够可靠确定父级领域时，只能选择候选中已有的 nodeType=parent 节点，并选择证据支持的最深层父节点。\n"
            "4. 文档证据不足以支持任一候选时，architectureId 必须输出 null，不得猜测。\n"
            "5. architectureId 有值时必须是 JSON 数字，不能是字符串。\n"
            "6. 只输出严格 JSON 对象，唯一键为 architectureId；不要输出 Markdown、概率、解释、候选列表或思考过程。\n"
        )
    return (
        "你是文档领域分类器。请仅依据文档内容和下方有限候选确定所属领域。\n"
        "【请求上下文】\n"
        f"fileName: {request_params.get('fileName', '')}\n"
        f"originalFileName: {request_params.get('originalFileName', '')}\n"
        f"{context_text}"
        "【分类规则】\n"
        f"{classification_rules}"
        "【输出结构】\n"
        '{"architectureId": null}\n'
        "【模型候选】\n"
        f"{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}\n"
    )


def build_data_standard_classification_prompt(
    request_params: Mapping[str, Any],
    architecture_candidates: Iterable[Any],
    *,
    standard_context: Mapping[str, Any],
) -> str:
    """构造已确认标准正文的六类叶节点专用分类 Prompt。"""

    candidates = _project_architecture_candidates(architecture_candidates)
    if not candidates:
        raise ValueError("architecture_candidates 不能为空")
    for candidate in candidates:
        guidance = data_standard_candidate_remark(candidate.get("pathName"))
        if not guidance:
            raise ValueError(
                "数据标准专用分类只允许六类已知叶节点"
            )
        if not str(candidate.get("remark") or "").strip():
            candidate["remark"] = guidance

    context_payload = {
        key: standard_context[key]
        for key in (
            "standardNumber",
            "standardTitle",
            "documentKind",
            "evidenceSources",
        )
        if key in standard_context
        and standard_context[key] not in (None, "", (), [])
    }
    return (
        "你是数据标准正文分类器。服务端已经通过文件名、首页和标准结构确认该文档是标准正文；"
        "你的任务只是在下方数据标准叶节点中判断主题。\n"
        "【请求上下文】\n"
        f"fileName: {request_params.get('fileName', '')}\n"
        f"originalFileName: {request_params.get('originalFileName', '')}\n"
        "serverExtractedStandardContext: "
        f"{json.dumps(context_payload, ensure_ascii=False, separators=(',', ':'))}\n"
        "【分类规则】\n"
        "1. 只能选择下方候选中的数字 id，不得返回数据标准父节点、候选外 ID 或分类名称。\n"
        "2. 优先依据 standardTitle、标准范围和全文主要对象判断，不得由起草单位、"
        "引用标准或局部章节决定分类。\n"
        "3. 建模与仿真、军用软件、目标特性、术语与定义、元数据都需要标题或范围提供"
        "对应专门主题证据。\n"
        "4. “范围”“规范性引用文件”“术语和定义”等目录项是多数标准的固定章节；"
        "仅出现这些章节不能归为“术语与定义”。\n"
        "5. 已确认是标准正文，但不属于上述五个专门主题时，选择候选中的“通用要求”；"
        "如果候选中不存在“通用要求”且其他类别也无充分证据，则输出 null。\n"
        "6. architectureId 有值时必须是 JSON 数字，不能是字符串。\n"
        "7. 只输出严格 JSON 对象，唯一键为 architectureId；不要输出解释、概率、"
        "Markdown 或思考过程。\n"
        "【输出结构】\n"
        '{"architectureId": null}\n'
        "【数据标准叶节点候选】\n"
        f"{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}\n"
    )


def build_chat_title_prompt(
    messages: Sequence[Mapping[str, str]],
    *,
    max_title_chars: int = 20,
) -> str:
    """构建文件对话标题生成 Prompt。

    标题接口只允许使用本地 committed 历史作为输入。这里把消息序列序列化为 JSON，
    而不是拼接自然语言段落，避免历史内容中的换行、冒号或提示词片段破坏边界。
    """
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


def build_file_analysis_prompt(request_params: dict) -> str:
    from app.services.llm_service.analysis_service import build_effective_analysis_ranges

    ranges = build_effective_analysis_ranges(request_params)
    standard_ranges = ranges["architectureStandardList"]
    schema = {
        "country": "",
        "channel": "",
        "maturity": "",
        "security": "",
        "format": "",
        "architectureId": "",
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
            "relatedTechnologyEvidence": [],
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
        "2. 顶层键只能是：country, channel, maturity, security, format, architectureId, fileDataItem。\n"
        "3. 不要直接原样返回候选对象、候选数组、key/value 对象或中文键名。\n"
        "4. country/maturity/security/format 只能输出候选项中的 value 字符串或者空字符串；channel 规则见第 12 条；security 表示文件密级，必须根据文档开头、首页、页眉或标题附近的密级/保密说明判断；找不到相关说明时：若密级候选包含“公开”则输出“公开”，否则输出密级候选中的第一个 value；fileDataItem.dataFormat 必须与顶层 format 完全一致，也只能输出格式候选中的 value；不能输出 key，也不能输出对象。\n"
        "5. architectureId 只能输出候选 architectureList 中的叶子 id 数字；无法匹配时输出空字符串，禁止使用 1 或任意候选作为默认值。\n"
        "6. fileDataItem.fileName 必须与请求中的 fileName 一致。\n"
        "7. documentTranslationOne 和 documentTranslationTwo 固定输出空字符串。\n"
        "8. originalText 当前由服务端回填，输出空字符串即可，不要编造长段原文。\n"
        "9. fileDataItem 中的 summary, keyword, score, source, fileNo, dataFormat 字段不允许留空，必须根据文档内容推断；relatedTechnologyEvidence 只用于所属技术证据核验，不属于最终回调字段。source 必须是具体数据来源出处，找不到明确出处时输出“未明确数据来源”。score 必须按下方评分规则输出 95、85、75、65、55 之一。\n"
        "10. documentOverview 字段为文件概述，必须按资料原有目录、章节或标题层级进行概述，全文不超过 1000 字。优先说明全文整体结构，例如全文共多少章、核心内容集中在哪些章节；再按章节顺序概述各章主题、关键对象、重要结论或核心信息。不要机械复述目录，不要编造原文不存在的章节或内容；若资料无清晰目录结构，则按可识别的标题层级或内容模块进行概述。示例1：全文共 8 个章节。第一章主要包括 a、b、c 等内容；第二章主要描述……；第三章围绕 d、e 等内容展开；其余章节分别介绍……。示例2：全文共 8 个章节，核心内容集中在第 3 至第 7 章。第 1、2 章介绍基本概念和背景，第 8 章为结束语。第 3 章包括 a、b 等内容；第 4 章主要描述……；第 5 章主要描述……。\n"
        "11. fileDataItem.dataTime 必须输出文档中明确提到的资料年代，输出格式为 yyyy-MM-dd，找不到时输出空字符串。\n"
        "12. channel 字段表示“资料来源机构”，当 channel 候选为空时，channel 输出空字符串；当 channel 候选不为空时，必须从候选中选择一个 value 输出，不能输出 key，也不能输出对象。\n"
        + data_standard_contract
        + "13. fileDataItem.language 表示“原始资料正文的主要语种”，不是本次回答语言、摘要语言、翻译结果语言、文件名语言或提示词语言。\n"
        + "【正反例】\n"
        "- 正确：\"country\": \"美国\"\n"
        "- 错误：\"country\": {\"key\": \"02\", \"value\": \"美国\"}\n"
        "- 正确：\"architectureId\": 10502\n"
        "- 错误：\"architectureId\": \"作战指挥/组织机构\"\n"
        + ARCHITECTURE_CLASSIFICATION_RULES
        + SOURCE_SCORE_RULES
        + SOURCE_FIELD_RULES
        + ANALYSIS_COMPACT_OUTPUT_RULES
        + "输出 JSON 必须严格匹配以下结构：\n"
        + f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        + _format_architecture_options(ranges["architectureList"])
        + data_standard_options
        + _format_options("国家候选", ranges["country"])
        + _format_options("渠道候选", ranges["channel"])
        + _format_options("成熟度候选", ranges["maturity"])
        + _format_options("密级候选", ranges["security"])
        + _format_options("格式候选", ranges["format"])
        + "【抽取优先级】请优先抽取：密级、资料年代、关键词、摘要、文件编号、资料来源、原文链接、语种、资料格式、所属装备、所属技术、装备型号、文件概述。\n"
        + data_standard_priority
        + "【抽取字段解释】security：文件密级，只能从密级候选 value 中选取；优先依据文档开头内容判断，文档没有密级/保密说明时：若密级候选包含“公开”则输出“公开”，否则输出密级候选中的第一个 value。summary、keyword、relatedTechnology：严格按上方三个专用规则块生成；score：资料来源权威性评分；source：文档中提到的具体数据来源出处，缺少明确出处时输出“未明确数据来源”；fileNo：文件编号；dataFormat：资料格式，必须与顶层 format 完全一致，并且只能使用格式候选中的 value。\n"
        + "【输出前自检清单】\n"
        + "1. country/channel/maturity/security/format 是否都为候选 value 或空字符串；security 缺少文档开头密级说明时是否已按默认规则输出（候选包含“公开”则输出“公开”，否则输出候选第一个 value）；fileDataItem.dataFormat 是否与顶层 format 完全一致。\n"
        + "2. architectureId 是否为有文档证据支持的候选叶子 id；不得使用候选外 ID 或默认值。\n"
        + "3. score 是否为 95、85、75、65、55 之一；source 是否为具体来源出处或“未明确数据来源”；"
        + "当 source 为“未明确数据来源”时，score 是否严格等于 55。\n"
        + "4. fileDataItem.dataTime 是否为 yyyy-MM-dd 或空字符串。\n"
        + "5. 当文件内容与数据标准相关时，architectureId 【禁止】输出「数据标准」对应的ID，而是输出其下的六个子类别之一的对应ID：建模与仿真标准，军用软件标准，目标特性标准，术语与定义标准，通用要求标准，元数据标准。\n"
        + data_standard_self_check
        + f"{final_self_check_index}. 是否仅使用英文键名且 JSON 语法可解析。\n"
    )


def _normalize_confirmed_architecture_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("resolved_architecture_id 必须是正整数")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped or any(char not in "0123456789" for char in stripped):
            raise ValueError("resolved_architecture_id 必须是正整数")
        normalized = int(stripped)
    else:
        raise ValueError("resolved_architecture_id 必须是正整数")
    if normalized <= 0:
        raise ValueError("resolved_architecture_id 必须是正整数")
    return normalized


def build_file_extraction_prompt(
    request_params: dict,
    *,
    resolved_architecture_id: int,
    resolved_architecture_path_name: str = "",
    resolved_architecture_path_node_names: Sequence[str] | None = None,
    resolved_architecture_node_type: str = "",
    include_data_standard_fields: bool = False,
) -> str:
    """构造两阶段流程中不再承担分类职责的字段抽取 Prompt。

    已确认分类只作为只读上下文帮助模型理解文档，不属于输出 schema。数据标准扩展字段
    是否出现完全由调用方传入的布尔值决定，函数不会再次检查请求中的标准候选范围。
    """
    from app.services.llm_service.analysis_service import build_effective_analysis_ranges

    ranges = build_effective_analysis_ranges(request_params)
    classification_context = {
        "id": _normalize_confirmed_architecture_id(resolved_architecture_id),
        "pathName": str(resolved_architecture_path_name or ""),
        "pathNodeNames": [
            str(name).strip()
            for name in (resolved_architecture_path_node_names or ())
            if str(name).strip()
        ],
        "nodeType": str(resolved_architecture_node_type or ""),
    }
    schema = {
        "country": "",
        "channel": "",
        "maturity": "",
        "security": "",
        "format": "",
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
            "relatedTechnologyEvidence": [],
            "equipmentModel": "",
            "documentOverview": "",
            "originalText": "",
            "documentTranslationOne": "",
            "documentTranslationTwo": "",
        },
    }
    standard_contract = ""
    standard_priority = ""
    standard_self_check = ""
    if include_data_standard_fields:
        schema["fileDataItem"].update(
            {
                "militaryName": "",
                "num": "",
                "startTime": "",
                "implTime": "",
                "approvalDept": "",
            }
        )
        standard_contract = (
            "11. 调用方已确认当前分类需要数据标准扩展字段；fileDataItem 必须额外抽取 "
            "militaryName、num、startTime、implTime、approvalDept。startTime 和 implTime "
            "使用 yyyy-MM-dd；找不到则输出空字符串。\n"
        )
        standard_priority = (
            "【数据标准额外字段】militaryName=国军标名称，num=编号，startTime=发布时间，"
            "implTime=实施时间，approvalDept=批准部门。\n"
        )
        standard_self_check = (
            "5. 数据标准扩展字段是否完整，startTime/implTime 是否为 yyyy-MM-dd 或空字符串。\n"
        )

    return (
        "你是结构化字段抽取器。请仅基于文档内容抽取字段，并且只输出一个严格合法 JSON 对象。\n"
        "【请求上下文】\n"
        f"fileName: {request_params.get('fileName', '')}\n"
        f"originalFileName: {request_params.get('originalFileName', '')}\n"
        "已确认领域分类（只读，不得修改或写入输出）: "
        f"{json.dumps(classification_context, ensure_ascii=False, separators=(',', ':'))}\n"
        "【输出契约】\n"
        "1. 必须只输出 JSON，不要输出 Markdown、解释文本、候选列表或思考过程。\n"
        "2. 顶层键只能是：country, channel, maturity, security, format, fileDataItem；不得输出任何领域分类字段。\n"
        "3. 不要直接原样返回候选对象、候选数组、key/value 对象或中文键名。\n"
        "4. country/maturity/security/format 只能输出候选项中的 value 字符串或者空字符串；"
        "channel 规则见第 12 条；security 表示文件密级，必须根据文档开头、首页、页眉或标题附近的密级/保密说明判断；"
        "找不到相关说明时：若密级候选包含“公开”则输出“公开”，否则输出密级候选中的第一个 value；"
        "fileDataItem.dataFormat 必须与顶层 format 完全一致，也只能输出格式候选中的 value；不能输出 key，也不能输出对象。\n"
        "5. fileDataItem.fileName 必须与请求中的 fileName 一致。\n"
        "6. documentTranslationOne 和 documentTranslationTwo 固定输出空字符串。\n"
        "7. originalText 当前由服务端回填，输出空字符串即可，不要编造长段原文。\n"
        "8. fileDataItem 中的 summary、keyword、score、source、fileNo、dataFormat 字段不允许留空，必须根据文档内容推断；"
        "relatedTechnologyEvidence 只用于所属技术证据核验，不属于最终回调字段；"
        "source 必须是具体数据来源出处，找不到明确出处时输出“未明确数据来源”。score 必须按下方评分规则输出 95、85、75、65、55 之一。\n"
        "9. documentOverview 字段为文件概述，必须按资料原有目录、章节或标题层级进行概述，全文不超过 1000 字。"
        "优先说明全文整体结构，再按章节顺序概述主题、关键对象、重要结论或核心信息；不得机械复述目录或编造原文不存在的内容。\n"
        "10. fileDataItem.dataTime 必须输出文档中明确提到的资料年代，格式为 yyyy-MM-dd，找不到时输出空字符串。\n"
        + standard_contract
        + "12. channel 字段表示“资料来源机构”，当 channel 候选为空时输出空字符串；候选不为空时必须选择一个 value。\n"
        "13. fileDataItem.language 表示原始资料正文的主要语种，不是回答、摘要、翻译结果、文件名或提示词的语言。\n"
        + SOURCE_SCORE_RULES
        + SOURCE_FIELD_RULES
        + ANALYSIS_COMPACT_OUTPUT_RULES
        + "输出 JSON 必须严格匹配以下结构：\n"
        + f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        + _format_options("国家候选", ranges["country"])
        + _format_options("渠道候选", ranges["channel"])
        + _format_options("成熟度候选", ranges["maturity"])
        + _format_options("密级候选", ranges["security"])
        + _format_options("格式候选", ranges["format"])
        + "【抽取优先级】请优先抽取：密级、资料年代、关键词、摘要、文件编号、资料来源、原文链接、语种、资料格式、所属装备、所属技术、装备型号、文件概述。\n"
        + standard_priority
        + "【抽取字段解释】security：文件密级，只能从密级候选 value 中选取；"
        "summary、keyword、relatedTechnology：严格按上方三个专用规则块生成；score：资料来源权威性评分；"
        "source：具体数据来源出处，缺少明确出处时输出“未明确数据来源”；fileNo：文件编号；"
        "dataFormat：资料格式，必须与顶层 format 完全一致，并且只能使用格式候选中的 value。\n"
        "【输出前自检清单】\n"
        "1. 顶层是否只包含允许字段，且未输出领域分类字段。\n"
        "2. country/channel/maturity/security/format 是否为候选 value 或空字符串；fileDataItem.dataFormat 是否与顶层 format 完全一致。\n"
        "3. score 是否为 95、85、75、65、55 之一；source 是否为具体来源出处或“未明确数据来源”；"
        "当 source 为“未明确数据来源”时，score 是否严格等于 55。\n"
        "4. fileDataItem.dataTime 是否为 yyyy-MM-dd 或空字符串。\n"
        + standard_self_check
        + "6. 是否仅使用英文键名且 JSON 语法可解析。\n"
    )


def build_json_repair_prompt(raw_response: str) -> str:
    """构造一次独立、受限且可审计的 JSON 语法修复 Prompt。

    修复只允许改变序列化形式，不允许补充或改写字段语义。原始回答使用 JSON 字符串编码
    后嵌入，避免回答中的引号、花括号或伪指令破坏 Prompt 边界。
    """
    bounded_response = str(raw_response or "")[:MAX_REPAIR_CONTEXT_CHARS]
    return (
        "你是 JSON 语法修复器。请把下方原始回答修复为一个严格合法的 JSON 对象。\n"
        "只能修复引号、逗号、括号和 Markdown 包裹等序列化问题；不得新增、删除、猜测或改写字段语义。\n"
        "只输出修复后的 JSON 对象，不要输出 Markdown、解释或思考过程。\n"
        f"原始回答(JSON字符串): {json.dumps(bounded_response, ensure_ascii=False)}\n"
    )


def build_architecture_repair_prompt(
        parsed_result: dict[str, Any],
        architecture_candidates: Iterable[dict[str, Any]],
        failure_reason: str,
) -> str:
    """构造 architectureId 领域契约修复 Prompt。

    Prompt 显式携带失败原因、原始分类结果和首次分类使用的同一候选集，因而不依赖
    对话历史中的隐含上下文。候选使用与首次分类相同的最小投影。
    """
    normalized_candidates = _project_architecture_candidates(architecture_candidates)
    if not normalized_candidates:
        raise ValueError("architecture_candidates 不能为空")
    raw_result = json.dumps(
        {"architectureId": parsed_result.get("architectureId")},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )[:MAX_REPAIR_CONTEXT_CHARS]
    return (
        "你是领域分类契约修复器。请仅修复 architectureId。\n"
        f"失败原因: {str(failure_reason or '').strip()}\n"
        f"允许候选: {json.dumps(normalized_candidates, ensure_ascii=False, separators=(',', ':'))}\n"
        f"待修复原始结果: {raw_result}\n"
        "必须基于文档中已有证据选择一个允许候选的数字 id；证据不足时不要猜测，输出 null。\n"
        "只输出严格 JSON 对象 {\"architectureId\": 数字或null}，不得输出其他键、Markdown 或解释。\n"
    )


_ARCHITECTURE_RESELECT_IDENTITY_FIELDS = (
    "identifier",
    "matchedParentId",
    "matchedParentPath",
    "evidenceSources",
)
"""受限重选 Prompt 允许接收的服务端身份上下文字段。"""


def _normalize_reselect_architecture_id(value: Any, *, field_name: str) -> int:
    """将内部领域节点 ID 收敛为 JSON 数字，拒绝布尔值和非 ASCII 数字。"""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是正整数")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or not text.isdecimal():
            raise ValueError(f"{field_name} 必须是正整数")
        normalized = int(text)
    else:
        raise ValueError(f"{field_name} 必须是正整数")
    if normalized <= 0:
        raise ValueError(f"{field_name} 必须是正整数")
    return normalized


def build_architecture_reselect_prompt(
    initial_result: Mapping[str, Any],
    confirmed_identity_context: Mapping[str, Any],
    architecture_candidates: Iterable[Any],
) -> str:
    """构造双证据身份确认后的单装备分支受限重选 Prompt。

    该 Prompt 只允许一个已确认装备父节点和它的七个直接明细叶节点进入模型上下文。
    身份上下文只接收服务端生成的标识、匹配父节点和证据来源标签，不接收正文片段；因此
    重选不会把正文中的同级舰或部件再次升级为身份强证据。
    """

    if not isinstance(initial_result, Mapping):
        raise TypeError("initial_result 必须是 Mapping")
    if not isinstance(confirmed_identity_context, Mapping):
        raise TypeError("confirmed_identity_context 必须是 Mapping")

    unexpected_context_fields = set(confirmed_identity_context) - set(
        _ARCHITECTURE_RESELECT_IDENTITY_FIELDS
    )
    if unexpected_context_fields:
        raise ValueError("confirmed_identity_context 包含不允许的字段")

    identifier = str(confirmed_identity_context.get("identifier") or "").strip()
    matched_parent_path = str(
        confirmed_identity_context.get("matchedParentPath") or ""
    ).strip()
    if not identifier or "\n" in identifier or "\r" in identifier:
        raise ValueError("confirmed_identity_context.identifier 不能为空或包含换行")
    if len(identifier) > 128:
        raise ValueError("confirmed_identity_context.identifier 过长")
    if (
        not matched_parent_path
        or "\n" in matched_parent_path
        or "\r" in matched_parent_path
    ):
        raise ValueError("confirmed_identity_context.matchedParentPath 不能为空或包含换行")

    matched_parent_id = _normalize_reselect_architecture_id(
        confirmed_identity_context.get("matchedParentId"),
        field_name="confirmed_identity_context.matchedParentId",
    )
    raw_evidence_sources = confirmed_identity_context.get("evidenceSources")
    if (
        isinstance(raw_evidence_sources, (str, bytes))
        or not isinstance(raw_evidence_sources, Sequence)
    ):
        raise ValueError("confirmed_identity_context.evidenceSources 必须是序列")
    evidence_sources: list[str] = []
    for source in raw_evidence_sources:
        normalized_source = str(source or "").strip()
        if (
            not normalized_source
            or "\n" in normalized_source
            or "\r" in normalized_source
            or len(normalized_source) > 64
        ):
            raise ValueError("confirmed_identity_context.evidenceSources 包含非法来源标签")
        if normalized_source not in evidence_sources:
            evidence_sources.append(normalized_source)
    if len(evidence_sources) < 2:
        raise ValueError("受限重选至少需要两个独立身份凭据来源")

    projected_candidates = _project_architecture_candidates(architecture_candidates)
    if len(projected_candidates) != 8:
        raise ValueError("architecture_candidates 必须恰好包含 1 个 parent 和 7 个 leaf")
    parent_candidates = [
        item for item in projected_candidates if item.get("nodeType") == "parent"
    ]
    leaf_candidates = [
        item for item in projected_candidates if item.get("nodeType") == "leaf"
    ]
    if len(parent_candidates) != 1 or len(leaf_candidates) != 7:
        raise ValueError("architecture_candidates 必须恰好包含 1 个 parent 和 7 个 leaf")

    parent_candidate = parent_candidates[0]
    parent_id = _normalize_reselect_architecture_id(
        parent_candidate.get("id"),
        field_name="parent candidate id",
    )
    parent_path = str(parent_candidate.get("pathName") or "").strip().rstrip("/")
    if not parent_path:
        raise ValueError("parent candidate pathName 不能为空")
    if (
        matched_parent_id != parent_id
        or matched_parent_path.rstrip("/") != parent_path
    ):
        raise ValueError("confirmed_identity_context 与 parent candidate 不一致")

    normalized_parent = dict(parent_candidate)
    normalized_parent["id"] = parent_id
    normalized_parent["pathName"] = parent_path
    normalized_leaves: list[dict[str, Any]] = []
    candidate_ids = {parent_id}
    leaf_prefix = f"{parent_path}/"
    for leaf_candidate in leaf_candidates:
        leaf_id = _normalize_reselect_architecture_id(
            leaf_candidate.get("id"),
            field_name="leaf candidate id",
        )
        leaf_path = str(leaf_candidate.get("pathName") or "").strip().rstrip("/")
        leaf_suffix = leaf_path.removeprefix(leaf_prefix)
        if (
            not leaf_path.startswith(leaf_prefix)
            or not leaf_suffix
            or "/" in leaf_suffix
        ):
            raise ValueError("所有 leaf candidate 必须是 parent pathName 的直接子节点")
        if leaf_id in candidate_ids:
            raise ValueError("architecture_candidates id 必须唯一")
        candidate_ids.add(leaf_id)
        normalized_leaf = dict(leaf_candidate)
        normalized_leaf["id"] = leaf_id
        normalized_leaf["pathName"] = leaf_path
        normalized_leaves.append(normalized_leaf)

    raw_initial_id = initial_result.get("architectureId")
    initial_id = (
        None
        if raw_initial_id is None
        else _normalize_reselect_architecture_id(
            raw_initial_id,
            field_name="initial_result.architectureId",
        )
    )
    identity_context = {
        "identifier": identifier,
        "matchedParentId": matched_parent_id,
        "matchedParentPath": parent_path,
        "evidenceSources": evidence_sources,
    }
    normalized_candidates_by_id = {
        item["id"]: item for item in (normalized_parent, *normalized_leaves)
    }
    candidates = [
        normalized_candidates_by_id[
            _normalize_reselect_architecture_id(
                item.get("id"),
                field_name="candidate id",
            )
        ]
        for item in projected_candidates
    ]

    return (
        "你是领域分类受限重选器。服务端已用两个独立身份凭据确认文档的主要装备；"
        "请仅在该装备父节点及其七个直接明细叶节点中重新判断。\n"
        "【已确认身份上下文】\n"
        f"{json.dumps(identity_context, ensure_ascii=False, separators=(',', ':'))}\n"
        "【首次分类结果】\n"
        f"{json.dumps({'architectureId': initial_id}, ensure_ascii=False, separators=(',', ':'))}\n"
        "【重选规则】\n"
        "1. 首次分类结果只说明触发了分支冲突检查，不是重选依据；不得输出候选外 ID。\n"
        "2. 应按全文主要内容和覆盖粒度判断。只有全文主要描述某一个明细类别时，才选择对应 leaf。\n"
        "3. 已确认主要装备，但叶子证据不足、多个叶子同等成立或文档是综合资料时，必须选择 parent。\n"
        "4. 如果文档证据与已确认身份矛盾，或连 parent 都无法支持，architectureId 输出 null，不得猜测。\n"
        "5. architectureId 有值时必须是下方候选中的 JSON 数字，不能是字符串。\n"
        "6. 只输出严格 JSON 对象，唯一键为 architectureId；不要输出 Markdown、概率、解释、候选列表或思考过程。\n"
        "【输出结构】\n"
        '{"architectureId": null}\n'
        "【受限候选】\n"
        f"{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}\n"
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
