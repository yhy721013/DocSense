"""文件分析的纯规则计划、模型调用与分类/抽取工作流。

协作器不持有跨调用状态。每次执行所需的会话、审计证据和调用计数都显式放在
``_RagWorkflowState`` 中，由外观在单次 ``execute`` 中创建后传入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from app.modules.analysis.domain.architecture_recall import (
    ArchitecturePromptBudgetError,
    ArchitectureRecallDecision,
    ArchitectureRecallError,
    DocumentArchitectureSignals,
    recall_architecture_candidates,
)
from app.modules.analysis.domain.architecture_tree import (
    ArchitectureTreeIndex,
    ArchitectureTreeValidationError,
)
from app.modules.analysis.domain.classification_rules import (
    _ArchitectureScopeResolution,
    _EquipmentIdentityReselectProfile,
    _apply_topk_deterministic_architecture_constraints,
    _architecture_signal_digest,
    _build_analysis_architecture_signals,
    _build_data_standard_classification_profile,
    _build_equipment_identity_reselect_profile,
    _build_jane_classification_profile,
    _data_standard_candidate_scope,
    _data_standard_prompt_context,
    _decide_identity_reselect_gate,
    _jane_classification_prompt_context,
    _jane_recall_filename_signals,
    _match_gjb_architecture_candidate,
    _node_prompt_projection,
    _normalize_bounded_analysis_prompt,
    _parse_architecture_reselect_result,
    _parse_strict_json_object,
    _parse_topk_classification_result,
    _resolve_analysis_architecture_id,
    _resolve_jane_architecture_scope,
    _validate_architecture_repair_result,
    _validate_data_standard_leaf_requirement,
    _validate_topk_architecture_id,
    _visible_data_standard_fallback_id,
)
from app.modules.analysis.domain.errors import (
    AnalysisContractError,
    ArchitectureContractError,
    DataStandardParentContractError,
)
from app.modules.analysis.domain.models import (
    ANALYSIS_CLASSIFICATION_MODE_LEGACY,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
)
from app.modules.analysis.domain.prompts import (
    build_architecture_classification_prompt,
    build_architecture_repair_prompt,
    build_architecture_reselect_prompt,
    build_data_standard_classification_prompt,
    build_file_analysis_prompt,
    build_file_extraction_prompt,
    build_json_repair_prompt,
)
from app.modules.analysis.domain.ranges import (
    build_effective_analysis_ranges,
    validate_analysis_architecture_ranges,
)
from app.modules.analysis.domain.result_mapping import (
    _architecture_path_keyword_names,
    _general_data_standard_leaf_id,
    _is_architecture_in_standard_range,
    map_analysis_result,
)
from app.modules.analysis.domain.task_inputs import AnalysisTaskInputV1, FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisInteractionAttempt,
    AnalysisRagExecutionError,
    AnalysisRagOperation,
    AnalysisRagPort,
    AnalysisRagRequest,
    AnalysisRagSessionRef,
)

from .workflow_models import (
    AnalysisApplicationContractError,
    _AnalysisWorkflowPlan,
    _RagWorkflowState,
)


# 保持拆分前的日志分类，避免日志采集和既有检索规则因模块路径变化而失效。
logger = logging.getLogger("app.modules.analysis.application.run_analysis")


class _AnalysisModelWorkflow:
    """承载不依赖基础设施状态的模型工作流算法。"""

    def run_model_workflow(
        self,
        *,
        execution: AnalysisExecutionRef,
        snapshot: AnalysisTaskInputV1,
        plan: _AnalysisWorkflowPlan,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
    ) -> tuple[int, dict[str, Any]]:
        """严格复用现有三种分类模式的 Prompt、修复和确定性约束规则。"""

        # 旧链路对“数据标准通用要求”的强制回退有专门的禁止二次约束语义；其他路径
        # 都需要在最后统一执行确定性约束。用显式标志记录这一事实，避免靠当前
        # architectureId 的值猜测它来自哪条失败恢复分支。
        constraints_resolved = False
        if plan.direct_architecture_id is not None:
            architecture_id = plan.direct_architecture_id
            parsed = self.execute_extraction_with_repair(
                execution=execution,
                state=state,
                rag=rag,
                prompt=plan.initial_prompt,
                operation=AnalysisRagOperation.EXTRACTION,
                phase_name="字段抽取",
                max_phase_calls=snapshot.policy_snapshot.max_phase_calls,
                max_model_calls=snapshot.policy_snapshot.max_model_calls,
            )
        elif snapshot.policy_snapshot.classification_mode == ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE:
            architecture_id, constraints_resolved = self.classify_topk_two_stage(
                execution=execution,
                snapshot=snapshot,
                plan=plan,
                state=state,
                rag=rag,
            )
            selected = plan.tree_index.require(architecture_id)
            extraction_prompt = _normalize_bounded_analysis_prompt(
                build_file_extraction_prompt(
                    plan.params,
                    resolved_architecture_id=architecture_id,
                    resolved_architecture_path_name=selected.semantic_path,
                    resolved_architecture_path_node_names=_architecture_path_keyword_names(
                        plan.tree_index,
                        architecture_id,
                    ),
                    resolved_architecture_node_type=("leaf" if selected.is_leaf else "parent"),
                    include_data_standard_fields=_is_architecture_in_standard_range(
                        architecture_id,
                        plan.ranges["architectureList"],
                        plan.ranges["architectureStandardList"],
                    ),
                )
            )
            parsed = self.execute_extraction_with_repair(
                execution=execution,
                state=state,
                rag=rag,
                prompt=extraction_prompt,
                operation=AnalysisRagOperation.EXTRACTION,
                phase_name="字段抽取",
                max_phase_calls=snapshot.policy_snapshot.max_phase_calls,
                max_model_calls=snapshot.policy_snapshot.max_model_calls,
            )
        else:
            combined_started = len(state.attempts)
            parsed = self.execute_extraction_with_repair(
                execution=execution,
                state=state,
                rag=rag,
                prompt=plan.initial_prompt,
                operation=AnalysisRagOperation.COMBINED,
                phase_name="combined",
                max_phase_calls=snapshot.policy_snapshot.max_phase_calls,
                max_model_calls=snapshot.policy_snapshot.max_model_calls,
            )
            architecture_id, constraints_resolved = self.resolve_combined_architecture(
                execution=execution,
                snapshot=snapshot,
                plan=plan,
                state=state,
                rag=rag,
                parsed_result=parsed,
                phase_calls_used=len(state.attempts) - combined_started,
            )

        if not constraints_resolved:
            # 分类正常、直接抽取和 combined 路径均沿用既有确定性范围保护，防止数据库和
            # Callback 投影出现未经范围保护的 architectureId。
            architecture_id = _apply_topk_deterministic_architecture_constraints(
                architecture_id,
                file_name=snapshot.file_name,
                original_name=plan.original_name,
                visible_ids=set(plan.visible_ids),
                tree_index=plan.tree_index,
                architecture_list=plan.ranges["architectureList"],
                filename_constraint_mode=snapshot.policy_snapshot.filename_constraint_mode,
                data_standard_mode=snapshot.policy_snapshot.data_standard_mode,
                data_standard_profile=plan.data_standard_profile,
                jane_profile=plan.jane_profile,
                scope_resolution=plan.scope_resolution,
            )
        if len(state.attempts) > snapshot.policy_snapshot.max_model_calls:
            raise AnalysisContractError("文件分析实际模型调用超过固定预算")
        return architecture_id, parsed

    def classify_topk_two_stage(
        self,
        *,
        execution: AnalysisExecutionRef,
        snapshot: AnalysisTaskInputV1,
        plan: _AnalysisWorkflowPlan,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
    ) -> tuple[int, bool]:
        """执行分类、有限 repair 和可选身份受限重选。"""

        model_call_count_before_classification = len(state.attempts)
        result = self.execute_rag(
            execution=execution,
            state=state,
            rag=rag,
            operation=AnalysisRagOperation.CLASSIFICATION,
            prompt=plan.initial_prompt,
            max_model_calls=snapshot.policy_snapshot.max_model_calls,
        )
        parsed = _parse_strict_json_object(result.answer)
        architecture_id: int | None = None
        fallback_applied = False
        try:
            _, architecture_id = _parse_topk_classification_result(
                result.answer,
                visible_ids=set(plan.visible_ids),
                tree_index=plan.tree_index,
                architecture_list=plan.ranges["architectureList"],
            )
        except ArchitectureContractError as error:
            force_standard = isinstance(error, DataStandardParentContractError)
            architecture_id = (
                None
                if plan.data_standard_scope_guard_active
                else _visible_data_standard_fallback_id(
                    visible_ids=set(plan.visible_ids),
                    architecture_list=plan.ranges["architectureList"],
                    force=force_standard,
                    context_values=(plan.original_text, plan.original_name),
                )
            )
            if architecture_id is None:
                classification_phase_calls = (
                    len(state.attempts) - model_call_count_before_classification
                )
                if classification_phase_calls >= snapshot.policy_snapshot.max_phase_calls:
                    if not plan.data_standard_scope_guard_active:
                        raise ArchitectureContractError(
                            "分类阶段实际模型调用预算已耗尽，无法 repair"
                        ) from error
                    architecture_id = _visible_data_standard_fallback_id(
                        visible_ids=set(plan.visible_ids),
                        architecture_list=plan.ranges["architectureList"],
                        force=True,
                        context_values=(plan.original_text, plan.original_name),
                    )
                    if architecture_id is None:
                        raise ArchitectureContractError(
                            "标准正文分类预算耗尽，且候选中不存在通用要求叶节点"
                        ) from error
                    fallback_applied = True
                if architecture_id is None:
                    repair_prompt = _normalize_bounded_analysis_prompt(
                        build_architecture_repair_prompt(
                            parsed or {"architectureId": None},
                            plan.visible_candidates,
                            str(error),
                        )
                    )
                    repaired = self.execute_rag(
                        execution=execution,
                        state=state,
                        rag=rag,
                        operation=AnalysisRagOperation.CLASSIFICATION_REPAIR,
                        prompt=repair_prompt,
                        max_model_calls=snapshot.policy_snapshot.max_model_calls,
                    )
                    try:
                        _, architecture_id = _parse_topk_classification_result(
                            repaired.answer,
                            visible_ids=set(plan.visible_ids),
                            tree_index=plan.tree_index,
                            architecture_list=plan.ranges["architectureList"],
                        )
                    except ArchitectureContractError as repair_error:
                        architecture_id = (
                            _visible_data_standard_fallback_id(
                                visible_ids=set(plan.visible_ids),
                                architecture_list=plan.ranges["architectureList"],
                                force=True,
                                context_values=(plan.original_text, plan.original_name),
                            )
                            if plan.data_standard_scope_guard_active
                            else None
                        )
                        if architecture_id is None:
                            raise ArchitectureContractError(
                                "分类 repair 后仍无法确定类别"
                            ) from repair_error
                        fallback_applied = True
        if architecture_id is None:
            raise ArchitectureContractError("无法确定领域分类")

        identity_profile = _EquipmentIdentityReselectProfile()
        if (
            snapshot.policy_snapshot.identity_reselect_mode != "off"
            and snapshot.policy_snapshot.filename_constraint_mode
            == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        ):
            try:
                identity_profile = _build_equipment_identity_reselect_profile(
                    requested_original_name=snapshot.original_file_name,
                    original_text=plan.original_text,
                    tree_index=plan.tree_index,
                    visible_ids=set(plan.visible_ids),
                    jane_active=plan.jane_profile.active,
                    data_standard_active=plan.data_standard_scope_guard_active,
                )
            except Exception:
                # 身份重选是对已完成分类的受限增强；画像本身失败时保留首轮结果，不能把
                # 普通文档分类升级为系统失败。
                logger.exception(
                    "文件分析身份重选画像构建失败，保留首轮分类: task_id=%s",
                    execution.task_id,
                )
        gate = _decide_identity_reselect_gate(
            architecture_id,
            profile=identity_profile,
            tree_index=plan.tree_index,
        )
        classification_call_count = len(state.attempts) - model_call_count_before_classification
        if gate.should_reselect and classification_call_count == 1:
            candidates = tuple(
                _node_prompt_projection(plan.tree_index.require(node_id))
                for node_id in identity_profile.candidate_ids
            )
            prompt = _normalize_bounded_analysis_prompt(
                build_architecture_reselect_prompt(
                    {"architectureId": architecture_id},
                    {
                        "identifier": identity_profile.identifier,
                        "matchedParentId": identity_profile.target_parent_id,
                        "matchedParentPath": identity_profile.target_parent_path,
                        "evidenceSources": list(identity_profile.evidence_sources),
                    },
                    candidates,
                )
            )
            try:
                reselected = self.execute_rag(
                    execution=execution,
                    state=state,
                    rag=rag,
                    operation=AnalysisRagOperation.IDENTITY_RESELECT,
                    prompt=prompt,
                    max_model_calls=snapshot.policy_snapshot.max_model_calls,
                )
                selected_id = _parse_architecture_reselect_result(
                    reselected.answer,
                    scoped_ids=set(identity_profile.candidate_ids),
                    tree_index=plan.tree_index,
                    architecture_list=plan.ranges["architectureList"],
                )
                if (
                    selected_id is not None
                    and snapshot.policy_snapshot.identity_reselect_mode
                    == ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
                ):
                    architecture_id = selected_id
            except AnalysisRagExecutionError as error:
                if error.outcome_unknown:
                    raise
                logger.warning(
                    "文件分析身份重选查询失败，保留首轮分类: task_id=%s error_code=%s",
                    execution.task_id,
                    error.error_code,
                )
            except ArchitectureContractError:
                logger.warning(
                    "文件分析身份重选结果不合法，保留首轮分类: task_id=%s",
                    execution.task_id,
                )
        if fallback_applied:
            # 通用要求 fallback 已由领域规则确认，不再把它改写为另一条确定性约束结果。
            return architecture_id, True
        return _apply_topk_deterministic_architecture_constraints(
            architecture_id,
            file_name=snapshot.file_name,
            original_name=plan.original_name,
            visible_ids=set(plan.visible_ids),
            tree_index=plan.tree_index,
            architecture_list=plan.ranges["architectureList"],
            filename_constraint_mode=snapshot.policy_snapshot.filename_constraint_mode,
            data_standard_mode=snapshot.policy_snapshot.data_standard_mode,
            data_standard_profile=plan.data_standard_profile,
            jane_profile=plan.jane_profile,
            scope_resolution=plan.scope_resolution,
        ), True

    def resolve_combined_architecture(
        self,
        *,
        execution: AnalysisExecutionRef,
        snapshot: AnalysisTaskInputV1,
        plan: _AnalysisWorkflowPlan,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        parsed_result: dict[str, Any],
        phase_calls_used: int,
    ) -> tuple[int, bool]:
        """解析 combined 结果，并在同一阶段预算内处理分类 repair。

        返回的 bool 表示是否已经完成（或明确跳过）最终确定性约束。数据标准通用要求
        只有在既有强制回退分支中才允许跳过二次约束，其他 fallback 仍须回到统一规则。
        """

        if (
            isinstance(phase_calls_used, bool)
            or not isinstance(phase_calls_used, int)
            or phase_calls_used < 1
        ):
            raise AnalysisApplicationContractError("combined 阶段调用计数无效")

        try:
            if snapshot.policy_snapshot.classification_mode == ANALYSIS_CLASSIFICATION_MODE_LEGACY:
                architecture_id = _resolve_analysis_architecture_id(parsed_result, plan.params)
                # legacy 模式仍须受同一轮可见候选与领域树校验约束。旧执行器会在解析
                # ``architectureId`` 后立即执行此校验，不能把越界 ID 留给后续映射或
                # 确定性约束函数再间接报错。
                architecture_id = _validate_topk_architecture_id(
                    architecture_id,
                    visible_ids=set(plan.visible_ids),
                    tree_index=plan.tree_index,
                    architecture_list=plan.ranges["architectureList"],
                )
            else:
                architecture_id = _validate_topk_architecture_id(
                    parsed_result.get("architectureId"),
                    visible_ids=set(plan.visible_ids),
                    tree_index=plan.tree_index,
                    architecture_list=plan.ranges["architectureList"],
                )
            return architecture_id, False
        except ArchitectureContractError as error:
            force_standard = isinstance(error, DataStandardParentContractError)
            if snapshot.policy_snapshot.classification_mode == ANALYSIS_CLASSIFICATION_MODE_LEGACY:
                fallback = (
                    _general_data_standard_leaf_id(plan.ranges["architectureList"])
                    if force_standard
                    else _match_gjb_architecture_candidate(
                        parsed_result,
                        plan.params,
                        plan.original_text,
                        plan.ranges["architectureList"],
                    )
                )
            else:
                fallback = _visible_data_standard_fallback_id(
                    visible_ids=set(plan.visible_ids),
                    architecture_list=plan.ranges["architectureList"],
                    force=force_standard,
                    context_values=(plan.original_text, plan.original_name),
                )
            if fallback is not None:
                return fallback, False
            if phase_calls_used >= snapshot.policy_snapshot.max_phase_calls:
                if not plan.data_standard_scope_guard_active:
                    raise ArchitectureContractError(
                        "combined 阶段实际模型调用预算已耗尽，无法 architecture repair"
                    ) from error
                fallback = _visible_data_standard_fallback_id(
                    visible_ids=set(plan.visible_ids),
                    architecture_list=plan.ranges["architectureList"],
                    force=True,
                    context_values=(plan.original_text, plan.original_name),
                )
                if fallback is None:
                    raise ArchitectureContractError(
                        "标准正文分类预算耗尽，且候选中不存在通用要求叶节点"
                    ) from error
                return fallback, True
            repair_prompt = _normalize_bounded_analysis_prompt(
                build_architecture_repair_prompt(
                    parsed_result,
                    plan.visible_candidates,
                    str(error),
                )
            )
            repaired = self.execute_rag(
                execution=execution,
                state=state,
                rag=rag,
                operation=AnalysisRagOperation.CLASSIFICATION_REPAIR,
                prompt=repair_prompt,
                max_model_calls=snapshot.policy_snapshot.max_model_calls,
            )
            if snapshot.policy_snapshot.classification_mode == ANALYSIS_CLASSIFICATION_MODE_LEGACY:
                repaired_id = _validate_architecture_repair_result(repaired.answer, plan.params)
                return (
                    _validate_topk_architecture_id(
                        repaired_id,
                        visible_ids=set(plan.visible_ids),
                        tree_index=plan.tree_index,
                        architecture_list=plan.ranges["architectureList"],
                    ),
                    False,
                )
            try:
                _, repaired_id = _parse_topk_classification_result(
                    repaired.answer,
                    visible_ids=set(plan.visible_ids),
                    tree_index=plan.tree_index,
                    architecture_list=plan.ranges["architectureList"],
                )
            except ArchitectureContractError as repair_error:
                fallback = (
                    _visible_data_standard_fallback_id(
                        visible_ids=set(plan.visible_ids),
                        architecture_list=plan.ranges["architectureList"],
                        force=True,
                        context_values=(plan.original_text, plan.original_name),
                    )
                    if plan.data_standard_scope_guard_active
                    else None
                )
                if fallback is None:
                    raise ArchitectureContractError(
                        "分类 repair 后仍无法确定类别"
                    ) from repair_error
                return fallback, True
            return repaired_id, False

    def execute_extraction_with_repair(
        self,
        *,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        prompt: str,
        operation: AnalysisRagOperation,
        phase_name: str,
        max_phase_calls: int,
        max_model_calls: int,
    ) -> dict[str, Any]:
        """执行字段抽取/combined，并只在严格 JSON 失败后使用一次 JSON repair。"""

        result = self.execute_rag(
            execution=execution,
            state=state,
            rag=rag,
            operation=operation,
            prompt=prompt,
            max_model_calls=max_model_calls,
        )
        parsed = _parse_strict_json_object(result.answer)
        if parsed is not None:
            return parsed
        # 1F 既有合同最多两次模型调用；本 Port 把每次真实请求显式记录，避免 Gateway
        # 内部重试绕过审计。当前入口总预算由 execute_rag 再次限制。
        phase_calls = 1
        if phase_calls >= max_phase_calls:
            raise AnalysisContractError(f"{phase_name}阶段实际模型调用预算已耗尽，无法 JSON repair")
        repair_prompt = _normalize_bounded_analysis_prompt(build_json_repair_prompt(result.answer))
        repaired = self.execute_rag(
            execution=execution,
            state=state,
            rag=rag,
            operation=AnalysisRagOperation.EXTRACTION_REPAIR,
            prompt=repair_prompt,
            max_model_calls=max_model_calls,
        )
        parsed = _parse_strict_json_object(repaired.answer)
        if parsed is None:
            raise AnalysisContractError("JSON 修复后仍不是严格 JSON 对象")
        return parsed

    def execute_rag(
        self,
        *,
        execution: AnalysisExecutionRef,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        operation: AnalysisRagOperation,
        prompt: str,
        max_model_calls: int,
    ):
        """发送一次显式模型调用，并把成功或失败证据立刻汇入待审计状态。"""

        if state.session is None:
            raise AnalysisApplicationContractError("RAG 查询前缺少 SessionRef")
        if len(state.attempts) >= max_model_calls:
            raise AnalysisContractError("文件分析实际模型调用超过固定预算")
        attempt_number = state.attempt_counts.get(operation, 0) + 1
        state.attempt_counts[operation] = attempt_number
        request = AnalysisRagRequest(
            execution=execution,
            session=state.session,
            operation=operation,
            prompt=prompt,
            attempt_number=attempt_number,
        )
        state.last_prompt = prompt
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        try:
            result = rag.execute(request)
        except AnalysisRagExecutionError as error:
            state.lifecycle_events.extend(error.lifecycle_events)
            state.attempts.append(
                AnalysisInteractionAttempt(
                    operation=operation,
                    attempt_number=attempt_number,
                    prompt_digest=prompt_digest,
                    raw_response=error.raw_response,
                    sources=error.sources,
                    error_code=error.error_code,
                )
            )
            state.preserve_scene = state.preserve_scene or error.outcome_unknown
            # 错误分支同样可能已经创建 Context/Document 或触发未知请求；必须先把已知
            # 生命周期事实持久化，失败则由 state.preserve_scene 阻止自动 close/delete。
            state.checkpoint_resource_facts()
            raise
        if result.execution != execution or result.operation is not operation or result.attempt_number != attempt_number:
            raise AnalysisApplicationContractError("RAG 查询结果与请求身份不一致")
        self.validate_session_transition(state.session, result.session, execution)
        state.session = result.session
        state.lifecycle_events.extend(result.lifecycle_events)
        state.attempts.append(
            AnalysisInteractionAttempt(
                operation=operation,
                attempt_number=attempt_number,
                prompt_digest=prompt_digest,
                raw_response=result.answer,
                sources=result.sources,
            )
        )
        # 成功结果可能首次带回绑定后的 Document 或新的 Conversation，不能等整段模型
        # 工作流成功后再一次性登记，否则进程中断会失去可恢复的外部身份。
        state.checkpoint_resource_facts()
        return result

    @staticmethod
    def map_result(
        parsed_result: dict[str, Any],
        plan: _AnalysisWorkflowPlan,
        *,
        original_text: str,
        architecture_id: int,
    ) -> dict[str, Any]:
        """把模型 JSON 投影为既有持久化/回调结果，并验证映射类型。"""

        mapped_result = map_analysis_result(
            parsed_result,
            plan.params,
            original_text=original_text,
            resolved_architecture_id=architecture_id,
        )
        if not isinstance(mapped_result, dict):
            raise AnalysisApplicationContractError("map_analysis_result 必须返回 dict")
        return mapped_result

    @staticmethod
    def validate_session_transition(
        previous: AnalysisRagSessionRef,
        current: AnalysisRagSessionRef,
        execution: AnalysisExecutionRef,
    ) -> None:
        """验证同一会话的上下文和已绑定文档身份没有漂移。"""

        if current.execution != execution or current.context_ref != previous.context_ref:
            raise AnalysisApplicationContractError("RAG SessionRef 跨 execution 或 context 漂移")
        if previous.document_bound:
            if not current.document_bound:
                raise AnalysisApplicationContractError("RAG 已绑定文档不能回退为未绑定")
            previous_document = (
                previous.document_ref,
                previous.document_location,
                previous.content_sha256,
                previous.ingested_file_name,
            )
            current_document = (
                current.document_ref,
                current.document_location,
                current.content_sha256,
                current.ingested_file_name,
            )
            if current_document != previous_document:
                raise AnalysisApplicationContractError("RAG SessionRef 文档身份发生漂移")

    @staticmethod
    def returned_rank(plan: _AnalysisWorkflowPlan, architecture_id: int) -> int:
        """返回最终类别在已审计候选中的稳定排名。"""

        for index, candidate in enumerate(plan.visible_candidates, start=1):
            if candidate.get("id") == architecture_id:
                return index
        raise AnalysisContractError("最终 architectureId 不在已审计模型候选中")

    @staticmethod
    def elapsed_ms(started_at: float) -> int:
        """按拆分前规则向上取整执行耗时，避免审计时间投影变化。"""

        return max(0, int((time.perf_counter() - started_at) * 1000.0 + 0.999999))

    def build_plan(
        self,
        snapshot: AnalysisTaskInputV1,
        original_text: str,
    ) -> _AnalysisWorkflowPlan:
        """从受理时冻结的范围和策略计算稳定 RAG 计划，不重读环境变量。"""

        if not isinstance(original_text, str):
            raise TypeError("original_text 必须是 str")
        # 召回审计只统计领域树索引、信号构造与召回本身。文件下载和正文读取已在调用
        # 本方法前完成，不能混入 recall_elapsed_ms，否则会破坏旧链路的指标语义。
        recall_started_at = time.perf_counter()
        params = snapshot.raw_params.to_dict()
        # Worker 必须使用受理时已经冻结的有效范围，而不是重新套用当前默认配置。
        params.update(snapshot.effective_ranges.to_dict())
        ranges = build_effective_analysis_ranges(params)
        tree_index = validate_analysis_architecture_ranges(params)
        original_name = snapshot.original_file_name or snapshot.file_name
        data_standard_profile = _build_data_standard_classification_profile(
            file_name=snapshot.file_name,
            original_name=original_name,
            original_text=original_text,
        )
        data_standard_scope_guard_active = (
            snapshot.policy_snapshot.data_standard_mode == ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
            and snapshot.policy_snapshot.classification_mode != ANALYSIS_CLASSIFICATION_MODE_LEGACY
            and data_standard_profile.identity_confirmed
            and data_standard_profile.document_kind == "standard_body"
        )
        jane_profile = _build_jane_classification_profile(
            file_name=snapshot.file_name,
            original_name=original_name,
            original_text=original_text,
        )
        scope_guard_active = (
            snapshot.policy_snapshot.filename_constraint_mode == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
            and jane_profile.active
            and not data_standard_scope_guard_active
        )
        recall_file_name, recall_original_name = _jane_recall_filename_signals(
            file_name=snapshot.file_name,
            original_name=original_name,
            profile=jane_profile,
            scope_guard_active=scope_guard_active,
        )
        signals: DocumentArchitectureSignals = _build_analysis_architecture_signals(
            file_name=recall_file_name,
            original_name=recall_original_name,
            original_text=original_text,
            title_override=(
                data_standard_profile.title
                if data_standard_scope_guard_active
                else jane_profile.title if scope_guard_active else ""
            ),
        )
        scope_resolution = _ArchitectureScopeResolution()
        if scope_guard_active:
            scope_resolution = _resolve_jane_architecture_scope(
                jane_profile,
                original_text=original_text,
                tree_index=tree_index,
            )
        data_standard_scope_ids: tuple[int, ...] = ()
        data_standard_remark_overrides: dict[int, str] = {}
        if data_standard_scope_guard_active:
            data_standard_scope_ids, data_standard_remark_overrides = _data_standard_candidate_scope(
                tree_index=tree_index,
                architecture_list=ranges["architectureList"],
            )
            if not data_standard_scope_ids:
                raise ArchitectureRecallError("已确认标准正文，但没有可用的数据标准叶节点")

        direct_architecture_id: int | None = None
        recall_decision: ArchitectureRecallDecision | None = None
        if snapshot.policy_snapshot.classification_mode == ANALYSIS_CLASSIFICATION_MODE_LEGACY:
            visible_candidates = tuple(_node_prompt_projection(node) for node in tree_index.nodes)
            visible_ids = frozenset(node.id for node in tree_index.nodes)
            prompt = _normalize_bounded_analysis_prompt(build_file_analysis_prompt(params))
            recall_payload = self.direct_recall_payload(
                tree_index=tree_index,
                signals=signals,
                candidates=tree_index.nodes,
                prompt_chars=len(prompt),
                elapsed_ms=self.elapsed_ms(recall_started_at),
                channel_name="legacy",
            )
        else:
            if len(tree_index.nodes) == 1:
                node = tree_index.nodes[0]
                direct_architecture_id = node.id
                _validate_data_standard_leaf_requirement(node.id, ranges["architectureList"])
                visible_candidates = (_node_prompt_projection(node),)
                visible_ids = frozenset({node.id})
            else:
                recall_decision = recall_architecture_candidates(
                    tree_index,
                    signals,
                    prompt_char_limit=(
                        snapshot.policy_snapshot.classification_prompt_char_limit
                    ),
                    prompt_overhead_chars=0,
                    base_leaf_limit=snapshot.policy_snapshot.base_leaf_limit,
                    parent_candidate_limit=(
                        snapshot.policy_snapshot.parent_candidate_limit
                    ),
                    model_candidate_limit=(
                        snapshot.policy_snapshot.model_candidate_limit
                    ),
                    strong_evidence_only=(
                        snapshot.policy_snapshot.filename_constraint_mode == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                        or data_standard_scope_guard_active
                    ),
                    strong_identity_enabled=(jane_profile.recall_identity_enabled if scope_guard_active else True),
                    jane_title_type_alias_enabled=scope_guard_active,
                    preferred_parent_reasons=(scope_resolution.preferred_parent_reasons if scope_guard_active else None),
                    candidate_scope_ids=(data_standard_scope_ids if data_standard_scope_guard_active else None),
                    candidate_scope_reason=("data-standard-scope" if data_standard_scope_guard_active else ""),
                    candidate_remark_overrides=(data_standard_remark_overrides if data_standard_scope_guard_active else None),
                )
                visible_candidates = tuple(recall_decision.prompt_candidates)
                visible_ids = frozenset(recall_decision.final_candidate_ids)
                if len(visible_candidates) == 1:
                    direct_architecture_id = _validate_topk_architecture_id(
                        visible_candidates[0]["id"],
                        visible_ids=set(visible_ids),
                        tree_index=tree_index,
                        architecture_list=ranges["architectureList"],
                    )
            if direct_architecture_id is not None:
                node = tree_index.require(direct_architecture_id)
                prompt = _normalize_bounded_analysis_prompt(
                    build_file_extraction_prompt(
                        params,
                        resolved_architecture_id=direct_architecture_id,
                        resolved_architecture_path_name=node.semantic_path,
                        resolved_architecture_path_node_names=_architecture_path_keyword_names(tree_index, direct_architecture_id),
                        resolved_architecture_node_type=("leaf" if node.is_leaf else "parent"),
                        include_data_standard_fields=_is_architecture_in_standard_range(
                            direct_architecture_id,
                            ranges["architectureList"],
                            ranges["architectureStandardList"],
                        ),
                    )
                )
            elif snapshot.policy_snapshot.classification_mode == ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE:
                prompt = _normalize_bounded_analysis_prompt(
                    build_data_standard_classification_prompt(
                        params,
                        visible_candidates,
                        standard_context=_data_standard_prompt_context(data_standard_profile),
                    )
                    if data_standard_scope_guard_active
                    else build_architecture_classification_prompt(
                        params,
                        visible_candidates,
                        classification_context=(
                            _jane_classification_prompt_context(jane_profile, scope_resolution)
                            if scope_guard_active else None
                        ),
                    )
                )
            else:
                limited_params = dict(params)
                limited_params["architectureList"] = list(visible_candidates)
                scope_contract = ""
                if data_standard_scope_guard_active:
                    scope_contract = (
                        "\n【数据标准作用域分类补充规则】\n"
                        "服务端已确认该文件是标准正文；只能在下方数据标准叶节点中分类。"
                        "专业类别必须由标准标题或范围支持；普通目录中的“术语和定义”不能"
                        "单独决定分类。不属于五个专业主题时选择“通用要求”。\n"
                        "服务端标准画像："
                        + json.dumps(
                            _data_standard_prompt_context(data_standard_profile),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                elif scope_guard_active:
                    scope_contract = (
                        "\n【简氏作用域分类补充规则】\n"
                        "按全文主要对象和覆盖粒度分类；class 文档的首舰号只标识舰级，"
                        "Fleetlist 成员不能单独决定最终分类；Flight、Block、批次限定词"
                        "优先于基础型号；只有全文主要描述明细类别时才选择明细叶子。\n"
                        "服务端首页画像："
                        + json.dumps(
                            _jane_classification_prompt_context(
                                jane_profile,
                                scope_resolution,
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                prompt = _normalize_bounded_analysis_prompt(
                    build_file_analysis_prompt(limited_params)
                    + "\n【topk_single 受限候选补充合同】\n"
                    + "下方 JSON 是本次完整且唯一可选的模型候选，nodeType 必须保留语义。"
                    + "证据足以支持 leaf 时优先叶子；叶子证据不足但能可靠确定 parent 时，"
                    + "允许返回候选中最深的 parent。此规则替代上文只允许叶子的旧规则。\n"
                    + scope_contract
                    + json.dumps(
                        list(visible_candidates),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            if recall_decision is not None:
                recall_payload = self.recall_payload(recall_decision, len(prompt))
            else:
                nodes = tuple(tree_index.require(item["id"]) for item in visible_candidates)
                recall_payload = self.direct_recall_payload(
                    tree_index=tree_index,
                    signals=signals,
                    candidates=nodes,
                    prompt_chars=len(prompt),
                    elapsed_ms=self.elapsed_ms(recall_started_at),
                    channel_name="direct",
                )
        if len(visible_candidates) > snapshot.policy_snapshot.model_candidate_limit:
            raise ArchitecturePromptBudgetError("领域模型候选数量超过任务策略上限")
        if (
            len(prompt)
            > snapshot.policy_snapshot.classification_prompt_char_limit
        ):
            raise ArchitecturePromptBudgetError(
                f"模型 Prompt 共 {len(prompt)} 字符，超过任务策略上限 "
                f"{snapshot.policy_snapshot.classification_prompt_char_limit}"
            )
        return _AnalysisWorkflowPlan(
            params=params,
            ranges=ranges,
            tree_index=tree_index,
            visible_candidates=tuple(visible_candidates),
            visible_ids=frozenset(visible_ids),
            initial_prompt=prompt,
            direct_architecture_id=direct_architecture_id,
            recall_payload=recall_payload,
            original_name=original_name,
            original_text=original_text,
            data_standard_profile=data_standard_profile,
            data_standard_scope_guard_active=data_standard_scope_guard_active,
            data_standard_scope_ids=data_standard_scope_ids,
            data_standard_remark_overrides=data_standard_remark_overrides,
            jane_profile=jane_profile,
            scope_resolution=scope_resolution,
        )

    @staticmethod
    def recall_payload(
        decision: ArchitectureRecallDecision,
        prompt_chars: int,
    ) -> FrozenJsonObject:
        """把多通道召回决策投影为既有审计载荷。"""

        return FrozenJsonObject.from_mapping(
            {
                "tree_fingerprint": decision.tree_fingerprint,
                "query_digest": decision.query_digest,
                "base_top64": list(decision.base_leaf_ids),
                "final_candidates": list(decision.prompt_candidates),
                "channel_rankings": {
                    ranking.channel: list(ranking.node_ids)
                    for ranking in decision.channel_rankings
                },
                "rrf_scores": {
                    str(key): value for key, value in decision.rrf_scores
                },
                "protected_reasons": {
                    str(key): list(value)
                    for key, value in decision.protected_reasons
                },
                "prompt_chars": prompt_chars,
                "recall_elapsed_ms": int(decision.elapsed_ms + 0.999999),
            },
            name="analysis_recall",
        )

    @staticmethod
    def direct_recall_payload(
        *,
        tree_index: ArchitectureTreeIndex,
        signals: DocumentArchitectureSignals,
        candidates: Any,
        prompt_chars: int,
        elapsed_ms: int,
        channel_name: str,
    ) -> FrozenJsonObject:
        """为直接/legacy 候选路径生成与既有格式完全一致的审计载荷。"""

        nodes = tuple(candidates)
        return FrozenJsonObject.from_mapping(
            {
                "tree_fingerprint": tree_index.fingerprint,
                "query_digest": _architecture_signal_digest(signals),
                "base_top64": [node.id for node in nodes if node.is_leaf][:64],
                "final_candidates": [_node_prompt_projection(node) for node in nodes],
                "channel_rankings": {channel_name: [node.id for node in nodes]},
                "rrf_scores": {},
                "protected_reasons": (
                    {str(nodes[0].id): ["single_candidate"]}
                    if len(nodes) == 1 and channel_name == "direct" else {}
                ),
                "prompt_chars": prompt_chars,
                "recall_elapsed_ms": elapsed_ms,
            },
            name="analysis_direct_recall",
        )
