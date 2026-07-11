"""文件对话端口的可编程内存测试替身。"""

from __future__ import annotations

from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator, Sequence

from app.ports import (
    ChatChunk,
    ChatConversationNotFoundError,
    ChatDocumentRef,
    ChatMessageSnapshot,
    ChatOperationResult,
    ChatResourceError,
    ChatRole,
    ChatSessionRefs,
)


def _required_text(value: str, *, name: str) -> str:
    """规范化并校验测试替身操作所需的非空文本参数。"""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _required_content(value: str, *, name: str) -> str:
    """校验模型或历史正文，同时保留原始空白字符。"""
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    if value == "":
        raise ValueError(f"{name} 不能为空")
    return value


class _FakeChatConversationState:
    """多个请求级 Fake Port 共享的文件对话内存后端。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.conversation_sequence = 0
        self.known_context_refs: set[str] = set()
        self.sessions_by_conversation: dict[str, ChatSessionRefs] = {}
        self.documents_by_conversation: dict[str, tuple[ChatDocumentRef, ...]] = {}
        self.messages_by_conversation: dict[str, list[ChatMessageSnapshot]] = {}
        self.deleted_conversations: set[str] = set()
        self.deleted_contexts: set[str] = set()
        self.standalone_prompts: list[tuple[str, str]] = []


class FakeChatConversationPort:
    """无外部副作用的文件对话端口测试实现。"""

    def __init__(
        self,
        *,
        state: _FakeChatConversationState | None = None,
        stream_contents: Sequence[str] | None = None,
        standalone_reply: str = "模拟标题",
        open_conversation_error_message: str = "",
        open_conversation_resource_refs: Sequence[str] = (),
        delete_conversation_error_message: str = "",
        delete_context_error_message: str = "",
    ) -> None:
        """配置后续流式回复和一次性回复。"""
        self._state = state or _FakeChatConversationState()
        self._lock = self._state.lock
        self._stream_contents = tuple(stream_contents or ("模拟回答",))
        self._standalone_reply = _required_content(
            standalone_reply,
            name="standalone_reply",
        )
        self._open_conversation_error_message = str(
            open_conversation_error_message or ""
        ).strip()
        self._open_conversation_resource_refs = tuple(
            str(resource_ref or "").strip()
            for resource_ref in open_conversation_resource_refs
        )
        self._delete_conversation_error_message = str(
            delete_conversation_error_message or ""
        ).strip()
        self._delete_context_error_message = str(
            delete_context_error_message or ""
        ).strip()
        self.standalone_prompts = self._state.standalone_prompts

    def open_conversation(
        self,
        *,
        context_name: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        """创建一个可用于后续测试调用的对话引用。"""
        _required_text(context_name, name="context_name")
        _required_text(conversation_name, name="conversation_name")
        if self._open_conversation_error_message:
            raise ChatResourceError(
                self._open_conversation_error_message,
                resource_refs=self._open_conversation_resource_refs,
            )
        with self._lock:
            self._state.conversation_sequence += 1
            sequence = self._state.conversation_sequence
            session = ChatSessionRefs(
                context_ref=f"context:{sequence}",
                conversation_ref=f"conversation:{sequence}",
            )
            self._state.known_context_refs.add(session.context_ref)
            self._state.sessions_by_conversation[session.conversation_ref] = session
            self._state.documents_by_conversation[session.conversation_ref] = ()
            self._state.messages_by_conversation[session.conversation_ref] = []
            return session

    def attach_documents(
        self,
        session: ChatSessionRefs,
        documents: Sequence[ChatDocumentRef],
    ) -> tuple[ChatDocumentRef, ...]:
        """把文档引用加入测试对话，重复引用保持幂等。"""
        with self._lock:
            known_session = self._require_session(session)
            existing = list(
                self._state.documents_by_conversation[
                    known_session.conversation_ref
                ]
            )
            seen = {document.document_ref for document in existing}
            for document in documents:
                if not isinstance(document, ChatDocumentRef):
                    raise TypeError("documents 只能包含 ChatDocumentRef")
                if document.document_ref in seen:
                    continue
                existing.append(document)
                seen.add(document.document_ref)
            snapshot = tuple(existing)
            self._state.documents_by_conversation[
                known_session.conversation_ref
            ] = snapshot
            return snapshot

    def stream_message(
        self,
        session: ChatSessionRefs,
        message: str,
        *,
        document_refs: Sequence[str] = (),
    ) -> Iterator[ChatChunk]:
        """记录用户消息，流式返回预设片段，并在完整消费后提交助手消息。"""
        with self._lock:
            known_session = self._require_session(session)
            normalized_message = _required_text(message, name="message")
            linked_documents = self._resolve_linked_documents(
                known_session,
                document_refs,
            )
            self._state.messages_by_conversation[
                known_session.conversation_ref
            ].append(
                ChatMessageSnapshot(
                    role=ChatRole.USER,
                    content=normalized_message,
                    linked_documents=linked_documents,
                )
            )

        response_parts: list[str] = []
        for index, raw_content in enumerate(self._stream_contents, start=1):
            content = _required_content(raw_content, name="stream_content")
            response_parts.append(content)
            yield ChatChunk(content=content, sequence_no=index)

        with self._lock:
            self._require_session(session)
            self._state.messages_by_conversation[
                session.conversation_ref
            ].append(
                ChatMessageSnapshot(
                    role=ChatRole.ASSISTANT,
                    content="".join(response_parts),
                )
            )

    def fetch_messages(
        self,
        session: ChatSessionRefs,
    ) -> tuple[ChatMessageSnapshot, ...]:
        """返回目标测试对话的消息快照。"""
        with self._lock:
            known_session = self._require_session(session)
            return tuple(
                self._state.messages_by_conversation[
                    known_session.conversation_ref
                ]
            )

    def open_temporary_conversation(
        self,
        *,
        context_ref: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        """创建由标题服务显式清理的临时 thread。"""
        normalized_context_ref = _required_text(context_ref, name="context_ref")
        _required_text(conversation_name, name="conversation_name")
        with self._lock:
            if (
                normalized_context_ref not in self._state.known_context_refs
                or normalized_context_ref in self._state.deleted_contexts
            ):
                raise ChatConversationNotFoundError("目标上下文不存在")
            self._state.conversation_sequence += 1
            session = ChatSessionRefs(
                context_ref=normalized_context_ref,
                conversation_ref=f"temporary:{self._state.conversation_sequence}",
            )
            self._state.sessions_by_conversation[session.conversation_ref] = session
            self._state.documents_by_conversation[session.conversation_ref] = ()
            self._state.messages_by_conversation[session.conversation_ref] = []
            return session

    def generate_temporary_reply(
        self,
        *,
        session: ChatSessionRefs,
        prompt: str,
    ) -> str:
        """返回预设标题回复，并证明它没有写入主对话消息。"""
        normalized_prompt = _required_text(prompt, name="prompt")
        with self._lock:
            known_session = self._require_session(session)
            self._state.standalone_prompts.append(
                (known_session.context_ref, normalized_prompt)
            )
            return self._standalone_reply

    def delete_conversation(
        self,
        session: ChatSessionRefs,
    ) -> ChatOperationResult:
        """幂等删除测试对话资源。"""
        conversation_ref = session.conversation_ref
        with self._lock:
            if self._delete_conversation_error_message:
                return ChatOperationResult(
                    success=False,
                    error_message=self._delete_conversation_error_message,
                )
            if conversation_ref in self._state.deleted_conversations:
                return ChatOperationResult(success=True, already_applied=True)
            self._require_session(session)
            self._state.deleted_conversations.add(conversation_ref)
            return ChatOperationResult(success=True, already_applied=False)

    def delete_context(
        self,
        context_ref: str,
    ) -> ChatOperationResult:
        """幂等删除测试上下文资源。"""
        normalized_context_ref = _required_text(context_ref, name="context_ref")
        with self._lock:
            if self._delete_context_error_message:
                return ChatOperationResult(
                    success=False,
                    error_message=self._delete_context_error_message,
                )
            if normalized_context_ref in self._state.deleted_contexts:
                return ChatOperationResult(success=True, already_applied=True)
            if normalized_context_ref not in self._state.known_context_refs:
                raise ChatConversationNotFoundError("目标上下文不存在")
            self._state.deleted_contexts.add(normalized_context_ref)
            return ChatOperationResult(success=True, already_applied=False)

    def _require_session(self, session: ChatSessionRefs) -> ChatSessionRefs:
        """确认会话引用由当前测试端口创建且尚未删除。"""
        if not isinstance(session, ChatSessionRefs):
            raise TypeError("session 必须是 ChatSessionRefs")
        known_session = self._state.sessions_by_conversation.get(
            session.conversation_ref
        )
        if (
            known_session is None
            or known_session != session
            or session.conversation_ref in self._state.deleted_conversations
        ):
            raise ChatConversationNotFoundError("目标对话不存在")
        return known_session

    def _resolve_linked_documents(
        self,
        session: ChatSessionRefs,
        document_refs: Sequence[str],
    ) -> tuple[ChatDocumentRef, ...]:
        """按请求引用返回本轮用户消息关联的文档快照。"""
        requested_refs = tuple(
            _required_text(document_ref, name="document_ref")
            for document_ref in document_refs
        )
        if not requested_refs:
            return ()
        documents = self._state.documents_by_conversation[session.conversation_ref]
        documents_by_ref = {document.document_ref: document for document in documents}
        missing_refs = [
            document_ref
            for document_ref in requested_refs
            if document_ref not in documents_by_ref
        ]
        if missing_refs:
            raise ChatResourceError("请求引用了尚未加入对话的文档")
        return tuple(documents_by_ref[document_ref] for document_ref in requested_refs)


class FakeChatConversationFactory:
    """按请求创建独立 Fake Chat Port 的可观察工厂。"""

    def __init__(self, **port_options: Any) -> None:
        """保存后续每个 Fake Port 都要使用的独立配置快照。"""
        self._port_options = dict(port_options)
        self._ports: list[FakeChatConversationPort] = []
        self._active_leases = 0
        self._state = _FakeChatConversationState()

    @property
    def ports(self) -> tuple[FakeChatConversationPort, ...]:
        """返回已经进入过请求作用域的 Port 快照。"""
        return tuple(self._ports)

    @property
    def active_leases(self) -> int:
        """返回当前尚未退出的请求租约数量。"""
        return self._active_leases

    @contextmanager
    def create(self) -> Iterator[FakeChatConversationPort]:
        """为一次请求创建独立 Fake Port，并准确维护活动租约计数。"""
        port = FakeChatConversationPort(
            state=self._state,
            **self._port_options,
        )
        self._ports.append(port)
        self._active_leases += 1
        try:
            yield port
        finally:
            self._active_leases -= 1
