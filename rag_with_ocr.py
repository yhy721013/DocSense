#!/usr/bin/env python3
"""
Pipeline entrypoint: OCR (if needed) + AnythingLLM RAG call.
"""

from __future__ import annotations

import argparse
from typing import Optional

from anythingllm_client import AnythingLLMClient
from config import load_anythingllm_config
from pipeline import process_file_with_rag as pipeline_process_file_with_rag


PROMPT = (
    "【重要：语言要求】\n"
    "1) 请先判断文档主要语言。\n"
    "2) 若文档包含中文内容：所有输出必须使用中文。\n"
    "3) 若文档为英文：outline 保持英文原文，其他字段除专业名词、人名、型号等专有名词外必须使用中文。\n\n"

    "【任务】请仔细阅读上传文档，仅基于文档内容生成一个“严格有效 JSON”的结果（不要使用 markdown 代码块，不要输出多余解释）。\n\n"

    "【输出 JSON 结构】\n"
    "{\n"
    '  "outline": ["..."],\n'
    '  "category_confidence": 0.0,\n'
    '  "category": null,\n'
    '  "sub_category": null,\n'
    '  "category_candidates": [\n'
    '    {"category": "...", "sub_category": null, "confidence": 0.0}\n'
    "  ],\n"
    '  "extract": { ... }\n'
    "}\n\n"

    "=== 分类体系说明 ===\n\n"

    "1. 军事基地：军事设施、基地建设、基地布局、军事要塞、港口码头、机场跑道、后勤保障设施、营房工程、防御工事\n"
    "   extract字段：{\"base_name\":null, \"location\":null, \"country\":null, \"military_branch\":null, \"facility_type\":null, \"function\":null, \"capacity\":null, \"status\":null}\n\n"

    "2. 体系运用：作战体系、系统集成、联合作战、协同配合、多域作战、体系对抗\n"
    "   仅示例：全域作战体系、航母编队体系、水面编队体系、两栖编队体系、水下作战体系（注意：这些不是二级分类）\n"
    "   extract字段：{\"system_type\":null, \"components\":null, \"capabilities\":null, \"coordination_mode\":null, \"application_scenario\":null}\n\n"

    "3. 装备型号：武器装备、装备参数、技术指标、装备性能（【必须选择子分类】）\n"
    "   子分类：空中装备 / 水面装备 / 水下装备\n"
    "   extract字段：{\n"
    "     \"basic_info\": {\"model\":null, \"model_en\":null, \"country\":null, \"military_branch\":null, \"manufacturer\":null, \"status\":null, \"service_date\":null, \"quantity\":null, \"cost\":null, \"features\":null},\n"
    "     \"specifications\": {\"dimensions\":null, \"weight\":null, \"performance\":null, \"range\":null, \"speed\":null, \"ceiling\":null, \"payload\":null},\n"
    "     \"operational_data\": {\"deployment\":null, \"exercises\":null, \"maintenance\":null, \"incidents\":null},\n"
    "     \"effectiveness\": {\"damage_capability\":null, \"vulnerability\":null},\n"
    "     \"signatures\": {\"rcs\":null, \"optical\":null, \"infrared\":null}\n"
    "   }\n\n"

    "4. 作战环境：战场环境、地理条件、气象水文、电磁环境、海洋环境\n"
    "   extract字段：{\"region\":null, \"terrain\":null, \"climate\":null, \"ocean_data\":{\"current\":null, \"wave\":null, \"tide\":null, \"temperature\":null, \"salinity\":null}, \"electromagnetic\":null}\n\n"

    "5. 作战指挥：指挥控制、决策流程、作战计划、战术战法（【必须选择子分类】）\n"
    "   子分类：条令条例 / 组织机构\n"
    "   extract字段（条令条例）：{\"doc_name\":null, \"doc_number\":null, \"issuing_authority\":null, \"version\":null, \"issue_date\":null, \"scope\":null, \"key_content\":null}\n"
    "   extract字段（组织机构）：{\"org_name\":null, \"country\":null, \"military_branch\":null, \"commander\":null, \"function\":null, \"subordinate_units\":null, \"mechanism\":null}\n\n"

    "=== 输出要求（必须严格遵守）===\n\n"

    "A. outline\n"
    "1) 必须是数组；按文档顺序列出章节标题，保留原有序号/编号。\n"
    "2) 若文档没有明确章节标题，返回空数组 []。\n\n"

    "B. 分类输出采用“严格二选一模式”（互斥，不得混用）\n"
    "【总原则：默认不确信】\n"
    "除非满足下面“确信模式的硬性门槛”，否则一律输出“不确信模式”（即输出候选分类）。\n"
    "宁可输出候选分类，也不要输出确信分类。\n\n"

    "【模式1：确信分类】\n"
    "只有当满足以下全部条件时，才允许进入确信模式：\n"
    "1) 文档中出现明确的“硬证据”能够唯一锁定某一分类，且不存在与其他分类同等强度的证据。\n"
    "2) 该分类的关键信息在文档中有多处直接呈现（至少2处），例如：标题/章节名/表格字段/规范条目/参数条款等。\n"
    "3) 若 category 为“装备型号”，必须在文档中直接出现装备型号/平台名称，并伴随参数/指标/性能/配置等明确技术信息；同时还能唯一确定子分类（空中/水面/水下）。\n"
    "4) 若 category 为“作战指挥”，必须在文档中直接体现条令/出版物编号/发布机构/版本等（条令条例）或机构编制/隶属/职能等（组织机构），且能唯一确定子分类。\n"
    "5) 若文档内容跨多个主题、信息稀少、或存在明显混合（例如同时大量出现基地建设与装备参数），则禁止进入确信模式，必须输出候选模式。\n\n"
    "当且仅当满足上述全部条件：\n"
    "- category_confidence 必须为 1.0（number）。\n"
    "- 必须输出 category。\n"
    "- 若 category 为“装备型号”或“作战指挥”，必须输出 sub_category；否则 sub_category 必须为 null。\n"
    "- category_candidates 必须是空数组 []。\n\n"

    "【模式2：不确信分类（默认模式）】\n"
    "在下列任一情况出现时，必须使用不确信模式：\n"
    "1) 文档未提供足以唯一锁定某一分类的硬证据；\n"
    "2) 多个分类都有合理证据；\n"
    "3) 文档主题混合或信息不完整；\n"
    "4) 你需要依赖常识/猜测才能做唯一判断（此时必须不确信）。\n\n"
    "不确信模式输出规则：\n"
    "- category_confidence 必须为 0.10~0.90 之间的小数（number），禁止输出 1.0。\n"
    "- category 必须为 null；sub_category 必须为 null。\n"
    "- 必须输出 category_candidates，列表长度 >= 3（除非确实只有2类有任何证据）。\n"
    "- 每个候选包含：category（一级分类）、sub_category（需要二级分类则填写，否则为 null）、confidence（0~1 number）。\n"
    "- 所有候选 confidence 之和必须等于 1.0（允许 1e-6 的浮点误差）。\n"
    "- 概率分配要“保守”：当证据不足时，最高候选的 confidence 不应超过 0.60；当有较强倾向但仍非唯一时，最高候选不应超过 0.80。\n"
    "- 若候选的 category 为“装备型号”或“作战指挥”，该候选的 sub_category 不得为 null；其他三类候选 sub_category 必须为 null。\n\n"

    "C. category / sub_category 取值约束（严格枚举）\n"
    "1) category 只能是：军事基地、体系运用、装备型号、作战环境、作战指挥 或 null。\n"
    "2) sub_category 只能是：\n"
    "   - 当 category=装备型号：空中装备、水面装备、水下装备\n"
    "   - 当 category=作战指挥：条令条例、组织机构\n"
    "   - 其他情况必须为 null\n"
    "3) category_candidates 中的 category/sub_category 同样必须严格遵守以上枚举。\n\n"

    "D. extract\n"
    "1) extract 必须是对象。\n"
    "2) 确信模式：extract 必须使用该分类对应的结构。\n"
    "3) 不确信模式：extract 仍按“最可能”的候选分类（confidence 最大者）选择结构。\n"
    "4) 对于文档中不存在的信息：必须返回 null（不要用空字符串，不要臆测）。\n\n"

    "【关键约束】\n"
    "- 你只能基于文档原文内容回答，不得使用常识推断或猜测。\n"
    "- 不允许补充文档未明确出现的信息。\n"
    "- 必须输出严格有效 JSON；不要输出额外文本、解释、Markdown、代码块。\n"
)


def process_file_with_rag(
    file_path: str,
    workspace_name: str = "1928",
    thread_name: str = "文档分析",
    user_id: int = 1,
) -> Optional[str]:
    """
    CLI/脚本入口：加载配置并执行统一流水线。
    所有文件直接上传到 AnythingLLM 进行解析存储。
    """
    client = AnythingLLMClient(load_anythingllm_config())
    return pipeline_process_file_with_rag(
        client=client,
        file_path=file_path,
        prompt=PROMPT,
        workspace_name=workspace_name,
        thread_name=thread_name,
        user_id=user_id,
    )


def main() -> Optional[str]:
    parser = argparse.ArgumentParser(
        description="AnythingLLM RAG pipeline - 文件直接上传到 AnythingLLM 进行解析",
    )
    parser.add_argument("file_path", type=str, help="File path to process")
    parser.add_argument("--workspace", type=str, default="1928", help="Workspace name")
    parser.add_argument("--thread", type=str, default="文档分析", help="Thread name")
    parser.add_argument("--user-id", type=int, default=1, help="User id")

    args = parser.parse_args()

    return process_file_with_rag(
        file_path=args.file_path,
        workspace_name=args.workspace,
        thread_name=args.thread,
        user_id=args.user_id,
    )


if __name__ == "__main__":
    main()
