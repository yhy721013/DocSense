"""武器谱字段级 Application 编排。

本模块把目标检索、确定性 Evidence Selection、来源级抽取、翻译和纯领域组装收敛在
一个高内聚组件中。它不知道 Flask、SQLite、AnythingLLM 或环境变量；慢调用前后的
审计与 latest 复核均通过注入的抽象完成。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import logging

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.domain import (
    AuxiliaryGuidance,
    EvidenceSelectionResult,
    RetrievalColumn,
    RetrievalField,
    SelectedEvidence,
    TableRowResult,
    WeaponryAnalyseDataSource,
    WeaponryDocumentSnapshot,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryInputSnapshot,
    WeaponryRetrievalValidationError,
    assemble_table_rows,
    build_forced_empty_result,
    build_table_empty_fallback_result,
    build_input_extraction_prompt,
    build_retrieval_query,
    build_table_extraction_prompt,
    external_processing_specification,
    merge_table_rows,
    parse_table_json_rows,
    select_evidence,
)
from app.modules.weaponry.ports import (
    AuxiliaryGuidanceOutcome,
    AuxiliaryGuidancePort,
    AuxiliaryGuidanceRequest,
    AuxiliaryGuidanceResult,
    CompleteWeaponryInteraction,
    EvidenceExtractionPort,
    EvidenceExtractionRequest,
    ExtractionAnswer,
    ExtractionValidationOutcome,
    ReserveWeaponryInteraction,
    SearchTargetEvidence,
    TargetEvidenceRetrievalPort,
    TargetEvidenceScope,
    TargetEvidenceSearchResult,
    WeaponryAuditOutcome,
    WeaponryAuditReceipt,
    WeaponryAuditReservation,
    WeaponryAuditReserveOutcome,
    WeaponryAuditReserveResult,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryInteractionAuditPort,
    WeaponryOperation,
    WeaponrySourceBoundaryError,
    WeaponryTranslationOutcome,
    WeaponryTranslationPort,
    WeaponryTranslationRequest,
    WeaponryTranslationResult,
    WeaponryCallIdentity,
    validate_auxiliary_result_policy,
)

from .errors import (
    WeaponryAuditError,
    WeaponryPortContractError,
    WeaponryScenePreservationError,
    WeaponryStaleExecutionError,
    WeaponryTaskPersistenceError,
)


logger = logging.getLogger(__name__)

# 来源会话创建、上下文校验等明确失败允许一次稳定重试。OUTCOME_UNKNOWN 禁止重试，
# 避免供应商已创建资源但响应丢失时再制造第二份副作用。
_MAX_EXTRACTION_ATTEMPTS = 2
_TRANSLATION_TARGET_LANGUAGE = "Chinese"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    """生成只供摘要使用的稳定 JSON；调用方不得把返回值写入日志。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


@dataclass(frozen=True)
class WeaponryFieldExecution:
    """一个字段的完整结果和内部诊断摘要，不进入公开 Callback。

    ``diagnostic_error_codes`` 只用于区分“合法业务空结果”和“外部容量/协议错误后降级
    为空”。它不是公开 DTO，也不会写入甲方回调；保留该事实可以避免 Dispatcher 把
    供应商 413/429 等故障误计为正常零结果。
    """

    result: WeaponryFieldResult
    selected_evidence_count: int
    model_call_count: int
    diagnostic_error_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.result, WeaponryFieldResult):
            raise TypeError("result 必须是 WeaponryFieldResult")
        for name in ("selected_evidence_count", "model_call_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if not isinstance(self.diagnostic_error_codes, tuple):
            raise TypeError("diagnostic_error_codes 必须是 tuple")
        if any(
            not isinstance(code, str) or not code.strip()
            for code in self.diagnostic_error_codes
        ):
            raise ValueError("diagnostic_error_codes 只能包含非空字符串")
        if len(set(self.diagnostic_error_codes)) != len(
            self.diagnostic_error_codes
        ):
            raise ValueError("diagnostic_error_codes 不得重复")


@dataclass(frozen=True)
class _EvidenceSelectionExecution:
    """Selection 结果及其降级原因；仅在字段用例内部流转。"""

    selection: EvidenceSelectionResult
    diagnostic_error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SourceExtractionExecution:
    """单文档抽取结果、实际调用次数及最终降级原因。"""

    answer: ExtractionAnswer | None
    model_call_count: int
    diagnostic_error_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _RankedInputSource:
    selection_order: int
    source: WeaponryAnalyseDataSource


@dataclass(frozen=True)
class _RankedTableRows:
    selection_order: int
    rows: tuple[TableRowResult, ...]


class WeaponryFieldExecutor:
    """串行执行一个字段，并保证 Candidate 不能绕过 Selection 进入模型。"""

    def __init__(
        self,
        *,
        retrieval: TargetEvidenceRetrievalPort,
        extraction: EvidenceExtractionPort,
        guidance: AuxiliaryGuidancePort,
        translation: WeaponryTranslationPort,
        audit: WeaponryInteractionAuditPort,
    ) -> None:
        self._retrieval = retrieval
        self._extraction = extraction
        self._guidance = guidance
        self._translation = translation
        self._audit = audit

    @property
    def retrieval(self) -> TargetEvidenceRetrievalPort:
        """供组合根核对 Scope 与字段搜索使用同一个任务级 Adapter。"""

        return self._retrieval

    @property
    def extraction(self) -> EvidenceExtractionPort:
        return self._extraction

    @property
    def guidance(self) -> AuxiliaryGuidancePort:
        return self._guidance

    @property
    def translation(self) -> WeaponryTranslationPort:
        return self._translation

    @property
    def audit(self) -> WeaponryInteractionAuditPort:
        return self._audit

    def execute(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        snapshot: WeaponryInputSnapshot,
        scope: TargetEvidenceScope,
        field: WeaponryFieldSpecification,
        field_sequence: int,
        is_current: Callable[[], bool],
    ) -> WeaponryFieldExecution:
        """按冻结文档顺序执行来源调用，再按 Evidence 排名组装公开结果。"""

        self._validate_context(
            task_id=task_id,
            business_ref=business_ref,
            snapshot=snapshot,
            scope=scope,
            field=field,
            field_sequence=field_sequence,
            is_current=is_current,
        )
        processing_field = external_processing_specification(field)
        if processing_field is None:
            # 甲方保留字段必须在任何 Guidance、检索、审计、模型和翻译之前收敛为空；
            # 但仍先完成 latest 复核，避免失去所有权的 execution 继续产出业务结果。
            self._ensure_current(is_current)
            logger.info(
                "武器谱保留字段确定性置空，跳过全部字段级外部调用: "
                "task_id=%s field_sequence=%d field_type=%s",
                task_id.value,
                field_sequence,
                field.field_type,
            )
            return WeaponryFieldExecution(
                result=build_forced_empty_result(field),
                selected_evidence_count=0,
                model_call_count=0,
            )
        # 每次字段执行独享一个诊断列表；它既不会跨字段共享，也不会进入公开结果，
        # 只用于 Dispatcher 区分业务空结果和外部能力降级。
        diagnostic_error_codes: list[str] = []
        guidance = self._load_guidance(
            task_id=task_id,
            business_ref=business_ref,
            snapshot=snapshot,
            field=processing_field,
            field_sequence=field_sequence,
            is_current=is_current,
            diagnostics=diagnostic_error_codes,
        )
        selection_execution = self._retrieve_and_select(
            task_id=task_id,
            business_ref=business_ref,
            snapshot=snapshot,
            scope=scope,
            field=processing_field,
            field_sequence=field_sequence,
            is_current=is_current,
        )
        selection = selection_execution.selection
        diagnostic_error_codes.extend(
            selection_execution.diagnostic_error_codes
        )
        if not selection.selected:
            logger.info(
                "武器谱字段无 Selected Evidence，跳过全部模型调用: "
                "task_id=%s field_sequence=%d field_type=%s",
                task_id.value,
                field_sequence,
                field.field_type,
            )
            result = (
                build_table_empty_fallback_result(field)
                if field.field_type == "TABLE" and processing_field != field
                else WeaponryFieldResult(specification=field)
            )
            return WeaponryFieldExecution(
                result=result,
                selected_evidence_count=0,
                model_call_count=0,
                diagnostic_error_codes=tuple(diagnostic_error_codes),
            )

        evidence_by_document: dict[str, list[SelectedEvidence]] = {}
        selection_order: dict[str, int] = {}
        for index, evidence in enumerate(selection.selected):
            evidence_by_document.setdefault(evidence.document_key, []).append(evidence)
            selection_order.setdefault(evidence.document_key, index)

        if field.field_type == "INPUT":
            ranked_sources: list[_RankedInputSource] = []
            model_calls = 0
            # 调用顺序只由受理时 document_sequence 决定，不能随供应商分数漂移；最终来源
            # 展示顺序仍按冻结 Selection 排名，保持既有“置信度/排名优先”契约。
            for document in snapshot.document_scope.documents:
                evidence = tuple(evidence_by_document.get(document.document_key, ()))
                if not evidence:
                    continue
                extraction = self._extract_source(
                    task_id=task_id,
                    business_ref=business_ref,
                    snapshot=snapshot,
                    document=document,
                    field=processing_field,
                    field_sequence=field_sequence,
                    evidence=evidence,
                    guidance=guidance,
                    is_current=is_current,
                )
                diagnostic_error_codes.extend(
                    extraction.diagnostic_error_codes
                )
                model_calls += extraction.model_call_count
                answer = extraction.answer
                if answer is None:
                    continue
                translation = self._translate(
                    task_id=task_id,
                    business_ref=business_ref,
                    field_sequence=field_sequence,
                    document=document,
                    item_sequence=1,
                    text=answer.text,
                    is_current=is_current,
                    diagnostics=diagnostic_error_codes,
                )
                ranked_sources.append(
                    _RankedInputSource(
                        selection_order=selection_order[document.document_key],
                        source=WeaponryAnalyseDataSource(
                            content=answer.text,
                            source=document.original_name,
                            # 接口含义是“内容产生时间”而非解析时刻；当前 Evidence/模型没有
                            # 提供可信业务时间，必须保持空值，禁止写入当前系统时间冒充。
                            occurred_at="",
                            file_name=document.file_name,
                            rows=tuple(item.text for item in evidence),
                            translation=translation,
                        ),
                    )
                )
            ranked_sources.sort(key=lambda item: item.selection_order)
            sources = tuple(item.source for item in ranked_sources)
            return WeaponryFieldExecution(
                result=WeaponryFieldResult(
                    specification=field,
                    analyse_data=sources[0].content if sources else "",
                    sources=sources,
                ),
                selected_evidence_count=len(selection.selected),
                model_call_count=model_calls,
                diagnostic_error_codes=tuple(
                    dict.fromkeys(diagnostic_error_codes)
                ),
            )

        ranked_rows: list[_RankedTableRows] = []
        model_calls = 0
        for document in snapshot.document_scope.documents:
            evidence = tuple(evidence_by_document.get(document.document_key, ()))
            if not evidence:
                continue
            extraction = self._extract_source(
                task_id=task_id,
                business_ref=business_ref,
                snapshot=snapshot,
                document=document,
                field=processing_field,
                field_sequence=field_sequence,
                evidence=evidence,
                guidance=guidance,
                is_current=is_current,
            )
            diagnostic_error_codes.extend(
                extraction.diagnostic_error_codes
            )
            model_calls += extraction.model_call_count
            answer = extraction.answer
            if answer is None:
                continue
            parsed_rows = parse_table_json_rows(
                answer.text,
                processing_field,
                max_rows=snapshot.execution_policy.max_table_rows,
            )
            if not parsed_rows:
                logger.info(
                    "武器谱 TABLE 来源没有可组装行: task_id=%s "
                    "field_sequence=%d document_sequence=%d answer_chars=%d",
                    task_id.value,
                    field_sequence,
                    document.sequence_no,
                    len(answer.text),
                )
                continue
            table_rows: list[TableRowResult] = []
            translation_item_sequence = 0
            for parsed_row in parsed_rows:
                translations: list[tuple[str, str]] = []
                for column in processing_field.columns:
                    cell_value = parsed_row.get(column.field_name)
                    if not cell_value:
                        continue
                    translation_item_sequence += 1
                    translations.append(
                        (
                            column.field_name,
                            self._translate(
                                task_id=task_id,
                                business_ref=business_ref,
                                field_sequence=field_sequence,
                                document=document,
                                item_sequence=translation_item_sequence,
                                text=cell_value,
                                is_current=is_current,
                                diagnostics=diagnostic_error_codes,
                            ),
                        )
                    )
                table_rows.append(
                    TableRowResult(
                        row=parsed_row,
                        source_name=document.original_name,
                        file_name=document.file_name,
                        evidence_rows=tuple(item.text for item in evidence),
                        occurred_at="",
                        translations=tuple(translations),
                    )
                )
            ranked_rows.append(
                _RankedTableRows(
                    selection_order=selection_order[document.document_key],
                    rows=tuple(table_rows),
                )
            )
        ranked_rows.sort(key=lambda item: item.selection_order)
        merged = merge_table_rows(
            (
                row
                for group in ranked_rows
                for row in group.rows
            ),
            field,
            max_rows=snapshot.execution_policy.max_table_rows,
        )
        assembled_rows = assemble_table_rows(merged, field)
        result = (
            WeaponryFieldResult(
                specification=field,
                table_rows=assembled_rows,
            )
            if assembled_rows
            else build_table_empty_fallback_result(field)
            if processing_field != field
            else WeaponryFieldResult(specification=field)
        )
        return WeaponryFieldExecution(
            result=result,
            selected_evidence_count=len(selection.selected),
            model_call_count=model_calls,
            diagnostic_error_codes=tuple(
                dict.fromkeys(diagnostic_error_codes)
            ),
        )

    @staticmethod
    def _validate_context(
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        snapshot: WeaponryInputSnapshot,
        scope: TargetEvidenceScope,
        field: WeaponryFieldSpecification,
        field_sequence: int,
        is_current: Callable[[], bool],
    ) -> None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(business_ref, TaskBusinessRef):
            raise TypeError("business_ref 必须是 TaskBusinessRef")
        if not isinstance(snapshot, WeaponryInputSnapshot):
            raise TypeError("snapshot 必须是 WeaponryInputSnapshot")
        if not isinstance(scope, TargetEvidenceScope) or scope.task_id != task_id:
            raise WeaponryPortContractError("Target Evidence Scope 不属于当前任务")
        if not isinstance(field, WeaponryFieldSpecification):
            raise TypeError("field 必须是 WeaponryFieldSpecification")
        if (
            isinstance(field_sequence, bool)
            or not isinstance(field_sequence, int)
            or field_sequence < 1
        ):
            raise ValueError("field_sequence 必须是正整数")
        if field_sequence > len(snapshot.fields) or snapshot.fields[field_sequence - 1] != field:
            raise WeaponryPortContractError("字段顺序与输入快照不一致")
        if not callable(is_current):
            raise TypeError("is_current 必须可调用")

    def _load_guidance(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        snapshot: WeaponryInputSnapshot,
        field: WeaponryFieldSpecification,
        field_sequence: int,
        is_current: Callable[[], bool],
        diagnostics: list[str],
    ) -> tuple[AuxiliaryGuidance, ...]:
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=field_sequence,
            document_sequence=None,
            operation=WeaponryOperation.AUXILIARY_GUIDANCE,
        )
        input_payload = _canonical_json(
            {
                "field_name": field.field_name,
                "field_description": field.field_description,
                "columns": [
                    {
                        "field_name": column.field_name,
                        "field_description": column.field_description,
                    }
                    for column in field.columns
                ],
                "policy_id": snapshot.auxiliary_guidance_policy.policy_id,
                "catalog_fingerprint": snapshot.auxiliary_guidance_policy.catalog_fingerprint,
                "top_n": snapshot.auxiliary_guidance_policy.top_n,
                "max_context_chars": snapshot.auxiliary_guidance_policy.max_context_chars,
            }
        )
        # latest 复核必须位于审计预留和慢 I/O 之前。当前单实例阶段这主要防御人工
        # supersede/错误派发；未来执行租约落地后，组合根会把同一回调替换为 fencing 校验。
        self._ensure_current(is_current)
        reservation = self._reserve(
            business_ref=business_ref,
            call=call,
            input_text=input_payload,
        )
        request = AuxiliaryGuidanceRequest(
            call=call,
            field=field,
            policy=snapshot.auxiliary_guidance_policy,
        )
        try:
            result = self._guidance.load(request)
        except WeaponryExternalOperationError as error:
            self._complete_failed(reservation, error.error_code)
            diagnostics.append(error.error_code)
            self._ensure_current(is_current)
            logger.warning(
                "武器谱辅助语境降级为空: task_id=%s field_sequence=%d "
                "error_code=%s outcome=%s",
                task_id.value,
                field_sequence,
                error.error_code,
                error.outcome.value,
            )
            return ()
        except Exception as error:
            self._complete_failed(reservation, "auxiliary_guidance_port_error")
            raise WeaponryPortContractError("Auxiliary Guidance Port 调用失败") from error
        if not isinstance(result, AuxiliaryGuidanceResult) or result.call != call:
            self._complete_failed(reservation, "auxiliary_guidance_result_invalid")
            raise WeaponryPortContractError("Auxiliary Guidance 结果身份不一致")
        try:
            validate_auxiliary_result_policy(request, result)
        except (TypeError, ValueError) as error:
            self._complete_failed(reservation, "auxiliary_guidance_policy_mismatch")
            raise WeaponryPortContractError(
                "Auxiliary Guidance 结果违反冻结策略"
            ) from error

        output_payload = _canonical_json(
            {
                "outcome": result.outcome.value,
                "guidance": [
                    {
                        "guidance_id": item.guidance_id,
                        "text_digest": _sha256_text(item.text),
                    }
                    for item in result.guidance
                ],
            }
        )
        if result.outcome is AuxiliaryGuidanceOutcome.DEGRADED:
            self._complete_failed(reservation, result.error_code)
            diagnostics.append(result.error_code)
        else:
            self._complete_succeeded(
                reservation,
                output_digest=_sha256_text(output_payload),
                output_chars=sum(len(item.text) for item in result.guidance),
            )
        self._ensure_current(is_current)
        return tuple(result.guidance)

    def _retrieve_and_select(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        snapshot: WeaponryInputSnapshot,
        scope: TargetEvidenceScope,
        field: WeaponryFieldSpecification,
        field_sequence: int,
        is_current: Callable[[], bool],
    ) -> _EvidenceSelectionExecution:
        retrieval_field = RetrievalField(
            field_name=field.field_name,
            field_description=field.field_description,
            field_type=field.field_type,
            columns=tuple(
                RetrievalColumn(
                    field_name=column.field_name,
                    field_description=column.field_description,
                )
                for column in field.columns
            ),
        )
        query = build_retrieval_query(retrieval_field)
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=field_sequence,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        allowed_document_keys = tuple(
            item.document_key for item in snapshot.document_scope.documents
        )
        self._ensure_current(is_current)
        reservation = self._reserve(
            business_ref=business_ref,
            call=call,
            input_text=query.text,
            allowed_document_keys=allowed_document_keys,
        )
        candidate_top_n = (
            snapshot.evidence_selection_policy.input_candidate_top_n
            if field.field_type == "INPUT"
            else snapshot.evidence_selection_policy.table_candidate_top_n
        )
        try:
            search = self._retrieval.search_target(
                SearchTargetEvidence(
                    scope=scope,
                    call=call,
                    query=query,
                    allowed_document_keys=allowed_document_keys,
                    candidate_top_n=candidate_top_n,
                )
            )
        except (WeaponryExternalOperationError, WeaponrySourceBoundaryError) as error:
            error_code = getattr(error, "error_code", "target_retrieval_failed")
            outcome = (
                WeaponryAuditOutcome.REJECTED
                if isinstance(error, WeaponrySourceBoundaryError)
                else WeaponryAuditOutcome.FAILED
            )
            self._complete_non_success(
                reservation,
                outcome=outcome,
                error_code=error_code,
            )
            self._ensure_current(is_current)
            logger.warning(
                "武器谱目标检索按字段级空结果降级: task_id=%s "
                "field_sequence=%d error_code=%s",
                task_id.value,
                field_sequence,
                error_code,
            )
            return _EvidenceSelectionExecution(
                selection=EvidenceSelectionResult(selected=(), rejected=()),
                diagnostic_error_codes=(error_code,),
            )
        except Exception as error:
            self._complete_failed(reservation, "target_retrieval_port_error")
            raise WeaponryPortContractError("Target Retrieval Port 调用失败") from error
        if (
            not isinstance(search, TargetEvidenceSearchResult)
            or search.call != call
            or search.scope_ref != scope.scope_ref
        ):
            self._complete_failed(reservation, "target_retrieval_result_invalid")
            raise WeaponryPortContractError("Target Retrieval 结果身份不一致")

        try:
            selection = select_evidence(
                search.candidates,
                score_mode=search.score_mode,
                query=query,
                profile=snapshot.evidence_selection_policy,
                provider_fingerprint=search.provider_fingerprint,
                embedding_fingerprint=search.embedding_fingerprint,
                expected_document_keys=allowed_document_keys,
            )
        except WeaponryRetrievalValidationError:
            # Profile/score/rank 契约异常属于已批准的字段级空结果语义，但必须完成一条
            # 可诊断的 rejected 审计，绝不能静默把非法分数改写成零。
            rejection_reasons = tuple(
                "selection-protocol-invalid" for _ in search.candidates
            )
            self._complete_non_success(
                reservation,
                outcome=WeaponryAuditOutcome.REJECTED,
                error_code="evidence_selection_protocol_invalid",
                output_digest=self._retrieval_output_digest(search, None),
                candidate_count=len(search.candidates),
                selected_count=0,
                rejection_reasons=rejection_reasons,
            )
            self._ensure_current(is_current)
            logger.warning(
                "武器谱 Evidence Selection 协议异常，字段降级为空: "
                "task_id=%s field_sequence=%d candidate_count=%d",
                task_id.value,
                field_sequence,
                len(search.candidates),
            )
            return _EvidenceSelectionExecution(
                selection=EvidenceSelectionResult(selected=(), rejected=()),
                diagnostic_error_codes=(
                    "evidence_selection_protocol_invalid",
                ),
            )

        rejection_reasons = tuple(item.reason for item in selection.rejected)
        all_rejected = bool(search.candidates) and not selection.selected
        if all_rejected:
            self._complete_non_success(
                reservation,
                outcome=WeaponryAuditOutcome.REJECTED,
                error_code="evidence_all_candidates_rejected",
                output_digest=self._retrieval_output_digest(search, selection),
                candidate_count=len(search.candidates),
                selected_count=0,
                rejection_reasons=rejection_reasons,
            )
        else:
            self._complete_succeeded(
                reservation,
                output_digest=self._retrieval_output_digest(search, selection),
                candidate_count=len(search.candidates),
                selected_count=len(selection.selected),
                rejection_reasons=rejection_reasons,
            )
        self._ensure_current(is_current)
        logger.info(
            "武器谱 Evidence Selection 完成: task_id=%s field_sequence=%d "
            "candidate_count=%d selected_count=%d rejected_count=%d",
            task_id.value,
            field_sequence,
            len(search.candidates),
            len(selection.selected),
            len(selection.rejected),
        )
        return _EvidenceSelectionExecution(selection=selection)

    def _extract_source(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        snapshot: WeaponryInputSnapshot,
        document: WeaponryDocumentSnapshot,
        field: WeaponryFieldSpecification,
        field_sequence: int,
        evidence: tuple[SelectedEvidence, ...],
        guidance: tuple[AuxiliaryGuidance, ...],
        is_current: Callable[[], bool],
    ) -> _SourceExtractionExecution:
        prompt = (
            build_input_extraction_prompt(field, evidence, guidance=guidance)
            if field.field_type == "INPUT"
            else build_table_extraction_prompt(field, evidence, guidance=guidance)
        )
        for attempt_no in range(1, _MAX_EXTRACTION_ATTEMPTS + 1):
            call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=field_sequence,
                document_sequence=document.sequence_no,
                operation=WeaponryOperation.EVIDENCE_EXTRACTION,
                attempt_no=attempt_no,
            )
            self._ensure_current(is_current)
            reservation = self._reserve(
                business_ref=business_ref,
                call=call,
                input_text=prompt.text,
                allowed_document_keys=(document.document_key,),
                source_marker_digests=tuple(
                    _sha256_text(item.candidate_id) for item in evidence
                ),
            )
            request = EvidenceExtractionRequest(
                call=call,
                document=document,
                field=field,
                evidence=evidence,
                prompt=prompt,
                guidance=guidance,
                context_strategy=snapshot.execution_policy.extraction_context_strategy,
                model_fingerprint=snapshot.execution_policy.extraction_model_fingerprint,
            )
            try:
                answer = self._extraction.extract(request)
            except WeaponryExternalOperationError as error:
                self._complete_non_success(
                    reservation,
                    outcome=WeaponryAuditOutcome.FAILED,
                    error_code=error.error_code,
                )
                self._ensure_current(is_current)
                unknown = error.outcome is WeaponryExternalOutcome.OUTCOME_UNKNOWN
                logger.warning(
                    "武器谱来源抽取失败，禁止父会话回退: task_id=%s "
                    "field_sequence=%d document_sequence=%d attempt_no=%d "
                    "error_code=%s retry_allowed=%s",
                    task_id.value,
                    field_sequence,
                    document.sequence_no,
                    attempt_no,
                    error.error_code,
                    not unknown and attempt_no < _MAX_EXTRACTION_ATTEMPTS,
                )
                if unknown:
                    # Extraction 可能已经创建 workspace/thread 或提交模型写请求。结果未知
                    # 不能按普通空来源继续并自动清理，否则会丢失唯一对账现场。
                    raise WeaponryScenePreservationError(
                        "武器谱来源抽取外部结果未知",
                        error_code=error.error_code,
                    ) from error
                if attempt_no < _MAX_EXTRACTION_ATTEMPTS:
                    continue
                return _SourceExtractionExecution(
                    answer=None,
                    model_call_count=attempt_no,
                    diagnostic_error_codes=(error.error_code,),
                )
            except WeaponrySourceBoundaryError as error:
                self._complete_non_success(
                    reservation,
                    outcome=WeaponryAuditOutcome.REJECTED,
                    error_code=error.error_code,
                )
                self._ensure_current(is_current)
                logger.warning(
                    "武器谱来源边界校验失败，禁止父会话回退: task_id=%s "
                    "field_sequence=%d document_sequence=%d attempt_no=%d "
                    "error_code=%s retry_allowed=%s",
                    task_id.value,
                    field_sequence,
                    document.sequence_no,
                    attempt_no,
                    error.error_code,
                    attempt_no < _MAX_EXTRACTION_ATTEMPTS,
                )
                if attempt_no < _MAX_EXTRACTION_ATTEMPTS:
                    continue
                return _SourceExtractionExecution(
                    answer=None,
                    model_call_count=attempt_no,
                    diagnostic_error_codes=(error.error_code,),
                )
            except Exception as error:
                self._complete_failed(reservation, "evidence_extraction_port_error")
                raise WeaponryPortContractError("Evidence Extraction Port 调用失败") from error

            if (
                not isinstance(answer, ExtractionAnswer)
                or answer.call != call
                or answer.evidence_ids != tuple(item.candidate_id for item in evidence)
            ):
                self._complete_failed(reservation, "evidence_extraction_result_invalid")
                raise WeaponryPortContractError("Evidence Extraction 回答身份不一致")
            source_count = len(answer.sources)
            if (
                answer.text
                and answer.validation_outcome
                is not ExtractionValidationOutcome.MATCHED
            ):
                self._complete_non_success(
                    reservation,
                    outcome=WeaponryAuditOutcome.REJECTED,
                    error_code="extraction_validation_not_matched",
                    output_digest=answer.raw_response_digest,
                    output_chars=answer.raw_response_chars,
                    source_count=source_count,
                    mismatched_source_count=source_count,
                )
                self._ensure_current(is_current)
                logger.warning(
                    "武器谱来源抽取回答未通过来源校验，丢弃当前来源并继续: "
                    "task_id=%s field_sequence=%d document_sequence=%d "
                    "validation_outcome=%s",
                    task_id.value,
                    field_sequence,
                    document.sequence_no,
                    answer.validation_outcome.value,
                )
                return _SourceExtractionExecution(
                    answer=None,
                    model_call_count=attempt_no,
                    diagnostic_error_codes=(
                        "extraction_validation_not_matched",
                    ),
                )

            # INPUT Prompt 冻结的无信息哨兵是完整回答 ``未找到``，不是任意位置出现的
            # 子串。否则“未找到号”等真实名称，或 TABLE JSON 某个单元格中的同名文本，
            # 都会让整份来源被静默丢弃。TABLE 的无数据语义由严格 JSON ``[]`` 解析负责。
            normalized_answer = answer.text.strip()
            no_information = not normalized_answer or (
                field.field_type == "INPUT" and normalized_answer == "未找到"
            )
            if no_information:
                self._complete_non_success(
                    reservation,
                    outcome=WeaponryAuditOutcome.REJECTED,
                    error_code="extraction_no_information",
                    output_digest=answer.raw_response_digest,
                    output_chars=answer.raw_response_chars,
                    source_count=source_count,
                    verified_source_count=source_count,
                )
                self._ensure_current(is_current)
                return _SourceExtractionExecution(
                    answer=None,
                    model_call_count=attempt_no,
                )

            self._complete_succeeded(
                reservation,
                output_digest=answer.raw_response_digest,
                output_chars=answer.raw_response_chars,
                source_count=source_count,
                verified_source_count=source_count,
            )
            self._ensure_current(is_current)
            return _SourceExtractionExecution(
                answer=answer,
                model_call_count=attempt_no,
            )
        raise AssertionError("抽取重试循环必须在有界范围内收敛")

    def _translate(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        field_sequence: int,
        document: WeaponryDocumentSnapshot,
        item_sequence: int,
        text: str,
        is_current: Callable[[], bool],
        diagnostics: list[str],
    ) -> str:
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=field_sequence,
            document_sequence=document.sequence_no,
            operation=WeaponryOperation.TRANSLATION,
            item_sequence=item_sequence,
        )
        self._ensure_current(is_current)
        reservation = self._reserve(
            business_ref=business_ref,
            call=call,
            input_text=text,
            allowed_document_keys=(document.document_key,),
        )
        try:
            result = self._translation.translate(
                WeaponryTranslationRequest(
                    call=call,
                    text=text,
                    target_language=_TRANSLATION_TARGET_LANGUAGE,
                )
            )
        except WeaponryExternalOperationError as error:
            self._complete_failed(reservation, error.error_code)
            diagnostics.append(error.error_code)
            self._ensure_current(is_current)
            logger.warning(
                "武器谱翻译异常，按兼容语义返回空文本: task_id=%s "
                "field_sequence=%d document_sequence=%d item_sequence=%d "
                "error_code=%s",
                task_id.value,
                field_sequence,
                document.sequence_no,
                item_sequence,
                error.error_code,
            )
            return ""
        except Exception as error:
            self._complete_failed(reservation, "translation_port_error")
            raise WeaponryPortContractError("Translation Port 调用失败") from error
        if not isinstance(result, WeaponryTranslationResult) or result.call != call:
            self._complete_failed(reservation, "translation_result_invalid")
            raise WeaponryPortContractError("Translation 结果身份不一致")
        if result.outcome is WeaponryTranslationOutcome.FAILED:
            self._complete_failed(reservation, result.error_code)
            diagnostics.append(result.error_code)
            self._ensure_current(is_current)
            logger.warning(
                "武器谱翻译失败，按兼容语义保留空文本: task_id=%s "
                "field_sequence=%d document_sequence=%d item_sequence=%d "
                "error_code=%s",
                task_id.value,
                field_sequence,
                document.sequence_no,
                item_sequence,
                result.error_code,
            )
            return ""
        self._complete_succeeded(
            reservation,
            output_digest=_sha256_text(result.text),
            output_chars=len(result.text),
        )
        self._ensure_current(is_current)
        return result.text

    def _reserve(
        self,
        *,
        business_ref: TaskBusinessRef,
        call: WeaponryCallIdentity,
        input_text: str,
        allowed_document_keys: tuple[str, ...] = (),
        source_marker_digests: tuple[str, ...] = (),
    ) -> WeaponryAuditReservation:
        try:
            reserve_result = self._audit.reserve(
                ReserveWeaponryInteraction(
                    business_ref=business_ref,
                    call=call,
                    input_digest=_sha256_text(input_text),
                    input_chars=len(input_text),
                    allowed_document_keys=allowed_document_keys,
                    source_marker_digests=source_marker_digests,
                )
            )
        except Exception as error:
            logger.critical(
                "武器谱交互审计预留失败，禁止外部调用: task_id=%s "
                "call_id=%s error_type=%s",
                call.task_id.value,
                call.call_id,
                type(error).__name__,
                exc_info=True,
            )
            raise WeaponryAuditError("武器谱交互审计预留失败") from error
        if not isinstance(reserve_result, WeaponryAuditReserveResult):
            raise WeaponryAuditError("武器谱审计预留返回类型错误")
        reservation = reserve_result.reservation
        if (
            reservation.call != call
            or reservation.business_ref != business_ref
        ):
            raise WeaponryAuditError("武器谱审计预留身份不一致")
        if reserve_result.outcome is not WeaponryAuditReserveOutcome.RESERVED:
            # pending 表示此前外部请求可能已发送；completed 表示此前已经形成审计终态。
            # 本审计只保存摘要，无法安全重建原始业务回答，因此两者都必须停止当前执行并
            # 保留资源现场，由资源恢复用例按已落地的保守规则隔离。
            error_code = (
                "weaponry_audit_attempt_pending"
                if reserve_result.outcome is WeaponryAuditReserveOutcome.PENDING
                else "weaponry_audit_attempt_completed"
            )
            logger.critical(
                "武器谱审计 attempt 已存在，禁止重复外部调用: task_id=%s "
                "call_id=%s outcome=%s",
                call.task_id.value,
                call.call_id,
                reserve_result.outcome.value,
            )
            raise WeaponryScenePreservationError(
                "武器谱交互存在历史审计事实，禁止盲目重放",
                error_code=error_code,
            )
        return reservation

    def _complete_succeeded(
        self,
        reservation: WeaponryAuditReservation,
        *,
        output_digest: str,
        output_chars: int = 0,
        candidate_count: int = 0,
        selected_count: int = 0,
        source_count: int = 0,
        verified_source_count: int = 0,
        rejection_reasons: tuple[str, ...] = (),
    ) -> None:
        self._complete(
            CompleteWeaponryInteraction(
                reservation=reservation,
                outcome=WeaponryAuditOutcome.SUCCEEDED,
                output_digest=output_digest,
                output_chars=output_chars,
                candidate_count=candidate_count,
                selected_count=selected_count,
                source_count=source_count,
                verified_source_count=verified_source_count,
                rejection_reasons=rejection_reasons,
            )
        )

    def _complete_failed(
        self,
        reservation: WeaponryAuditReservation,
        error_code: str,
    ) -> None:
        self._complete_non_success(
            reservation,
            outcome=WeaponryAuditOutcome.FAILED,
            error_code=error_code,
        )

    def _complete_non_success(
        self,
        reservation: WeaponryAuditReservation,
        *,
        outcome: WeaponryAuditOutcome,
        error_code: str,
        output_digest: str = "",
        output_chars: int = 0,
        candidate_count: int = 0,
        selected_count: int = 0,
        source_count: int = 0,
        verified_source_count: int = 0,
        missing_source_count: int = 0,
        mismatched_source_count: int = 0,
        rejection_reasons: tuple[str, ...] = (),
    ) -> None:
        self._complete(
            CompleteWeaponryInteraction(
                reservation=reservation,
                outcome=outcome,
                output_digest=output_digest,
                output_chars=output_chars,
                candidate_count=candidate_count,
                selected_count=selected_count,
                source_count=source_count,
                verified_source_count=verified_source_count,
                missing_source_count=missing_source_count,
                mismatched_source_count=mismatched_source_count,
                rejection_reasons=rejection_reasons,
                error_code=error_code,
            )
        )

    def _complete(self, command: CompleteWeaponryInteraction) -> None:
        try:
            receipt = self._audit.complete(command)
        except Exception as error:
            call = command.reservation.call
            logger.critical(
                "武器谱交互审计完成失败，禁止成功终态并保留现场: "
                "task_id=%s call_id=%s error_type=%s",
                call.task_id.value,
                call.call_id,
                type(error).__name__,
                exc_info=True,
            )
            raise WeaponryAuditError("武器谱交互审计完成失败") from error
        if (
            not isinstance(receipt, WeaponryAuditReceipt)
            or receipt.task_id != command.reservation.call.task_id
            or receipt.attempt_key != command.reservation.call.attempt_key
            or receipt.reservation_id != command.reservation.reservation_id
        ):
            raise WeaponryAuditError("武器谱审计完成凭据身份不一致")

    @staticmethod
    def _ensure_current(is_current: Callable[[], bool]) -> None:
        try:
            current = is_current()
        except Exception as error:
            raise WeaponryTaskPersistenceError("武器谱 latest 事实读取失败") from error
        if not isinstance(current, bool):
            raise WeaponryPortContractError("is_current 必须返回 bool")
        if not current:
            raise WeaponryStaleExecutionError("武器谱 execution 已失去 latest 所有权")

    @staticmethod
    def _retrieval_output_digest(
        search: TargetEvidenceSearchResult,
        selection: EvidenceSelectionResult | None,
    ) -> str:
        selected_ids = (
            [item.candidate_id for item in selection.selected]
            if selection is not None
            else []
        )
        rejected = (
            [
                {"candidate_id": item.candidate_id, "reason": item.reason}
                for item in selection.rejected
            ]
            if selection is not None
            else []
        )
        payload = {
            "score_mode": search.score_mode,
            "provider_fingerprint": search.provider_fingerprint,
            "embedding_fingerprint": search.embedding_fingerprint,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "document_key": item.document_key,
                    "text_digest": _sha256_text(item.text),
                    "provider_rank": item.provider_rank,
                    "provider_score": item.provider_score,
                    "provider_score_present": item.provider_score_present,
                    "score_profile_id": item.score_profile_id,
                }
                for item in search.candidates
            ],
            "selected_ids": selected_ids,
            "rejected": rejected,
        }
        try:
            serialized = _canonical_json(payload)
        except (TypeError, ValueError):
            # 非法 NaN/对象本身就是协议损坏的一部分；摘要仍需稳定且不能回显原值。
            serialized = _canonical_json(
                {
                    "score_mode": search.score_mode,
                    "candidate_ids": [item.candidate_id for item in search.candidates],
                    "selected_ids": selected_ids,
                    "rejection_reasons": [item["reason"] for item in rejected],
                    "contains_non_json_provider_value": True,
                }
            )
        return _sha256_text(serialized)


__all__ = ["WeaponryFieldExecution", "WeaponryFieldExecutor"]
