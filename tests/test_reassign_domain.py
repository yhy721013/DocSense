"""阶段 1E-1：分类节点变更领域 DTO、状态机和补偿纯规则测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from itertools import product
import unittest

from app.modules.reassign.domain import (
    REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
    REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
    REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
    ReassignDocumentCommand,
    ReassignmentBindingState,
    ReassignmentCompensationAction,
    ReassignmentCompensationDecision,
    ReassignmentCompensationError,
    ReassignmentCompensationFacts,
    ReassignmentCompensationMode,
    ReassignmentConcurrencyError,
    ReassignmentContractError,
    ReassignmentDocumentSnapshot,
    ReassignmentDomainValidationError,
    ReassignmentError,
    ReassignmentExternalOutcomeUnknownError,
    ReassignmentFencingError,
    ReassignmentMutationOutcome,
    ReassignmentOperation,
    ReassignmentOperationStatus,
    ReassignmentPublicMessage,
    ReassignmentRawValue,
    ReassignmentRecoveryRequiredError,
    ReassignmentResult,
    ReassignmentResultCategory,
    ReassignmentStateTransitionError,
    ReassignmentStep,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
    allowed_operation_transitions,
    allowed_step_transitions,
    build_step_idempotency_key,
    decide_compensation,
    operation_holds_document_protection,
    operation_releases_document_protection,
    record_step_write_intent,
    transition_operation_status,
    transition_step_state,
)


def _snapshot(
    *,
    doc_path: str | None = "custom-documents/example.pdf",
) -> ReassignmentDocumentSnapshot:
    """创建一个已确认来源分类和外部文档位置的最小文档快照。"""

    return ReassignmentDocumentSnapshot(
        document_row_id=101,
        file_name="example.pdf",
        source_architecture_id=11,
        anything_doc_id="anything-document-101",
        doc_path=doc_path,
        original_file_name="甲方原始文件.pdf",
    )


def _operation(
    *,
    status: ReassignmentOperationStatus = ReassignmentOperationStatus.RESERVED,
    document: ReassignmentDocumentSnapshot | None = None,
    operation_id: str = "operation-001",
    source_raw: object = 11,
    target_raw: object = 12,
) -> ReassignmentOperation:
    """创建可用于状态机和幂等键测试的完整内部 Operation。"""

    document = document or _snapshot()
    return ReassignmentOperation(
        operation_id=operation_id,
        document=document,
        source_architecture_id=document.source_architecture_id,
        source_architecture_raw=source_raw,
        target_architecture_raw=target_raw,
        status=status,
        current_step=ReassignmentStepName.RESERVE_DOCUMENT,
    )


def _step(
    *,
    state: ReassignmentStepState = ReassignmentStepState.PENDING,
    write_intent_recorded: bool | None = None,
) -> ReassignmentStep:
    """创建满足模型不变量的 Step；非 pending 状态默认已有写意图。"""

    if write_intent_recorded is None:
        write_intent_recorded = state is not ReassignmentStepState.PENDING
    return ReassignmentStep(
        operation_id="operation-001",
        step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        idempotency_key="reassign-v1:test-key",
        state=state,
        write_intent_recorded=write_intent_recorded,
    )


def _terminal_evidence_for(
    status: ReassignmentOperationStatus,
) -> ReassignmentTerminalEvidence | None:
    """为会释放文档保护的终态生成与状态严格匹配的测试证据。"""

    kind_by_status = {
        ReassignmentOperationStatus.SUCCEEDED: (
            ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED
        ),
        ReassignmentOperationStatus.FAILED: (
            ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
        ),
        ReassignmentOperationStatus.COMPENSATED: (
            ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED
        ),
    }
    kind = kind_by_status.get(status)
    return ReassignmentTerminalEvidence(kind=kind) if kind is not None else None


class ReassignmentImmutableModelTests(unittest.TestCase):
    """验证领域快照不共享调用方可变输入，也不重新解释公开 ID。"""

    def test_raw_value_deep_freezes_nested_json_without_making_it_a_valid_id(self) -> None:
        new_raw = {"target": [12, {"enabled": False}]}
        frozen = ReassignmentRawValue.from_external_value(new_raw)
        new_raw["target"].append("mutated-after-command")

        self.assertEqual(
            {"target": [12, {"enabled": False}]},
            frozen.to_python(),
        )

    def test_command_preserves_non_strict_target_raw_value_without_normalization(self) -> None:
        command = ReassignDocumentCommand(
            file_name="example.pdf",
            old_architecture_id_raw="011",
            new_architecture_id_raw=False,
            old_architecture_id_query_value=11,
        )

        self.assertEqual("011", command.old_architecture_id_raw.to_python())
        self.assertIs(False, command.new_architecture_id_raw.to_python())
        self.assertEqual(11, command.old_architecture_id_query_value)

    def test_command_accepts_only_frozen_legacy_target_projection_whitelist(self) -> None:
        for target in (False, 12, "12", "  +12  "):
            with self.subTest(target=target):
                command = ReassignDocumentCommand(
                    file_name="example.pdf",
                    old_architecture_id_raw=11,
                    new_architecture_id_raw=target,
                    old_architecture_id_query_value=11,
                )
                self.assertEqual(target, command.new_architecture_id_raw.to_python())

        for target in (True, "12.0", "1e2", "abc", "", 12.0, 12.5, [], {}, 2**63):
            with self.subTest(target=target):
                with self.assertRaises(ReassignmentDomainValidationError):
                    ReassignDocumentCommand(
                        file_name="example.pdf",
                        old_architecture_id_raw=11,
                        new_architecture_id_raw=target,
                        old_architecture_id_query_value=11,
                    )

    def test_command_rejects_missing_or_non_json_raw_values_and_unparsed_query_value(self) -> None:
        cases = (
            {
                "old_architecture_id_raw": None,
                "new_architecture_id_raw": 12,
                "old_architecture_id_query_value": 11,
            },
            {
                "old_architecture_id_raw": 11,
                "new_architecture_id_raw": None,
                "old_architecture_id_query_value": 11,
            },
            {
                "old_architecture_id_raw": 11,
                "new_architecture_id_raw": object(),
                "old_architecture_id_query_value": 11,
            },
            {
                "old_architecture_id_raw": 11,
                "new_architecture_id_raw": 12,
                "old_architecture_id_query_value": "11",
            },
            {
                "old_architecture_id_raw": "999",
                "new_architecture_id_raw": 12,
                "old_architecture_id_query_value": 11,
            },
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(ReassignmentDomainValidationError):
                    ReassignDocumentCommand(file_name="example.pdf", **values)

    def test_document_snapshot_preserves_empty_doc_path_compatibility(self) -> None:
        local_only = _snapshot(doc_path="")
        absent_path = _snapshot(doc_path=None)

        self.assertFalse(local_only.requires_remote_membership_change)
        self.assertFalse(absent_path.requires_remote_membership_change)
        self.assertTrue(_snapshot().requires_remote_membership_change)
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentDocumentSnapshot(
                document_row_id=True,  # type: ignore[arg-type]
                file_name="example.pdf",
                source_architecture_id=11,
            )

    def test_operation_requires_matching_document_identity_and_complete_lease_fact(self) -> None:
        snapshot = _snapshot()
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentOperation(
                operation_id="operation-001",
                document=snapshot,
                source_architecture_id=12,
                source_architecture_raw=11,
                target_architecture_raw=12,
            )
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentOperation(
                operation_id="operation-001",
                document=snapshot,
                source_architecture_id=11,
                source_architecture_raw=11,
                target_architecture_raw=12,
                lease_owner="worker-a",
            )

        operation = ReassignmentOperation(
            operation_id="operation-001",
            document=snapshot,
            source_architecture_id=11,
            source_architecture_raw={"raw": 11},
            target_architecture_raw={"raw": 12},
            lease_owner="worker-a",
            lease_token="lease-001",
            lease_expires_at="2026-07-24T12:00:00Z",
            fencing_token=1,
        )

        self.assertEqual("worker-a", operation.lease_owner)
        self.assertEqual(1, operation.fencing_token)
        self.assertEqual(
            "2026-07-24T12:00:00.000000Z",
            operation.lease_expires_at,
        )
        self.assertIsInstance(
            operation.target_architecture_raw,
            ReassignmentRawValue,
        )
        with self.assertRaises(ReassignmentDomainValidationError):
            replace(operation, lease_expires_at="not-a-timestamp")

    def test_step_requires_persisted_write_intent_after_pending(self) -> None:
        with self.assertRaises(ReassignmentDomainValidationError):
            _step(
                state=ReassignmentStepState.MUTATION_STARTED,
                write_intent_recorded=False,
            )

        step = _step()
        marked = record_step_write_intent(step)
        self.assertTrue(marked.write_intent_recorded)
        self.assertIs(marked, record_step_write_intent(marked))
        with self.assertRaises(FrozenInstanceError):
            marked.error_code = "cannot-change"  # type: ignore[misc]

    def test_result_only_exposes_business_category_and_public_message(self) -> None:
        success = ReassignmentResult(
            category=ReassignmentResultCategory.SUCCEEDED,
            public_message=ReassignmentPublicMessage.SUCCEEDED,
        )
        recovered_failure = ReassignmentResult(
            category=ReassignmentResultCategory.RECOVERY_REQUIRED,
            public_message=ReassignmentPublicMessage.RECOVERY_PENDING,
        )

        self.assertTrue(success.success)
        self.assertEqual("变更成功", success.public_message_text)
        self.assertFalse(recovered_failure.success)
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentResult(
                category=ReassignmentResultCategory.FAILED,
                public_message="供应商密钥与堆栈",  # type: ignore[arg-type]
            )
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentResult(
                category=ReassignmentResultCategory.SUCCEEDED,
                public_message=ReassignmentPublicMessage.COMPENSATION_FAILED,
            )

    def test_step_diagnostic_fields_are_bounded(self) -> None:
        """持久化诊断字段不得接收完整供应商响应或无限堆栈。"""

        cases = (
            {
                "external_reference": "x"
                * (REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH + 1)
            },
            {
                "error_code": "x"
                * (REASSIGNMENT_ERROR_CODE_MAX_LENGTH + 1)
            },
            {
                "error_summary": "x"
                * (REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH + 1)
            },
        )
        for values in cases:
            with self.subTest(field=next(iter(values))):
                with self.assertRaises(ReassignmentDomainValidationError):
                    ReassignmentStep(
                        operation_id="operation-001",
                        step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                        idempotency_key="reassign-v1:test-key",
                        **values,
                    )

    def test_fixed_step_names_cover_the_full_saga_sequence(self) -> None:
        self.assertEqual(
            (
                "reserve_document",
                "detach_source_document",
                "prepare_target_workspace",
                "attach_target_document",
                "commit_local_architecture",
                "compensate_target_document",
                "compensate_source_document",
                "finalize_operation",
            ),
            tuple(item.value for item in ReassignmentStepName),
        )


class ReassignmentOperationStateMachineTests(unittest.TestCase):
    """覆盖每一个 Operation 状态的全部合法和非法普通转换。"""

    def test_all_normal_operation_state_transitions_are_explicit(self) -> None:
        expected = {
            ReassignmentOperationStatus.RESERVED: {
                ReassignmentOperationStatus.RUNNING,
                ReassignmentOperationStatus.FAILED,
                ReassignmentOperationStatus.RECOVERY_REQUIRED,
            },
            ReassignmentOperationStatus.RUNNING: {
                ReassignmentOperationStatus.SUCCEEDED,
                ReassignmentOperationStatus.FAILED,
                ReassignmentOperationStatus.COMPENSATING,
                ReassignmentOperationStatus.RECOVERY_REQUIRED,
            },
            ReassignmentOperationStatus.COMPENSATING: {
                ReassignmentOperationStatus.COMPENSATED,
                ReassignmentOperationStatus.RECOVERY_REQUIRED,
            },
            ReassignmentOperationStatus.COMPENSATED: set(),
            ReassignmentOperationStatus.FAILED: set(),
            ReassignmentOperationStatus.RECOVERY_REQUIRED: set(),
            ReassignmentOperationStatus.SUCCEEDED: set(),
        }
        for current_status, expected_targets in expected.items():
            operation = _operation(status=current_status)
            self.assertEqual(
                frozenset(expected_targets),
                allowed_operation_transitions(current_status),
            )
            for next_status in ReassignmentOperationStatus:
                with self.subTest(
                    current=current_status.value,
                    next=next_status.value,
                ):
                    if next_status in expected_targets:
                        transitioned = transition_operation_status(
                            operation,
                            next_status,
                            terminal_evidence=_terminal_evidence_for(next_status),
                        )
                        self.assertEqual(next_status, transitioned.status)
                        self.assertEqual(operation.operation_id, transitioned.operation_id)
                    else:
                        with self.assertRaises(ReassignmentStateTransitionError):
                            transition_operation_status(operation, next_status)

    def test_recovery_required_requires_explicit_recovery_authorization(self) -> None:
        operation = _operation(
            status=ReassignmentOperationStatus.RECOVERY_REQUIRED
        )
        expected_targets = {
            ReassignmentOperationStatus.RUNNING,
            ReassignmentOperationStatus.COMPENSATING,
            ReassignmentOperationStatus.SUCCEEDED,
            ReassignmentOperationStatus.COMPENSATED,
            ReassignmentOperationStatus.FAILED,
        }

        self.assertEqual(
            frozenset(expected_targets),
            allowed_operation_transitions(
                operation.status,
                recovery_authorized=True,
            ),
        )
        for target in expected_targets:
            with self.subTest(target=target.value):
                self.assertEqual(
                    target,
                    transition_operation_status(
                        operation,
                        target,
                        recovery_authorized=True,
                        terminal_evidence=_terminal_evidence_for(target),
                    ).status,
                )
        with self.assertRaises(ReassignmentStateTransitionError):
            transition_operation_status(
                operation,
                ReassignmentOperationStatus.RESERVED,
                recovery_authorized=True,
            )

    def test_terminal_operation_states_require_matching_evidence(self) -> None:
        """不能只传目标枚举就释放同文档保护。"""

        operation = _operation(status=ReassignmentOperationStatus.RUNNING)
        for target in (
            ReassignmentOperationStatus.SUCCEEDED,
            ReassignmentOperationStatus.FAILED,
        ):
            with self.subTest(target=target.value):
                with self.assertRaises(ReassignmentStateTransitionError):
                    transition_operation_status(operation, target)
                with self.assertRaises(ReassignmentStateTransitionError):
                    transition_operation_status(
                        operation,
                        target,
                        terminal_evidence=ReassignmentTerminalEvidence(
                            kind=(
                                ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED
                            )
                        ),
                    )
        with self.assertRaises(ReassignmentStateTransitionError):
            transition_operation_status(
                operation,
                ReassignmentOperationStatus.RECOVERY_REQUIRED,
                terminal_evidence=ReassignmentTerminalEvidence(
                    kind=(
                        ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
                    )
                ),
            )

    def test_document_protection_statuses_match_recovery_policy(self) -> None:
        protecting = {
            ReassignmentOperationStatus.RESERVED,
            ReassignmentOperationStatus.RUNNING,
            ReassignmentOperationStatus.COMPENSATING,
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
        }
        for status in ReassignmentOperationStatus:
            with self.subTest(status=status.value):
                self.assertEqual(
                    status in protecting,
                    operation_holds_document_protection(status),
                )
                self.assertEqual(
                    status not in protecting,
                    operation_releases_document_protection(status),
                )


class ReassignmentStepStateMachineTests(unittest.TestCase):
    """覆盖步骤写意图门禁、普通转换和受控恢复转换。"""

    def test_mutation_cannot_start_before_write_intent_is_persisted(self) -> None:
        with self.assertRaises(ReassignmentStateTransitionError):
            transition_step_state(
                _step(),
                ReassignmentStepState.MUTATION_STARTED,
            )

        marked = record_step_write_intent(_step())
        started = transition_step_state(
            marked,
            ReassignmentStepState.MUTATION_STARTED,
        )
        self.assertEqual(ReassignmentStepState.MUTATION_STARTED, started.state)

    def test_all_normal_step_state_transitions_are_explicit(self) -> None:
        expected = {
            ReassignmentStepState.PENDING: {
                ReassignmentStepState.MUTATION_STARTED,
            },
            ReassignmentStepState.MUTATION_STARTED: {
                ReassignmentStepState.SUCCEEDED,
                ReassignmentStepState.KNOWN_FAILED,
                ReassignmentStepState.OUTCOME_UNKNOWN,
            },
            ReassignmentStepState.SUCCEEDED: set(),
            ReassignmentStepState.KNOWN_FAILED: set(),
            ReassignmentStepState.OUTCOME_UNKNOWN: set(),
        }
        for current_state, expected_targets in expected.items():
            step = _step(
                state=current_state,
                write_intent_recorded=True,
            )
            self.assertEqual(
                frozenset(expected_targets),
                allowed_step_transitions(current_state),
            )
            for next_state in ReassignmentStepState:
                with self.subTest(current=current_state.value, next=next_state.value):
                    if next_state in expected_targets:
                        transitioned = transition_step_state(step, next_state)
                        self.assertEqual(next_state, transitioned.state)
                    else:
                        with self.assertRaises(ReassignmentStateTransitionError):
                            transition_step_state(step, next_state)

    def test_unknown_steps_require_recovery_authorization(self) -> None:
        cases = {
            ReassignmentStepState.OUTCOME_UNKNOWN: {
                ReassignmentStepState.SUCCEEDED,
                ReassignmentStepState.KNOWN_FAILED,
            },
            ReassignmentStepState.KNOWN_FAILED: {
                ReassignmentStepState.MUTATION_STARTED,
            },
        }
        for current_state, targets in cases.items():
            step = _step(state=current_state)
            with self.subTest(current=current_state.value):
                self.assertEqual(
                    frozenset(targets),
                    allowed_step_transitions(
                        current_state,
                        recovery_authorized=True,
                    ),
                )
            for target in targets:
                with self.subTest(current=current_state.value, target=target.value):
                    self.assertEqual(
                        target,
                        transition_step_state(
                            step,
                            target,
                            recovery_authorized=True,
                        ).state,
                    )
                    with self.assertRaises(ReassignmentStateTransitionError):
                        transition_step_state(step, target)


class ReassignmentCompensationRuleTests(unittest.TestCase):
    """穷举三个已确认副作用的组合，确保未知结果和固定动作顺序不会回归。"""

    def test_all_compensation_fact_combinations_follow_conservative_policy(self) -> None:
        for source_detach, target_attach, local_commit in product(
            ReassignmentMutationOutcome,
            repeat=3,
        ):
            target_binding_state = (
                ReassignmentBindingState.CONFIRMED_PRESENT
                if (
                    local_commit is ReassignmentMutationOutcome.CONFIRMED_EFFECT
                    or target_attach is ReassignmentMutationOutcome.CONFIRMED_EFFECT
                )
                else ReassignmentBindingState.CONFIRMED_ABSENT
            )
            source_binding_state = (
                ReassignmentBindingState.OUTCOME_UNKNOWN
                if source_detach is ReassignmentMutationOutcome.OUTCOME_UNKNOWN
                else (
                    ReassignmentBindingState.CONFIRMED_ABSENT
                    if source_detach
                    is ReassignmentMutationOutcome.CONFIRMED_EFFECT
                    else ReassignmentBindingState.NOT_APPLICABLE
                )
            )
            facts = ReassignmentCompensationFacts(
                source_detach_outcome=source_detach,
                target_attach_outcome=target_attach,
                local_commit_outcome=local_commit,
                source_binding_state=source_binding_state,
                target_binding_state=target_binding_state,
            )
            decision = decide_compensation(facts)
            with self.subTest(
                source=source_detach.value,
                target=target_attach.value,
                local=local_commit.value,
            ):
                if ReassignmentMutationOutcome.OUTCOME_UNKNOWN in (
                    source_detach,
                    target_attach,
                    local_commit,
                ):
                    self.assertEqual(
                        ReassignmentCompensationMode.RECOVERY_REQUIRED,
                        decision.mode,
                    )
                    self.assertEqual((), decision.actions)
                elif local_commit is ReassignmentMutationOutcome.CONFIRMED_EFFECT:
                    self.assertEqual(
                        ReassignmentCompensationMode.PRESERVE_CONFIRMED_LOCAL_COMMIT,
                        decision.mode,
                    )
                    self.assertEqual((), decision.actions)
                else:
                    expected_actions: tuple[ReassignmentCompensationAction, ...] = ()
                    if target_attach is ReassignmentMutationOutcome.CONFIRMED_EFFECT:
                        expected_actions += (
                            ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT,
                        )
                    if source_detach is ReassignmentMutationOutcome.CONFIRMED_EFFECT:
                        expected_actions += (
                            ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT,
                        )
                    if expected_actions:
                        self.assertEqual(
                            ReassignmentCompensationMode.COMPENSATE,
                            decision.mode,
                        )
                        self.assertEqual(expected_actions, decision.actions)
                    else:
                        self.assertEqual(
                            ReassignmentCompensationMode.NO_COMPENSATION_NEEDED,
                            decision.mode,
                        )
                        self.assertEqual((), decision.actions)

    def test_confirmed_local_commit_requires_consistent_target_binding(self) -> None:
        """本地 CAS 生效但目标文档缺失或未知时必须保留现场等待恢复。"""

        for target_binding_state in (
            ReassignmentBindingState.CONFIRMED_ABSENT,
            ReassignmentBindingState.OUTCOME_UNKNOWN,
        ):
            with self.subTest(target_binding_state=target_binding_state.value):
                decision = decide_compensation(
                    ReassignmentCompensationFacts(
                        source_detach_outcome=(
                            ReassignmentMutationOutcome.CONFIRMED_EFFECT
                        ),
                        target_attach_outcome=(
                            ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
                        ),
                        local_commit_outcome=(
                            ReassignmentMutationOutcome.CONFIRMED_EFFECT
                        ),
                        source_binding_state=(
                            ReassignmentBindingState.CONFIRMED_ABSENT
                        ),
                        target_binding_state=target_binding_state,
                    )
                )
                self.assertEqual(
                    ReassignmentCompensationMode.RECOVERY_REQUIRED,
                    decision.mode,
                )

    def test_local_only_commit_does_not_require_remote_binding(self) -> None:
        """空 doc_path 路径以 not_applicable 明确表达无需远端成员关系。"""

        decision = decide_compensation(
            ReassignmentCompensationFacts(
                source_detach_outcome=ReassignmentMutationOutcome.NOT_STARTED,
                target_attach_outcome=ReassignmentMutationOutcome.NOT_STARTED,
                local_commit_outcome=ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                remote_membership_required=False,
                source_binding_state=(
                    ReassignmentBindingState.NOT_APPLICABLE
                ),
                target_binding_state=(
                    ReassignmentBindingState.NOT_APPLICABLE
                ),
            )
        )
        self.assertEqual(
            ReassignmentCompensationMode.PRESERVE_CONFIRMED_LOCAL_COMMIT,
            decision.mode,
        )

    def test_confirmed_local_commit_rejects_unexpected_source_membership(self) -> None:
        """来源映射未跳过时，旧 workspace 仍含文档不能被宣称为一致成功。"""

        decision = decide_compensation(
            ReassignmentCompensationFacts(
                source_detach_outcome=(
                    ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
                ),
                target_attach_outcome=(
                    ReassignmentMutationOutcome.CONFIRMED_EFFECT
                ),
                local_commit_outcome=(
                    ReassignmentMutationOutcome.CONFIRMED_EFFECT
                ),
                source_binding_state=(
                    ReassignmentBindingState.CONFIRMED_PRESENT
                ),
                target_binding_state=(
                    ReassignmentBindingState.CONFIRMED_PRESENT
                ),
            )
        )
        self.assertEqual(
            ReassignmentCompensationMode.RECOVERY_REQUIRED,
            decision.mode,
        )

    def test_compensation_decision_rejects_reversed_or_irrelevant_actions(self) -> None:
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentCompensationDecision(
                mode=ReassignmentCompensationMode.COMPENSATE,
                actions=(
                    ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT,
                    ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT,
                ),
            )
        with self.assertRaises(ReassignmentDomainValidationError):
            ReassignmentCompensationDecision(
                mode=ReassignmentCompensationMode.RECOVERY_REQUIRED,
                actions=(ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT,),
            )


class ReassignmentIdempotencyAndErrorTests(unittest.TestCase):
    """验证步骤幂等身份覆盖真实定位事实，错误分类可供后续日志和恢复使用。"""

    def test_idempotency_key_uses_operation_step_location_and_raw_categories(self) -> None:
        operation = _operation()
        base_key = build_step_idempotency_key(
            operation,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        )

        self.assertEqual(
            base_key,
            build_step_idempotency_key(
                replace(operation, status=ReassignmentOperationStatus.RUNNING),
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            ),
        )
        self.assertEqual(
            base_key,
            build_step_idempotency_key(
                replace(
                    operation,
                    document=replace(operation.document, file_name="renamed.pdf"),
                ),
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            ),
        )

        changed_operations = (
            replace(operation, operation_id="operation-002"),
            replace(
                operation,
                document=replace(
                    operation.document,
                    doc_path="custom-documents/other.pdf",
                ),
            ),
            replace(operation, source_architecture_raw="11"),
            replace(operation, target_architecture_raw="12"),
        )
        for changed in changed_operations:
            with self.subTest(changed=changed):
                self.assertNotEqual(
                    base_key,
                    build_step_idempotency_key(
                        changed,
                        ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                    ),
                )
        self.assertNotEqual(
            base_key,
            build_step_idempotency_key(
                operation,
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            ),
        )
        self.assertTrue(base_key.startswith("reassign-v1:"))

    def test_error_categories_have_stable_codes_and_stages(self) -> None:
        expected = {
            ReassignmentDomainValidationError: (
                "reassignment_domain_validation",
                "domain",
            ),
            ReassignmentStateTransitionError: (
                "reassignment_invalid_state_transition",
                "state_machine",
            ),
            ReassignmentContractError: (
                "reassignment_contract_error",
                "contract",
            ),
            ReassignmentConcurrencyError: (
                "reassignment_concurrency_error",
                "concurrency",
            ),
            ReassignmentFencingError: (
                "reassignment_fencing_error",
                "fencing",
            ),
            ReassignmentExternalOutcomeUnknownError: (
                "reassignment_external_outcome_unknown",
                "external_outcome",
            ),
            ReassignmentCompensationError: (
                "reassignment_compensation_error",
                "compensation",
            ),
            ReassignmentRecoveryRequiredError: (
                "reassignment_recovery_required",
                "recovery",
            ),
        }
        for error_type, (code, stage) in expected.items():
            with self.subTest(error_type=error_type.__name__):
                error = error_type("测试错误")
                self.assertIsInstance(error, ReassignmentError)
                self.assertEqual(code, error.code)
                self.assertEqual(stage, error.stage)


if __name__ == "__main__":
    unittest.main()
