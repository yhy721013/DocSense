"""AnythingLLM 任务级目标 Evidence Candidate 检索 Adapter。"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from threading import RLock
from typing import Mapping

from app.integrations.anythingllm import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
    AnythingLLMTransportError,
)
from app.integrations.anythingllm.models import (
    normalize_document_location_key,
    normalize_document_ref,
)
from app.modules.tasks.domain import TaskId
from app.modules.weaponry.domain import (
    EVIDENCE_SCORE_MODE_RANK,
    EVIDENCE_SCORE_MODE_SCORE,
    EvidenceCandidate,
    WeaponryDocumentSnapshot,
)
from app.modules.weaponry.ports import (
    IdempotentOperationResult,
    OpenTargetEvidenceScope,
    SearchTargetEvidence,
    TargetEvidenceScope,
    TargetEvidenceSearchResult,
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

from .anythingllm_clients import (
    WeaponryAnythingLLMClientFactoryProtocol,
    WeaponryAnythingLLMClients,
    workspace_slug_is_absent,
)
from .resource_registration import WeaponryCreatedResourceRegistrarProtocol


logger = logging.getLogger(__name__)


def normalize_anythingllm_source_url_ref(value: object) -> str:
    """只把具有路径结构的 URL/路径转换为完整末段身份。

    上游若错误地把单独文件名塞进 ``url``，它与展示 title 没有可区分的结构，不能取得
    身份权。真实 ``file://`` URL、绝对路径和带目录的相对路径均至少包含路径分隔符。
    """

    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if not normalized or ("/" not in normalized and "\\" not in normalized):
        return ""
    return normalize_document_ref(normalized)


@dataclass
class _RetrievalScopeState:
    """一个 execution 独占的供应商对象和强身份映射。"""

    scope: TargetEvidenceScope
    lease: AbstractContextManager[WeaponryAnythingLLMClients]
    clients: WeaponryAnythingLLMClients
    documents_by_key: Mapping[str, WeaponryDocumentSnapshot]
    document_key_by_location: Mapping[str, str]
    document_key_by_provider_id: Mapping[str, str]
    document_key_by_ingested_ref: Mapping[str, str]
    lock: RLock = field(default_factory=RLock)
    closed: bool = False


def resolve_anythingllm_source_document_key(
    source: AnythingLLMSource,
    *,
    document_key_by_location: Mapping[str, str],
    document_key_by_provider_id: Mapping[str, str],
    document_key_by_ingested_ref: Mapping[str, str],
) -> str:
    """把真实 vector-search 来源唯一映射到冻结文档，不接受展示标题猜测。

    上游不同版本可能让 ``metadata.url`` 返回完整全局位置，也可能返回上传前 hotdir URL。
    前者必须与冻结 location 精确相等；后者只允许把 URL 的完整末段与受理时冻结的
    ``ingested_file_name`` 精确相等，并且该名称必须在任务范围内唯一。title、
    ``document_ref``、原始业务名和模糊子串均不参与判断。多个权威字段若指向不同文档，
    仍按协议冲突拒绝。
    """

    if not isinstance(source, AnythingLLMSource):
        raise TypeError("source 必须是 AnythingLLMSource")
    matches: set[str] = set()
    metadata = source.metadata or {}
    location_values = [
        metadata.get(key)
        for key in ("location", "docpath", "docPath", "sourceDocument", "url")
    ]
    location_values.append(source.url)
    for value in location_values:
        if value is None:
            continue
        location_key = normalize_document_location_key(str(value))
        mapped = document_key_by_location.get(location_key)
        if mapped:
            matches.add(mapped)
    # 当前真实 AnythingLLM vector-search 返回 ``file://.../hotdir/<入库文件名>``，而
    # workspace 文档位置是 ``custom-documents/<生成名>.json``。只在结构化 URL 字段上
    # 使用完整末段精确匹配，不读取 title/document_ref；任务打开时已拒绝名称碰撞。
    for value in (metadata.get("url"), source.url):
        if value is None:
            continue
        ingested_ref = normalize_anythingllm_source_url_ref(value)
        mapped = document_key_by_ingested_ref.get(ingested_ref)
        if mapped:
            matches.add(mapped)
    for key in ("docId", "documentId"):
        value = str(metadata.get(key) or "").strip()
        mapped = document_key_by_provider_id.get(value)
        if mapped:
            matches.add(mapped)
    if source.id:
        mapped = document_key_by_provider_id.get(source.id)
        if mapped:
            matches.add(mapped)
    if len(matches) != 1:
        raise WeaponrySourceBoundaryError(
            "retrieval_source_identity_unresolved",
            "检索来源无法唯一映射到冻结文档身份",
        )
    return next(iter(matches))


class AnythingLLMTargetEvidenceRetrievalAdapter:
    """只写 execution 新建 workspace，绝不修改永久来源 workspace。

    打开范围时把冻结文档的全局位置绑定到新 workspace；检索来源只能通过完整 location、
    供应商全局 document ID，或结构化 URL 末段与冻结 ``ingested_file_name`` 的唯一精确
    匹配映射回 ``document_key``。这里不信任 title、业务原始名、``document_ref`` 或模糊
    子串；任务范围内任何入库文件名碰撞都会在检索前失败。每个任务持有自己的
    Transport，单任务内搜索与关闭用细粒度锁串行，不阻塞其他 task_id。
    """

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        resource_registrar: WeaponryCreatedResourceRegistrarProtocol,
        *,
        provider_fingerprint: str,
        embedding_fingerprint: str,
        user_id: int | None = 1,
    ) -> None:
        if not isinstance(client_factory, WeaponryAnythingLLMClientFactoryProtocol):
            raise TypeError("client_factory 必须实现武器谱 AnythingLLM Client 工厂")
        if not isinstance(resource_registrar, WeaponryCreatedResourceRegistrarProtocol):
            raise TypeError("resource_registrar 必须实现创建后资源登记契约")
        self._provider_fingerprint = self._required_text(
            provider_fingerprint,
            "provider_fingerprint",
        )
        self._embedding_fingerprint = self._required_text(
            embedding_fingerprint,
            "embedding_fingerprint",
        )
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._client_factory = client_factory
        self._resource_registrar = resource_registrar
        self._user_id = user_id
        self._states_lock = RLock()
        self._states: dict[TaskId, _RetrievalScopeState] = {}
        # ``_states`` 只能描述已经完整建好的 scope。外部创建发生在慢 I/O 中，若不额外
        # 预留 task_id，两个线程会同时看到“尚无 state”并各自创建同名 workspace。
        # 该集合只保护单进程内的 Adapter 并发；未来多实例仍由 execution claim/fencing
        # 保证同一任务只有一个 Worker 获得执行权。
        self._opening_tasks: set[TaskId] = set()

    def open_scope(self, command: OpenTargetEvidenceScope) -> TargetEvidenceScope:
        if not isinstance(command, OpenTargetEvidenceScope):
            raise TypeError("command 必须是 OpenTargetEvidenceScope")
        self._validate_policy_fingerprints(command)
        self._resource_registrar.ensure_ready(command.task_id)
        with self._states_lock:
            existing = self._states.get(command.task_id)
            if existing is not None and not existing.closed:
                raise WeaponryPortStateError(
                    "retrieval_scope_already_open",
                    "同一 task_id 已存在活动检索范围",
                )
            if command.task_id in self._opening_tasks:
                raise WeaponryPortStateError(
                    "retrieval_scope_open_in_progress",
                    "同一 task_id 的检索范围正在创建",
                )
            self._opening_tasks.add(command.task_id)

        lease: AbstractContextManager[WeaponryAnythingLLMClients] | None = None
        clients: WeaponryAnythingLLMClients | None = None
        workspace_slug = ""
        workspace_registered = False
        external_mutation_started = False
        creation_intent: WeaponryCreationIntent | None = None
        state: _RetrievalScopeState | None = None
        state_installed = False
        try:
            # ``create`` 本身也可能因配置/工厂故障同步抛错，因此必须位于 try 内；否则
            # task_id 会永久遗留在 opening 集合，后续健康重试也会被错误拒绝。
            lease = self._client_factory.create()
            clients = lease.__enter__()
            workspace_name = self._workspace_name(command.task_id)
            intent = WeaponryCreationIntent(
                task_id=command.task_id,
                intent_id="retrieval-workspace",
                kind=WeaponryCreationIntentKind.RETRIEVAL_WORKSPACE,
                expected_name=workspace_name,
                identity_digest=self._scope_identity_digest(command),
            )
            reservation = self._resource_registrar.reserve_creation(intent)
            intent = reservation.intent
            creation_intent = intent
            if intent.state is WeaponryCreationIntentState.QUARANTINED:
                raise WeaponryPortStateError(
                    "retrieval_workspace_creation_quarantined",
                    "检索 workspace 创建意图已经隔离，禁止再次创建",
                )
            if intent.state is WeaponryCreationIntentState.RESOLVED:
                raise WeaponryPortStateError(
                    "retrieval_workspace_creation_resolved",
                    "检索 workspace 创建意图已经完成，禁止重复打开",
                )
            if intent.state is WeaponryCreationIntentState.RECOVERING:
                raise WeaponryPortStateError(
                    "retrieval_workspace_creation_recovering",
                    "检索 workspace 创建意图正由恢复器接管，禁止 Worker 继续修改现场",
                )
            # 标记必须发生在调用前。超时/断连时即使尚未取得 slug，供应商也可能已经
            # 创建 workspace，不能再把它误报成可以安全重试的明确失败。
            if reservation.created:
                external_mutation_started = True
                try:
                    workspace = clients.workspaces.create_workspace(
                        workspace_name,
                        user_id=self._user_id,
                    )
                except Exception as create_error:
                    if not self._write_outcome_may_be_unknown(create_error):
                        self._resource_registrar.quarantine_creation(
                            intent,
                            error_code="retrieval_workspace_create_failed",
                        )
                        raise
                    workspace = self._reconcile_workspace_creation(
                        clients,
                        intent,
                        cause=create_error,
                    )
            else:
                # 历史 pending 表示旧 Worker 可能已经发送 create。唯一允许的动作是按
                # 确定性名称查回并核验，绝不能再次发送创建请求。
                workspace = self._reconcile_workspace_creation(
                    clients,
                    intent,
                    cause=None,
                )
            workspace_slug = workspace.slug
            # workspace 已经真实创建，必须先持久登记，之后才能继续绑定文档。
            self._resource_registrar.register_created(
                task_id=command.task_id,
                resource_id=f"retrieval-scope:{workspace.slug}",
                kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                external_ref=workspace.slug,
                ownership=WeaponryResourceOwnership.OWNED,
                idempotency_key=f"weaponry:{command.task_id.value}:retrieval-scope",
            )
            workspace_registered = True
            self._resource_registrar.resolve_creation(
                intent,
                external_ref=workspace.slug,
            )

            locations = tuple(
                document.external_document_ref
                for document in command.document_scope.documents
            )
            clients.workspaces.update_embeddings(
                workspace.slug,
                adds=locations,
                user_id=self._user_id,
            )
            for document in command.document_scope.documents:
                self._resource_registrar.register_created(
                    task_id=command.task_id,
                    resource_id=(
                        f"retrieval-binding:{workspace.slug}:{document.sequence_no}"
                    ),
                    kind=WeaponryResourceKind.DOCUMENT_BINDING,
                    external_ref=(
                        f"{workspace.slug}\x1f{document.external_document_ref}"
                    ),
                    ownership=WeaponryResourceOwnership.OWNED,
                    idempotency_key=(
                        f"weaponry:{command.task_id.value}:retrieval-binding:"
                        f"{document.sequence_no}"
                    ),
                    document_key=document.document_key,
                )

            provider_documents = clients.workspaces.list_documents(
                workspace.slug,
                user_id=self._user_id,
            )
            identity_maps = self._verify_bound_documents(
                command.document_scope.documents,
                provider_documents,
            )
            scope = TargetEvidenceScope(
                task_id=command.task_id,
                scope_ref=workspace.slug,
                allowed_document_keys=tuple(
                    item.document_key for item in command.document_scope.documents
                ),
                selection_profile_id=command.policy.profile_id,
                provider_fingerprint=self._provider_fingerprint,
                embedding_fingerprint=self._embedding_fingerprint,
            )
            state = _RetrievalScopeState(
                scope=scope,
                lease=lease,
                clients=clients,
                documents_by_key={
                    item.document_key: item
                    for item in command.document_scope.documents
                },
                document_key_by_location=identity_maps[0],
                document_key_by_provider_id=identity_maps[1],
                document_key_by_ingested_ref=identity_maps[2],
            )
            with self._states_lock:
                self._states[command.task_id] = state
                self._opening_tasks.discard(command.task_id)
                state_installed = True
            logger.info(
                "武器谱任务级检索范围已打开: task_id=%s document_count=%d",
                command.task_id.value,
                len(scope.allowed_document_keys),
            )
            return scope
        except Exception as exc:
            with self._states_lock:
                self._opening_tasks.discard(command.task_id)
                if state_installed and self._states.get(command.task_id) is state:
                    self._states.pop(command.task_id, None)

            # workspace 尚未登记时，恢复程序无法定位它，必须立即补偿删除；一旦登记成功，
            # 则保留资源事实交给后续恢复，禁止在结果未知时盲删或重建。
            compensation_failed = False
            if clients is not None and workspace_slug and not workspace_registered:
                try:
                    clients.workspaces.delete_workspace(
                        workspace_slug,
                        user_id=self._user_id,
                    )
                    logger.warning(
                        "武器谱检索 workspace 登记失败后已补偿删除: task_id=%s",
                        command.task_id.value,
                    )
                    if (
                        creation_intent is not None
                        and creation_intent.state
                        is WeaponryCreationIntentState.PENDING
                    ):
                        self._resource_registrar.quarantine_creation(
                            creation_intent,
                            error_code="retrieval_workspace_compensated",
                        )
                except Exception:
                    logger.critical(
                        "武器谱检索 workspace 登记失败且补偿删除失败: task_id=%s",
                        command.task_id.value,
                        exc_info=True,
                    )
                    compensation_failed = True
            if clients is not None and lease is not None:
                try:
                    lease.__exit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    logger.exception(
                        "武器谱检索范围打开失败后关闭 Transport 再次失败: task_id=%s",
                        command.task_id.value,
                    )
            if compensation_failed:
                # 原创建已经成功、登记未提交且补偿删除未确认，此时不能把调用顺序错误
                # 伪装成“明确失败”。稳定 workspace 名会保留在日志中供恢复对账与人工审计。
                raise WeaponryExternalOperationError(
                    "retrieval_scope_untracked_resource_unknown",
                    "检索 workspace 登记失败且补偿结果未知",
                    outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                ) from exc
            if isinstance(
                exc,
                (
                    WeaponryExternalOperationError,
                    WeaponryPortStateError,
                    WeaponrySourceBoundaryError,
                ),
            ):
                raise
            raise self._external_error(
                exc,
                error_code="retrieval_scope_open_failed",
                mutation_started=external_mutation_started,
            ) from exc

    def search_target(
        self,
        command: SearchTargetEvidence,
    ) -> TargetEvidenceSearchResult:
        if not isinstance(command, SearchTargetEvidence):
            raise TypeError("command 必须是 SearchTargetEvidence")
        state = self._require_state(command.scope)
        with state.lock:
            if state.closed:
                raise WeaponryPortStateError(
                    "retrieval_scope_closed",
                    "检索范围已经关闭",
                )
            try:
                sources = state.clients.workspaces.vector_search(
                    command.scope.scope_ref,
                    command.query.text,
                    top_n=command.candidate_top_n,
                    # 显式发送 0.0，避免 workspace 默认阈值暗中成为 Schema v2 的
                    # 绝对相关性门禁。最终保留规则只由冻结 Policy 和稳定排序决定。
                    score_threshold=0.0,
                    user_id=self._user_id,
                )
                candidates, score_mode = self._adapt_candidates(
                    state,
                    command,
                    tuple(sources),
                )
            except (WeaponrySourceBoundaryError, WeaponryPortStateError):
                raise
            except Exception as exc:
                raise self._external_error(
                    exc,
                    error_code="target_retrieval_failed",
                    mutation_started=False,
                ) from exc

        logger.info(
            "武器谱目标 Candidate 检索完成: task_id=%s call_id=%s "
            "candidate_count=%d score_mode=%s",
            command.call.task_id.value,
            command.call.call_id,
            len(candidates),
            score_mode,
        )
        return TargetEvidenceSearchResult(
            scope_ref=command.scope.scope_ref,
            call=command.call,
            candidates=candidates,
            score_mode=score_mode,
            provider_fingerprint=self._provider_fingerprint,
            embedding_fingerprint=self._embedding_fingerprint,
        )

    def close_scope(self, scope: TargetEvidenceScope) -> IdempotentOperationResult:
        if not isinstance(scope, TargetEvidenceScope):
            raise TypeError("scope 必须是 TargetEvidenceScope")
        with self._states_lock:
            state = self._states.get(scope.task_id)
        if state is None:
            return IdempotentOperationResult(success=True, already_applied=True)
        if state.scope != scope:
            raise WeaponryPortStateError(
                "retrieval_scope_not_owned",
                "检索范围不属于当前 task_id",
            )
        with state.lock:
            if state.closed:
                return IdempotentOperationResult(success=True, already_applied=True)
            try:
                state.clients.workspaces.delete_workspace(
                    scope.scope_ref,
                    user_id=self._user_id,
                )
            except AnythingLLMHTTPError as exc:
                if exc.status_code == 400:
                    try:
                        workspaces = state.clients.workspaces.list_workspaces(
                            user_id=self._user_id,
                        )
                        absent = workspace_slug_is_absent(
                            workspaces,
                            scope.scope_ref,
                        )
                    except Exception as verification_error:
                        # DELETE 已收到明确 400，但只读查回失败，不能据此推断资源已经
                        # 消失。保留 scope 与租约，使上层按“明确失败”进入后续清理链。
                        logger.warning(
                            "武器谱检索 workspace 删除 400 后查回失败: "
                            "task_id=%s error_type=%s",
                            scope.task_id.value,
                            type(verification_error).__name__,
                        )
                        raise self._external_error(
                            exc,
                            error_code="retrieval_scope_close_failed",
                            mutation_started=True,
                        ) from verification_error
                    if not absent:
                        logger.warning(
                            "武器谱检索 workspace 删除返回 400，且查回确认资源仍存在: "
                            "task_id=%s",
                            scope.task_id.value,
                        )
                        raise self._external_error(
                            exc,
                            error_code="retrieval_scope_close_failed",
                            mutation_started=True,
                        ) from exc
                    logger.info(
                        "武器谱检索 workspace 删除返回 400，但查回确认资源已不存在，"
                        "按幂等关闭处理: task_id=%s",
                        scope.task_id.value,
                    )
                elif exc.status_code != 404:
                    raise self._external_error(
                        exc,
                        error_code="retrieval_scope_close_failed",
                        mutation_started=True,
                    ) from exc
            except Exception as exc:
                raise self._external_error(
                    exc,
                    error_code="retrieval_scope_close_failed",
                    mutation_started=True,
                ) from exc
            state.closed = True
            transport_close_error: Exception | None = None
            try:
                state.lease.__exit__(None, None, None)
            except Exception as exc:
                transport_close_error = exc
            finally:
                # 远端 workspace 已确定删除；即使本地 Transport.close 报错，也不能保留一个
                # 看似可再次搜索的僵尸 scope。连接关闭错误单独上报，不改变远端清理事实。
                with self._states_lock:
                    self._states.pop(scope.task_id, None)
            if transport_close_error is not None:
                raise self._external_error(
                    transport_close_error,
                    error_code="retrieval_transport_close_failed",
                    mutation_started=False,
                ) from transport_close_error
        logger.info("武器谱任务级检索范围已关闭: task_id=%s", scope.task_id.value)
        return IdempotentOperationResult(success=True)

    def _adapt_candidates(
        self,
        state: _RetrievalScopeState,
        command: SearchTargetEvidence,
        sources: tuple[AnythingLLMSource, ...],
    ) -> tuple[tuple[EvidenceCandidate, ...], str]:
        if any(not isinstance(source, AnythingLLMSource) for source in sources):
            raise WeaponrySourceBoundaryError(
                "retrieval_source_protocol_invalid",
                "检索响应包含非法来源对象",
            )
        score_presence = tuple(source.score_present for source in sources)
        if sources and any(score_presence) and not all(score_presence):
            raise WeaponrySourceBoundaryError(
                "retrieval_score_mode_mixed",
                "同一检索批次混合了有分数和无分数来源",
            )
        score_mode = (
            EVIDENCE_SCORE_MODE_SCORE
            if sources and all(score_presence)
            else EVIDENCE_SCORE_MODE_RANK
        )
        if score_mode == EVIDENCE_SCORE_MODE_SCORE and any(
            not source.score_valid for source in sources
        ):
            raise WeaponrySourceBoundaryError(
                "retrieval_score_invalid",
                "检索批次包含无法解析的供应商分数",
            )

        allowed = set(command.allowed_document_keys)
        candidates: list[EvidenceCandidate] = []
        for provider_rank, source in enumerate(sources, start=1):
            document_key = self._resolve_source_document_key(state, source)
            if document_key not in allowed:
                raise WeaponrySourceBoundaryError(
                    "retrieval_source_out_of_scope",
                    "检索来源超出字段允许文档范围",
                )
            candidate_id = self._candidate_id(
                command.call.call_id,
                provider_rank,
                document_key,
                source.text,
            )
            candidates.append(
                EvidenceCandidate(
                    candidate_id=candidate_id,
                    document_key=document_key,
                    text=source.text,
                    provider_rank=provider_rank,
                    provider_score=source.score,
                    provider_score_present=source.score_present,
                    score_profile_id=command.scope.selection_profile_id,
                )
            )
        return tuple(candidates), score_mode

    @staticmethod
    def _resolve_source_document_key(
        state: _RetrievalScopeState,
        source: AnythingLLMSource,
    ) -> str:
        return resolve_anythingllm_source_document_key(
            source,
            document_key_by_location=state.document_key_by_location,
            document_key_by_provider_id=state.document_key_by_provider_id,
            document_key_by_ingested_ref=state.document_key_by_ingested_ref,
        )

    @staticmethod
    def _verify_bound_documents(
        expected_documents: tuple[WeaponryDocumentSnapshot, ...],
        provider_documents: list[AnythingLLMDocument],
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        if any(
            not isinstance(item, AnythingLLMDocument)
            for item in provider_documents
        ):
            raise WeaponrySourceBoundaryError(
                "retrieval_document_protocol_invalid",
                "任务检索 workspace 返回了非法文档对象",
            )
        expected_by_location = {
            normalize_document_location_key(item.external_document_ref): item
            for item in expected_documents
        }
        if "" in expected_by_location or len(expected_by_location) != len(expected_documents):
            raise WeaponrySourceBoundaryError(
                "retrieval_document_identity_invalid",
                "冻结文档位置无法形成唯一完整身份",
            )
        location_map: dict[str, str] = {}
        provider_id_map: dict[str, str] = {}
        ingested_ref_map: dict[str, str] = {}

        for expected in expected_documents:
            ingested_ref = normalize_document_ref(expected.ingested_file_name)
            if not ingested_ref:
                raise WeaponrySourceBoundaryError(
                    "retrieval_ingested_identity_invalid",
                    "冻结入库文件名无法形成来源身份",
                )
            existing = ingested_ref_map.get(ingested_ref)
            if existing is not None and existing != expected.document_key:
                raise WeaponrySourceBoundaryError(
                    "retrieval_ingested_identity_conflict",
                    "任务范围内入库文件名无法唯一映射",
                )
            ingested_ref_map[ingested_ref] = expected.document_key

        def bind_provider_id(provider_id: str, document_key: str) -> None:
            if not provider_id:
                return
            existing = provider_id_map.get(provider_id)
            if existing is not None and existing != document_key:
                raise WeaponrySourceBoundaryError(
                    "retrieval_provider_document_id_conflict",
                    "供应商文档 ID 无法唯一映射",
                )
            provider_id_map[provider_id] = document_key

        for provider_document in provider_documents:
            location = normalize_document_location_key(
                str(getattr(provider_document, "location", ""))
            )
            expected = expected_by_location.get(location)
            if expected is None:
                raise WeaponrySourceBoundaryError(
                    "retrieval_workspace_contaminated",
                    "任务检索 workspace 包含未冻结文档",
                )
            if location in location_map:
                raise WeaponrySourceBoundaryError(
                    "retrieval_workspace_duplicate_binding",
                    "任务检索 workspace 对同一冻结位置存在重复绑定",
                )
            location_map[location] = expected.document_key
            provider_id = str(getattr(provider_document, "id", "") or "").strip()
            raw_provider_id = str(
                getattr(provider_document, "raw_document_id", "") or ""
            ).strip()
            if expected.anything_document_id and expected.anything_document_id not in {
                provider_id,
                raw_provider_id,
            }:
                raise WeaponrySourceBoundaryError(
                    "retrieval_provider_document_id_mismatch",
                    "供应商文档 ID 与冻结文档身份不一致",
                )
            bind_provider_id(provider_id, expected.document_key)
            bind_provider_id(raw_provider_id, expected.document_key)
            if expected.anything_document_id:
                bind_provider_id(
                    expected.anything_document_id,
                    expected.document_key,
                )
        if set(location_map) != set(expected_by_location):
            raise WeaponrySourceBoundaryError(
                "retrieval_workspace_binding_incomplete",
                "任务检索 workspace 文档绑定不完整",
            )
        return location_map, provider_id_map, ingested_ref_map

    def _require_state(self, scope: TargetEvidenceScope) -> _RetrievalScopeState:
        with self._states_lock:
            state = self._states.get(scope.task_id)
        if state is None or state.scope != scope:
            raise WeaponryPortStateError(
                "retrieval_scope_not_owned",
                "检索范围不存在或不属于当前 task_id",
            )
        return state

    def _validate_policy_fingerprints(self, command: OpenTargetEvidenceScope) -> None:
        if command.policy.provider_fingerprint != self._provider_fingerprint:
            raise WeaponryPortStateError(
                "retrieval_provider_fingerprint_mismatch",
                "实际检索供应商指纹与 execution 快照不一致",
            )
        if command.policy.embedding_fingerprint != self._embedding_fingerprint:
            raise WeaponryPortStateError(
                "retrieval_embedding_fingerprint_mismatch",
                "实际 embedding 指纹与 execution 快照不一致",
            )

    @staticmethod
    def _candidate_id(
        call_id: str,
        rank: int,
        document_key: str,
        text: str,
    ) -> str:
        digest = hashlib.sha256(
            "\x1f".join((call_id, str(rank), document_key, text)).encode("utf-8")
        ).hexdigest()
        return f"candidate-{digest[:32]}"

    @staticmethod
    def _workspace_name(task_id: TaskId) -> str:
        return f"docsense-weaponry-retrieval-{task_id.value}"

    @staticmethod
    def _scope_identity_digest(command: OpenTargetEvidenceScope) -> str:
        """冻结查回所需身份，不把文件正文或供应商凭据写入创建意图。"""

        payload = {
            "task_id": command.task_id.value,
            "documents": [
                {
                    "sequence_no": item.sequence_no,
                    "document_key": item.document_key,
                    "external_document_ref": item.external_document_ref,
                }
                for item in command.document_scope.documents
            ],
            "profile_id": command.policy.profile_id,
            "provider_fingerprint": command.policy.provider_fingerprint,
            "embedding_fingerprint": command.policy.embedding_fingerprint,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _reconcile_workspace_creation(
        self,
        clients: WeaponryAnythingLLMClients,
        intent: WeaponryCreationIntent,
        *,
        cause: Exception | None,
    ):
        """唯一查回空 workspace；任何歧义都持久隔离并保持 outcome unknown。"""

        try:
            matches = tuple(
                workspace
                for workspace in clients.workspaces.list_workspaces(user_id=self._user_id)
                if workspace.name == intent.expected_name
            )
            if len(matches) != 1:
                raise WeaponrySourceBoundaryError(
                    "retrieval_workspace_reconciliation_ambiguous",
                    "检索 workspace 创建结果无法唯一查回",
                )
            workspace = matches[0]
            documents = tuple(
                clients.workspaces.list_documents(
                    workspace.slug,
                    user_id=self._user_id,
                )
            )
            if documents:
                raise WeaponrySourceBoundaryError(
                    "retrieval_workspace_reconciliation_not_empty",
                    "查回的检索 workspace 已包含文档，无法证明归属",
                )
            logger.warning(
                "武器谱检索 workspace 创建结果已通过唯一查回恢复: "
                "task_id=%s intent_id=%s",
                intent.task_id.value,
                intent.intent_id,
            )
            return workspace
        except Exception as reconcile_error:
            error_code = getattr(
                reconcile_error,
                "error_code",
                "retrieval_workspace_reconciliation_failed",
            )
            self._resource_registrar.quarantine_creation(
                intent,
                error_code=error_code,
            )
            raise WeaponryExternalOperationError(
                "retrieval_workspace_creation_outcome_unknown",
                "检索 workspace 创建结果无法安全核对",
                outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
            ) from (cause or reconcile_error)

    @staticmethod
    def _required_text(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空 str")
        return value.strip()

    @staticmethod
    def _external_error(
        error: Exception,
        *,
        error_code: str,
        mutation_started: bool,
    ) -> WeaponryExternalOperationError:
        unknown = mutation_started and (
            AnythingLLMTargetEvidenceRetrievalAdapter._write_outcome_may_be_unknown(
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
            AnythingLLMTargetEvidenceRetrievalAdapter._capacity_error_code(error)
            or error_code
        )
        logger.warning(
            "武器谱 AnythingLLM 检索操作失败: error_code=%s "
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
            "武器谱目标 Evidence 检索外部操作失败",
            outcome=outcome,
        )

    @staticmethod
    def _capacity_error_code(error: Exception) -> str:
        """只对供应商明确容量状态分类，避免把业务零命中混成基础设施错误。"""

        if not isinstance(error, AnythingLLMHTTPError):
            return ""
        if error.status_code == 413:
            return "provider_payload_too_large"
        if error.status_code == 429:
            return "provider_rate_limited"
        return ""

    @staticmethod
    def _write_outcome_may_be_unknown(error: Exception) -> bool:
        """保守判断供应商写请求是否可能已执行但响应未被可靠接收。"""

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
            # Workspace 使用当前 TaskId 的确定性名称。创建请求返回 409 时，最重要的
            # 可能性不是“本次明确未创建”，而是前一次请求已经成功、响应却在本地登记前
            # 丢失。当前恢复链会保守冻结并隔离无法唯一证明的现场，禁止把冲突当作普通
            # 失败后再创建第二套资源。
            return status_code >= 500 or status_code in {408, 409, 425, 429}
        return isinstance(error, AnythingLLMTransportError)


__all__ = [
    "AnythingLLMTargetEvidenceRetrievalAdapter",
    "normalize_anythingllm_source_url_ref",
    "resolve_anythingllm_source_document_key",
]
