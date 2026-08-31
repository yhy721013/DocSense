"""共享 Chat 运行与历史的 File/Weaponry 双策略门禁。"""

from __future__ import annotations

import tempfile
import unittest

from app.modules.chat.adapters.sqlite.identity_repository import (
    SQLiteConversationIdentityRepository,
)
from app.modules.chat.adapters.sqlite.locking.lock_service import ChatRunLockService
from app.modules.chat.adapters.sqlite.store import ChatStore
from app.modules.chat.application.command_service import ChatCommandService
from app.modules.chat.application.history_service import ChatHistoryService
from app.modules.chat.application.run_executor import (
    ChatRunStreamRequest,
    SynchronousChatRunExecutor,
    record_chat_run_events,
)
from app.modules.chat.adapters.knowledge_documents import DatabaseChatDocumentResolver
from app.modules.chat.domain.document_scope import ChatScopeSelector
from app.modules.chat.domain.events import ChatStreamEvent
from app.modules.chat.domain.identity import (
    IDENTITY_KIND_WEAPONRY,
    FileChatIdentity,
    WeaponryChatIdentity,
)
from app.modules.chat.domain.workspace_naming import chat_workspace_name
from app.modules.chat.ports.conversations import ChatSourceEvidence
from app.services.core.database import DatabaseService
from tests.fakes.chat import FakeChatConversationFactory


class ChatWeaponryPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = f"{self.temporary.name}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.identities = SQLiteConversationIdentityRepository(
            self.db_path,
            owner_instance_id="weaponry-policy-test",
        )
        self.coordinator = ChatRunLockService(
            self.db_path,
            owner_instance_id="weaponry-policy-test",
        )
        self.commands = ChatCommandService(self.coordinator)
        self.history = ChatHistoryService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _running_request(
        self,
        identity: FileChatIdentity | WeaponryChatIdentity,
        *,
        suffix: str,
    ) -> ChatRunStreamRequest:
        resolution = self.identities.create_conversation(identity)
        self.store.runs.create(
            run_id=f"run-{suffix}",
            conversation_id=resolution.conversation_id,
        )
        self.store.runs.mark_running(f"run-{suffix}")
        return ChatRunStreamRequest(
            run_id=f"run-{suffix}",
            conversation_id=resolution.conversation_id,
            workspace_name=chat_workspace_name(resolution.binding),
            message="请回答",
            identity_kind=identity.identity_kind,
        )

    def test_weaponry_commits_before_source_snapshot_is_exposed(self) -> None:
        identity = WeaponryChatIdentity(user_id=8, architecture_id=7)
        request = self._running_request(identity, suffix="weaponry")
        original_content = "  第一段\r\n第二段 Ω  "
        source_event = ChatStreamEvent(
            "source_snapshot",
            {
                "chunks": (
                    {
                        "content": original_content,
                        "fileName": "alpha.pdf",
                        "originalFileName": "原始 Alpha.pdf",
                    },
                )
            },
        )
        recorded = record_chat_run_events(
            request=request,
            events=(
                ChatStreamEvent("chatInfo", {"isNewChat": True}),
                ChatStreamEvent("textChunk", {"content": "完整回答"}),
                source_event,
                ChatStreamEvent("done", {}),
            ),
            store=self.store,
            chat_commands=self.commands,
        )
        iterator = iter(recorded)
        self.assertEqual("chatInfo", next(iterator).event_type)
        self.assertEqual("textChunk", next(iterator).event_type)

        # Recorder 只有在 done 成功事务完成后才公开 source_snapshot。
        self.assertEqual(source_event, next(iterator))
        history = self.history.list_history(identity)
        self.assertEqual(
            [
                {"role": "user", "content": "请回答", "timestamp": history[0]["timestamp"]},
                {
                    "role": "assistant",
                    "content": "完整回答",
                    "timestamp": history[1]["timestamp"],
                    "chunks": [
                        {
                            "content": original_content,
                            "fileName": "alpha.pdf",
                            "originalFileName": "原始 Alpha.pdf",
                        }
                    ],
                },
            ],
            history,
        )
        self.assertEqual("done", next(iterator).event_type)

    def test_executor_maps_finalization_and_history_from_one_frozen_snapshot(self) -> None:
        source_key = "docsense_ref:" + "d" * 32
        knowledge = DatabaseService(f"{self.temporary.name}/knowledge.sqlite3")
        knowledge.save_document_record(
            "alpha.pdf",
            7,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
            original_name="原始 Alpha.pdf",
            ingested_file_name="alpha.pdf",
            metadata={"docSource": source_key},
        )
        factory = FakeChatConversationFactory(
            stream_contents=("第一段", "第二段"),
            stream_sources=(
                ChatSourceEvidence("  原文\r\nΩ  ", source_key),
                ChatSourceEvidence("重复来源", source_key),
            ),
        )
        executor = SynchronousChatRunExecutor(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            document_resolver=DatabaseChatDocumentResolver(knowledge),
        )
        identity = WeaponryChatIdentity(user_id=11, architecture_id=7)
        prepared = executor.prepare_chat_run(
            identity=identity,
            message="问题",
            scope_selector=ChatScopeSelector.for_architecture(7),
        )

        events = list(executor.execute_chat_run(prepared.run_id))
        history = self.history.list_history(identity)

        self.assertEqual(
            ["chatInfo", "textChunk", "textChunk", "source_snapshot", "done"],
            [event.event_type for event in events],
        )
        self.assertEqual(events[-2].data["chunks"], tuple(history[1]["chunks"]))
        self.assertEqual("  原文\r\nΩ  ", history[1]["chunks"][0]["content"])
        self.assertEqual(
            ("alpha.pdf", "alpha.pdf"),
            tuple(chunk["fileName"] for chunk in history[1]["chunks"]),
        )

    def test_weaponry_empty_sources_and_file_source_suppression(self) -> None:
        weaponry = WeaponryChatIdentity(user_id=9, architecture_id=7)
        weaponry_request = self._running_request(weaponry, suffix="empty")
        result = list(
            record_chat_run_events(
                request=weaponry_request,
                events=(
                    ChatStreamEvent("textChunk", {"content": "无来源回答"}),
                    ChatStreamEvent("source_snapshot", {"chunks": ()}),
                    ChatStreamEvent("done", {}),
                ),
                store=self.store,
                chat_commands=self.commands,
            )
        )
        self.assertEqual(["textChunk", "source_snapshot", "done"], [x.event_type for x in result])
        self.assertEqual([], self.history.list_history(weaponry)[1]["chunks"])

        file_identity = FileChatIdentity(chat_id=17)
        file_request = self._running_request(file_identity, suffix="file")
        with self.assertRaisesRegex(ValueError, "file chat"):
            list(
                record_chat_run_events(
                    request=file_request,
                    events=(
                        ChatStreamEvent("source_snapshot", {"chunks": ()}),
                        ChatStreamEvent("done", {}),
                    ),
                    store=self.store,
                    chat_commands=self.commands,
                )
            )

    def test_error_after_buffered_source_never_commits_assistant_or_chunks(self) -> None:
        identity = WeaponryChatIdentity(user_id=10, architecture_id=7)
        request = self._running_request(identity, suffix="failure")
        result = list(
            record_chat_run_events(
                request=request,
                events=(
                    ChatStreamEvent("textChunk", {"content": "半截"}),
                    ChatStreamEvent(
                        "source_snapshot",
                        {
                            "chunks": (
                                {
                                    "content": "来源",
                                    "fileName": "a.pdf",
                                    "originalFileName": "原始 a.pdf",
                                },
                            )
                        },
                    ),
                    ChatStreamEvent("error", {"error": "forced"}),
                ),
                store=self.store,
                chat_commands=self.commands,
            )
        )
        self.assertEqual(["textChunk", "error"], [x.event_type for x in result])
        history = self.history.list_history(identity)
        self.assertEqual(1, len(history))
        self.assertEqual("user", history[0]["role"])
        self.assertEqual((), self.store.message_sources.list_by_conversation(request.conversation_id))

    def test_disconnect_before_completion_never_commits_reply_facts(self) -> None:
        identity = WeaponryChatIdentity(user_id=12, architecture_id=7)
        request = self._running_request(identity, suffix="disconnect")
        recorded = record_chat_run_events(
            request=request,
            events=iter(
                (
                    ChatStreamEvent("textChunk", {"content": "半截"}),
                    ChatStreamEvent("source_snapshot", {"chunks": ()}),
                    ChatStreamEvent("done", {}),
                )
            ),
            store=self.store,
            chat_commands=self.commands,
        )
        iterator = iter(recorded)
        self.assertEqual("textChunk", next(iterator).event_type)
        iterator.close()

        history = self.history.list_history(identity)
        self.assertEqual(1, len(history))
        self.assertEqual("user", history[0]["role"])
        self.assertEqual(
            (),
            self.store.message_sources.list_by_conversation(
                request.conversation_id
            ),
        )


if __name__ == "__main__":
    unittest.main()
