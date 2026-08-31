"""单次文件对话运行的执行边界。"""

from __future__ import annotations

import logging
from time import monotonic
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import BoundedSemaphore
from typing import Protocol, runtime_checkable

from app.modules.chat.ports import (
    ChatChunk,
    ChatConversationFactory,
    ChatDocumentRef,
    ChatQueryRejection,
    ChatResourceError,
    ChatSessionRefs,
    ChatSourceFinalization,
)
from app.modules.chat.application.command_service import ChatCommandService
from app.modules.chat.domain.document_candidates import (
    CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
    CHAT_ARCHITECTURE_ERROR_NOT_FOUND,
    ChatArchitectureCandidates,
    ChatDocumentCandidate,
    ChatDocumentSelectionCandidates,
)
from app.modules.chat.application.document_resolver import (
    ChatArchitectureDocumentResolver,
    ChatDocumentResolver,
    ResolvedChatDocument,
)
from app.modules.chat.application.policy import chat_policy_for
from app.modules.chat.application.source_mapper import (
    ChatSourceDocument,
    ChatSourceMapper,
)
from app.modules.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_ARCHITECTURE,
    CHAT_SCOPE_MODE_FILES,
    ChatScopeSelector,
)
from app.modules.chat.domain.resource_ids import (
    chat_document_binding_lease_id,
    chat_scoped_external_ref,
    chat_thread_lease_id,
    chat_workspace_lease_id,
)
from app.modules.chat.domain.events import ChatStreamEvent
from app.modules.chat.domain.identity import ConversationIdentity, IDENTITY_KIND_FILE
from app.modules.chat.domain.workspace_naming import chat_workspace_name
from app.modules.chat.domain.models import (
    MESSAGE_PENDING,
    MESSAGE_ROLE_ASSISTANT,
    MESSAGE_ROLE_USER,
    ChatMessageSourceChunk,
    RESOURCE_DOCUMENT_BINDING,
    RESOURCE_THREAD,
    RESOURCE_WORKSPACE,
    SESSION_ACTIVE,
)
from app.modules.chat.domain.limits import (
    DEFAULT_CHAT_MAX_CONCURRENT_STREAMS,
    DEFAULT_CHAT_MAX_FILES_PER_REQUEST,
    DEFAULT_CHAT_MAX_MESSAGE_CHARS,
    DEFAULT_CHAT_MAX_OUTPUT_CHARS,
    MAX_CHAT_ARCHITECTURE_ID,
)
from app.modules.chat.ports.coordination import (
    ChatAdmissionLease,
    ChatRunLease,
    ChatRunLeaseLostError,
)
from app.modules.chat.ports.persistence import ChatPersistenceStore


logger = logging.getLogger(__name__)

_TERMINAL_EVENT_TYPES = frozenset({"aborted", "done", "error"})


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


def _text_tuple(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence of strings")
    normalized: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError(f"{name}[{index}] must be str")
        item = str(value or "").strip()
        if not item:
            raise ValueError(f"{name}[{index}] cannot be empty")
        normalized.append(item)
    return tuple(normalized)


@dataclass(frozen=True)
class ChatRunStreamRequest:
    """执行一次文件对话流所需的应用层输入。"""

    run_id: str
    conversation_id: str
    workspace_name: str
    message: str
    file_names: tuple[str, ...] = ()
    file_original_names: tuple[str, ...] = ()
    documents: tuple["ChatRunDocumentSnapshot", ...] = ()
    # ``file_names`` 表示本次模型调用实际采用的活动范围；下面两个字段只表示
    # 前端本轮显式传入的文件。两者必须分离，否则空数组自动选择全量文件时，
    # 历史接口会错误暴露庞大的有效范围。
    #
    # ``None`` 仅用于兼容直接构造该 DTO 的既有内部测试/调用方：未提供时沿用
    # 历史行为，将有效范围视为显式请求。持久化恢复路径始终明确传入元组，
    # 因而真正的空请求会保持为空，不会触发该兼容分支。
    requested_file_names: tuple[str, ...] | None = None
    requested_file_original_names: tuple[str, ...] | None = None
    requested_architecture_id: int | None = None
    identity_kind: str = IDENTITY_KIND_FILE

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "run_id",
            _required_text(self.run_id, name="run_id"),
        )
        object.__setattr__(
            self,
            "conversation_id",
            _required_text(self.conversation_id, name="conversation_id"),
        )
        object.__setattr__(
            self,
            "workspace_name",
            _required_text(self.workspace_name, name="workspace_name"),
        )
        object.__setattr__(
            self,
            "file_names",
            _text_tuple(self.file_names, name="file_names"),
        )
        object.__setattr__(
            self,
            "file_original_names",
            _text_tuple(self.file_original_names, name="file_original_names"),
        )
        object.__setattr__(
            self,
            "message",
            _required_text(self.message, name="message"),
        )
        if len(self.file_names) != len(self.file_original_names):
            raise ValueError(
                "file_names and file_original_names must have the same length"
            )
        if (
            self.requested_file_names is None
            and self.requested_file_original_names is None
        ):
            if self.requested_architecture_id is None:
                requested_file_names = self.file_names
                requested_file_original_names = self.file_original_names
            else:
                requested_file_names = ()
                requested_file_original_names = ()
        elif (
            self.requested_file_names is None
            or self.requested_file_original_names is None
        ):
            raise ValueError(
                "requested_file_names and requested_file_original_names "
                "must be provided together"
            )
        else:
            requested_file_names = _text_tuple(
                self.requested_file_names,
                name="requested_file_names",
            )
            requested_file_original_names = _text_tuple(
                self.requested_file_original_names,
                name="requested_file_original_names",
            )
        if len(requested_file_names) != len(requested_file_original_names):
            raise ValueError(
                "requested_file_names and requested_file_original_names "
                "must have the same length"
            )
        requested_architecture_id = self.requested_architecture_id
        if isinstance(requested_architecture_id, bool) or (
            requested_architecture_id is not None
            and (
                not isinstance(requested_architecture_id, int)
                or requested_architecture_id < 1
                or requested_architecture_id > MAX_CHAT_ARCHITECTURE_ID
            )
        ):
            raise ValueError(
                "requested_architecture_id must be a positive integer or None"
            )
        if requested_architecture_id is not None and requested_file_names:
            raise ValueError(
                "requested architecture and requested files are mutually exclusive"
            )
        # 非空的前端选择会完整替换活动范围，因此受理完成后两套顺序必须一致。
        # 空请求则允许有效范围来自首次全量快照或已有活动范围。
        if requested_file_names and requested_file_names != self.file_names:
            raise ValueError(
                "non-empty requested_file_names must match effective file_names"
            )
        if (
            requested_file_original_names
            and requested_file_original_names != self.file_original_names
        ):
            raise ValueError(
                "non-empty requested_file_original_names must match "
                "effective file_original_names"
            )
        object.__setattr__(
            self,
            "requested_file_names",
            requested_file_names,
        )
        object.__setattr__(
            self,
            "requested_file_original_names",
            requested_file_original_names,
        )
        object.__setattr__(
            self,
            "requested_architecture_id",
            requested_architecture_id,
        )
        chat_policy_for(self.identity_kind)
        documents = tuple(self.documents)
        if any(not isinstance(item, ChatRunDocumentSnapshot) for item in documents):
            raise TypeError("documents must contain ChatRunDocumentSnapshot")
        if documents and tuple(item.file_name for item in documents) != self.file_names:
            raise ValueError("documents must match file_names in the same order")
        if documents and tuple(item.original_name for item in documents) != self.file_original_names:
            raise ValueError("documents must match file_original_names in the same order")
        object.__setattr__(self, "documents", documents)


@dataclass(frozen=True)
class ChatRunDocumentSnapshot:
    """一次同步对话执行使用的不可变文档身份快照。"""

    file_name: str
    original_name: str
    document: ChatDocumentRef
    structured_source_key: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_name", _required_text(self.file_name, name="file_name"))
        object.__setattr__(
            self,
            "original_name",
            _required_text(self.original_name, name="original_name"),
        )
        if not isinstance(self.document, ChatDocumentRef):
            raise TypeError("document must be ChatDocumentRef")
        if not isinstance(self.structured_source_key, str):
            raise TypeError("structured_source_key must be str")
        if self.structured_source_key.strip() != self.structured_source_key:
            raise ValueError("structured_source_key must be normalized")


@dataclass(frozen=True)
class PreparedChatRun:
    """已持久化受理、可随后通过 ``run_id`` 执行的运行。

    此对象刻意不携带请求快照。快照会在受理时原子持久化，执行器必须重新加载，
    从而使内联 HTTP 请求与未来工作进程走同一条执行路径。
    """

    run_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required_text(self.run_id, name="run_id"))
        object.__setattr__(self, "conversation_id", _required_text(self.conversation_id, name="conversation_id"))


@runtime_checkable
class ChatRunExecutor(Protocol):
    """执行对话运行并产出供应商无关的流事件。"""

    def execute_chat_run(
        self,
        run_id: str,
    ) -> Iterable[ChatStreamEvent]:
        """通过内部键加载并执行一条已持久化受理的运行。"""
        ...


class SynchronousChatRunExecutor:
    """承担新供应商无关对话路径的单实例执行器。"""

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        chat_commands: ChatCommandService,
        conversation_factory: ChatConversationFactory,
        document_resolver: ChatDocumentResolver,
        max_files_per_request: int = DEFAULT_CHAT_MAX_FILES_PER_REQUEST,
        max_message_chars: int = DEFAULT_CHAT_MAX_MESSAGE_CHARS,
        max_output_chars: int = DEFAULT_CHAT_MAX_OUTPUT_CHARS,
        max_concurrent_streams: int = DEFAULT_CHAT_MAX_CONCURRENT_STREAMS,
    ) -> None:
        if not isinstance(store, ChatPersistenceStore):
            raise TypeError("store must implement ChatPersistenceStore")
        if not isinstance(chat_commands, ChatCommandService):
            raise TypeError("chat_commands must be ChatCommandService")
        if not isinstance(conversation_factory, ChatConversationFactory):
            raise TypeError("conversation_factory must implement ChatConversationFactory")
        if not isinstance(document_resolver, ChatDocumentResolver):
            raise TypeError("document_resolver must implement ChatDocumentResolver")
        for name, value in (
            ("max_files_per_request", max_files_per_request),
            ("max_message_chars", max_message_chars),
            ("max_output_chars", max_output_chars),
            ("max_concurrent_streams", max_concurrent_streams),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._store = store
        self._chat_commands = chat_commands
        self._conversation_factory = conversation_factory
        self._document_resolver = document_resolver
        # file Resolver 仍保持既有最小协议；architecture 是按需校验的独立能力，
        # 避免所有旧 Fake 因新增方法被迫伪造从未使用的行为。
        self._architecture_document_resolver = (
            document_resolver
            if isinstance(document_resolver, ChatArchitectureDocumentResolver)
            else None
        )
        self._max_files_per_request = max_files_per_request
        self._max_message_chars = max_message_chars
        self._max_output_chars = max_output_chars
        self._max_concurrent_streams = max_concurrent_streams
        self._stream_slots = BoundedSemaphore(max_concurrent_streams)

    @property
    def max_concurrent_streams(self) -> int:
        """返回显式配置的单进程流并发容量。"""
        return self._max_concurrent_streams

    def try_acquire_stream_slot(self) -> bool:
        """不阻塞 Web 工作进程地预留一个同步流槽位。"""
        return self._stream_slots.acquire(blocking=False)

    def release_stream_slot(self) -> None:
        """路由对应的 SSE 可迭代对象关闭后释放其预留槽位。"""
        self._stream_slots.release()

    def resolve_document_candidates(
        self,
        *,
        identity: ConversationIdentity,
        file_names: Sequence[str],
    ) -> ChatDocumentSelectionCandidates:
        """冻结显式文件或仅供首次 session 使用的默认全量候选。

        本方法只准备候选，不决定最终采用哪一组文档。事务外读取 session 仅用于已有会话的
        安全快速路径：当前 session 行不会被物理删除，因此一旦读到记录，空数组无需扫描
        全量知识库。若未读到 session，即使并发请求随后先创建了它，也只会多准备一份默认
        候选；阶段 3 的受理事务仍必须根据实际 ``session_created`` 决定是否采用。

        阶段 3 由 ``prepare_chat_run()`` 把该对象交给受理事务；本方法本身仍不推断最终
        有效集合，也不把 selection mode 暴露给路由或供应商 Port。
        """
        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        normalized_file_names = _text_tuple(file_names, name="file_names")
        if normalized_file_names:
            resolved = self._document_resolver.resolve_many(
                normalized_file_names
            )
            snapshots = tuple(
                self._snapshot(document) for document in resolved
            )
            self._ensure_snapshot_order(
                snapshots=snapshots,
                expected_file_names=normalized_file_names,
            )
            logger.info(
                "文件对话文档候选已冻结: identity_kind=%s selection_mode=explicit "
                "explicit_count=%d default_count=0",
                identity.identity_kind,
                len(resolved),
            )
            return ChatDocumentSelectionCandidates(
                explicit_documents=tuple(
                    self._candidate(document) for document in resolved
                )
            )

        if self._store.identities.resolve_active(identity) is not None:
            logger.info(
                "文件对话文档候选已冻结: identity_kind=%s "
                "selection_mode=existing_session_empty "
                "explicit_count=0 default_count=0",
                identity.identity_kind,
            )
            return ChatDocumentSelectionCandidates()

        default_documents = self._document_resolver.resolve_all_available()
        logger.info(
            "文件对话文档候选已冻结: identity_kind=%s "
            "selection_mode=new_session_default "
            "explicit_count=0 default_count=%d",
            identity.identity_kind,
            len(default_documents),
        )
        return ChatDocumentSelectionCandidates(
            new_session_default_documents=tuple(
                self._candidate(document) for document in default_documents
            )
        )

    def resolve_architecture_candidates(
        self,
        *,
        identity: ConversationIdentity,
        architecture_id: int,
    ) -> ChatDocumentSelectionCandidates:
        """准备 architecture 候选，但绝不替已有会话刷新目录快照。

        已存在不可变 Binding 时只构造一个不会被采用的有界占位 outcome，交给
        Coordinator 先校验 mode/ID 并复用旧 Head。只有尚无 Binding 的竞争首轮才读取
        精确类别目录；即使多个首轮并发都读取，最终也只有事务赢家能够冻结候选。
        """
        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        if isinstance(architecture_id, bool) or not isinstance(
            architecture_id,
            int,
        ):
            raise TypeError("architecture_id must be int")
        resolution = self._store.identities.resolve_active(identity)
        binding = (
            None
            if resolution is None
            else self._store.session_scope_bindings.get(
                resolution.conversation_id
            )
        )
        if binding is not None:
            logger.info(
                "已有文件对话范围绑定，跳过 architecture 目录重查: "
                "identity_kind=%s requested_architecture_id=%s "
                "bound_scope_mode=%s bound_architecture_id=%s",
                identity.identity_kind,
                architecture_id,
                binding.scope_mode,
                binding.architecture_id,
            )
            return ChatDocumentSelectionCandidates(
                architecture_candidates=ChatArchitectureCandidates(
                    architecture_id=architecture_id,
                    resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
                    error_code=CHAT_ARCHITECTURE_ERROR_NOT_FOUND,
                )
            )
        resolver = self._architecture_document_resolver
        if resolver is None:
            raise TypeError(
                "document_resolver must implement "
                "ChatArchitectureDocumentResolver for architecture scope"
            )
        candidates = resolver.resolve_by_architecture_id(architecture_id)
        logger.info(
            "architecture 文件对话候选已冻结: identity_kind=%s "
            "requested_architecture_id=%s architecture_candidate_outcome=%s "
            "candidate_count=%d",
            identity.identity_kind,
            architecture_id,
            candidates.resolution_outcome,
            len(candidates.documents),
        )
        return ChatDocumentSelectionCandidates(
            architecture_candidates=candidates
        )

    def prepare_chat_run(
        self,
        *,
        identity: ConversationIdentity,
        message: str,
        file_names: Sequence[str] | None = None,
        scope_selector: ChatScopeSelector | None = None,
        admission_lease: ChatAdmissionLease | None = None,
    ) -> PreparedChatRun:
        """解析不可变输入，并原子受理一条新的单会话运行。"""
        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        normalized_message = _required_text(message, name="message")
        if scope_selector is not None and not isinstance(
            scope_selector,
            ChatScopeSelector,
        ):
            raise TypeError("scope_selector must be ChatScopeSelector or None")
        if admission_lease is not None and not isinstance(
            admission_lease,
            ChatAdmissionLease,
        ):
            raise TypeError(
                "admission_lease must be ChatAdmissionLease or None"
            )
        if scope_selector is None:
            normalized_file_names = _text_tuple(
                () if file_names is None else file_names,
                name="file_names",
            )
            normalized_selector = ChatScopeSelector.for_files(
                normalized_file_names
            )
        elif scope_selector.scope_mode == CHAT_SCOPE_MODE_FILES:
            normalized_file_names = scope_selector.file_names
            if file_names is not None and _text_tuple(
                file_names,
                name="file_names",
            ) != normalized_file_names:
                raise ValueError(
                    "file_names does not match the provided scope_selector"
                )
            normalized_selector = scope_selector
        else:
            if file_names is not None:
                raise ValueError(
                    "architecture scope cannot be combined with file_names"
                )
            normalized_file_names = ()
            normalized_selector = scope_selector
        logger.info(
            "开始受理 Chat 运行: identity_kind=%s message_chars=%d "
            "scope_mode=%s requested_file_count=%d "
            "requested_architecture_id=%s",
            identity.identity_kind,
            len(normalized_message),
            normalized_selector.scope_mode,
            len(normalized_file_names),
            normalized_selector.architecture_id,
        )
        if len(normalized_file_names) > self._max_files_per_request:
            logger.warning(
                "文件对话受理被拒绝：文件数量超过上限: identity_kind=%s requested_file_count=%d limit=%d",
                identity.identity_kind,
                len(normalized_file_names),
                self._max_files_per_request,
            )
            raise ValueError("fileNames exceeds the configured chat file limit")
        if len(normalized_message) > self._max_message_chars:
            logger.warning(
                "文件对话受理被拒绝：消息长度超过上限: identity_kind=%s message_chars=%d limit=%d",
                identity.identity_kind,
                len(normalized_message),
                self._max_message_chars,
            )
            raise ValueError("message exceeds the configured chat message limit")
        resolved_admission = admission_lease
        if resolved_admission is None:
            resolved_admission = self._chat_commands.reserve_chat_admission(
                identity=identity,
                scope_selector=normalized_selector,
            )
        try:
            if normalized_selector.scope_mode == CHAT_SCOPE_MODE_ARCHITECTURE:
                document_candidates = self.resolve_architecture_candidates(
                    identity=identity,
                    architecture_id=normalized_selector.architecture_id,
                )
            else:
                document_candidates = self.resolve_document_candidates(
                    identity=identity,
                    file_names=normalized_file_names,
                )
            run = self._chat_commands.start_chat_run(
                identity=identity,
                user_message=normalized_message,
                document_candidates=document_candidates,
                scope_selector=normalized_selector,
                admission_lease=resolved_admission,
                max_files_per_request=self._max_files_per_request,
            )
        finally:
            # 成功受理时 Guard 已在同一事务中消费；失败或容量回退时这里按 token
            # 幂等释放，确保不会让 chatId 卡到过期时间。
            self._chat_commands.release_chat_admission(
                lease=resolved_admission
            )
        requested_file_count: int | str = "unknown"
        effective_file_count: int | str = "unknown"
        selection_mode = "unknown"
        effective_scope_revision_id = "unknown"
        try:
            accepted_input = self._store.run_inputs.get(run.run_id)
            if accepted_input is not None:
                requested_file_count = len(accepted_input.requested_files)
                effective_file_count = len(accepted_input.files)
                selection_mode = accepted_input.selection_mode
                effective_scope_revision_id = (
                    accepted_input.effective_scope_revision_id
                )
            else:
                logger.error(
                    "文件对话运行已受理但日志计数未读到输入快照: "
                    "conversation_id=%s run_id=%s",
                    run.conversation_id,
                    run.run_id,
                )
        except Exception:
            # 运行和不可变输入已经在前一个原子事务中提交。这里的二次读取只服务于
            # 可观测计数，绝不能因为日志查询瞬时失败把已受理请求伪装成 HTTP 500，
            # 否则调用方重试可能与仍然活跃的 run 冲突。
            logger.exception(
                "文件对话运行已受理但日志计数读取失败，继续返回已受理运行: "
                "conversation_id=%s run_id=%s",
                run.conversation_id,
                run.run_id,
            )
        logger.info(
            "文件对话运行已受理并冻结输入快照: "
            "conversation_id=%s run_id=%s requested_file_count=%s "
            "effective_file_count=%s selection_mode=%s "
            "scope_revision_id=%s",
            run.conversation_id,
            run.run_id,
            requested_file_count,
            effective_file_count,
            selection_mode,
            effective_scope_revision_id,
        )
        # 命令服务会在受理事务中保存完整的消息和文档快照。将这些对象直接传给执行器会
        # 让未来工作进程依赖请求内存，因此执行路径刻意只接收持久化运行键。
        return PreparedChatRun(run_id=run.run_id, conversation_id=run.conversation_id)

    @staticmethod
    def _ensure_snapshot_order(
        *,
        snapshots: Sequence[ChatRunDocumentSnapshot],
        expected_file_names: Sequence[str],
    ) -> None:
        """拒绝 Resolver 返回缺项、增项或乱序结果，保护不可变受理输入。"""
        if tuple(item.file_name for item in snapshots) != tuple(
            expected_file_names
        ):
            raise ValueError("document resolver returned unexpected file order")

    def execute_chat_run(self, run_id: str) -> Iterator[ChatStreamEvent]:
        """从持久化输入快照中领取并执行一条已受理运行。"""
        normalized_run_id = _required_text(run_id, name="run_id")
        run = self._store.runs.get(normalized_run_id)
        run_input = self._store.run_inputs.get(normalized_run_id)
        if run is None or run_input is None:
            logger.warning(
                "文件对话运行无法执行：缺少持久化运行或输入快照: run_id=%s has_run=%s has_input=%s",
                normalized_run_id,
                run is not None,
                run_input is not None,
            )
            error_message = "chat run input is unavailable"
            if run is not None:
                self._chat_commands.discard_unstarted_chat_run(
                    run_id=normalized_run_id,
                    error_message=error_message,
                )
            yield ChatStreamEvent("error", {"error": "文件对话执行输入缺失"})
            return

        request = self._request_from_persisted_input(run_id=normalized_run_id)
        logger.info(
            "开始领取并执行文件对话运行: "
            "conversation_id=%s run_id=%s requested_file_count=%d "
            "effective_file_count=%d selection_mode=%s scope_revision_id=%s",
            request.conversation_id,
            request.run_id,
            len(run_input.requested_files),
            len(request.documents),
            run_input.selection_mode,
            run_input.effective_scope_revision_id,
        )
        try:
            execution_lease = self._chat_commands.issue_execution_lease(
                run_id=normalized_run_id,
            )
            logger.info(
                "文件对话运行已领取执行权: conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )
        except ChatRunLeaseLostError as exc:
            # 重复执行器可能在另一执行器已领取后仍观察到同一持久化运行。不能仅因为
            # 这次过期投递未领取到执行权，就丢弃合法所有者的待处理用户轮次。
            logger.warning(
                "文件对话运行已被其他执行路径领取，跳过未启动收敛: "
                "conversation_id=%s run_id=%s reason=%s",
                run.conversation_id,
                normalized_run_id,
                exc,
            )
            yield ChatStreamEvent("error", {"error": "文件对话执行启动失败"})
            return
        except Exception as exc:
            logger.exception(
                "文件对话运行领取执行权失败: conversation_id=%s run_id=%s",
                run.conversation_id,
                normalized_run_id,
            )
            try:
                self._chat_commands.discard_unstarted_chat_run(
                    run_id=normalized_run_id,
                    error_message=str(exc) or exc.__class__.__name__,
                )
            except ChatRunLeaseLostError:
                # 运行状态在领取失败与尝试收敛之间已发生变化。它现已归另一条执行路径
                # 所有，因此其待处理用户轮次必须保持不变。
                logger.warning(
                    "文件对话运行已被其他执行路径领取，跳过未启动收敛: "
                    "conversation_id=%s run_id=%s",
                    run.conversation_id,
                    normalized_run_id,
                )
            except Exception:
                # 即便持久化暂时不可用，也要保持 SSE 响应确定。既有的过期运行回收器
                # 仍负责收敛本次请求中无法完成的已受理运行。
                logger.exception(
                    "文件对话运行领取失败后的未启动收敛异常: conversation_id=%s run_id=%s",
                    run.conversation_id,
                    normalized_run_id,
                )
            yield ChatStreamEvent("error", {"error": "文件对话执行启动失败"})
            return

        yield from record_chat_run_events(
            request=request,
            events=self.stream_chat_run(request),
            store=self._store,
            chat_commands=self._chat_commands,
            execution_lease=execution_lease,
        )

    def _request_from_persisted_input(self, *, run_id: str) -> ChatRunStreamRequest:
        """仅根据不可变受理数据重建执行 DTO。"""
        run = self._store.runs.get(run_id)
        run_input = self._store.run_inputs.get(run_id)
        if run is None or run_input is None:
            raise ValueError("chat run or accepted input does not exist")
        snapshots = tuple(
            ChatRunDocumentSnapshot(
                file_name=item.file_name,
                original_name=item.original_name,
                document=ChatDocumentRef(
                    document_ref=item.document_ref,
                    external_location=item.external_location,
                ),
                structured_source_key=item.structured_source_key,
            )
            for item in run_input.files
        )
        identity_resolution = self._store.identities.get_by_conversation_id(
            run.conversation_id
        )
        if identity_resolution is None:
            raise ValueError("chat run conversation identity does not exist")
        return ChatRunStreamRequest(
            run_id=run.run_id,
            conversation_id=run.conversation_id,
            # Workspace 名称只能来自数据库中的不可变身份绑定。执行恢复不重新读取
            # Web 请求，也不允许调用方把任意供应商资源名称注入后台任务。
            workspace_name=chat_workspace_name(identity_resolution.binding),
            message=run_input.message,
            file_names=tuple(item.file_name for item in snapshots),
            file_original_names=tuple(item.original_name for item in snapshots),
            documents=snapshots,
            requested_file_names=tuple(
                item.file_name for item in run_input.requested_files
            ),
            requested_file_original_names=tuple(
                item.original_name for item in run_input.requested_files
            ),
            requested_architecture_id=run_input.requested_architecture_id,
            identity_kind=identity_resolution.binding.identity_kind,
        )

    def stream_chat_run(
        self,
        request: ChatRunStreamRequest,
    ) -> Iterator[ChatStreamEvent]:
        """创建或复用资源、绑定新文档，并产出供应商无关事件。"""
        try:
            session = self._store.sessions.get(request.conversation_id)
            if session is None or session.status != SESSION_ACTIVE:
                logger.warning(
                    "文件对话运行被拒绝：会话当前不可执行: conversation_id=%s run_id=%s has_session=%s session_status=%s",
                    request.conversation_id,
                    request.run_id,
                    session is not None,
                    session.status if session is not None else "",
                )
                raise ChatResourceError("当前对话不可用于执行")
            documents = request.documents
            if request.file_names and not documents:
                logger.warning(
                    "文件对话运行缺少已受理文档快照: "
                    "conversation_id=%s run_id=%s effective_file_count=%d",
                    request.conversation_id,
                    request.run_id,
                    len(request.file_names),
                )
                raise ChatResourceError("已受理的文件对话缺少不可变文档快照")
            with self._conversation_factory.create() as conversation:
                logger.debug(
                    "已创建任务级文件对话端口，开始准备远端会话资源: conversation_id=%s run_id=%s",
                    request.conversation_id,
                    request.run_id,
                )
                refs, is_new_chat = self._open_or_reuse_conversation(
                    request=request,
                    session=session,
                    conversation=conversation,
                )
                logger.info(
                    "文件对话远端会话资源已准备: conversation_id=%s run_id=%s is_new_chat=%s",
                    request.conversation_id,
                    request.run_id,
                    is_new_chat,
                )
                active_documents = self._attach_new_documents(
                    request=request,
                    refs=refs,
                    documents=documents,
                    conversation=conversation,
                )
                logger.info(
                    "文件对话可用文档已准备: conversation_id=%s run_id=%s active_document_count=%d",
                    request.conversation_id,
                    request.run_id,
                    len(active_documents),
                )
                yield ChatStreamEvent(
                    "chatInfo",
                    {
                        "isNewChat": is_new_chat,
                    },
                )
                output_chars = 0
                logger.info(
                    "开始消费文件对话模型流: conversation_id=%s run_id=%s",
                    request.conversation_id,
                    request.run_id,
                )
                provider_terminal_seen = False
                source_snapshot_emitted = False
                policy = chat_policy_for(request.identity_kind)
                for provider_event in conversation.stream_message(
                    refs,
                    request.message,
                    document_refs=active_documents,
                ):
                    if isinstance(provider_event, ChatSourceFinalization):
                        provider_terminal_seen = True
                        logger.info(
                            "文件对话已接收完整来源终态: conversation_id=%s "
                            "run_id=%s source_count=%d",
                            request.conversation_id,
                            request.run_id,
                            len(provider_event.sources),
                        )
                        if policy.expose_source_chunks:
                            mapped_sources = ChatSourceMapper.map_sources(
                                provider_event.sources,
                                tuple(
                                    ChatSourceDocument(
                                        file_name=item.file_name,
                                        original_file_name=item.original_name,
                                        structured_source_key=item.structured_source_key,
                                    )
                                    for item in request.documents
                                ),
                            )
                            sanitized_source_count = sum(
                                1
                                for source, mapped in zip(
                                    provider_event.sources,
                                    mapped_sources,
                                )
                                if source.content != mapped.content
                            )
                            removed_char_count = sum(
                                len(source.content) - len(mapped.content)
                                for source, mapped in zip(
                                    provider_event.sources,
                                    mapped_sources,
                                )
                                if source.content != mapped.content
                            )
                            # 只记录关联 ID 和数量，禁止记录 Metadata、Chunk 正文、来源键或
                            # 文件身份。该日志既可审计规则是否命中，也不会把被删除的信息
                            # 重新写入日志系统。
                            logger.info(
                                "知识谱系对话来源公开清洗完成: conversation_id=%s "
                                "run_id=%s requested_architecture_id=%s "
                                "source_count=%d sanitized_source_count=%d "
                                "removed_char_count=%d",
                                request.conversation_id,
                                request.run_id,
                                request.requested_architecture_id,
                                len(provider_event.sources),
                                sanitized_source_count,
                                removed_char_count,
                            )
                            yield ChatStreamEvent(
                                "source_snapshot",
                                {
                                    "chunks": tuple(
                                        {
                                            "content": item.content,
                                            "fileName": item.file_name,
                                            "originalFileName": item.original_file_name,
                                        }
                                        for item in mapped_sources
                                    )
                                },
                            )
                            source_snapshot_emitted = True
                        continue
                    if isinstance(provider_event, ChatQueryRejection):
                        provider_terminal_seen = True
                        content = provider_event.content
                    elif isinstance(provider_event, ChatChunk):
                        content = provider_event.content
                    else:
                        raise ChatResourceError(
                            "chat provider returned an unsupported stream event"
                        )
                    output_chars += len(content)
                    if output_chars > self._max_output_chars:
                        logger.warning(
                            "文件对话模型输出超过上限，终止本次运行: conversation_id=%s run_id=%s output_chars=%d limit=%d",
                            request.conversation_id,
                            request.run_id,
                            output_chars,
                            self._max_output_chars,
                        )
                        raise ChatResourceError(
                            "chat output exceeds the configured output limit"
                        )
                    yield ChatStreamEvent("textChunk", {"content": content})
                if not provider_terminal_seen:
                    raise ChatResourceError(
                        "chat provider stream ended without a legal terminal"
                    )
                if policy.expose_source_chunks and not source_snapshot_emitted:
                    # Query 无上下文拒答没有常规 Finalization，但公开合同仍要求恰好一个
                    # 空来源快照；Recorder 会把它和成功终态一起收敛后再公开。
                    yield ChatStreamEvent("source_snapshot", {"chunks": ()})
                yield ChatStreamEvent("done", {})
                logger.info(
                    "文件对话模型流正常结束: conversation_id=%s run_id=%s output_chars=%d",
                    request.conversation_id,
                    request.run_id,
                    output_chars,
                )
        except GeneratorExit:
            raise
        except Exception:
            logger.exception(
                "文件对话新主链路执行异常: conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )
            yield ChatStreamEvent("error", {"error": "大模型服务响应异常"})

    def _open_or_reuse_conversation(
        self,
        *,
        request: ChatRunStreamRequest,
        session,
        conversation,
    ) -> tuple[ChatSessionRefs, bool]:
        if session.workspace_ref and session.thread_ref:
            logger.info(
                "复用已有文件对话远端会话资源: conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )
            return (
                ChatSessionRefs(
                    context_ref=session.workspace_ref,
                    conversation_ref=session.thread_ref,
                ),
                False,
            )
        workspace_lease_id = chat_workspace_lease_id(request.conversation_id)
        thread_lease_id = chat_thread_lease_id(request.conversation_id)
        self._store.resource_leases.begin(
            lease_id=workspace_lease_id,
            conversation_id=request.conversation_id,
            resource_type=RESOURCE_WORKSPACE,
            run_id=request.run_id,
        )
        self._store.resource_leases.begin(
            lease_id=thread_lease_id,
            conversation_id=request.conversation_id,
            resource_type=RESOURCE_THREAD,
            run_id=request.run_id,
        )
        logger.info(
            "已创建文件对话工作区和线程计划租约: conversation_id=%s run_id=%s",
            request.conversation_id,
            request.run_id,
        )
        try:
            refs = conversation.open_conversation(
                context_name=request.workspace_name,
                conversation_name=f"thread-{request.conversation_id}",
            )
        except ChatResourceError as exc:
            logger.warning(
                "创建文件对话远端会话资源失败，开始记录可恢复状态: conversation_id=%s run_id=%s reported_resource_count=%d",
                request.conversation_id,
                request.run_id,
                len(exc.resource_refs),
            )
            self._record_uncompensated_workspace_reference(
                request=request,
                workspace_lease_id=workspace_lease_id,
                resource_refs=exc.resource_refs,
            )
            self._close_planned_lease(thread_lease_id)
            if not exc.resource_refs:
                self._close_planned_lease(workspace_lease_id)
            raise
        # 变更会话前先保存远端引用。若后续会话更新失败，清理流程仍可发现这两类资源。
        self._store.resource_leases.activate(
            lease_id=workspace_lease_id,
            external_ref=refs.context_ref,
        )
        self._store.resource_leases.activate(
            lease_id=thread_lease_id,
            external_ref=chat_scoped_external_ref(
                context_ref=refs.context_ref,
                resource_ref=refs.conversation_ref,
            ),
        )
        self._store.sessions.update_refs(
            conversation_id=request.conversation_id,
            workspace_ref=refs.context_ref,
            thread_ref=refs.conversation_ref,
        )
        logger.info(
            "文件对话远端会话资源已持久化到本地: conversation_id=%s run_id=%s",
            request.conversation_id,
            request.run_id,
        )
        return refs, True

    def _record_uncompensated_workspace_reference(
        self,
        *,
        request: ChatRunStreamRequest,
        workspace_lease_id: str,
        resource_refs: Sequence[str],
    ) -> None:
        """持久化由适配器报告且可恢复的工作区引用。"""
        if not resource_refs:
            return
        workspace_ref = resource_refs[0]
        try:
            lease = self._store.resource_leases.begin(
                lease_id=workspace_lease_id,
                conversation_id=request.conversation_id,
                resource_type=RESOURCE_WORKSPACE,
                run_id=request.run_id,
                external_ref=workspace_ref,
            )
            if lease.status == "planned":
                self._store.resource_leases.activate(
                    lease_id=workspace_lease_id,
                    external_ref=workspace_ref,
                )
            logger.info(
                "已记录可恢复的文件对话工作区引用: conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )
        except Exception:
            logger.exception(
                "记录可恢复的文件对话工作区引用失败: conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )

    def _close_planned_lease(self, lease_id: str) -> None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is not None and lease.status == "planned":
            self._store.resource_leases.mark_closed(lease_id)
            logger.info("远端资源尚未创建，已关闭文件对话计划租约: lease_id=%s", lease_id)

    def _attach_new_documents(
        self,
        *,
        request: ChatRunStreamRequest,
        refs: ChatSessionRefs,
        documents: Sequence[ChatRunDocumentSnapshot],
        conversation,
    ) -> tuple[str, ...]:
        current_bindings = {
            item.file_name: item
            for item in self._store.document_bindings.list_current_by_chat(
                request.conversation_id
            )
        }
        logger.debug(
            "开始准备文件对话文档绑定: conversation_id=%s run_id=%s requested_document_count=%d current_binding_count=%d",
            request.conversation_id,
            request.run_id,
            len(documents),
            len(current_bindings),
        )
        # 未显式传入 ``fileNames`` 的续聊会刻意选择每个业务文件的最新版本。历史版本
        # 仍保留绑定以供审计和清理，但绝不会被静默加入 RAG。
        if not documents:
            selected_existing = tuple(
                binding.document_ref
                for binding in current_bindings.values()
                if binding.document_ref
            )
            logger.info(
                "续聊未指定新文件，复用当前文档版本: conversation_id=%s run_id=%s selected_document_count=%d",
                request.conversation_id,
                request.run_id,
                len(selected_existing),
            )
            return selected_existing
        new_documents = [
            item
            for item in documents
            if (
                item.file_name not in current_bindings
                or current_bindings[item.file_name].document_ref
                != item.document.document_ref
                or current_bindings[item.file_name].external_location
                != item.document.external_location
            )
        ]
        logger.info(
            "已计算本次需要新绑定的文件对话文档: conversation_id=%s run_id=%s new_document_count=%d",
            request.conversation_id,
            request.run_id,
            len(new_documents),
        )
        for item in new_documents:
            self._store.resource_leases.begin(
                lease_id=chat_document_binding_lease_id(
                    conversation_id=request.conversation_id,
                    file_name=item.file_name,
                    document_ref=item.document.document_ref,
                ),
                conversation_id=request.conversation_id,
                resource_type=RESOURCE_DOCUMENT_BINDING,
                run_id=request.run_id,
                # 仅按 document_ref 解析的适配器可以合法地没有文档位置。应在租约中
                # 保留该可选身份，而不是在适配器有机会绑定前拒绝已受理运行。
                external_ref=(
                    f"{refs.context_ref}::{item.document.external_location}"
                ),
            )
        if new_documents:
            logger.info(
                "开始调用远端绑定新文件对话文档: conversation_id=%s run_id=%s document_count=%d",
                request.conversation_id,
                request.run_id,
                len(new_documents),
            )
            attached = conversation.attach_documents(
                refs,
                [item.document for item in new_documents],
            )
            attached_by_location: dict[str, list[ChatDocumentRef]] = {}
            for index, attached_document in enumerate(attached):
                if not isinstance(attached_document, ChatDocumentRef):
                    logger.error(
                        "远端文档绑定结果类型无效，执行失败关闭: "
                        "conversation_id=%s run_id=%s result_index=%d result_type=%s",
                        request.conversation_id,
                        request.run_id,
                        index,
                        type(attached_document).__name__,
                    )
                    raise ChatResourceError("远端文档绑定结果无效")
                normalized_location = self._normalize_external_location(
                    attached_document.external_location
                )
                if normalized_location:
                    attached_by_location.setdefault(
                        normalized_location,
                        [],
                    ).append(attached_document)

            confirmed_documents: list[ChatDocumentRef] = []
            for item in new_documents:
                requested_location = self._normalize_external_location(
                    item.document.external_location
                )
                matches = attached_by_location.get(requested_location, [])
                if len(matches) != 1:
                    logger.error(
                        "远端文档绑定结果无法唯一确认，执行失败关闭: "
                        "conversation_id=%s run_id=%s matched_count=%d "
                        "requested_document_count=%d returned_document_count=%d",
                        request.conversation_id,
                        request.run_id,
                        len(matches),
                        len(new_documents),
                        len(attached),
                    )
                    raise ChatResourceError("远端文档绑定结果无法唯一确认")
                returned_document = matches[0]
                # external_location 是本次绑定回执的匹配键；同一位置只允许唯一结果。
                # 供应商可以返回规范化后的 document_ref，后续执行和本地 Binding 均以
                # 该回执值为准，但不得用另一个远端位置替换已冻结的文档。
                confirmed_documents.append(
                    ChatDocumentRef(
                        document_ref=returned_document.document_ref,
                        external_location=requested_location,
                    )
                )

            # 必须先完整确认全部回执，再写任何本地 Binding 或激活租约，避免半批成功。
            for item, attached_document in zip(
                new_documents,
                confirmed_documents,
                strict=True,
            ):
                stored_binding = self._store.document_bindings.add(
                    conversation_id=request.conversation_id,
                    file_name=item.file_name,
                    original_name=item.original_name,
                    document_ref=attached_document.document_ref,
                    external_location=attached_document.external_location,
                    structured_source_key=item.structured_source_key,
                    added_by_run_id=request.run_id,
                )
                self._store.resource_leases.activate(
                    lease_id=chat_document_binding_lease_id(
                        conversation_id=request.conversation_id,
                        file_name=item.file_name,
                        document_ref=item.document.document_ref,
                    ),
                    external_ref=(
                        f"{refs.context_ref}::{attached_document.external_location}"
                    ),
                )
                current_bindings[item.file_name] = stored_binding
            logger.info(
                "新文件对话文档绑定已持久化: conversation_id=%s run_id=%s document_count=%d",
                request.conversation_id,
                request.run_id,
                len(new_documents),
            )
        selected: list[str] = []
        for item in documents:
            stored = current_bindings.get(item.file_name)
            document_ref = (
                stored.document_ref
                if stored and stored.document_ref
                else item.document.document_ref
            )
            selected.append(document_ref)
        logger.debug(
            "文件对话文档选择完成: conversation_id=%s run_id=%s selected_document_count=%d",
            request.conversation_id,
            request.run_id,
            len(selected),
        )
        return tuple(selected)

    @staticmethod
    def _normalize_external_location(value: str) -> str:
        """统一远端文档位置分隔符，避免同一位置因平台写法不同而匹配失败。"""
        return str(value or "").strip().replace("\\", "/")

    @staticmethod
    def _snapshot(document: ResolvedChatDocument) -> ChatRunDocumentSnapshot:
        return ChatRunDocumentSnapshot(
            file_name=document.file_name,
            original_name=document.original_name,
            document=document.document,
            structured_source_key=document.structured_source_key,
        )

    @staticmethod
    def _candidate(document: ResolvedChatDocument) -> ChatDocumentCandidate:
        """把 Resolver DTO 收敛为不依赖 Resolver/Database 的受理候选。"""
        return ChatDocumentCandidate(
            file_name=document.file_name,
            original_name=document.original_name,
            document_ref=document.document.document_ref,
            external_location=document.document.external_location,
            structured_source_key=document.structured_source_key,
        )


class ChatRunEventRecorder:
    """在保留流事件的同时持久化本地权威消息。"""

    def __init__(
        self,
        store: ChatPersistenceStore,
        *,
        heartbeat_interval_seconds: float = 10.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        self._store = store
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    def record(
        self,
        *,
        request: ChatRunStreamRequest,
        events: Iterable[ChatStreamEvent],
        chat_commands: ChatCommandService,
        execution_lease: ChatRunLease | None = None,
    ) -> Iterator[ChatStreamEvent]:
        user_message_id = self._message_id(request.run_id, MESSAGE_ROLE_USER)
        assistant_message_id = self._message_id(
            request.run_id,
            MESSAGE_ROLE_ASSISTANT,
        )
        user_written = False
        terminal_event = ""
        assistant_parts: list[str] = []
        source_snapshot_event: ChatStreamEvent | None = None
        last_heartbeat_at: float | None = None
        try:
            if execution_lease is not None:
                # 在访问外部会话资源之前校验运行权。当前 SQLite 仅作单实例
                # 校验；未来协调器会在这里校验实际 token/fencing 信息。
                chat_commands.validate_execution_lease(lease=execution_lease)
            logger.info(
                "开始记录文件对话运行事件: "
                "conversation_id=%s run_id=%s requested_file_count=%d "
                "effective_file_count=%d",
                request.conversation_id,
                request.run_id,
                len(request.requested_file_names),
                len(request.file_names),
            )
            self._append_user_pending(
                request=request,
                message_id=user_message_id,
            )
            user_written = True
            logger.debug(
                "用户消息已写入pending: conversation_id=%s run_id=%s message_id=%s",
                request.conversation_id,
                request.run_id,
                user_message_id,
            )

            event_iterator = iter(events)
            while True:
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "消费上游事件前检测到中断: conversation_id=%s run_id=%s",
                        request.conversation_id,
                        request.run_id,
                    )
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                    break
                try:
                    event = next(event_iterator)
                except StopIteration:
                    logger.warning(
                        "文件对话运行上游事件流未产生终态便结束: conversation_id=%s run_id=%s",
                        request.conversation_id,
                        request.run_id,
                    )
                    break
                if not isinstance(event, ChatStreamEvent):
                    raise TypeError("chat stream must yield ChatStreamEvent")
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "处理上游事件前检测到中断: conversation_id=%s run_id=%s upstream_event=%s",
                        request.conversation_id,
                        request.run_id,
                        event.event_type,
                    )
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                    break
                if event.event_type == "source_snapshot":
                    if not chat_policy_for(request.identity_kind).expose_source_chunks:
                        raise ValueError("file chat cannot emit source_snapshot")
                    if source_snapshot_event is not None:
                        raise ValueError("chat stream emitted duplicate source_snapshot")
                    # 先严格构造一次持久化 DTO，验证字段集合和正文类型；直到 done 到达
                    # 才会在成功事务中写入，客户端此刻也看不到该内部快照。
                    self._source_chunks_from_event(
                        event=event,
                        assistant_message_id=assistant_message_id,
                    )
                    source_snapshot_event = event
                    continue
                if event.event_type == "textChunk":
                    content = event.data.get("content")
                    if isinstance(content, str) and content:
                        assistant_parts.append(content)
                if event.event_type in _TERMINAL_EVENT_TYPES:
                    logger.info(
                        "文件对话运行收到终态事件: conversation_id=%s run_id=%s event=%s chunks=%d",
                        request.conversation_id,
                        request.run_id,
                        event.event_type,
                        len(assistant_parts),
                    )
                    if event.event_type == "done":
                        policy = chat_policy_for(request.identity_kind)
                        if policy.expose_source_chunks and source_snapshot_event is None:
                            raise ValueError("weaponry chat completion lacks source_snapshot")
                        source_chunks = (
                            ()
                            if source_snapshot_event is None
                            else self._source_chunks_from_event(
                                event=source_snapshot_event,
                                assistant_message_id=assistant_message_id,
                            )
                        )
                        chat_commands.complete_chat_run_with_messages(
                            run_id=request.run_id,
                            user_message_id=user_message_id,
                            assistant_message_id=assistant_message_id,
                            assistant_content="".join(assistant_parts),
                            source_chunks=source_chunks,
                            terminal_event=event,
                            **self._execution_lease_kwargs(execution_lease),
                        )
                    elif event.event_type == "aborted":
                        chat_commands.abort_chat_run_with_user(
                            run_id=request.run_id,
                            user_message_id=user_message_id,
                            terminal_event=event,
                            **self._execution_lease_kwargs(execution_lease),
                        )
                    else:
                        chat_commands.fail_chat_run_with_user(
                            run_id=request.run_id,
                            user_message_id=user_message_id,
                            error_message="chat stream emitted error event",
                            terminal_event=event,
                            **self._execution_lease_kwargs(execution_lease),
                        )
                    terminal_event = event.event_type
                else:
                    # 当前 HTTP/SSE 路径刻意在产出前写入这个单事件。上游模型流是惰性的：
                    # 在此处按大小或时间聚合，要么会无限延迟首个 token，要么需要生产者线程
                    # 在客户端断开后继续运行模型。未来基于队列的工作进程可使用
                    # ``append_many``，在持久化生产者/消费者生命周期下安全地权衡这一点。
                    self._store.events.append(
                        run_id=request.run_id,
                        event=event,
                    )
                    last_heartbeat_at = self._heartbeat_if_due(
                        run_id=request.run_id,
                        chat_commands=chat_commands,
                        last_heartbeat_at=last_heartbeat_at,
                        execution_lease=execution_lease,
                    )
                if event.event_type == "done" and source_snapshot_event is not None:
                    # 成功事实已经原子落库；即使客户端在本事件后、done 前断开，History
                    # 也能立即读取与 SSE 值和顺序完全一致的 Chunk。
                    yield source_snapshot_event
                yield event
                if terminal_event:
                    break

            if not terminal_event:
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "上游无终态结束但检测到中断请求: conversation_id=%s run_id=%s",
                        request.conversation_id,
                        request.run_id,
                    )
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                else:
                    logger.warning(
                        "文件对话运行因缺失终态标记而失败: conversation_id=%s run_id=%s",
                        request.conversation_id,
                        request.run_id,
                    )
                    self._finish_failed(
                        request=request,
                        run_id=request.run_id,
                        user_message_id=user_message_id,
                        error_message="chat stream ended without terminal event",
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
        except GeneratorExit:
            if user_written and not terminal_event:
                if self._abort_requested(request.run_id):
                    terminal_event = "aborted"
                    logger.info(
                        "SSE连接关闭时检测到中断请求: conversation_id=%s run_id=%s",
                        request.conversation_id,
                        request.run_id,
                    )
                    self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                else:
                    logger.warning(
                        "SSE连接在终态前关闭: conversation_id=%s run_id=%s",
                        request.conversation_id,
                        request.run_id,
                    )
                    self._finish_failed(
                        request=request,
                        run_id=request.run_id,
                        user_message_id=user_message_id,
                        error_message="chat stream closed before completion",
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
            raise
        except Exception as exc:
            if not terminal_event and self._abort_requested(request.run_id):
                terminal_event = "aborted"
                logger.warning(
                    "上游异常后检测到中断请求，按中断收敛: conversation_id=%s run_id=%s error_type=%s",
                    request.conversation_id,
                    request.run_id,
                    exc.__class__.__name__,
                )
                if user_written:
                    yield self._finish_aborted(
                        request=request,
                        user_message_id=user_message_id,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                else:
                    chat_commands.abort_chat_run(run_id=request.run_id)
                return
            if not terminal_event:
                logger.exception(
                    "文件对话运行事件记录发生异常，按失败状态收敛: conversation_id=%s run_id=%s",
                    request.conversation_id,
                    request.run_id,
                )
                if user_written:
                    self._finish_failed(
                        request=request,
                        run_id=request.run_id,
                        user_message_id=user_message_id,
                        error_message=str(exc) or exc.__class__.__name__,
                        chat_commands=chat_commands,
                        execution_lease=execution_lease,
                    )
                else:
                    chat_commands.fail_chat_run(
                        run_id=request.run_id,
                        error_message=str(exc) or exc.__class__.__name__,
                    )
            raise
        finally:
            self._close_events(events=events, run_id=request.run_id)

    @staticmethod
    def _source_chunks_from_event(
        *,
        event: ChatStreamEvent,
        assistant_message_id: str,
    ) -> tuple[ChatMessageSourceChunk, ...]:
        """严格把已清洗的内部来源快照转换为原子持久化 DTO，不再改写正文。"""
        if event.event_type != "source_snapshot" or set(event.data) != {"chunks"}:
            raise ValueError("source_snapshot event fields are invalid")
        raw_chunks = event.data["chunks"]
        if isinstance(raw_chunks, (str, bytes)) or not isinstance(
            raw_chunks,
            Sequence,
        ):
            raise TypeError("source_snapshot chunks must be a sequence")
        created_at = datetime.now(timezone.utc).isoformat()
        chunks: list[ChatMessageSourceChunk] = []
        for position, raw_chunk in enumerate(raw_chunks):
            if not isinstance(raw_chunk, dict) or set(raw_chunk) != {
                "content",
                "fileName",
                "originalFileName",
            }:
                raise ValueError("source_snapshot chunk fields are invalid")
            content = raw_chunk["content"]
            file_name = raw_chunk["fileName"]
            original_file_name = raw_chunk["originalFileName"]
            if not isinstance(content, str):
                raise TypeError("source_snapshot content must be str")
            if not content.strip():
                raise ValueError("source_snapshot content cannot be empty")
            if not isinstance(file_name, str) or not file_name.strip():
                raise ValueError("source_snapshot fileName must be non-empty str")
            if (
                not isinstance(original_file_name, str)
                or not original_file_name.strip()
            ):
                raise ValueError(
                    "source_snapshot originalFileName must be non-empty str"
                )
            chunks.append(
                ChatMessageSourceChunk(
                    message_id=assistant_message_id,
                    position=position,
                    content=content,
                    file_name=file_name,
                    original_file_name=original_file_name,
                    created_at=created_at,
                )
            )
        return tuple(chunks)

    def _append_user_pending(
        self,
        *,
        request: ChatRunStreamRequest,
        message_id: str,
    ) -> None:
        # 用户消息先以待处理状态写入，只有运行明确进入完成、失败或中断等终态后
        # 才提交为已提交状态。这样可避免进程崩溃时将未完成轮次误暴露给历史接口。
        self._store.messages.append(
            message_id=message_id,
            conversation_id=request.conversation_id,
            run_id=request.run_id,
            role=MESSAGE_ROLE_USER,
            content=request.message,
            status=MESSAGE_PENDING,
            files=tuple(
                zip(
                    request.requested_file_names,
                    request.requested_file_original_names,
                )
            ),
            architecture_id=request.requested_architecture_id,
        )

    def _commit_user(self, message_id: str) -> None:
        self._store.messages.set_status(
            message_id=message_id,
            status=MESSAGE_COMMITTED,
        )

    def _append_assistant_committed(
        self,
        *,
        request: ChatRunStreamRequest,
        message_id: str,
        content: str,
    ) -> None:
        if not content:
            logger.info(
                "跳过空助手消息入库: conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )
            return
        self._store.messages.append(
            message_id=message_id,
            conversation_id=request.conversation_id,
            run_id=request.run_id,
            role=MESSAGE_ROLE_ASSISTANT,
            content=content,
            status=MESSAGE_COMMITTED,
        )

    def _finish_aborted(
        self,
        *,
        request: ChatRunStreamRequest,
        user_message_id: str,
        chat_commands: ChatCommandService,
        execution_lease: ChatRunLease | None,
    ) -> ChatStreamEvent:
        # 中断时保留已提交的用户消息，丢弃已输出但不完整的助手片段；这是本地历史的
        # 权威语义，不依赖供应商是否已经写入远端线程。
        event = ChatStreamEvent(
            "aborted",
            {},
        )
        chat_commands.abort_chat_run_with_user(
            run_id=request.run_id,
            user_message_id=user_message_id,
            terminal_event=event,
            **self._execution_lease_kwargs(execution_lease),
        )
        logger.info(
            "文件对话运行已按中断完成收敛: conversation_id=%s run_id=%s",
            request.conversation_id,
            request.run_id,
        )
        return event

    @staticmethod
    def _failure_event(*, error_message: str) -> ChatStreamEvent:
        return ChatStreamEvent("error", {"error": error_message})

    def _finish_failed(
        self,
        *,
        request: ChatRunStreamRequest,
        run_id: str,
        user_message_id: str,
        error_message: str,
        chat_commands: ChatCommandService,
        execution_lease: ChatRunLease | None,
    ) -> None:
        """在保留运行收敛能力的同时，持久化未展示的终态错误。

        已冻结协议中，客户端断开连接不会产生最后一个 SSE 帧。内部事件账本仍需记录
        终态，但账本写入失败不能使活动运行永久保持锁定。
        """
        terminal_event = self._failure_event(error_message=error_message)
        try:
            chat_commands.fail_chat_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                error_message=error_message,
                terminal_event=terminal_event,
                **self._execution_lease_kwargs(execution_lease),
            )
            logger.warning(
                "文件对话运行已按失败路径收敛: conversation_id=%s run_id=%s error_chars=%d",
                request.conversation_id,
                run_id,
                len(str(error_message or "")),
            )
        except Exception:
            logger.exception(
                "终态事件写入失败，使用降级路径收敛文件对话运行: conversation_id=%s run_id=%s",
                request.conversation_id,
                run_id,
            )
            chat_commands.fail_chat_run_with_user(
                run_id=run_id,
                user_message_id=user_message_id,
                error_message=error_message,
                **self._execution_lease_kwargs(execution_lease),
            )
            logger.warning(
                "文件对话运行已使用降级路径收敛失败状态: conversation_id=%s run_id=%s error_chars=%d",
                request.conversation_id,
                run_id,
                len(str(error_message or "")),
            )

    def _abort_requested(self, run_id: str) -> bool:
        run = self._store.runs.get(run_id)
        return bool(run and run.abort_requested)

    def _heartbeat_if_due(
        self,
        *,
        run_id: str,
        chat_commands: ChatCommandService,
        last_heartbeat_at: float | None,
        execution_lease: ChatRunLease | None,
    ) -> float:
        now = monotonic()
        if (
            last_heartbeat_at is not None
            and now - last_heartbeat_at < self._heartbeat_interval_seconds
        ):
            return last_heartbeat_at
        chat_commands.heartbeat_chat_run(
            run_id=run_id,
            **self._execution_lease_kwargs(execution_lease),
        )
        return now

    @staticmethod
    def _execution_lease_kwargs(
        execution_lease: ChatRunLease | None,
    ) -> dict[str, ChatRunLease]:
        """仅在有内部执行租约时把它传给命令层，兼容已有离线调用。"""
        if execution_lease is None:
            return {}
        return {"execution_lease": execution_lease}

    @staticmethod
    def _message_id(run_id: str, role: str) -> str:
        return f"{run_id}:{role}"

    @staticmethod
    def _close_events(*, events: Iterable[ChatStreamEvent], run_id: str) -> None:
        close = getattr(events, "close", None)
        if not callable(close):
            return
        try:
            close()
            logger.debug("已关闭文件对话上游事件流: run_id=%s", run_id)
        except Exception:
            logger.exception("关闭文件对话上游事件流失败: run_id=%s", run_id)


def record_chat_run_events(
    *,
    request: ChatRunStreamRequest,
    events: Iterable[ChatStreamEvent],
    store: ChatPersistenceStore,
    chat_commands: ChatCommandService,
    execution_lease: ChatRunLease | None = None,
) -> Iterator[ChatStreamEvent]:
    return ChatRunEventRecorder(store).record(
        request=request,
        events=events,
        chat_commands=chat_commands,
        execution_lease=execution_lease,
    )


__all__ = [
    "ChatRunEventRecorder",
    "ChatRunExecutor",
    "ChatRunStreamRequest",
    "record_chat_run_events",
]
