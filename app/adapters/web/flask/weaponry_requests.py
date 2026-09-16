"""``POST /llm/weaponry`` 的框架无关严格请求解析器。

文件位于 Flask Adapter 包，是因为当前公开入口仍使用 Flask；本模块本身不读取
``flask.request``，也不访问数据库、文件、AnythingLLM 或任务表。1D-6 切路由时只需把
``request.get_json`` 的返回值传入本解析器。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from urllib.parse import unquote_to_bytes, urlparse

from app.adapters.web.weaponry_ids import (
    ArchitectureIdValidationError,
    normalize_architecture_id,
)
from app.modules.weaponry.domain import (
    AuxiliaryGuidancePolicySnapshot,
    EvidenceSelectionPolicy,
    FrozenJsonObject,
    WeaponryDocumentScope,
    WeaponryDomainValidationError,
    WeaponryExecutionPolicySnapshot,
    WeaponryFieldSpecification,
    WeaponrySubmission,
)


_INVALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")


class WeaponryRequestValidationError(ValueError):
    """武器谱 HTTP 请求违反已批准 D01～D03 参数契约。"""


@dataclass(frozen=True)
class ParsedWeaponryRequest:
    """公开形状校验后的不可变请求及内部规范化结果。"""

    request_payload: FrozenJsonObject
    params: FrozenJsonObject
    architecture_id: int
    selected_file_names: tuple[str, ...]
    fields: tuple[WeaponryFieldSpecification, ...]

    @property
    def business_key(self) -> str:
        return str(self.architecture_id)

    def to_submission(
        self,
        *,
        document_scope: WeaponryDocumentScope,
        evidence_selection_policy: EvidenceSelectionPolicy,
        execution_policy: WeaponryExecutionPolicySnapshot,
        auxiliary_guidance_policy: AuxiliaryGuidancePolicySnapshot,
        trace_id: str,
    ) -> WeaponrySubmission:
        """组合已冻结文档与内部策略，形成可交给通用 Task Adapter 的命令。"""

        if not isinstance(document_scope, WeaponryDocumentScope):
            raise TypeError("document_scope 必须是 WeaponryDocumentScope")
        if document_scope.requested_file_names != self.selected_file_names:
            raise ValueError("document_scope 与解析后的 filePathList 不一致")
        return WeaponrySubmission(
            architecture_id=self.architecture_id,
            request_projection=self.request_payload,
            fields=self.fields,
            document_scope=document_scope,
            evidence_selection_policy=evidence_selection_policy,
            execution_policy=execution_policy,
            auxiliary_guidance_policy=auxiliary_guidance_policy,
            trace_id=trace_id,
        )


def _normalize_file_path_list(value: object) -> tuple[str, ...]:
    """提取 URL/裸路径末尾文件名，并按大小写不敏感首次出现顺序去重。"""

    # 旧入口把缺省和显式 null 都视为“当前类别全部文档”；D01 进一步要求在受理时冻结。
    if value is None:
        return ()
    if not isinstance(value, list):
        raise WeaponryRequestValidationError("filePathList必须为数组")

    normalized: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item.strip():
            raise WeaponryRequestValidationError(
                f"filePathList中第{index}项不是有效字符串"
            )
        try:
            parsed = urlparse(item.strip())
        except ValueError as exc:
            raise WeaponryRequestValidationError(
                f"filePathList中第{index}项无法提取文件名"
            ) from exc
        try:
            # ``urllib.parse.unquote`` 会用 U+FFFD 静默替换非法 UTF-8，且会原样保留
            # ``%ZZ`` 这类非法转义。文件名是后续文档身份的一部分，不能让两个不同的坏 URL
            # 被宽松转换为同一快照，因此在进入 Document Scope 前严格拒绝两类输入。
            if _INVALID_PERCENT_ESCAPE_PATTERN.search(parsed.path):
                raise ValueError("URL path 包含非法 percent escape")
            decoded_path = (
                unquote_to_bytes(parsed.path)
                .decode("utf-8", errors="strict")
                .replace("\\", "/")
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise WeaponryRequestValidationError(
                f"filePathList中第{index}项无法提取文件名"
            ) from exc
        file_name = PurePosixPath(decoded_path).name.strip()
        # ``PurePosixPath('/path/')`` 会把 ``path`` 当作 name；HTTP 语义中尾部斜杠
        # 实际表示目录，不能被误当成可解析文件名。
        if decoded_path.endswith("/") or not file_name or file_name in {".", ".."}:
            raise WeaponryRequestValidationError(
                f"filePathList中第{index}项无法提取文件名"
            )
        dedup_key = file_name.casefold()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        normalized.append(file_name)
    return tuple(normalized)


def _analysis_fields_are_empty(value: Mapping[str, object]) -> bool:
    """严格接受文档冻结资产中列明的空值，不借助 Python truthiness 放宽类型。"""

    if "analyseData" in value and value.get("analyseData") not in (None, ""):
        return False
    if "analyseDataSource" in value:
        sources = value.get("analyseDataSource")
        if sources is not None and not (
            isinstance(sources, list) and not sources
        ):
            return False
    return True


def _validate_table(
    field: Mapping[str, object],
    *,
    field_index: int,
) -> None:
    raw_rows = field.get("tableFieldList")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise WeaponryRequestValidationError(
            f"weaponryTemplateFieldList中第{field_index}项tableFieldList必须为非空数组"
        )
    for row_index, raw_row in enumerate(raw_rows, start=1):
        if not isinstance(raw_row, list) or not raw_row:
            raise WeaponryRequestValidationError(
                f"tableFieldList中第{row_index}行必须为非空数组"
            )
        for column_index, raw_cell in enumerate(raw_row, start=1):
            if not isinstance(raw_cell, Mapping):
                raise WeaponryRequestValidationError(
                    f"tableFieldList中第{row_index}行第{column_index}项必须为对象"
                )
            field_name = raw_cell.get("fieldName")
            if not isinstance(field_name, str) or not field_name.strip():
                raise WeaponryRequestValidationError(
                    f"tableFieldList中第{row_index}行第{column_index}项fieldName不能为空"
                )
            if raw_cell.get("fieldType") != "INPUT":
                raise WeaponryRequestValidationError(
                    f"tableFieldList中第{row_index}行第{column_index}项fieldType必须为INPUT"
                )
            field_description = raw_cell.get("fieldDescription")
            if field_description is not None and not isinstance(
                field_description,
                str,
            ):
                raise WeaponryRequestValidationError(
                    f"tableFieldList中第{row_index}行第{column_index}项fieldDescription必须为字符串"
                )
            if not _analysis_fields_are_empty(raw_cell):
                raise WeaponryRequestValidationError(
                    "analyseData和analyseDataSource必须清空"
                )


def parse_weaponry_request(payload: object) -> ParsedWeaponryRequest:
    """按冻结顺序完成武器谱请求校验，并返回无共享可变引用的结果。"""

    if not isinstance(payload, Mapping):
        raise WeaponryRequestValidationError("请求体必须是JSON对象")
    if payload.get("businessType") != "weaponry":
        raise WeaponryRequestValidationError("businessType必须为weaponry")
    raw_params = payload.get("params")
    if not isinstance(raw_params, Mapping):
        raise WeaponryRequestValidationError("params不能为空")

    if raw_params.get("architectureId") is None:
        raise WeaponryRequestValidationError("architectureId不能为空")
    try:
        architecture_id = normalize_architecture_id(
            raw_params.get("architectureId")
        ).value
    except ArchitectureIdValidationError as exc:
        raise WeaponryRequestValidationError(str(exc)) from exc
    selected_file_names = _normalize_file_path_list(
        raw_params.get("filePathList")
    )

    raw_fields = raw_params.get("weaponryTemplateFieldList")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise WeaponryRequestValidationError(
            "weaponryTemplateFieldList不能为空"
        )
    for field_index, raw_field in enumerate(raw_fields, start=1):
        if not isinstance(raw_field, Mapping):
            raise WeaponryRequestValidationError(
                f"weaponryTemplateFieldList中第{field_index}项必须为对象"
            )
        template_classify_id = raw_field.get("templateClassifyId")
        if isinstance(template_classify_id, bool) or not isinstance(
            template_classify_id,
            int,
        ):
            raise WeaponryRequestValidationError(
                f"weaponryTemplateFieldList中第{field_index}项templateClassifyId必须为整数"
            )
        field_name = raw_field.get("fieldName")
        if not isinstance(field_name, str) or not field_name.strip():
            raise WeaponryRequestValidationError(
                f"weaponryTemplateFieldList中第{field_index}项fieldName不能为空"
            )
        field_description = raw_field.get("fieldDescription")
        if field_description is not None and not isinstance(
            field_description,
            str,
        ):
            raise WeaponryRequestValidationError(
                f"weaponryTemplateFieldList中第{field_index}项fieldDescription必须为字符串"
            )
        field_type = raw_field.get("fieldType")
        if not isinstance(field_type, str) or field_type not in {"INPUT", "TABLE"}:
            raise WeaponryRequestValidationError(
                f"weaponryTemplateFieldList中第{field_index}项fieldType必须为INPUT或TABLE"
            )
        if not _analysis_fields_are_empty(raw_field):
            raise WeaponryRequestValidationError(
                "analyseData和analyseDataSource必须清空"
            )
        if field_type == "TABLE":
            _validate_table(raw_field, field_index=field_index)

    # 完成公开错误映射后再深冻结。NaN/Infinity、非字符串对象键或 Python 专有值不是严格
    # JSON；当前目标契约将它们收敛到既有“请求体必须是 JSON 对象”，不新增接口错误文本。
    try:
        request_snapshot = FrozenJsonObject.from_mapping(
            payload,
            name="weaponry_request",
        )
        params_snapshot = FrozenJsonObject.from_mapping(
            raw_params,
            name="weaponry_params",
        )
        fields = tuple(
            WeaponryFieldSpecification.from_mapping(raw_field)
            for raw_field in raw_fields
        )
    except WeaponryDomainValidationError as exc:
        raise WeaponryRequestValidationError(
            "请求体必须是JSON对象"
        ) from exc

    return ParsedWeaponryRequest(
        request_payload=request_snapshot,
        params=params_snapshot,
        architecture_id=architecture_id,
        selected_file_names=selected_file_names,
        fields=fields,
    )


__all__ = [
    "ParsedWeaponryRequest",
    "WeaponryRequestValidationError",
    "parse_weaponry_request",
]
