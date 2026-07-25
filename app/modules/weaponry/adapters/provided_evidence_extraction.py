"""只使用请求 Selected Evidence 的 AnythingLLM 抽取 Adapter。"""

from __future__ import annotations

import hashlib
import json
import logging

from app.integrations.anythingllm import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
    AnythingLLMTransportError,
)
from app.modules.weaponry.domain import EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1
from app.modules.weaponry.ports import (
    EvidenceExtractionRequest,
    ExtractionAnswer,
    ExtractionValidationOutcome,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryCreationIntent,
    WeaponryCreationIntentKind,
    WeaponryCreationIntentState,
    WeaponryPortStateError,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponrySourceBoundaryError,
)

from .anythingllm_clients import WeaponryAnythingLLMClientFactoryProtocol
from .resource_registration import WeaponryCreatedResourceRegistrarProtocol


logger = logging.getLogger(__name__)


class AnythingLLMProvidedEvidenceExtractionAdapter:
    """每个来源级 attempt 创建全新空 workspace/thread，并只发送本次 rows。

    该实现固定使用 AnythingLLM ``chat`` 模式和零绑定文档 workspace。它不会访问任务检索
    workspace，也不会复用父 Thread。供应商若返回任意 RAG source，说明上下文边界已经被
    污染，整次非空回答立即作废。外部 workspace/thread 创建后通过 Registrar 立即持久登记，
    最终幂等清理由资源恢复用例统一执行；1D-7 负责阶段关闭验收。
    """

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        resource_registrar: WeaponryCreatedResourceRegistrarProtocol,
        *,
        model_fingerprint: str,
        user_id: int | None = 1,
    ) -> None:
        if not isinstance(client_factory, WeaponryAnythingLLMClientFactoryProtocol):
            raise TypeError("client_factory 必须实现武器谱 AnythingLLM Client 工厂")
        if not isinstance(resource_registrar, WeaponryCreatedResourceRegistrarProtocol):
            raise TypeError("resource_registrar 必须实现创建后资源登记契约")
        if not isinstance(model_fingerprint, str) or not model_fingerprint.strip():
            raise ValueError("model_fingerprint 必须是非空 str")
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._client_factory = client_factory
        self._resource_registrar = resource_registrar
        self._model_fingerprint = model_fingerprint.strip()
        self._user_id = user_id

    def extract(self, request: EvidenceExtractionRequest) -> ExtractionAnswer:
        if not isinstance(request, EvidenceExtractionRequest):
            raise TypeError("request 必须是 EvidenceExtractionRequest")
        if request.model_fingerprint != self._model_fingerprint:
            raise WeaponryPortStateError(
                "extraction_model_fingerprint_mismatch",
                "实际抽取模型指纹与 execution 快照不一致",
            )
        if request.context_strategy != EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1:
            # 当前供应商能力尚未真实证明 evidence-only RAG 的来源标记协议。宁可明确
            # 停止，也不能悄悄回退到任务/分类 workspace 或把两种策略伪装成同一路径。
            raise WeaponryPortStateError(
                "extraction_context_strategy_not_installed",
                "当前只安装 provided_evidence_model_v1 抽取策略",
            )
        self._resource_registrar.ensure_ready(request.call.task_id)

        evidence_digest = self._evidence_digest(request)
        prompt = self._render_provider_prompt(request, evidence_digest)
        external_mutation_started = False
        try:
            with self._client_factory.create() as clients:
                workspace_name = self._workspace_name(request.call.attempt_key)
                workspace_intent = WeaponryCreationIntent(
                    task_id=request.call.task_id,
                    intent_id=f"{request.call.attempt_key}:extraction-workspace",
                    kind=WeaponryCreationIntentKind.EXTRACTION_WORKSPACE,
                    expected_name=workspace_name,
                    identity_digest=self._extraction_identity_digest(
                        request,
                        evidence_digest,
                    ),
                    document_key=request.document.document_key,
                    call_id=request.call.call_id,
                )
                workspace_reservation = self._resource_registrar.reserve_creation(
                    workspace_intent
                )
                workspace_intent = workspace_reservation.intent
                if workspace_intent.state is WeaponryCreationIntentState.QUARANTINED:
                    raise WeaponryPortStateError(
                        "extraction_workspace_creation_quarantined",
                        "抽取 workspace 创建意图已经隔离，禁止再次创建",
                    )
                if workspace_intent.state is WeaponryCreationIntentState.RESOLVED:
                    raise WeaponryPortStateError(
                        "extraction_workspace_creation_resolved",
                        "抽取 workspace 创建意图已经完成，禁止重复执行",
                    )
                if workspace_intent.state is WeaponryCreationIntentState.RECOVERING:
                    raise WeaponryPortStateError(
                        "extraction_workspace_creation_recovering",
                        "抽取 workspace 创建意图正由恢复器接管，禁止 Worker 继续修改现场",
                    )
                # 供应商写请求从发出前即进入可能不确定区间；不能依赖响应中的 slug 才
                # 标记副作用开始，否则创建超时会被错误归为“明确未创建”。
                if workspace_reservation.created:
                    external_mutation_started = True
                    try:
                        workspace = clients.workspaces.create_workspace(
                            workspace_name,
                            user_id=self._user_id,
                        )
                    except Exception as create_error:
                        if not self._write_outcome_may_be_unknown(create_error):
                            self._resource_registrar.quarantine_creation(
                                workspace_intent,
                                error_code="extraction_workspace_create_failed",
                            )
                            raise
                        workspace = self._reconcile_workspace_creation(
                            clients,
                            workspace_intent,
                            cause=create_error,
                        )
                else:
                    workspace = self._reconcile_workspace_creation(
                        clients,
                        workspace_intent,
                        cause=None,
                    )
                try:
                    self._resource_registrar.register_created(
                        task_id=request.call.task_id,
                        resource_id=f"extraction-context:{workspace.slug}",
                        kind=WeaponryResourceKind.EXTRACTION_CONTEXT,
                        external_ref=workspace.slug,
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key=(
                            f"{request.call.attempt_key}:extraction-context"
                        ),
                        document_key=request.document.document_key,
                        call_id=request.call.call_id,
                    )
                except Exception as registration_error:
                    # 本地记录尚未成功时，后续恢复无法定位该 workspace，因此必须在同一
                    # Transport 内立即补偿；补偿结果无法确认时必须按 outcome_unknown 上报，
                    # 不能继续暴露为普通登记状态错误。
                    compensation_failed = False
                    try:
                        clients.workspaces.delete_workspace(
                            workspace.slug,
                            user_id=self._user_id,
                        )
                        self._resource_registrar.quarantine_creation(
                            workspace_intent,
                            error_code="extraction_workspace_compensated",
                        )
                    except Exception:
                        logger.critical(
                            "武器谱抽取 workspace 登记失败且补偿删除失败: task_id=%s",
                            request.call.task_id.value,
                            exc_info=True,
                        )
                        compensation_failed = True
                    if compensation_failed:
                        raise WeaponryExternalOperationError(
                            "extraction_context_untracked_resource_unknown",
                            "抽取 workspace 登记失败且补偿结果未知",
                            outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                        ) from registration_error
                    raise
                self._resource_registrar.resolve_creation(
                    workspace_intent,
                    external_ref=workspace.slug,
                )
                self._require_empty_workspace(clients, workspace.slug, stage="before")

                thread_name = self._thread_name(request.call.attempt_key)
                thread_intent = WeaponryCreationIntent(
                    task_id=request.call.task_id,
                    intent_id=f"{request.call.attempt_key}:source-thread",
                    kind=WeaponryCreationIntentKind.SOURCE_THREAD,
                    expected_name=thread_name,
                    identity_digest=self._thread_identity_digest(
                        request,
                        workspace.slug,
                    ),
                    parent_external_ref=workspace.slug,
                    document_key=request.document.document_key,
                    call_id=request.call.call_id,
                )
                thread_reservation = self._resource_registrar.reserve_creation(
                    thread_intent
                )
                thread_intent = thread_reservation.intent
                if not thread_reservation.created:
                    if thread_intent.state is WeaponryCreationIntentState.PENDING:
                        self._resource_registrar.quarantine_creation(
                            thread_intent,
                            error_code="source_thread_unique_lookup_unavailable",
                        )
                        raise WeaponryExternalOperationError(
                            "source_thread_creation_outcome_unknown",
                            "历史 Thread 创建结果无法唯一查回，已隔离父 workspace",
                            outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                        )
                    raise WeaponryPortStateError(
                        "source_thread_creation_not_available",
                        "来源 Thread 创建意图已经终结，禁止重复创建",
                    )
                external_mutation_started = True
                try:
                    thread = clients.threads.create_thread(
                        workspace.slug,
                        thread_name,
                        user_id=self._user_id,
                    )
                except Exception as thread_error:
                    error_code = (
                        "source_thread_creation_outcome_unknown"
                        if self._write_outcome_may_be_unknown(thread_error)
                        else "source_thread_create_failed"
                    )
                    self._resource_registrar.quarantine_creation(
                        thread_intent,
                        error_code=error_code,
                    )
                    if self._write_outcome_may_be_unknown(thread_error):
                        raise WeaponryExternalOperationError(
                            error_code,
                            "来源 Thread 创建结果无法安全核对",
                            outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                        ) from thread_error
                    raise
                try:
                    self._resource_registrar.register_created(
                        task_id=request.call.task_id,
                        resource_id=f"source-conversation:{workspace.slug}:{thread.slug}",
                        kind=WeaponryResourceKind.SOURCE_CONVERSATION,
                        external_ref=f"{workspace.slug}\x1f{thread.slug}",
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key=(
                            f"{request.call.attempt_key}:source-conversation"
                        ),
                        document_key=request.document.document_key,
                        call_id=request.call.call_id,
                    )
                    self._resource_registrar.resolve_creation(
                        thread_intent,
                        external_ref=thread.slug,
                    )
                except Exception:
                    # Thread 无独立唯一查询 API，但其父 workspace 已经可靠登记。冻结创建
                    # 意图后由父 workspace 的删除承担级联补偿，禁止重复 create_thread。
                    self._resource_registrar.quarantine_creation(
                        thread_intent,
                        error_code="source_thread_registration_failed",
                    )
                    raise
                answer = clients.threads.ask(
                    workspace.slug,
                    thread.slug,
                    prompt,
                    mode="chat",
                    user_id=self._user_id,
                    document_ids=(),
                )
                self._require_empty_workspace(clients, workspace.slug, stage="after")
                if answer.sources:
                    raise WeaponrySourceBoundaryError(
                        "extraction_unexpected_rag_source",
                        "Provided-Evidence 抽取返回了不允许的供应商 RAG 来源",
                    )
        except (
            WeaponryExternalOperationError,
            WeaponryPortStateError,
            WeaponrySourceBoundaryError,
        ):
            raise
        except Exception as exc:
            raise self._external_error(
                exc,
                mutation_started=external_mutation_started,
            ) from exc

        raw_text = answer.raw_text
        cleaned_text = answer.text.strip()
        raw_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        logger.info(
            "武器谱 Provided-Evidence 抽取完成: task_id=%s call_id=%s "
            "evidence_count=%d answer_chars=%d",
            request.call.task_id.value,
            request.call.call_id,
            len(request.evidence),
            len(cleaned_text),
        )
        return ExtractionAnswer(
            call=request.call,
            text=cleaned_text,
            raw_response_digest=raw_digest,
            raw_response_chars=len(raw_text),
            evidence_ids=tuple(item.candidate_id for item in request.evidence),
            # 直接 Provided-Evidence 调用没有 RAG source；权威边界由空 workspace、零
            # sources、请求 DTO 的 rows 同序校验和 evidence_digest 共同证明。
            sources=(),
            validation_outcome=(
                ExtractionValidationOutcome.MATCHED
                if cleaned_text
                else ExtractionValidationOutcome.EMPTY_ANSWER
            ),
        )

    def _require_empty_workspace(self, clients, workspace_slug: str, *, stage: str) -> None:
        documents = clients.workspaces.list_documents(
            workspace_slug,
            user_id=self._user_id,
        )
        if documents:
            logger.error(
                "武器谱 Provided-Evidence workspace 非空: stage=%s document_count=%d",
                stage,
                len(documents),
            )
            raise WeaponrySourceBoundaryError(
                "extraction_context_workspace_not_empty",
                "Provided-Evidence 抽取 workspace 包含供应商文档",
            )

    @staticmethod
    def _evidence_digest(request: EvidenceExtractionRequest) -> str:
        payload = [
            {
                "evidence_id": item.candidate_id,
                "document_key": item.document_key,
                "text": item.text,
            }
            for item in request.evidence
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _render_provider_prompt(
        request: EvidenceExtractionRequest,
        evidence_digest: str,
    ) -> str:
        # 摘要写入模型输入包，便于 Fake Transport 和交互审计证明实际发送的是当前
        # Evidence 集合；摘要不要求模型回显，也不会作为目标事实来源。
        return (
            "[DOCSENSE_PROVIDED_EVIDENCE_V1]\n"
            f"evidence_sha256={evidence_digest}\n"
            f"evidence_count={len(request.evidence)}\n"
            "[/DOCSENSE_PROVIDED_EVIDENCE_V1]\n\n"
            f"{request.prompt.text}"
        )

    @staticmethod
    def _workspace_name(attempt_key: str) -> str:
        digest = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()[:24]
        return f"docsense-weaponry-extraction-{digest}"

    @staticmethod
    def _thread_name(attempt_key: str) -> str:
        digest = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()[:24]
        return f"weaponry-source-{digest}"

    def _reconcile_workspace_creation(
        self,
        clients,
        intent: WeaponryCreationIntent,
        *,
        cause: Exception | None,
    ):
        """只接管唯一、同名且为空的 workspace；其余场景一律隔离。"""

        try:
            matches = tuple(
                workspace
                for workspace in clients.workspaces.list_workspaces(user_id=self._user_id)
                if workspace.name == intent.expected_name
            )
            if len(matches) != 1:
                raise WeaponrySourceBoundaryError(
                    "extraction_workspace_reconciliation_ambiguous",
                    "抽取 workspace 创建结果无法唯一查回",
                )
            workspace = matches[0]
            self._require_empty_workspace(clients, workspace.slug, stage="reconcile")
            logger.warning(
                "武器谱抽取 workspace 创建结果已通过唯一查回恢复: "
                "task_id=%s intent_id=%s",
                intent.task_id.value,
                intent.intent_id,
            )
            return workspace
        except Exception as reconcile_error:
            error_code = getattr(
                reconcile_error,
                "error_code",
                "extraction_workspace_reconciliation_failed",
            )
            self._resource_registrar.quarantine_creation(
                intent,
                error_code=error_code,
            )
            raise WeaponryExternalOperationError(
                "extraction_workspace_creation_outcome_unknown",
                "抽取 workspace 创建结果无法安全核对",
                outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
            ) from (cause or reconcile_error)

    def _extraction_identity_digest(
        self,
        request: EvidenceExtractionRequest,
        evidence_digest: str,
    ) -> str:
        payload = {
            "attempt_key": request.call.attempt_key,
            "call_id": request.call.call_id,
            "document_key": request.document.document_key,
            "model_fingerprint": request.model_fingerprint,
            "context_strategy": request.context_strategy,
            "evidence_digest": evidence_digest,
        }
        return self._canonical_digest(payload)

    @staticmethod
    def _thread_identity_digest(
        request: EvidenceExtractionRequest,
        workspace_slug: str,
    ) -> str:
        return AnythingLLMProvidedEvidenceExtractionAdapter._canonical_digest(
            {
                "attempt_key": request.call.attempt_key,
                "call_id": request.call.call_id,
                "document_key": request.document.document_key,
                "workspace_slug": workspace_slug,
            }
        )

    @staticmethod
    def _canonical_digest(payload: dict[str, object]) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _external_error(
        error: Exception,
        *,
        mutation_started: bool,
    ) -> WeaponryExternalOperationError:
        unknown = mutation_started and (
            AnythingLLMProvidedEvidenceExtractionAdapter._write_outcome_may_be_unknown(
                error
            )
        )
        outcome = (
            WeaponryExternalOutcome.OUTCOME_UNKNOWN
            if unknown
            else WeaponryExternalOutcome.DEFINITELY_FAILED
        )
        error_type = (
            error.code
            if isinstance(error, AnythingLLMTransportError)
            else type(error).__name__
        )
        classified_error_code = (
            AnythingLLMProvidedEvidenceExtractionAdapter._capacity_error_code(error)
            or "provided_evidence_extraction_failed"
        )
        logger.warning(
            "武器谱 Provided-Evidence 外部操作失败: error_code=%s "
            "error_category=%s external_error_type=%s outcome=%s",
            classified_error_code,
            (
                "provider_capacity"
                if classified_error_code.startswith("provider_")
                else "external_operation"
            ),
            error_type,
            outcome.value,
        )
        return WeaponryExternalOperationError(
            classified_error_code,
            "武器谱 Provided-Evidence 抽取外部操作失败",
            outcome=outcome,
        )

    @staticmethod
    def _capacity_error_code(error: Exception) -> str:
        if not isinstance(error, AnythingLLMHTTPError):
            return ""
        if error.status_code == 413:
            return "provider_payload_too_large"
        if error.status_code == 429:
            return "provider_rate_limited"
        return ""

    @staticmethod
    def _write_outcome_may_be_unknown(error: Exception) -> bool:
        """保守判断模型/上下文写请求是否可能已经产生供应商副作用。"""

        if isinstance(error, AnythingLLMTransportClosedError):
            return False
        if isinstance(
            error,
            (
                AnythingLLMTimeoutError,
                AnythingLLMConnectionError,
                AnythingLLMProtocolError,
            ),
        ):
            return True
        if isinstance(error, AnythingLLMHTTPError):
            status_code = error.status_code or 0
            # Extraction workspace/thread 都使用 attempt_key 派生的确定性名称。409 可能
            # 表示崩溃前的同一 attempt 已经创建成功，必须按结果未知保留现场，等待后续
            # 唯一查回或隔离，不能把它降级成允许下一次模型调用的明确失败。
            return status_code >= 500 or status_code in {408, 409, 425, 429}
        return isinstance(error, AnythingLLMTransportError)


__all__ = ["AnythingLLMProvidedEvidenceExtractionAdapter"]
