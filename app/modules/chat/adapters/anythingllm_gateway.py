"""基于 AnythingLLM 的文件对话端口实现。"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional, Sequence

from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTransportError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    AnythingLLMFinalization,
    AnythingLLMQueryRejection,
    AnythingLLMTextDelta,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.policies import chat_workspace_settings
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.modules.chat.ports import (
    ChatChunk,
    ChatConversationNotFoundError,
    ChatConversationPort,
    ChatDocumentRef,
    ChatMessageSnapshot,
    ChatOperationResult,
    ChatResourceError,
    ChatResponseError,
    ChatRole,
    ChatQueryRejection,
    ChatSessionRefs,
    ChatSourceEvidence,
    ChatSourceFinalization,
)


logger = logging.getLogger(__name__)

_ALLOWED_CHAT_MODES = frozenset({"chat", "query"})
_THINK_BLOCK_PATTERN = re.compile(r"<think>[\s\S]*?(?:</think>|$)")


class AnythingLLMChatGateway(ChatConversationPort):
    """在文件对话端口之后编排 AnythingLLM 工作区和线程操作。"""

    def __init__(
        self,
        workspace_client: AnythingLLMWorkspaceClient,
        thread_client: AnythingLLMThreadClient,
        *,
        user_id: Optional[int] = 1,
        workspace_settings: Optional[Mapping[str, Any]] = None,
        stream_mode: str = "query",
        standalone_mode: str = "chat",
    ) -> None:
        """绑定任务级原子客户端与不可变的对话策略。"""
        if user_id is not None and (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1
        ):
            raise ValueError("user_id must be a positive integer or None")
        self._workspace_client = workspace_client
        self._thread_client = thread_client
        self._user_id = user_id
        self._workspace_settings = MappingProxyType(
            dict(
                chat_workspace_settings()
                if workspace_settings is None
                else workspace_settings
            )
        )
        self._stream_mode = self._normalize_mode(stream_mode)
        self._standalone_mode = self._normalize_mode(standalone_mode)

    def open_conversation(
        self,
        *,
        context_name: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        """创建或复用指定名称的对话上下文，并在其中创建新线程。"""
        workspace: AnythingLLMWorkspace | None = None
        created_workspace = False
        logger.info(
            "开始准备 AnythingLLM 文件对话会话: context_name=%s conversation_name=%s",
            context_name,
            conversation_name,
        )
        try:
            workspace = self._find_workspace(context_name)
            if workspace is None:
                workspace = self._workspace_client.create_workspace(
                    self._required_text(context_name, "context_name"),
                    settings=self._workspace_settings,
                    user_id=self._user_id,
                )
                created_workspace = True
                logger.info(
                    "已创建文件对话工作区，准备创建远端线程: context_name=%s",
                    context_name,
                )
            else:
                logger.info(
                    "复用已有文件对话工作区，准备创建远端线程: context_name=%s",
                    context_name,
                )
            thread = self._thread_client.create_thread(
                workspace.slug,
                conversation_name,
                user_id=self._user_id,
            )
            logger.info(
                "AnythingLLM 文件对话会话准备完成: context_name=%s created_workspace=%s",
                context_name,
                created_workspace,
            )
            return ChatSessionRefs(
                context_ref=workspace.slug,
                conversation_ref=thread.slug,
            )
        except AnythingLLMTransportError as exc:
            logger.warning(
                "准备 AnythingLLM 文件对话会话失败: context_name=%s created_workspace=%s error_type=%s",
                context_name,
                created_workspace,
                exc.__class__.__name__,
            )
            if created_workspace and workspace is not None:
                logger.info(
                    "远端线程创建失败，开始补偿刚创建的文件对话工作区: context_name=%s",
                    context_name,
                )
                if not self._compensate_new_workspace(workspace.slug):
                    raise ChatResourceError(
                        "new chat workspace compensation failed",
                        resource_refs=(workspace.slug,),
                    ) from exc
            raise self._port_error(exc, "open chat conversation") from exc

    def attach_documents(
        self,
        session: ChatSessionRefs,
        documents: Sequence[ChatDocumentRef],
    ) -> tuple[ChatDocumentRef, ...]:
        """将已上传文档的位置绑定到指定对话工作区。"""
        self._require_session(session)
        logger.info(
            "开始绑定文件对话文档: requested_document_count=%d",
            len(documents),
        )
        try:
            locations = self._document_locations(documents)
            if locations:
                self._workspace_client.update_embeddings(
                    session.context_ref,
                    adds=locations,
                    user_id=self._user_id,
                )
            attached_documents = tuple(
                self._chat_document_ref(document)
                for document in self._workspace_client.list_documents(
                    session.context_ref,
                    user_id=self._user_id,
                )
            )
            logger.info(
                "文件对话文档绑定完成: requested_document_count=%d workspace_document_count=%d",
                len(locations),
                len(attached_documents),
            )
            return attached_documents
        except AnythingLLMTransportError as exc:
            logger.warning(
                "绑定文件对话文档失败: requested_document_count=%d error_type=%s",
                len(documents),
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "attach chat documents") from exc

    def stream_message(
        self,
        session: ChatSessionRefs,
        message: str,
        *,
        document_refs: Sequence[str] = (),
    ) -> Iterator[ChatChunk | ChatSourceFinalization | ChatQueryRejection]:
        """发送消息并映射文本、完整来源或 Query 拒答终态。"""
        self._require_session(session)
        logger.info(
            "开始请求 AnythingLLM 文件对话流: message_chars=%d requested_document_count=%d mode=%s",
            len(message),
            len(document_refs),
            self._stream_mode,
        )
        try:
            document_ids = self._resolve_document_ids(session, document_refs)
            logger.debug(
                "文件对话文档引用已解析为远端文档标识: document_count=%d",
                len(document_ids),
            )
            upstream = self._thread_client.stream(
                session.context_ref,
                session.conversation_ref,
                message,
                mode=self._stream_mode,
                user_id=self._user_id,
                document_ids=document_ids,
            )
        except ChatResourceError:
            raise
        except AnythingLLMTransportError as exc:
            logger.warning(
                "启动 AnythingLLM 文件对话流失败: error_type=%s",
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "start chat stream") from exc

        def _chunks() -> Iterator[
            ChatChunk | ChatSourceFinalization | ChatQueryRejection
        ]:
            sequence_no = 0
            try:
                logger.info("开始消费 AnythingLLM 文件对话上游流")
                with self._closing_iterator(upstream) as events:
                    for event in events:
                        if isinstance(event, AnythingLLMFinalization):
                            yield ChatSourceFinalization(
                                sources=tuple(
                                    ChatSourceEvidence(
                                        content=source.content,
                                        structured_source_key=(
                                            source.structured_source_key
                                        ),
                                    )
                                    for source in event.sources
                                )
                            )
                            continue
                        if isinstance(event, AnythingLLMQueryRejection):
                            yield ChatQueryRejection(content=event.content)
                            continue
                        if not isinstance(event, AnythingLLMTextDelta):
                            raise AnythingLLMProtocolError(
                                "AnythingLLM 流返回未知事件类型"
                            )
                        sequence_no += 1
                        yield ChatChunk(
                            content=event.content,
                            sequence_no=sequence_no,
                        )
            except AnythingLLMTransportError as exc:
                logger.warning(
                    "消费 AnythingLLM 文件对话上游流失败: emitted_chunk_count=%d error_type=%s",
                    sequence_no,
                    exc.__class__.__name__,
                )
                raise self._port_error(exc, "consume chat stream") from exc
            finally:
                logger.info(
                    "AnythingLLM 文件对话上游流已结束或关闭: emitted_chunk_count=%d",
                    sequence_no,
                )

        return _chunks()

    def fetch_messages(
        self,
        session: ChatSessionRefs,
    ) -> tuple[ChatMessageSnapshot, ...]:
        """读取并规范化外部线程历史。"""
        self._require_session(session)
        logger.debug("开始读取 AnythingLLM 文件对话外部历史")
        try:
            history = self._thread_client.history(
                session.context_ref,
                session.conversation_ref,
                user_id=self._user_id,
            )
            messages: list[ChatMessageSnapshot] = []
            for item in history:
                snapshot = self._message_snapshot(item)
                if snapshot is not None:
                    messages.append(snapshot)
            snapshots = tuple(messages)
            logger.info(
                "AnythingLLM 文件对话外部历史读取完成: message_count=%d",
                len(snapshots),
            )
            return snapshots
        except AnythingLLMTransportError as exc:
            logger.warning(
                "读取 AnythingLLM 文件对话外部历史失败: error_type=%s",
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "fetch chat messages") from exc

    def open_temporary_conversation(
        self,
        *,
        context_ref: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        """创建临时线程，其完整生命周期由调用方负责。"""
        normalized_context_ref = self._required_text(context_ref, "context_ref")
        logger.info("开始创建文件对话标题临时线程")
        try:
            temp_thread = self._thread_client.create_thread(
                normalized_context_ref,
                self._required_text(conversation_name, "conversation_name"),
                user_id=self._user_id,
            )
            session = ChatSessionRefs(
                context_ref=normalized_context_ref,
                conversation_ref=temp_thread.slug,
            )
            logger.info("文件对话标题临时线程创建完成")
            return session
        except AnythingLLMTransportError as exc:
            logger.warning(
                "创建文件对话标题临时线程失败: error_type=%s",
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "create temporary chat thread") from exc

    def generate_temporary_reply(
        self,
        *,
        session: ChatSessionRefs,
        prompt: str,
    ) -> str:
        """向调用方追踪的临时线程请求一条独立回复。"""
        self._require_session(session)
        logger.info(
            "开始请求文件对话标题临时回复: prompt_chars=%d mode=%s",
            len(prompt),
            self._standalone_mode,
        )
        try:
            answer = self._thread_client.ask(
                session.context_ref,
                session.conversation_ref,
                prompt,
                mode=self._standalone_mode,
                user_id=self._user_id,
                document_ids=(),
            )
            if not answer.text:
                logger.warning("文件对话标题临时回复为空")
                raise ChatResponseError("standalone chat reply is empty")
            logger.info(
                "文件对话标题临时回复生成完成: reply_chars=%d",
                len(answer.text),
            )
            return answer.text
        except ChatResponseError:
            raise
        except AnythingLLMTransportError as exc:
            logger.warning(
                "生成文件对话标题临时回复失败: error_type=%s",
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "generate standalone chat reply") from exc

    def delete_conversation(
        self,
        session: ChatSessionRefs,
    ) -> ChatOperationResult:
        """幂等删除远端对话线程。"""
        self._require_session(session)
        logger.info("开始删除远端文件对话线程")
        try:
            self._thread_client.delete_thread(
                session.context_ref,
                session.conversation_ref,
                user_id=self._user_id,
            )
            logger.info("远端文件对话线程删除完成")
            return ChatOperationResult(success=True)
        except AnythingLLMHTTPError as exc:
            if exc.status_code == 404:
                logger.info("远端文件对话线程已不存在，无需重复删除")
                return ChatOperationResult(success=True, already_applied=True)
            logger.warning(
                "删除远端文件对话线程失败: status_code=%s",
                exc.status_code,
            )
            raise self._port_error(exc, "delete chat conversation") from exc
        except AnythingLLMTransportError as exc:
            logger.warning(
                "删除远端文件对话线程失败: error_type=%s",
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "delete chat conversation") from exc

    def delete_context(
        self,
        context_ref: str,
    ) -> ChatOperationResult:
        """幂等删除远端对话工作区。"""
        normalized_context_ref = self._required_text(context_ref, "context_ref")
        logger.info("开始删除远端文件对话工作区")
        try:
            self._workspace_client.delete_workspace(
                normalized_context_ref,
                user_id=self._user_id,
            )
            logger.info("远端文件对话工作区删除完成")
            return ChatOperationResult(success=True)
        except AnythingLLMHTTPError as exc:
            if exc.status_code == 404:
                logger.info("远端文件对话工作区已不存在，无需重复删除")
                return ChatOperationResult(success=True, already_applied=True)
            logger.warning(
                "删除远端文件对话工作区失败: status_code=%s",
                exc.status_code,
            )
            raise self._port_error(exc, "delete chat context") from exc
        except AnythingLLMTransportError as exc:
            logger.warning(
                "删除远端文件对话工作区失败: error_type=%s",
                exc.__class__.__name__,
            )
            raise self._port_error(exc, "delete chat context") from exc

    def _find_workspace(self, name: str) -> AnythingLLMWorkspace | None:
        normalized_name = self._required_text(name, "context_name")
        for workspace in self._workspace_client.list_workspaces(
            user_id=self._user_id,
        ):
            if workspace.name == normalized_name:
                return workspace
        return None

    def _compensate_new_workspace(self, workspace_ref: str) -> bool:
        """在外部引用尚未持久化前，尽力执行适配器内部补偿。"""
        try:
            self._workspace_client.delete_workspace(
                workspace_ref,
                user_id=self._user_id,
            )
        except AnythingLLMTransportError:
            logger.exception(
                "远端线程创建失败后，补偿删除新建文件对话工作区失败"
            )
            return False
        logger.info("远端线程创建失败后，新建文件对话工作区补偿删除完成")
        return True

    @staticmethod
    def _chat_document_ref(document: AnythingLLMDocument) -> ChatDocumentRef:
        return ChatDocumentRef(
            document_ref=document.document_ref or f"document:{document.id}",
            external_location=document.location,
        )

    @staticmethod
    def _document_locations(
        documents: Sequence[ChatDocumentRef],
    ) -> tuple[str, ...]:
        locations: list[str] = []
        for document in documents:
            if not isinstance(document, ChatDocumentRef):
                raise TypeError("documents may only contain ChatDocumentRef")
            location = document.external_location.strip()
            if not location:
                raise ChatResourceError(
                    "ChatDocumentRef.external_location is required for binding"
                )
            locations.append(location)
        return tuple(dict.fromkeys(locations))

    @staticmethod
    def _document_refs(document_refs: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                ref
                for ref in (str(item or "").strip() for item in document_refs)
                if ref
            )
        )

    def _resolve_document_ids(
        self,
        session: ChatSessionRefs,
        document_refs: Sequence[str],
    ) -> tuple[str, ...]:
        requested_refs = self._document_refs(document_refs)
        if not requested_refs:
            return ()
        logger.debug(
            "开始解析文件对话请求中的文档引用: requested_document_count=%d",
            len(requested_refs),
        )
        documents_by_ref = {
            self._chat_document_ref(document).document_ref: document
            for document in self._workspace_client.list_documents(
                session.context_ref,
                user_id=self._user_id,
            )
        }
        document_ids: list[str] = []
        missing_refs: list[str] = []
        for document_ref in requested_refs:
            document = documents_by_ref.get(document_ref)
            if document is None:
                missing_refs.append(document_ref)
                continue
            document_ids.append(document.id)
        if missing_refs:
            logger.warning(
                "文件对话请求引用了未绑定文档: missing_document_count=%d",
                len(missing_refs),
            )
            raise ChatResourceError(
                "requested documents are not attached to this conversation"
            )
        logger.debug(
            "文件对话文档引用解析完成: resolved_document_count=%d",
            len(document_ids),
        )
        return tuple(document_ids)

    @classmethod
    def _message_snapshot(
        cls,
        item: Mapping[str, Any],
    ) -> ChatMessageSnapshot | None:
        role = cls._role(item)
        if role is None:
            return None
        content = cls._content(item)
        if content == "":
            return None
        if role == ChatRole.ASSISTANT.value:
            content = _THINK_BLOCK_PATTERN.sub("", content)
            if content == "":
                return None
        return ChatMessageSnapshot(
            role=role,
            content=content,
            timestamp_ms=cls._extract_timestamp_ms(item),
        )

    @staticmethod
    def _role(item: Mapping[str, Any]) -> str | None:
        raw_role = str(item.get("role") or item.get("type") or "").strip().casefold()
        if raw_role in {"user", "human"}:
            return ChatRole.USER.value
        if raw_role in {"assistant", "ai", "bot"}:
            return ChatRole.ASSISTANT.value
        return None

    @staticmethod
    def _content(item: Mapping[str, Any]) -> str:
        for key in ("content", "text", "message", "textResponse"):
            value = item.get(key)
            if value is not None:
                return str(value)
        return ""

    @staticmethod
    def _extract_timestamp_ms(item: Mapping[str, Any]) -> int | None:
        for key in (
            "timestamp",
            "sentAt",
            "createdAt",
            "updatedAt",
            "created_at",
            "updated_at",
        ):
            if key in item:
                return AnythingLLMChatGateway._to_timestamp_ms(item.get(key))
        return None

    @staticmethod
    def _to_timestamp_ms(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                return int(text)
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp() * 1000)
        return None

    @staticmethod
    def _require_session(session: ChatSessionRefs) -> None:
        if not isinstance(session, ChatSessionRefs):
            raise TypeError("session must be ChatSessionRefs")

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{name} cannot be empty")
        return normalized

    @staticmethod
    def _normalize_mode(mode: str) -> str:
        if not isinstance(mode, str):
            raise ValueError("mode must be chat or query")
        normalized = mode.strip().casefold()
        if normalized not in _ALLOWED_CHAT_MODES:
            raise ValueError("mode must be chat or query")
        return normalized

    @staticmethod
    def _port_error(
        exc: AnythingLLMTransportError,
        operation: str,
    ) -> ChatResponseError | ChatResourceError | ChatConversationNotFoundError:
        message = f"{operation} failed: {exc}"
        if isinstance(exc, AnythingLLMHTTPError) and exc.status_code == 404:
            return ChatConversationNotFoundError(message)
        if isinstance(exc, AnythingLLMProtocolError):
            return ChatResponseError(message)
        return ChatResourceError(message)

    @staticmethod
    @contextmanager
    def _closing_iterator(iterator: Iterator[Any]) -> Iterator[Iterator[Any]]:
        try:
            yield iterator
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
