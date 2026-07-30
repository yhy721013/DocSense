"""只接受 Selected Evidence 的 INPUT/TABLE Extraction Prompt 纯规则。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from .errors import WeaponryDomainValidationError
from .models import AuxiliaryGuidance, WeaponryFieldSpecification
from .retrieval_quality import SelectedEvidence


EXTRACTION_PROMPT_VERSION = "selected-evidence-extraction-v1"


@dataclass(frozen=True)
class ExtractionPrompt:
    """仅供模型抽取使用的 Prompt，类型上不能替代 RetrievalQuery。"""

    text: str
    field_type: str
    document_key: str
    evidence_ids: tuple[str, ...]
    rows: tuple[str, ...]
    version: str = EXTRACTION_PROMPT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise WeaponryDomainValidationError("ExtractionPrompt.text 必须是非空 str")
        if self.field_type not in {"INPUT", "TABLE"}:
            raise WeaponryDomainValidationError(
                "ExtractionPrompt.field_type 只能是 INPUT 或 TABLE"
            )
        if not isinstance(self.document_key, str) or not self.document_key:
            raise WeaponryDomainValidationError("document_key 必须是非空 str")
        if not isinstance(self.evidence_ids, (tuple, list)) or not isinstance(
            self.rows,
            (tuple, list),
        ):
            raise WeaponryDomainValidationError(
                "evidence_ids 与 rows 必须是有序文本序列"
            )
        evidence_ids = tuple(self.evidence_ids)
        rows = tuple(self.rows)
        if not evidence_ids or len(evidence_ids) != len(rows):
            raise WeaponryDomainValidationError(
                "evidence_ids 与 rows 必须非空且逐项对应"
            )
        if len(set(evidence_ids)) != len(evidence_ids):
            raise WeaponryDomainValidationError("evidence_ids 不能重复")
        if any(not isinstance(item, str) or not item for item in evidence_ids):
            raise WeaponryDomainValidationError("evidence_ids 只能包含非空 str")
        if any(not isinstance(item, str) or not item for item in rows):
            raise WeaponryDomainValidationError("rows 只能包含非空 str")
        if not isinstance(self.version, str) or not self.version:
            raise WeaponryDomainValidationError("version 必须是非空 str")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "rows", rows)


def _freeze_evidence(
    evidence: Iterable[SelectedEvidence],
) -> tuple[SelectedEvidence, ...]:
    selected = tuple(evidence)
    if not selected:
        raise WeaponryDomainValidationError("Extraction Prompt 至少需要一条 SelectedEvidence")
    if any(not isinstance(item, SelectedEvidence) for item in selected):
        raise WeaponryDomainValidationError(
            "Extraction Prompt 只能接收 SelectedEvidence，不能接收 Candidate"
        )
    document_keys = {item.document_key for item in selected}
    if len(document_keys) != 1:
        raise WeaponryDomainValidationError(
            "一次 Extraction Prompt 只能包含同一 document_key 的 Evidence"
        )
    return selected


def _freeze_guidance(
    guidance: Iterable[AuxiliaryGuidance],
) -> tuple[AuxiliaryGuidance, ...]:
    frozen = tuple(guidance)
    if any(not isinstance(item, AuxiliaryGuidance) for item in frozen):
        raise WeaponryDomainValidationError(
            "guidance 只能包含 AuxiliaryGuidance"
        )
    return frozen


def _render_guidance(guidance: tuple[AuxiliaryGuidance, ...]) -> str:
    if not guidance:
        return ""
    rendered = "\n\n".join(item.text for item in guidance)
    return (
        "【辅助语境开始】\n"
        f"{rendered}\n"
        "【辅助语境结束】\n"
        "辅助语境只用于理解字段口径、别名和单位，不是目标装备事实来源；"
        "不得从辅助语境生成 analyseData 或 rows。\n\n"
    )


def _render_evidence(selected: tuple[SelectedEvidence, ...]) -> str:
    return "".join(
        f"第{index}条目标证据：\n{item.text}\n\n"
        for index, item in enumerate(selected, 1)
    )


def build_input_extraction_prompt(
    specification: WeaponryFieldSpecification,
    evidence: Iterable[SelectedEvidence],
    *,
    guidance: Iterable[AuxiliaryGuidance] = (),
) -> ExtractionPrompt:
    """为单个文件构造 INPUT 抽取 Prompt。"""

    if not isinstance(specification, WeaponryFieldSpecification):
        raise WeaponryDomainValidationError(
            "specification 必须是 WeaponryFieldSpecification"
        )
    if specification.field_type != "INPUT":
        raise WeaponryDomainValidationError("INPUT Prompt 只能使用 INPUT specification")
    selected = _freeze_evidence(evidence)
    frozen_guidance = _freeze_guidance(guidance)
    description = (
        f"字段说明：{specification.field_description}\n"
        if specification.field_description
        else ""
    )
    text = (
        f"请仅基于下方目标证据提取字段“{specification.field_name}”的信息。\n"
        f"{description}"
        f"{_render_guidance(frozen_guidance)}"
        "要求：\n"
        "1. 只能使用给出的目标证据，不得访问其他文档、对话历史、常识或推测。\n"
        '2. 如果目标证据中找不到相关信息，请只回答“未找到”。\n'
        "3. 只回答字段具体值，不添加解释。\n"
        "4. 多个并列值使用英文逗号加空格分隔，不使用换行或项目符号。\n\n"
        "【目标证据开始】\n"
        f"{_render_evidence(selected)}"
        "【目标证据结束】"
    )
    return ExtractionPrompt(
        text=text,
        field_type="INPUT",
        document_key=selected[0].document_key,
        evidence_ids=tuple(item.candidate_id for item in selected),
        rows=tuple(item.text for item in selected),
    )


def build_table_extraction_prompt(
    specification: WeaponryFieldSpecification,
    evidence: Iterable[SelectedEvidence],
    *,
    guidance: Iterable[AuxiliaryGuidance] = (),
) -> ExtractionPrompt:
    """为单个文件构造 TABLE 多行 JSON 抽取 Prompt。"""

    if not isinstance(specification, WeaponryFieldSpecification):
        raise WeaponryDomainValidationError(
            "specification 必须是 WeaponryFieldSpecification"
        )
    if specification.field_type != "TABLE":
        raise WeaponryDomainValidationError("TABLE Prompt 只能使用 TABLE specification")
    selected = _freeze_evidence(evidence)
    frozen_guidance = _freeze_guidance(guidance)
    description = (
        f"表格说明：{specification.field_description}\n"
        if specification.field_description
        else ""
    )
    column_lines: list[str] = []
    example_row = {
        "__rowKey": "用于合并同一行的名称、型号或唯一标识",
    }
    for index, column in enumerate(specification.columns, 1):
        if column.field_description:
            column_lines.append(
                f"{index}. {column.field_name}: {column.field_description}"
            )
        else:
            column_lines.append(f"{index}. {column.field_name}")
        # 使用标准 JSON 编码任意字段名，避免引号、反斜杠或换行破坏示例边界。
        example_row[column.field_name] = ""
    example = json.dumps(
        [example_row],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    text = (
        f"请仅基于下方目标证据，抽取表格“{specification.field_name}”的多行结构化数据。\n"
        f"{description}"
        f"{_render_guidance(frozen_guidance)}"
        "列定义：\n"
        f"{chr(10).join(column_lines)}\n\n"
        "要求：\n"
        "1. 只能使用给出的目标证据，不得访问其他文档、对话历史、常识或推测。\n"
        "2. 每行表示一个独立对象、部件、型号或记录，不得合并不同实体。\n"
        "3. 没有明确依据的单元格填写空字符串；没有任何可抽取行时输出 []。\n"
        "4. __rowKey 填写最稳定的名称、型号或唯一标识。\n"
        "5. 只输出严格 JSON 数组，不输出 Markdown 或解释。\n"
        f"JSON 示例：{example}\n\n"
        "【目标证据开始】\n"
        f"{_render_evidence(selected)}"
        "【目标证据结束】"
    )
    return ExtractionPrompt(
        text=text,
        field_type="TABLE",
        document_key=selected[0].document_key,
        evidence_ids=tuple(item.candidate_id for item in selected),
        rows=tuple(item.text for item in selected),
    )


__all__ = [
    "EXTRACTION_PROMPT_VERSION",
    "ExtractionPrompt",
    "build_input_extraction_prompt",
    "build_table_extraction_prompt",
]
