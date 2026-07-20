"""武器谱 Retrieval/Extraction/Guidance/Translation/Audit 严格 Fake。"""

from __future__ import annotations

from threading import RLock

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.domain import AUXILIARY_GUIDANCE_NONE
from app.modules.weaponry.domain import (
    EVIDENCE_SCORE_MODE_RANK,
    EVIDENCE_SCORE_MODE_SCORE,
)
from app.modules.weaponry.ports import (
    AuxiliaryGuidanceOutcome,
    AuxiliaryGuidanceRequest,
    AuxiliaryGuidanceResult,
    CompleteWeaponryInteraction,
    EvidenceExtractionRequest,
    ExtractionAnswer,
    IdempotentOperationResult,
    OpenTargetEvidenceScope,
    ReserveWeaponryInteraction,
    SearchTargetEvidence,
    TargetEvidenceScope,
    TargetEvidenceSearchResult,
    WeaponryAuditReceipt,
    WeaponryAuditReservation,
    WeaponryAuditReserveOutcome,
    WeaponryAuditReserveResult,
    WeaponryPortStateError,
    WeaponrySourceBoundaryError,
    WeaponryTranslationRequest,
    WeaponryTranslationResult,
    validate_auxiliary_result_policy,
)

from .weaponry_support import WeaponryInvocationRecorder


def _raise_configured(errors: dict[str, BaseException], key: str) -> None:
    error = errors.get(key)
    if error is not None:
        raise error


class FakeTargetEvidenceRetrievalPort:
    """严格验证 scope 所有权、文档边界和 Candidate 数量的检索替身。"""

    def __init__(
        self,
        recorder: WeaponryInvocationRecorder | None = None,
        *,
        enforce_call_order: bool = True,
    ) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.enforce_call_order = enforce_call_order
        self.open_errors: dict[str, BaseException] = {}
        self.forced_open_results: dict[str, object] = {}
        self.search_errors: dict[str, BaseException] = {}
        self.search_results: dict[str, TargetEvidenceSearchResult] = {}
        self.close_error_codes: dict[str, str] = {}
        self.close_errors: dict[str, BaseException] = {}
        self._lock = RLock()
        self._scopes_by_task: dict[TaskId, TargetEvidenceScope] = {}
        self._open_commands: dict[TaskId, OpenTargetEvidenceScope] = {}
        self._closed_scope_refs: set[str] = set()
        self.search_calls: list[SearchTargetEvidence] = []

    @property
    def scopes(self) -> tuple[TargetEvidenceScope, ...]:
        with self._lock:
            return tuple(self._scopes_by_task.values())

    @property
    def active_scope_refs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                scope.scope_ref
                for scope in self._scopes_by_task.values()
                if scope.scope_ref not in self._closed_scope_refs
            )

    def open_scope(self, command: OpenTargetEvidenceScope) -> TargetEvidenceScope:
        if not isinstance(command, OpenTargetEvidenceScope):
            raise TypeError("command 必须是 OpenTargetEvidenceScope")
        task_value = command.task_id.value
        self.recorder.record("retrieval.open", task_id=task_value)
        _raise_configured(self.open_errors, task_value)
        if task_value in self.forced_open_results:
            return self.forced_open_results[task_value]  # type: ignore[return-value]
        with self._lock:
            existing = self._scopes_by_task.get(command.task_id)
            if existing is not None and existing.scope_ref not in self._closed_scope_refs:
                raise WeaponryPortStateError(
                    "retrieval_scope_already_open",
                    "同一 task_id 已存在活动检索范围",
                )
            scope = TargetEvidenceScope(
                task_id=command.task_id,
                scope_ref=f"fake-retrieval-scope:{task_value}",
                allowed_document_keys=tuple(
                    item.document_key for item in command.document_scope.documents
                ),
                selection_profile_id=command.policy.profile_id,
                provider_fingerprint=command.policy.provider_fingerprint,
                embedding_fingerprint=command.policy.embedding_fingerprint,
            )
            self._scopes_by_task[command.task_id] = scope
            self._open_commands[command.task_id] = command
            self._closed_scope_refs.discard(scope.scope_ref)
            return scope

    def search_target(
        self,
        command: SearchTargetEvidence,
    ) -> TargetEvidenceSearchResult:
        if not isinstance(command, SearchTargetEvidence):
            raise TypeError("command 必须是 SearchTargetEvidence")
        if self.enforce_call_order:
            if not self.recorder.contains(
                "audit.reserve",
                task_id=command.call.task_id.value,
                call_id=command.call.call_id,
            ):
                raise WeaponryPortStateError(
                    "audit_reservation_required",
                    "目标检索前必须成功预留审计",
                )
            if not self.recorder.contains(
                "resource.register",
                task_id=command.call.task_id.value,
                call_id="retrieval_scope",
            ):
                raise WeaponryPortStateError(
                    "retrieval_scope_registration_required",
                    "检索范围创建后必须先登记资源",
                )
        self.recorder.record(
            "retrieval.search",
            task_id=command.call.task_id.value,
            call_id=command.call.call_id,
        )
        with self._lock:
            known_scope = self._scopes_by_task.get(command.call.task_id)
            if known_scope != command.scope:
                raise WeaponryPortStateError(
                    "retrieval_scope_not_owned",
                    "检索范围不属于当前 task_id",
                )
            if command.scope.scope_ref in self._closed_scope_refs:
                raise WeaponryPortStateError(
                    "retrieval_scope_closed",
                    "检索范围已经关闭",
                )
            self.search_calls.append(command)
            key = command.call.attempt_key
            _raise_configured(self.search_errors, key)
            result = self.search_results.get(key)
            if result is None:
                raise AssertionError(
                    "FakeTargetEvidenceRetrievalPort 收到未配置调用: "
                    f"attempt_key={key}"
                )
            if result.call != command.call or result.scope_ref != command.scope.scope_ref:
                raise AssertionError("Fake 检索结果与请求 call/scope 不一致")
            if (
                result.provider_fingerprint != command.scope.provider_fingerprint
                or result.embedding_fingerprint != command.scope.embedding_fingerprint
            ):
                raise AssertionError("Fake 检索结果运行指纹与 scope 不一致")
            if len(result.candidates) > command.candidate_top_n:
                raise AssertionError("Fake 检索结果超过 candidate_top_n")
            score_presence = tuple(
                item.provider_score_present for item in result.candidates
            )
            if result.score_mode == EVIDENCE_SCORE_MODE_SCORE and not all(
                score_presence
            ):
                raise AssertionError("Fake score 批次包含无分数 Candidate")
            if result.score_mode == EVIDENCE_SCORE_MODE_RANK and any(
                score_presence
            ):
                raise AssertionError("Fake rank 批次包含有分数 Candidate")
            candidate_ids = tuple(item.candidate_id for item in result.candidates)
            if len(set(candidate_ids)) != len(candidate_ids):
                raise AssertionError("Fake 检索结果包含重复 candidate_id")
            allowed = set(command.allowed_document_keys)
            if any(item.document_key not in allowed for item in result.candidates):
                raise WeaponrySourceBoundaryError(
                    "retrieval_source_out_of_scope",
                    "检索 Candidate 超出允许文档范围",
                )
            return result

    def close_scope(self, scope: TargetEvidenceScope) -> IdempotentOperationResult:
        if not isinstance(scope, TargetEvidenceScope):
            raise TypeError("scope 必须是 TargetEvidenceScope")
        self.recorder.record("retrieval.close", task_id=scope.task_id.value)
        with self._lock:
            known_scope = self._scopes_by_task.get(scope.task_id)
            if known_scope != scope:
                raise WeaponryPortStateError(
                    "retrieval_scope_unknown",
                    "不能关闭未登记检索范围",
                )
            if scope.scope_ref in self._closed_scope_refs:
                return IdempotentOperationResult(success=True, already_applied=True)
            _raise_configured(self.close_errors, scope.scope_ref)
            error_code = self.close_error_codes.get(scope.scope_ref, "")
            if error_code:
                return IdempotentOperationResult(
                    success=False,
                    error_code=error_code,
                )
            self._closed_scope_refs.add(scope.scope_ref)
            return IdempotentOperationResult(success=True)


class FakeEvidenceExtractionPort:
    """每个 attempt 创建独立虚拟会话，并强校验 Evidence/来源边界。"""

    def __init__(
        self,
        recorder: WeaponryInvocationRecorder | None = None,
        *,
        enforce_call_order: bool = True,
    ) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.enforce_call_order = enforce_call_order
        self.results: dict[str, ExtractionAnswer] = {}
        self.errors: dict[str, BaseException] = {}
        self._lock = RLock()
        self._used_attempt_keys: set[str] = set()
        self._session_refs: dict[str, str] = {}
        self.calls: list[EvidenceExtractionRequest] = []

    @property
    def session_refs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._session_refs.values())

    def extract(self, request: EvidenceExtractionRequest) -> ExtractionAnswer:
        if not isinstance(request, EvidenceExtractionRequest):
            raise TypeError("request 必须是 EvidenceExtractionRequest")
        key = request.call.attempt_key
        if self.enforce_call_order and not self.recorder.contains(
            "audit.reserve",
            task_id=request.call.task_id.value,
            call_id=request.call.call_id,
        ):
            raise WeaponryPortStateError(
                "audit_reservation_required",
                "Evidence 抽取前必须成功预留审计",
            )
        self.recorder.record(
            "extraction.extract",
            task_id=request.call.task_id.value,
            call_id=request.call.call_id,
        )
        with self._lock:
            if key in self._used_attempt_keys:
                raise WeaponryPortStateError(
                    "extraction_attempt_reused",
                    "同一抽取 attempt 不得复用历史会话",
                )
            self._used_attempt_keys.add(key)
            self.calls.append(request)
            _raise_configured(self.errors, key)
            answer = self.results.get(key)
            if answer is None:
                raise AssertionError(
                    "FakeEvidenceExtractionPort 收到未配置调用: "
                    f"attempt_key={key}"
                )
            self._session_refs[key] = f"fake-source-session:{key}"
            if answer.call != request.call:
                raise AssertionError("Fake 抽取回答与请求 call 不一致")
            expected_ids = tuple(item.candidate_id for item in request.evidence)
            if answer.evidence_ids != expected_ids:
                raise WeaponrySourceBoundaryError(
                    "extraction_evidence_mismatch",
                    "抽取回答 Evidence 身份或顺序与请求不一致",
                )
            expected_id_set = set(expected_ids)
            for source in answer.sources:
                if (
                    source.document_key != request.document.document_key
                    or source.evidence_id not in expected_id_set
                ):
                    raise WeaponrySourceBoundaryError(
                        "extraction_source_out_of_scope",
                        "抽取回答包含越界来源",
                    )
            return answer


class FakeAuxiliaryGuidancePort:
    """严格区分 none 零 I/O 与可选 Provider 调用的辅助语境替身。"""

    def __init__(
        self,
        recorder: WeaponryInvocationRecorder | None = None,
        *,
        enforce_call_order: bool = True,
    ) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.enforce_call_order = enforce_call_order
        self.results: dict[str, AuxiliaryGuidanceResult] = {}
        self.errors: dict[str, BaseException] = {}
        self.calls: list[AuxiliaryGuidanceRequest] = []
        self.provider_io_calls = 0
        self._lock = RLock()

    def load(self, request: AuxiliaryGuidanceRequest) -> AuxiliaryGuidanceResult:
        if not isinstance(request, AuxiliaryGuidanceRequest):
            raise TypeError("request 必须是 AuxiliaryGuidanceRequest")
        key = request.call.attempt_key
        if self.enforce_call_order and not self.recorder.contains(
            "audit.reserve",
            task_id=request.call.task_id.value,
            call_id=request.call.call_id,
        ):
            raise WeaponryPortStateError(
                "audit_reservation_required",
                "辅助语境调用前必须成功预留审计",
            )
        self.recorder.record(
            "guidance.load",
            task_id=request.call.task_id.value,
            call_id=request.call.call_id,
        )
        with self._lock:
            self.calls.append(request)
            _raise_configured(self.errors, key)
            if request.policy.policy_id == AUXILIARY_GUIDANCE_NONE:
                result = self.results.get(
                    key,
                    AuxiliaryGuidanceResult(
                        call=request.call,
                        guidance=(),
                        outcome=AuxiliaryGuidanceOutcome.EMPTY,
                    ),
                )
            else:
                self.provider_io_calls += 1
                result = self.results.get(key)
                if result is None:
                    raise AssertionError(
                        "FakeAuxiliaryGuidancePort 收到未配置 Provider 调用: "
                        f"attempt_key={key}"
                    )
            validate_auxiliary_result_policy(request, result)
            return result


class FakeWeaponryTranslationPort:
    """结果按 attempt 显式配置且不共享跨任务缓存的翻译替身。"""

    def __init__(
        self,
        recorder: WeaponryInvocationRecorder | None = None,
        *,
        enforce_call_order: bool = True,
    ) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.enforce_call_order = enforce_call_order
        self.results: dict[str, WeaponryTranslationResult] = {}
        self.errors: dict[str, BaseException] = {}
        self.calls: list[WeaponryTranslationRequest] = []
        self._lock = RLock()

    def translate(
        self,
        request: WeaponryTranslationRequest,
    ) -> WeaponryTranslationResult:
        if not isinstance(request, WeaponryTranslationRequest):
            raise TypeError("request 必须是 WeaponryTranslationRequest")
        key = request.call.attempt_key
        if self.enforce_call_order and not self.recorder.contains(
            "audit.reserve",
            task_id=request.call.task_id.value,
            call_id=request.call.call_id,
        ):
            raise WeaponryPortStateError(
                "audit_reservation_required",
                "翻译调用前必须成功预留审计",
            )
        self.recorder.record(
            "translation.translate",
            task_id=request.call.task_id.value,
            call_id=request.call.call_id,
        )
        with self._lock:
            self.calls.append(request)
            _raise_configured(self.errors, key)
            result = self.results.get(key)
            if result is None:
                raise AssertionError(
                    "FakeWeaponryTranslationPort 收到未配置调用: "
                    f"attempt_key={key}"
                )
            if result.call != request.call:
                raise AssertionError("Fake 翻译结果与请求 call 不一致")
            return result


class FakeWeaponryInteractionAuditPort:
    """线程安全 reserve/complete Fake；pending 永远不会伪装成成功。"""

    def __init__(self, recorder: WeaponryInvocationRecorder | None = None) -> None:
        self.recorder = recorder or WeaponryInvocationRecorder()
        self.reserve_errors: dict[str, BaseException] = {}
        self.complete_errors: dict[str, BaseException] = {}
        self._lock = RLock()
        self._reserve_commands: dict[str, ReserveWeaponryInteraction] = {}
        self._reservations: dict[str, WeaponryAuditReservation] = {}
        self._complete_commands: dict[str, CompleteWeaponryInteraction] = {}
        self._receipts: dict[str, WeaponryAuditReceipt] = {}

    @property
    def pending(self) -> tuple[WeaponryAuditReservation, ...]:
        with self._lock:
            return tuple(
                reservation
                for key, reservation in self._reservations.items()
                if key not in self._receipts
            )

    @property
    def completions(self) -> tuple[CompleteWeaponryInteraction, ...]:
        """按提交顺序公开只读完成命令，避免测试越过 Fake 边界读取内部字典。"""

        with self._lock:
            return tuple(self._complete_commands.values())

    def reserve(
        self,
        command: ReserveWeaponryInteraction,
    ) -> WeaponryAuditReserveResult:
        if not isinstance(command, ReserveWeaponryInteraction):
            raise TypeError("command 必须是 ReserveWeaponryInteraction")
        key = command.call.attempt_key
        with self._lock:
            _raise_configured(self.reserve_errors, key)
            existing_command = self._reserve_commands.get(key)
            if existing_command is not None:
                if existing_command != command:
                    raise WeaponryPortStateError(
                        "audit_reservation_conflict",
                        "同一 attempt_key 的审计预留内容不一致",
                    )
                reservation = self._reservations[key]
                outcome = (
                    WeaponryAuditReserveOutcome.COMPLETED
                    if key in self._receipts
                    else WeaponryAuditReserveOutcome.PENDING
                )
                self.recorder.record(
                    "audit.reserve",
                    task_id=command.call.task_id.value,
                    call_id=command.call.call_id,
                )
                return WeaponryAuditReserveResult(outcome, reservation)
            reservation = WeaponryAuditReservation(
                reservation_id=f"fake-audit-reservation:{key}",
                business_ref=command.business_ref,
                call=command.call,
            )
            self._reserve_commands[key] = command
            self._reservations[key] = reservation
            self.recorder.record(
                "audit.reserve",
                task_id=command.call.task_id.value,
                call_id=command.call.call_id,
            )
            return WeaponryAuditReserveResult(
                WeaponryAuditReserveOutcome.RESERVED,
                reservation,
            )

    def complete(
        self,
        command: CompleteWeaponryInteraction,
    ) -> WeaponryAuditReceipt:
        if not isinstance(command, CompleteWeaponryInteraction):
            raise TypeError("command 必须是 CompleteWeaponryInteraction")
        key = command.reservation.call.attempt_key
        with self._lock:
            known = self._reservations.get(key)
            if known != command.reservation:
                raise WeaponryPortStateError(
                    "audit_reservation_missing",
                    "审计完成前必须存在匹配的 pending 预留",
                )
            _raise_configured(self.complete_errors, key)
            existing = self._complete_commands.get(key)
            if existing is not None:
                if existing != command:
                    raise WeaponryPortStateError(
                        "audit_completion_conflict",
                        "同一 attempt_key 的审计完成内容不一致",
                    )
                receipt = self._receipts[key]
                self.recorder.record(
                    "audit.complete",
                    task_id=command.reservation.call.task_id.value,
                    call_id=command.reservation.call.call_id,
                )
                return receipt
            receipt = WeaponryAuditReceipt(
                audit_id=f"fake-audit:{key}",
                reservation_id=command.reservation.reservation_id,
                task_id=command.reservation.call.task_id,
                attempt_key=key,
            )
            self._complete_commands[key] = command
            self._receipts[key] = receipt
            self.recorder.record(
                "audit.complete",
                task_id=command.reservation.call.task_id.value,
                call_id=command.reservation.call.call_id,
            )
            return receipt

    def list_pending(
        self,
        task_id: TaskId,
        *,
        limit: int,
    ) -> tuple[WeaponryAuditReservation, ...]:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        self.recorder.record("audit.list_pending", task_id=task_id.value)
        with self._lock:
            candidates = tuple(
                reservation
                for reservation in self.pending
                if reservation.call.task_id == task_id
            )
            return tuple(
                sorted(candidates, key=lambda item: item.call.attempt_key)[:limit]
            )


__all__ = [
    "FakeAuxiliaryGuidancePort",
    "FakeEvidenceExtractionPort",
    "FakeTargetEvidenceRetrievalPort",
    "FakeWeaponryInteractionAuditPort",
    "FakeWeaponryTranslationPort",
]
