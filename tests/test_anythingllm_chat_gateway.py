"""AnythingLLM 文件对话网关的离线测试。"""

from __future__ import annotations

import unittest
from typing import Any, Iterator, Mapping, Optional, Sequence

from app.integrations.anythingllm.chat_gateway import AnythingLLMChatGateway
from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.ports import (
    ChatConversationNotFoundError,
    ChatDocumentRef,
    ChatResourceError,
    ChatResponseError,
    ChatRole,
    ChatSessionRefs,
)


class _FakeWorkspaceClient:
    def __init__(self) -> None:
        self.workspaces: list[AnythingLLMWorkspace] = []
        self.documents: list[AnythingLLMDocument] = []
        self.created_settings: list[dict[str, Any]] = []
        self.embedding_updates: list[tuple[str, tuple[str, ...], int | None]] = []
        self.delete_calls: list[tuple[str, int | None]] = []
        self.list_error: Exception | None = None
        self.create_error: Exception | None = None
        self.delete_error: Exception | None = None

    def list_workspaces(
        self,
        *,
        user_id: int | None = None,
    ) -> list[AnythingLLMWorkspace]:
        if self.list_error:
            raise self.list_error
        return list(self.workspaces)

    def create_workspace(
        self,
        name: str,
        *,
        settings: Optional[Mapping[str, Any]] = None,
        user_id: int | None = None,
    ) -> AnythingLLMWorkspace:
        if self.create_error:
            raise self.create_error
        self.created_settings.append(dict(settings or {}))
        workspace = AnythingLLMWorkspace(
            id=f"workspace-{len(self.workspaces) + 1}",
            slug=f"slug-{len(self.workspaces) + 1}",
            name=name,
        )
        self.workspaces.append(workspace)
        return workspace

    def update_embeddings(
        self,
        workspace_slug: str,
        *,
        adds: Optional[Sequence[str]] = None,
        deletes: Optional[Sequence[str]] = None,
        user_id: int | None = None,
    ) -> AnythingLLMWorkspace | None:
        self.embedding_updates.append(
            (workspace_slug, tuple(adds or ()), user_id)
        )
        return AnythingLLMWorkspace(
            id=workspace_slug,
            slug=workspace_slug,
            name=workspace_slug,
        )

    def list_documents(
        self,
        workspace_slug: str,
        *,
        user_id: int | None = None,
    ) -> list[AnythingLLMDocument]:
        return list(self.documents)

    def delete_workspace(
        self,
        workspace_slug: str,
        *,
        user_id: int | None = None,
    ) -> None:
        if self.delete_error:
            raise self.delete_error
        self.delete_calls.append((workspace_slug, user_id))


class _FakeThreadClient:
    def __init__(self) -> None:
        self.created_threads: list[tuple[str, str, int | None]] = []
        self.deleted_threads: list[tuple[str, str, int | None]] = []
        self.stream_calls: list[dict[str, Any]] = []
        self.ask_calls: list[dict[str, Any]] = []
        self.history_items: list[Mapping[str, Any]] = []
        self.stream_chunks: list[str] = []
        self.stream_closed = False
        self.answer = AnythingLLMAnswer(text="ok", raw_text="ok", sources=())
        self.create_error: Exception | None = None
        self.delete_error: Exception | None = None

    def create_thread(
        self,
        workspace_slug: str,
        name: str,
        *,
        user_id: int | None = None,
    ) -> AnythingLLMThread:
        if self.create_error:
            raise self.create_error
        self.created_threads.append((workspace_slug, name, user_id))
        return AnythingLLMThread(id=name, slug=name)

    def delete_thread(
        self,
        workspace_slug: str,
        thread_slug: str,
        *,
        user_id: int | None = None,
    ) -> None:
        if self.delete_error:
            raise self.delete_error
        self.deleted_threads.append((workspace_slug, thread_slug, user_id))

    def stream(
        self,
        workspace_slug: str,
        thread_slug: str,
        message: str,
        *,
        mode: str,
        user_id: int | None = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> Iterator[str]:
        self.stream_calls.append(
            {
                "workspace_slug": workspace_slug,
                "thread_slug": thread_slug,
                "message": message,
                "mode": mode,
                "user_id": user_id,
                "document_ids": tuple(document_ids or ()),
            }
        )

        def _chunks() -> Iterator[str]:
            try:
                for chunk in self.stream_chunks:
                    yield chunk
            finally:
                self.stream_closed = True

        return _chunks()

    def history(
        self,
        workspace_slug: str,
        thread_slug: str,
        *,
        user_id: int | None = None,
    ) -> list[Mapping[str, Any]]:
        return list(self.history_items)

    def ask(
        self,
        workspace_slug: str,
        thread_slug: str,
        prompt: str,
        *,
        mode: str,
        user_id: int | None = None,
        document_ids: Optional[Sequence[str]] = None,
    ) -> AnythingLLMAnswer:
        self.ask_calls.append(
            {
                "workspace_slug": workspace_slug,
                "thread_slug": thread_slug,
                "prompt": prompt,
                "mode": mode,
                "user_id": user_id,
                "document_ids": tuple(document_ids or ()),
            }
        )
        return self.answer


def _not_found_error() -> AnythingLLMHTTPError:
    return AnythingLLMHTTPError(
        "missing",
        method="DELETE",
        url="workspace/test",
        status_code=404,
        response_summary="not found",
    )


class AnythingLLMChatGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace_client = _FakeWorkspaceClient()
        self.thread_client = _FakeThreadClient()
        self.gateway = AnythingLLMChatGateway(
            self.workspace_client,
            self.thread_client,
            user_id=7,
        )

    def test_open_conversation_reuses_matching_workspace(self) -> None:
        self.workspace_client.workspaces.append(
            AnythingLLMWorkspace(id="w1", slug="chat-slug", name="chat-c1")
        )

        session = self.gateway.open_conversation(
            context_name="chat-c1",
            conversation_name="thread-c1",
        )

        self.assertEqual("chat-slug", session.context_ref)
        self.assertEqual("thread-c1", session.conversation_ref)
        self.assertEqual([], self.workspace_client.created_settings)
        self.assertEqual(
            [("chat-slug", "thread-c1", 7)],
            self.thread_client.created_threads,
        )

    def test_open_conversation_creates_workspace_with_chat_settings(self) -> None:
        session = self.gateway.open_conversation(
            context_name="chat-c2",
            conversation_name="thread-c2",
        )

        self.assertEqual("slug-1", session.context_ref)
        self.assertEqual("thread-c2", session.conversation_ref)
        self.assertEqual("chat", self.workspace_client.created_settings[0]["chatMode"])
        self.assertEqual(20, self.workspace_client.created_settings[0]["topN"])

    def test_new_workspace_is_compensated_when_thread_creation_fails(self) -> None:
        self.thread_client.create_error = AnythingLLMProtocolError("thread failed")

        with self.assertRaises(ChatResponseError):
            self.gateway.open_conversation(
                context_name="chat-compensate",
                conversation_name="thread-compensate",
            )

        self.assertEqual([("slug-1", 7)], self.workspace_client.delete_calls)

    def test_uncompensated_new_workspace_is_exposed_as_recoverable_resource_ref(self) -> None:
        self.thread_client.create_error = AnythingLLMProtocolError("thread failed")
        self.workspace_client.delete_error = AnythingLLMProtocolError("delete failed")

        with self.assertRaises(ChatResourceError) as raised:
            self.gateway.open_conversation(
                context_name="chat-uncompensated",
                conversation_name="thread-uncompensated",
            )

        self.assertEqual(("slug-1",), raised.exception.resource_refs)

    def test_attach_documents_binds_locations_and_returns_workspace_snapshot(
        self,
    ) -> None:
        session = ChatSessionRefs("workspace-a", "thread-a")
        self.workspace_client.documents = [
            AnythingLLMDocument(
                id="doc-a",
                location="custom-documents/a.json",
                title="a.json",
                document_ref="document:doc-a",
            )
        ]

        documents = self.gateway.attach_documents(
            session,
            [
                ChatDocumentRef(
                    document_ref="document:source-a",
                    external_location="custom-documents/a.json",
                )
            ],
        )

        self.assertEqual(
            [("workspace-a", ("custom-documents/a.json",), 7)],
            self.workspace_client.embedding_updates,
        )
        self.assertEqual(
            (
                ChatDocumentRef(
                    document_ref="document:doc-a",
                    external_location="custom-documents/a.json",
                ),
            ),
            documents,
        )

    def test_attach_documents_requires_external_location(self) -> None:
        with self.assertRaises(ChatResourceError):
            self.gateway.attach_documents(
                ChatSessionRefs("workspace-a", "thread-a"),
                [ChatDocumentRef(document_ref="document:source-a")],
            )

    def test_stream_message_returns_chunks_and_closes_upstream(self) -> None:
        self.thread_client.stream_chunks = ["hello", "  ", ""]
        self.workspace_client.documents = [
            AnythingLLMDocument(
                id="doc-a",
                location="custom-documents/a.json",
                title="a.json",
                document_ref="document:doc-a",
            )
        ]

        stream = self.gateway.stream_message(
            ChatSessionRefs("workspace-a", "thread-a"),
            "hi",
            document_refs=[" document:doc-a ", "", "document:doc-a"],
        )

        self.assertEqual("hello", next(stream).content)
        stream.close()

        self.assertTrue(self.thread_client.stream_closed)
        self.assertEqual("query", self.thread_client.stream_calls[0]["mode"])
        self.assertEqual(
            ("doc-a",),
            self.thread_client.stream_calls[0]["document_ids"],
        )

    def test_stream_message_rejects_unattached_document_refs(self) -> None:
        with self.assertRaises(ChatResourceError):
            self.gateway.stream_message(
                ChatSessionRefs("workspace-a", "thread-a"),
                "hi",
                document_refs=["document:missing"],
            )
        self.assertEqual([], self.thread_client.stream_calls)

    def test_invalid_chat_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AnythingLLMChatGateway(
                self.workspace_client,
                self.thread_client,
                stream_mode="raw",
            )

    def test_fetch_messages_normalizes_history_fields(self) -> None:
        self.thread_client.history_items = [
            {
                "type": "human",
                "message": "hello",
                "createdAt": "2026-07-08T00:00:00Z",
            },
            {
                "role": "assistant",
                "content": "<think>hidden</think> answer ",
                "timestamp": 123.9,
            },
            {"role": "system", "content": "ignored"},
            {"role": "assistant", "content": "<think>only"},
        ]

        messages = self.gateway.fetch_messages(
            ChatSessionRefs("workspace-a", "thread-a")
        )

        self.assertEqual(2, len(messages))
        self.assertEqual(ChatRole.USER.value, messages[0].role)
        self.assertEqual("hello", messages[0].content)
        self.assertIsInstance(messages[0].timestamp_ms, int)
        self.assertEqual(ChatRole.ASSISTANT.value, messages[1].role)
        self.assertEqual(" answer ", messages[1].content)
        self.assertEqual(123, messages[1].timestamp_ms)

    def test_temporary_reply_leaves_thread_cleanup_to_the_application_layer(self) -> None:
        self.thread_client.answer = AnythingLLMAnswer(
            text="title",
            raw_text="title",
            sources=(),
        )

        temporary_session = self.gateway.open_temporary_conversation(
            context_ref="workspace-a",
            conversation_name="title-a",
        )
        reply = self.gateway.generate_temporary_reply(
            session=temporary_session,
            prompt="make a title",
        )

        self.assertEqual("title", reply)
        temp_thread = self.thread_client.created_threads[0][1]
        self.assertEqual("title-a", temp_thread)
        self.assertEqual(temp_thread, self.thread_client.ask_calls[0]["thread_slug"])
        self.assertEqual("chat", self.thread_client.ask_calls[0]["mode"])
        self.assertEqual([], self.thread_client.deleted_threads)
        self.gateway.delete_conversation(temporary_session)
        self.assertEqual(
            [("workspace-a", temp_thread, 7)],
            self.thread_client.deleted_threads,
        )

    def test_protocol_error_is_translated_to_chat_response_error(self) -> None:
        self.workspace_client.create_error = AnythingLLMProtocolError("bad response")

        with self.assertRaises(ChatResponseError):
            self.gateway.open_conversation(
                context_name="chat-c3",
                conversation_name="thread-c3",
            )

    def test_delete_treats_404_as_idempotent_success(self) -> None:
        self.thread_client.delete_error = _not_found_error()
        result = self.gateway.delete_conversation(
            ChatSessionRefs("workspace-a", "thread-a")
        )
        self.assertTrue(result.success)
        self.assertTrue(result.already_applied)

        self.workspace_client.delete_error = _not_found_error()
        result = self.gateway.delete_context("workspace-a")
        self.assertTrue(result.success)
        self.assertTrue(result.already_applied)

    def test_fetch_404_maps_to_not_found_error(self) -> None:
        self.thread_client.history = lambda *args, **kwargs: (_ for _ in ()).throw(
            _not_found_error()
        )

        with self.assertRaises(ChatConversationNotFoundError):
            self.gateway.fetch_messages(ChatSessionRefs("workspace-a", "thread-a"))


if __name__ == "__main__":
    unittest.main()
