"""Weaponry DTO 与通用 TaskCommand SQLite JSON 之间的唯一 Schema v2 Codec。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.modules.tasks.adapters import EncodedTaskResult, EncodedTaskSubmission
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import ExpectedTaskCompletion, TaskSubmissionCommand
from app.modules.weaponry.domain import (
    EVIDENCE_DEDUP_STRATEGY,
    EVIDENCE_RANKING_STRATEGY,
    EVIDENCE_SCORE_PROTOCOL,
    EVIDENCE_SCORE_SEMANTICS,
    WEAPONRY_BUSINESS_TYPE,
    WEAPONRY_INPUT_SCHEMA_VERSION,
    WEAPONRY_STATUS_SUCCEEDED,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceSelectionPolicy,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryExecutionPolicySnapshot,
    WeaponryFieldSpecification,
    WeaponryInputSnapshot,
    WeaponryResult,
    WeaponrySubmission,
    validate_weaponry_result_completeness,
)


_INPUT_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "architecture_id",
        "business_key",
        "field_templates",
        "document_scope",
        "evidence_selection_policy",
        "execution_policy",
        "auxiliary_guidance_policy",
        "accepted_at",
        "trace_id",
    }
)
_DOCUMENT_SCOPE_KEYS = frozenset(
    {"mode", "requested_file_names", "documents"}
)
_DOCUMENT_KEYS = frozenset(
    {
        "sequence_no",
        "document_key",
        "file_name",
        "original_name",
        "ingested_file_name",
        "source_architecture_id",
        "external_document_ref",
        "anything_document_id",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "profile_id",
        "provider_fingerprint",
        "embedding_fingerprint",
        "document_processing_fingerprint",
        "query_version",
        "score_semantics",
        "score_protocol",
        "ranking_strategy",
        "input_candidate_top_n",
        "table_candidate_top_n",
        "dedup_strategy",
        "reject_reference_like",
    }
)
_EXECUTION_POLICY_KEYS = frozenset(
    {
        "extraction_strategy",
        "extraction_prompt_version",
        "extraction_context_strategy",
        "extraction_model_fingerprint",
        "table_merge_policy_version",
        "max_table_rows",
    }
)
_AUXILIARY_POLICY_KEYS = frozenset(
    {"policy_id", "catalog_fingerprint", "top_n", "max_context_chars"}
)


def _exact_mapping(
    value: object,
    *,
    expected_keys: frozenset[str],
    name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} 必须是 Mapping")
    if frozenset(value.keys()) != expected_keys:
        raise ValueError(f"{name} 字段集合不完整或包含未知字段")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{name} 对象键必须是 str")
    return value


def _json_list(value: object, *, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{name} 必须是 JSON 数组")
    return value


def _analysis_fields_are_empty(value: Mapping[str, Any]) -> bool:
    if "analyseData" in value and value.get("analyseData") not in (None, ""):
        return False
    if "analyseDataSource" in value:
        sources = value.get("analyseDataSource")
        if sources is not None and not (
            isinstance(sources, list) and not sources
        ):
            return False
    return True


def _decode_field_template(value: object, *, index: int) -> WeaponryFieldSpecification:
    """在 execution 解码时重做公开字段形状校验，拒绝被手工篡改的 JSON。"""

    if not isinstance(value, Mapping):
        raise TypeError(f"field_templates[{index}] 必须是 Mapping")
    template_classify_id = value.get("templateClassifyId")
    if isinstance(template_classify_id, bool) or not isinstance(
        template_classify_id,
        int,
    ):
        raise ValueError(f"field_templates[{index}].templateClassifyId 必须是整数")
    field_name = value.get("fieldName")
    if not isinstance(field_name, str) or not field_name.strip():
        raise ValueError(f"field_templates[{index}].fieldName 不能为空")
    field_description = value.get("fieldDescription")
    if field_description is not None and not isinstance(field_description, str):
        raise TypeError(f"field_templates[{index}].fieldDescription 必须是字符串")
    field_type = value.get("fieldType")
    if not isinstance(field_type, str) or field_type not in {"INPUT", "TABLE"}:
        raise ValueError(f"field_templates[{index}].fieldType 非法")
    if not _analysis_fields_are_empty(value):
        raise ValueError(f"field_templates[{index}] 解析结果必须为空")
    if field_type == "TABLE":
        rows = _json_list(
            value.get("tableFieldList"),
            name=f"field_templates[{index}].tableFieldList",
        )
        if not rows:
            raise ValueError(f"field_templates[{index}].tableFieldList 不能为空")
        for row_index, row in enumerate(rows):
            cells = _json_list(
                row,
                name=f"field_templates[{index}].tableFieldList[{row_index}]",
            )
            if not cells:
                raise ValueError("TABLE 行不能为空")
            for column_index, cell in enumerate(cells):
                if not isinstance(cell, Mapping):
                    raise TypeError("TABLE 单元格必须是 Mapping")
                cell_name = cell.get("fieldName")
                if not isinstance(cell_name, str) or not cell_name.strip():
                    raise ValueError("TABLE 单元格 fieldName 不能为空")
                if cell.get("fieldType") != "INPUT":
                    raise ValueError("TABLE 单元格 fieldType 必须是 INPUT")
                cell_description = cell.get("fieldDescription")
                if cell_description is not None and not isinstance(
                    cell_description,
                    str,
                ):
                    raise TypeError("TABLE 单元格 fieldDescription 必须是字符串")
                if not _analysis_fields_are_empty(cell):
                    raise ValueError(
                        "TABLE 单元格 analyseData/analyseDataSource 必须为空"
                    )
    return WeaponryFieldSpecification.from_mapping(value)


def _profile_payload(profile: EvidenceSelectionPolicy) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "provider_fingerprint": profile.provider_fingerprint,
        "embedding_fingerprint": profile.embedding_fingerprint,
        "document_processing_fingerprint": profile.document_processing_fingerprint,
        "query_version": profile.query_version,
        "score_semantics": profile.score_semantics,
        "score_protocol": profile.score_protocol,
        "ranking_strategy": profile.ranking_strategy,
        "input_candidate_top_n": profile.input_candidate_top_n,
        "table_candidate_top_n": profile.table_candidate_top_n,
        "dedup_strategy": profile.dedup_strategy,
        "reject_reference_like": profile.reject_reference_like,
    }


def _decode_profile(value: object) -> EvidenceSelectionPolicy:
    payload = _exact_mapping(
        value,
        expected_keys=_PROFILE_KEYS,
        name="evidence_selection_policy",
    )
    # ``score_semantics`` 必须显式存在且精确匹配；不能由新进程默认补值，否则历史任务会
    # 在配置或供应商升级后悄悄改变分数方向。
    if payload.get("score_semantics") != EVIDENCE_SCORE_SEMANTICS:
        raise ValueError("evidence_selection_policy.score_semantics 不匹配")
    if payload.get("score_protocol") != EVIDENCE_SCORE_PROTOCOL:
        raise ValueError("evidence_selection_policy.score_protocol 不匹配")
    if payload.get("ranking_strategy") != EVIDENCE_RANKING_STRATEGY:
        raise ValueError("evidence_selection_policy.ranking_strategy 不匹配")
    if payload.get("dedup_strategy") != EVIDENCE_DEDUP_STRATEGY:
        raise ValueError("evidence_selection_policy.dedup_strategy 不匹配")
    return EvidenceSelectionPolicy(
        profile_id=payload.get("profile_id"),  # type: ignore[arg-type]
        provider_fingerprint=payload.get("provider_fingerprint"),  # type: ignore[arg-type]
        embedding_fingerprint=payload.get("embedding_fingerprint"),  # type: ignore[arg-type]
        document_processing_fingerprint=payload.get("document_processing_fingerprint"),  # type: ignore[arg-type]
        query_version=payload.get("query_version"),  # type: ignore[arg-type]
        score_semantics=payload.get("score_semantics"),  # type: ignore[arg-type]
        score_protocol=payload.get("score_protocol"),  # type: ignore[arg-type]
        ranking_strategy=payload.get("ranking_strategy"),  # type: ignore[arg-type]
        input_candidate_top_n=payload.get("input_candidate_top_n"),  # type: ignore[arg-type]
        table_candidate_top_n=payload.get("table_candidate_top_n"),  # type: ignore[arg-type]
        dedup_strategy=payload.get("dedup_strategy"),  # type: ignore[arg-type]
        reject_reference_like=payload.get("reject_reference_like"),  # type: ignore[arg-type]
    )


def _execution_policy_payload(
    policy: WeaponryExecutionPolicySnapshot,
) -> dict[str, Any]:
    return {
        "extraction_strategy": policy.extraction_strategy,
        "extraction_prompt_version": policy.extraction_prompt_version,
        "extraction_context_strategy": policy.extraction_context_strategy,
        "extraction_model_fingerprint": policy.extraction_model_fingerprint,
        "table_merge_policy_version": policy.table_merge_policy_version,
        "max_table_rows": policy.max_table_rows,
    }


def _decode_execution_policy(value: object) -> WeaponryExecutionPolicySnapshot:
    payload = _exact_mapping(
        value,
        expected_keys=_EXECUTION_POLICY_KEYS,
        name="execution_policy",
    )
    return WeaponryExecutionPolicySnapshot(
        extraction_strategy=payload.get("extraction_strategy"),  # type: ignore[arg-type]
        extraction_prompt_version=payload.get("extraction_prompt_version"),  # type: ignore[arg-type]
        extraction_context_strategy=payload.get("extraction_context_strategy"),  # type: ignore[arg-type]
        extraction_model_fingerprint=payload.get("extraction_model_fingerprint"),  # type: ignore[arg-type]
        table_merge_policy_version=payload.get("table_merge_policy_version"),  # type: ignore[arg-type]
        max_table_rows=payload.get("max_table_rows"),  # type: ignore[arg-type]
    )


def _auxiliary_policy_payload(
    policy: AuxiliaryGuidancePolicySnapshot,
) -> dict[str, Any]:
    return {
        "policy_id": policy.policy_id,
        "catalog_fingerprint": policy.catalog_fingerprint,
        "top_n": policy.top_n,
        "max_context_chars": policy.max_context_chars,
    }


def _decode_auxiliary_policy(value: object) -> AuxiliaryGuidancePolicySnapshot:
    payload = _exact_mapping(
        value,
        expected_keys=_AUXILIARY_POLICY_KEYS,
        name="auxiliary_guidance_policy",
    )
    return AuxiliaryGuidancePolicySnapshot(
        policy_id=payload.get("policy_id"),  # type: ignore[arg-type]
        catalog_fingerprint=payload.get("catalog_fingerprint"),  # type: ignore[arg-type]
        top_n=payload.get("top_n"),  # type: ignore[arg-type]
        max_context_chars=payload.get("max_context_chars"),  # type: ignore[arg-type]
    )


def _document_payload(document: WeaponryDocumentSnapshot) -> dict[str, Any]:
    return {
        "sequence_no": document.sequence_no,
        "document_key": document.document_key,
        "file_name": document.file_name,
        "original_name": document.original_name,
        "ingested_file_name": document.ingested_file_name,
        "source_architecture_id": document.source_architecture_id,
        "external_document_ref": document.external_document_ref,
        "anything_document_id": document.anything_document_id,
    }


def _decode_document(value: object) -> WeaponryDocumentSnapshot:
    payload = _exact_mapping(
        value,
        expected_keys=_DOCUMENT_KEYS,
        name="document",
    )
    return WeaponryDocumentSnapshot(
        sequence_no=payload.get("sequence_no"),  # type: ignore[arg-type]
        document_key=payload.get("document_key"),  # type: ignore[arg-type]
        file_name=payload.get("file_name"),  # type: ignore[arg-type]
        original_name=payload.get("original_name"),  # type: ignore[arg-type]
        ingested_file_name=payload.get("ingested_file_name"),  # type: ignore[arg-type]
        source_architecture_id=payload.get("source_architecture_id"),  # type: ignore[arg-type]
        external_document_ref=payload.get("external_document_ref"),  # type: ignore[arg-type]
        anything_document_id=payload.get("anything_document_id"),  # type: ignore[arg-type]
    )


def _scope_payload(scope: WeaponryDocumentScope) -> dict[str, Any]:
    return {
        "mode": scope.mode,
        "requested_file_names": list(scope.requested_file_names),
        "documents": [_document_payload(item) for item in scope.documents],
    }


def _decode_scope(value: object) -> WeaponryDocumentScope:
    payload = _exact_mapping(
        value,
        expected_keys=_DOCUMENT_SCOPE_KEYS,
        name="document_scope",
    )
    requested_file_names = _json_list(
        payload.get("requested_file_names"),
        name="document_scope.requested_file_names",
    )
    documents = _json_list(
        payload.get("documents"),
        name="document_scope.documents",
    )
    return WeaponryDocumentScope(
        mode=payload.get("mode"),  # type: ignore[arg-type]
        requested_file_names=tuple(requested_file_names),  # type: ignore[arg-type]
        documents=tuple(_decode_document(item) for item in documents),
    )


class WeaponryTaskCommandCodec:
    """保存完整武器谱 execution 输入，并隔离旧 ``llm_tasks`` 请求投影。"""

    task_type = WEAPONRY_BUSINESS_TYPE

    def encode_submission(
        self,
        command: TaskSubmissionCommand[WeaponrySubmission],
        *,
        task_id: TaskId,
        accepted_at: str,
    ) -> EncodedTaskSubmission[WeaponryInputSnapshot]:
        if not isinstance(command, TaskSubmissionCommand):
            raise TypeError("command 必须是 TaskSubmissionCommand")
        if not isinstance(command.submission, WeaponrySubmission):
            raise TypeError("Weaponry Codec 只接受 WeaponrySubmission")
        submission = command.submission
        if command.task_type != self.task_type:
            raise ValueError("Weaponry 命令 task_type 必须是 weaponry")
        if (
            command.business_ref.business_type != self.task_type
            or command.business_ref.business_key != submission.business_key
            or isinstance(command.input_schema_version, bool)
            or not isinstance(command.input_schema_version, int)
            or command.input_schema_version != WEAPONRY_INPUT_SCHEMA_VERSION
            or command.trace_id != submission.trace_id
        ):
            raise ValueError("Weaponry 命令身份、Schema 或 trace 不一致")

        snapshot = WeaponryInputSnapshot.from_submission(
            submission,
            task_id=task_id.value,
            accepted_at=accepted_at,
            schema_version=command.input_schema_version,
        )
        input_payload = {
            "schema_version": snapshot.schema_version,
            "task_id": snapshot.task_id,
            "architecture_id": snapshot.architecture_id,
            "business_key": snapshot.business_key,
            "field_templates": [
                item.template.to_dict() for item in snapshot.fields
            ],
            "document_scope": _scope_payload(snapshot.document_scope),
            "evidence_selection_policy": _profile_payload(
                snapshot.evidence_selection_policy
            ),
            "execution_policy": _execution_policy_payload(
                snapshot.execution_policy
            ),
            "auxiliary_guidance_policy": _auxiliary_policy_payload(
                snapshot.auxiliary_guidance_policy
            ),
            "accepted_at": snapshot.accepted_at,
            "trace_id": snapshot.trace_id,
        }
        return EncodedTaskSubmission(
            input_snapshot=snapshot,
            input_payload=input_payload,
            # 原始 URL、status 和未知扩展键仅保留在兼容投影；Worker 永远不读取它。
            projection_request_payload=submission.request_projection.to_dict(),
            initial_public_status="1",
            active_public_statuses=("0", "1"),
        )

    def decode_input(
        self,
        *,
        schema_version: int,
        payload: Mapping[str, Any],
    ) -> WeaponryInputSnapshot:
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != WEAPONRY_INPUT_SCHEMA_VERSION
        ):
            raise ValueError("不支持的 Weaponry 输入 Schema 版本")
        decoded = _exact_mapping(
            payload,
            expected_keys=_INPUT_KEYS,
            name="weaponry input_payload",
        )
        payload_schema_version = decoded.get("schema_version")
        if (
            isinstance(payload_schema_version, bool)
            or not isinstance(payload_schema_version, int)
            or payload_schema_version != schema_version
        ):
            raise ValueError("Weaponry input_payload Schema 与 execution 行不一致")
        architecture_id = decoded.get("architecture_id")
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or decoded.get("business_key") != str(architecture_id)
        ):
            raise ValueError("Weaponry architecture_id 与 business_key 不一致")
        field_payloads = _json_list(
            decoded.get("field_templates"),
            name="field_templates",
        )
        if not field_payloads:
            raise ValueError("field_templates 不能为空")
        fields = tuple(
            _decode_field_template(item, index=index)
            for index, item in enumerate(field_payloads)
        )
        return WeaponryInputSnapshot(
            schema_version=schema_version,
            task_id=decoded.get("task_id"),  # type: ignore[arg-type]
            architecture_id=architecture_id,
            fields=fields,
            document_scope=_decode_scope(decoded.get("document_scope")),
            evidence_selection_policy=_decode_profile(
                decoded.get("evidence_selection_policy")
            ),
            execution_policy=_decode_execution_policy(
                decoded.get("execution_policy")
            ),
            auxiliary_guidance_policy=_decode_auxiliary_policy(
                decoded.get("auxiliary_guidance_policy")
            ),
            accepted_at=decoded.get("accepted_at"),  # type: ignore[arg-type]
            trace_id=decoded.get("trace_id"),  # type: ignore[arg-type]
        )

    def encode_result(self, result: WeaponryResult) -> EncodedTaskResult:
        if not isinstance(result, WeaponryResult):
            raise TypeError("Weaponry Codec 只接受 WeaponryResult")
        callback_payload = result.to_callback()
        return EncodedTaskResult(
            execution_result_payload={
                # 当前没有历史 execution 或旧 Worker，输入/结果内部快照统一使用唯一
                # Schema v2；公开 Callback 投影不包含该内部版本字段。
                "schema_version": WEAPONRY_INPUT_SCHEMA_VERSION,
                "task_id": result.identity.task_id,
                "architecture_id": result.identity.architecture_id,
                "status": result.status,
                "message": callback_payload.message,
            },
            projection_result_payload=callback_payload.to_public_dict(),
        )

    def validate_result(
        self,
        input_snapshot: WeaponryInputSnapshot,
        result: WeaponryResult,
    ) -> None:
        """供通用 Task Adapter 在终态 CAS 前执行武器谱完整性门禁。"""

        validate_weaponry_result_completeness(input_snapshot, result)

    def validate_completion(
        self,
        input_snapshot: WeaponryInputSnapshot,
        completion: ExpectedTaskCompletion[WeaponryResult],
    ) -> None:
        """校验业务结果和通用任务终态元数据使用同一成功/失败语义。

        只检查字段完整性仍可能让成功结果与 ``failed/3`` 一起持久化，造成 execution、
        最新公开状态和回调 payload 互相矛盾。该门禁在 Repository 终态 CAS 之前执行；
        失败时数据库继续保持原状态，便于 Worker 修复后安全重试。
        """

        if not isinstance(completion, ExpectedTaskCompletion):
            raise TypeError("completion 必须是 ExpectedTaskCompletion")
        self.validate_result(input_snapshot, completion.result)
        expected_terminal = (
            ("succeeded", "2")
            if completion.result.status == WEAPONRY_STATUS_SUCCEEDED
            else ("failed", "3")
        )
        actual_terminal = (completion.execution_state, completion.public_status)
        if actual_terminal != expected_terminal:
            raise ValueError(
                "WeaponryResult 状态与 execution_state/public_status 不一致"
            )


__all__ = ["WeaponryTaskCommandCodec"]
