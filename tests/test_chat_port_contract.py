"""阶段 1 文件对话 Port、DTO 与内存 Fake 的离线契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path
import unittest

import app.ports as port_module
from app.ports import (
    ChatChunk,
    ChatConversationFactory,
    ChatConversationPort,
    ChatDocumentRef,
    ChatMessageSnapshot,
    ChatOperationResult,
    ChatResourceError,
    ChatRole,
    ChatSessionRefs,
)
from tests.fakes import FakeChatConversationFactory, FakeChatConversationPort


class ChatDtoContractTests(unittest.TestCase):
    """验证文件对话 DTO 的不可变性和最小数据约束。"""

    def test_session_document_and_chunk_snapshots_are_immutable(self) -> None:
        """端口 DTO 一旦创建，调用方不能再原地改写外部引用或流片段。"""
        session = ChatSessionRefs(
            context_ref=" context:1 ",
            conversation_ref=" conversation:1 ",
        )
        document = ChatDocumentRef(
            document_ref=" document:1 ",
            external_location=" external:1 ",
        )
        chunk = ChatChunk(content=" 你好 ", sequence_no=1)

        self.assertEqual("context:1", session.context_ref)
        self.assertEqual("conversation:1", session.conversation_ref)
        self.assertEqual("document:1", document.document_ref)
        self.assertEqual("external:1", document.external_location)
        self.assertEqual(" 你好 ", chunk.content)
        with self.assertRaises(FrozenInstanceError):
            setattr(session, "context_ref", "changed")
        with self.assertRaises(FrozenInstanceError):
            setattr(document, "document_ref", "changed")
        with self.assertRaises(FrozenInstanceError):
            setattr(chunk, "content", "changed")

    def test_message_snapshot_freezes_linked_documents(self) -> None:
        """历史消息快照必须复制关联文档序列，避免调用后被外部列表改写。"""
        document = ChatDocumentRef("document:1")
        documents = [document]

        snapshot = ChatMessageSnapshot(
            role=ChatRole.USER,
            content=" 请总结 ",
            timestamp_ms=1777364120677,
            linked_documents=documents,  # type: ignore[arg-type]
        )
        documents.append(ChatDocumentRef("document:2"))

        self.assertEqual("user", snapshot.role)
        self.assertEqual(" 请总结 ", snapshot.content)
        self.assertEqual(1777364120677, snapshot.timestamp_ms)
        self.assertEqual((document,), snapshot.linked_documents)

    def test_invalid_roles_timestamps_and_empty_chunks_are_rejected(self) -> None:
        """DTO 在端口边界拒绝模糊状态，而不是把脏数据留给适配器解释。"""
        with self.assertRaises(ValueError):
            ChatMessageSnapshot(role="system", content="隐藏消息")
        with self.assertRaises(ValueError):
            ChatMessageSnapshot(role=ChatRole.USER, content="hi", timestamp_ms=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ChatChunk(content="", sequence_no=1)
        with self.assertRaises(ValueError):
            ChatChunk(content="hi", sequence_no=0)

    def test_operation_result_rejects_ambiguous_failure_state(self) -> None:
        """资源操作失败必须携带错误信息，幂等命中必须是成功状态。"""
        with self.assertRaises(ValueError):
            ChatOperationResult(success=False)
        with self.assertRaises(ValueError):
            ChatOperationResult(
                success=False,
                already_applied=True,
                error_message="删除失败",
            )

    def test_dto_field_names_do_not_expose_supplier_terms(self) -> None:
        """公共 DTO 字段只能表达领域概念，不得暴露具体外部系统命名。"""
        forbidden_terms = {
            "workspace_slug",
            "thread_slug",
            "docId",
            "docpath",
            "files",
        }
        dto_types = (
            ChatSessionRefs,
            ChatDocumentRef,
            ChatMessageSnapshot,
            ChatChunk,
            ChatOperationResult,
        )

        for dto_type in dto_types:
            field_names = {field.name for field in fields(dto_type)}
            for term in forbidden_terms:
                with self.subTest(dto=dto_type.__name__, term=term):
                    self.assertNotIn(term, field_names)


class ChatPortContractTests(unittest.TestCase):
    """验证文件对话 Port 的 Fake 可替换性、流式边界和资源语义。"""

    def test_fake_implements_runtime_checkable_protocols(self) -> None:
        """Fake 必须能直接注入只依赖 Protocol 的业务服务。"""
        port = FakeChatConversationPort()
        factory = FakeChatConversationFactory()

        self.assertIsInstance(port, ChatConversationPort)
        self.assertIsInstance(factory, ChatConversationFactory)

    def test_factory_creates_independent_request_scopes(self) -> None:
        """每次进入 Factory 租约都必须产生独立 Port，并准确释放活动计数。"""
        factory = FakeChatConversationFactory()

        with factory.create() as first_port:
            self.assertEqual(1, factory.active_leases)
        with factory.create() as second_port:
            self.assertEqual(1, factory.active_leases)
            self.assertIsNot(first_port, second_port)

        self.assertEqual(0, factory.active_leases)
        self.assertEqual(2, len(factory.ports))

    def test_factory_ports_share_persisted_conversation_state(self) -> None:
        """请求级 Port 独立创建，但已持久化的会话引用必须可跨租约复用。"""
        factory = FakeChatConversationFactory(stream_contents=("后续回答",))
        with factory.create() as first_port:
            session = first_port.open_conversation(
                context_name="chat-c-shared",
                conversation_name="thread-c-shared",
            )
            document = ChatDocumentRef("document:shared")
            first_port.attach_documents(session, [document])

        with factory.create() as second_port:
            self.assertIsNot(first_port, second_port)
            chunks = list(
                second_port.stream_message(
                    session,
                    "继续总结",
                    document_refs=[document.document_ref],
                )
            )
            messages = second_port.fetch_messages(session)

        self.assertEqual([ChatChunk("后续回答", 1)], chunks)
        self.assertEqual(2, len(messages))
        self.assertEqual("继续总结", messages[0].content)
        self.assertEqual("后续回答", messages[1].content)

    def test_stream_message_returns_chunks_and_commits_history_on_completion(self) -> None:
        """流式接口只返回领域片段；完整消费后 Fake 才提交助手消息快照。"""
        port = FakeChatConversationPort(stream_contents=(" 第一段", "第二段\n"))
        session = port.open_conversation(
            context_name="chat-c1",
            conversation_name="thread-c1",
        )
        document = ChatDocumentRef("document:alpha")
        port.attach_documents(session, [document])

        chunks = list(
            port.stream_message(
                session,
                " 请总结 ",
                document_refs=[document.document_ref],
            )
        )
        messages = port.fetch_messages(session)

        self.assertEqual(
            [ChatChunk(" 第一段", 1), ChatChunk("第二段\n", 2)],
            chunks,
        )
        self.assertEqual(2, len(messages))
        self.assertEqual("user", messages[0].role)
        self.assertEqual("请总结", messages[0].content)
        self.assertEqual((document,), messages[0].linked_documents)
        self.assertEqual("assistant", messages[1].role)
        self.assertEqual(" 第一段第二段\n", messages[1].content)

    def test_stream_message_rejects_unattached_document_reference(self) -> None:
        """业务层请求引用尚未加入对话的文档时，端口必须明确失败。"""
        port = FakeChatConversationPort()
        session = port.open_conversation(
            context_name="chat-c2",
            conversation_name="thread-c2",
        )

        with self.assertRaises(ChatResourceError):
            list(
                port.stream_message(
                    session,
                    "请总结",
                    document_refs=["document:missing"],
                )
            )

    def test_standalone_reply_does_not_mutate_conversation_history(self) -> None:
        """标题生成等一次性回复不得污染主对话消息快照。"""
        port = FakeChatConversationPort(standalone_reply="标题")
        session = port.open_conversation(
            context_name="chat-c3",
            conversation_name="thread-c3",
        )

        reply = port.generate_standalone_reply(
            context_ref=session.context_ref,
            prompt="生成标题",
        )

        self.assertEqual("标题", reply)
        self.assertEqual((), port.fetch_messages(session))
        self.assertEqual([(session.context_ref, "生成标题")], port.standalone_prompts)

    def test_delete_operations_are_idempotent(self) -> None:
        """删除对话和上下文都应能重复调用，并明确 already_applied 状态。"""
        port = FakeChatConversationPort()
        session = port.open_conversation(
            context_name="chat-c4",
            conversation_name="thread-c4",
        )

        first_conversation = port.delete_conversation(session)
        second_conversation = port.delete_conversation(session)
        first_context = port.delete_context(session.context_ref)
        second_context = port.delete_context(session.context_ref)

        self.assertEqual(ChatOperationResult(success=True), first_conversation)
        self.assertEqual(
            ChatOperationResult(success=True, already_applied=True),
            second_conversation,
        )
        self.assertEqual(ChatOperationResult(success=True), first_context)
        self.assertEqual(
            ChatOperationResult(success=True, already_applied=True),
            second_context,
        )


class ChatPortBoundaryTests(unittest.TestCase):
    """防止供应商协议细节或测试替身进入生产端口包。"""

    def test_production_port_package_does_not_export_test_fakes(self) -> None:
        """生产抽象包不得反向依赖测试目录中的具体替身。"""
        self.assertFalse(hasattr(port_module, "FakeChatConversationPort"))
        self.assertFalse(hasattr(port_module, "FakeChatConversationFactory"))

    def test_chat_port_source_does_not_contain_supplier_protocol_terms(self) -> None:
        """文件对话 Port 源码不得出现具体客户端、字段或协议输出词。"""
        project_root = Path(__file__).resolve().parents[1]
        source = (project_root / "app" / "ports" / "chat.py").read_text(
            encoding="utf-8"
        )
        forbidden_terms = (
            "AnythingLLM",
            "workspace_slug",
            "thread_slug",
            "docId",
            "docpath",
            "files",
            "requests",
            "httpx",
            "Authorization",
            "api_key",
            "stream_sse",
            "event:",
            "data:",
            "tests.",
        )

        for term in forbidden_terms:
            with self.subTest(term=term):
                self.assertNotIn(term, source)


if __name__ == "__main__":
    unittest.main()
