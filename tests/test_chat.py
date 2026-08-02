"""阶段 10 文件对话链路的离线路由受理测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import tempfile
import unittest
from threading import Barrier
from unittest.mock import patch

from app import create_app
from app.container import ApplicationServices, UploadTaskLimiter
from app.modules.debug.composition import compose_debug_application_services
from app.modules.report.adapters import (
    ReportTaskCommandCodec,
    SQLiteReportCallbackAdapter,
    SQLiteReportCallbackRecoverySource,
)
from app.modules.report.application import (
    RecoverReportCallbackSynchronously,
    SubmitReportTask,
)
from app.modules.tasks.adapters import (
    InMemoryProgressAdapter,
    LegacyTaskCommandAdapter,
    LegacyTaskReadAdapter,
    LatestTaskProgressPublisherAdapter,
)
from app.modules.tasks.application import ProgressSubscriptionService
from app.modules.chat import (
    ChatAbortService,
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteService,
    ChatHistoryService,
    ChatRunLockService,
    ChatScopeSelector,
    ChatStore,
    ChatTitleService,
    DatabaseChatDocumentResolver,
    SynchronousChatRunExecutor,
    InlineChatRunDispatcher,
    InlineChatCleanupDispatcher,
    MESSAGE_COMMITTED,
    RUN_FAILED,
)
from app.modules.chat.domain.identity import FileChatIdentity
from app.modules.chat.ports import ChatSourceEvidence
from app.services.core.config import (
    AnythingLLMConfig,
    LLMIntegrationConfig,
    ReportInfrastructureConfig,
)
from app.services.core.database import DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.settings import CHAT_MAX_FILES_PER_REQUEST
from app.services.llm_service.task_service import LLMTaskService
from tests.fakes import (
    FakeChatConversationFactory,
    FakeDocumentRagFactory,
    FakeKnowledgeIndexFactory,
    FakeReportDispatcherPort,
    InvocationRecorder,
)


def _resolved_conversation_id(store: ChatStore, chat_id: int | str) -> str | None:
    """把公开 file chatId 解析为测试断言使用的内部聚合键。"""

    resolution = store.identities.resolve_any(
        FileChatIdentity(chat_id=int(chat_id))
    )
    return None if resolution is None else resolution.conversation_id


def _build_test_services(
    tmp: str,
    *,
    max_concurrent_streams: int | None = None,
    max_files_per_request: int | None = None,
    stream_sources: tuple[ChatSourceEvidence, ...] = (),
) -> ApplicationServices:
    """创建文件对话路径不依赖网络的隔离容器。"""
    chat_db_path = f"{tmp}/chat.sqlite3"
    chat_store = ChatStore(db_path=chat_db_path)
    chat_commands = ChatCommandService(ChatRunLockService(chat_db_path))
    chat_history = ChatHistoryService(chat_store)
    chat_conversation_factory = FakeChatConversationFactory(
        stream_contents=("第一段", "第二段"),
        stream_sources=stream_sources,
    )
    kb_service = DatabaseService(db_path=f"{tmp}/knowledge.sqlite3")
    executor_options = {}
    if max_concurrent_streams is not None:
        executor_options["max_concurrent_streams"] = max_concurrent_streams
    if max_files_per_request is not None:
        executor_options["max_files_per_request"] = max_files_per_request
    chat_run_executor = SynchronousChatRunExecutor(
        store=chat_store,
        chat_commands=chat_commands,
        conversation_factory=chat_conversation_factory,
        document_resolver=DatabaseChatDocumentResolver(
            kb_service,
            architecture_candidate_limit=(
                max_files_per_request
                if max_files_per_request is not None
                else CHAT_MAX_FILES_PER_REQUEST
            ),
        ),
        **executor_options,
    )
    chat_cleanup_executor = ChatCleanupJobExecutor(
        store=chat_store,
        conversation_factory=chat_conversation_factory,
    )
    chat_cleanup_dispatcher = InlineChatCleanupDispatcher(
        execute=chat_cleanup_executor.execute_cleanup_job,
    )
    chat_dispatcher = InlineChatRunDispatcher(
        execute=chat_run_executor.execute_chat_run,
    )
    task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
    progress_hub = LLMProgressHub()
    progress_adapter = InMemoryProgressAdapter(progress_hub)
    progress_subscription_service = ProgressSubscriptionService(
        progress_snapshots=progress_adapter,
        progress_subscriptions=progress_adapter,
        task_reader=LegacyTaskReadAdapter(task_service),
    )
    report_task_commands = LegacyTaskCommandAdapter(
        task_service,
        ReportTaskCommandCodec(),
    )
    report_dispatcher = FakeReportDispatcherPort(InvocationRecorder())
    report_submit = SubmitReportTask(
        task_commands=report_task_commands,
        progress_publisher=LatestTaskProgressPublisherAdapter(
            task_commands=report_task_commands,
            delegate=progress_adapter,
        ),
        dispatcher=report_dispatcher,
    )
    report_callback_recovery = RecoverReportCallbackSynchronously(
        source=SQLiteReportCallbackRecoverySource(task_service),
        callbacks=SQLiteReportCallbackAdapter(
            task_service,
            callback_url="",
            callback_timeout=5.0,
            lease_seconds=30.0,
        ),
    )
    return ApplicationServices(
        document_rag_factory=FakeDocumentRagFactory(),
        knowledge_index_factory=FakeKnowledgeIndexFactory(),
        chat_conversation_factory=chat_conversation_factory,
        task_service=task_service,
        kb_service=kb_service,
        chat_store=chat_store,
        chat_commands=chat_commands,
        chat_run_executor=chat_run_executor,
        chat_dispatcher=chat_dispatcher,
        chat_history=chat_history,
        chat_title=ChatTitleService(
            store=chat_store,
            history_service=chat_history,
            conversation_factory=chat_conversation_factory,
            cleanup_dispatcher=chat_cleanup_dispatcher,
            cleanup_executor=chat_cleanup_executor,
        ),
        chat_abort=ChatAbortService(
            store=chat_store,
            chat_commands=chat_commands,
        ),
        chat_delete=ChatDeleteService(
            store=chat_store,
            chat_commands=chat_commands,
            conversation_factory=chat_conversation_factory,
            cleanup_dispatcher=chat_cleanup_dispatcher,
            cleanup_executor=chat_cleanup_executor,
        ),
        chat_cleanup_executor=chat_cleanup_executor,
        progress_hub=progress_hub,
        progress_subscription_service=progress_subscription_service,
        upload_task_limiter=UploadTaskLimiter(max_concurrency=1),
        report_submit=report_submit,
        report_callback_recovery=report_callback_recovery,
        report_dispatcher=report_dispatcher,
        llm_config=LLMIntegrationConfig(
            callback_url=None,
            callback_timeout=5.0,
            task_db_path=f"{tmp}/tasks.sqlite3",
            download_timeout=5.0,
            download_dir=tmp,
        ),
        anythingllm_config=AnythingLLMConfig(
            base_url="http://anythingllm.invalid/api/v1",
            api_key="test-key",
            timeout=5.0,
            storage_root=None,
        ),
        report_infrastructure_config=ReportInfrastructureConfig.single_instance(),
        debug_services=compose_debug_application_services(
            chat_store=chat_store,
            kb_service=kb_service,
        ),
    )


class ChatRouteAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.services = _build_test_services(self.tmp)
        self.kb_service = self.services.kb_service
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _save_document(
        self,
        file_name: str = "hash-alpha.pdf",
        *,
        original_name: str = "alpha原名.pdf",
        document_id: str = "doc-alpha",
    ) -> None:
        self.kb_service.save_document_record(
            file_name,
            1,
            document_id,
            f"custom-documents/{document_id}.json",
            original_name=original_name,
            ingested_file_name=file_name,
            metadata={
                "docSource": (
                    "docsense_ref:"
                    + hashlib.sha256(document_id.encode()).hexdigest()[:32]
                )
            },
        )

    def _conversation_id(self, chat_id: int | str) -> str:
        conversation_id = _resolved_conversation_id(
            self.services.chat_store,
            chat_id,
        )
        self.assertIsNotNone(conversation_id)
        assert conversation_id is not None
        return conversation_id

    def _chat(self, *, chat_id: int, file_names: list[str], message: str):
        return self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": chat_id,
                    "fileNames": file_names,
                    "message": message,
                },
            },
        )

    def test_rejects_protocol_invalid_request_before_run_acceptance(self) -> None:
        response = self.client.post(
            "/llm/chat",
            json={"businessType": "chat", "params": {"chatId": 1000, "message": "hi"}},
        )

        self.assertEqual(400, response.status_code)
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1000))

    def test_chat_routes_strictly_reject_invalid_chat_id_values(self) -> None:
        """所有公开文件对话路由都必须在业务处理前拒绝非正整数。"""

        invalid_values = ("1001", True, False, 0, -1, 1.5)
        json_paths = (
            "/llm/chat",
            "/llm/chat/title",
            "/llm/chat/abort",
            "/llm/chat/delete",
        )
        for chat_id in invalid_values:
            for path in json_paths:
                with self.subTest(path=path, chat_id=chat_id):
                    response = self.client.post(
                        path,
                        json={
                            "businessType": "chat",
                            "params": {"chatId": chat_id},
                        },
                    )
                    self.assertEqual(400, response.status_code)
                    self.assertEqual(
                        {"error": "chatId必须为正整数"},
                        response.get_json(),
                    )

        for raw_chat_id in ("", "0", "-1", "1.5", "001", "legacy-chat"):
            with self.subTest(path="/llm/chat/history", chat_id=raw_chat_id):
                response = self.client.get(
                    "/llm/chat/history",
                    query_string={"chatId": raw_chat_id},
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {"error": "chatId必须为正整数"},
                    response.get_json(),
                )

    def test_chat_related_routes_echo_numeric_chat_id(self) -> None:
        title_response = self.client.post(
            "/llm/chat/title",
            json={"businessType": "chat", "params": {"chatId": 1011}},
        )
        abort_response = self.client.post(
            "/llm/chat/abort",
            json={"businessType": "chat", "params": {"chatId": 1011}},
        )
        history_response = self.client.get(
            "/llm/chat/history",
            query_string={"chatId": "1011"},
        )

        self.assertEqual({"chatId": 1011, "title": ""}, title_response.get_json())
        self.assertEqual(1011, abort_response.get_json()["chatId"])
        self.assertIsInstance(abort_response.get_json()["chatId"], int)
        self.assertEqual([], history_response.get_json())

    def test_new_empty_knowledge_base_chat_remains_free_chat(self) -> None:
        # 阶段 0 先把旧“空数组”资产收窄为空知识库场景。这样阶段 3 接入自动全量后，
        # 本测试仍能证明空知识库不会被误判为错误，也不会伪造文件绑定或历史附件。
        self.assertEqual([], self.kb_service.list_document_records())

        response = self._chat(chat_id=1001, file_names=[], message=" 你好 ")

        self.assertEqual(200, response.status_code)
        self.assertEqual("text/event-stream", response.mimetype)
        body = response.get_data(as_text=True)
        self.assertIn('event: chatInfo\ndata: {"chatId": 1001, "isNewChat": true}', body)
        self.assertIn('event: textChunk\ndata: {"content": "第一段"}', body)
        self.assertIn('event: done\ndata: {"chatId": 1001}', body)

        conversation_id = self._conversation_id(1001)
        session = self.services.chat_store.sessions.get(conversation_id)
        messages = self.services.chat_store.messages.list_by_chat(conversation_id)
        runs = self.services.chat_store.runs.list_active("1001")
        self.assertIsNotNone(session)
        assert session is not None
        self.assertTrue(session.workspace_ref)
        self.assertTrue(session.thread_ref)
        self.assertEqual([], list(runs))
        self.assertEqual(["user", "assistant"], [item.role for item in messages])
        self.assertEqual("你好", messages[0].content)
        self.assertEqual(0, len(messages[0].files))
        self.assertEqual("第一段第二段", messages[1].content)
        self.assertEqual(
            (),
            self.services.chat_store.document_bindings.list_current_by_chat(
                conversation_id
            ),
        )
        self.assertEqual(
            ["chatInfo", "textChunk", "textChunk", "done"],
            [
                event.event_type
                for event in self.services.chat_store.events.list_by_run(
                    messages[0].run_id
                )
            ],
        )
        self.assertFalse(hasattr(self.services, "chat_db"))

    def test_chat_sse_contract_does_not_expose_internal_run_identity(self) -> None:
        response = self._chat(
            chat_id=1002,
            file_names=[],
            message="验证既有 SSE 协议",
        )

        body = response.get_data(as_text=True)
        self.assertEqual("text/event-stream; charset=utf-8", response.content_type)
        self.assertEqual(
            'event: chatInfo\ndata: {"chatId": 1002, "isNewChat": true}\n\n'
            'event: textChunk\ndata: {"content": "第一段"}\n\n'
            'event: textChunk\ndata: {"content": "第二段"}\n\n'
            'event: done\ndata: {"chatId": 1002}\n\n',
            body,
        )
        self.assertNotIn("runId", body)
        self.assertNotIn("requestId", body)
        self.assertNotIn("\nid:", body)
        self.assertNotIn("X-Chat-Run-Id", response.headers)
        self.assertNotIn("X-Request-Id", response.headers)

    def test_document_snapshot_is_resolved_inside_application_layer(self) -> None:
        self._save_document()

        response = self._chat(
            chat_id=1003,
            file_names=["hash-alpha.pdf"],
            message="请总结",
        )

        self.assertEqual(200, response.status_code)
        response.get_data()
        documents = self.services.chat_store.document_bindings.list_current_by_chat(
            self._conversation_id(1003)
        )
        messages = self.services.chat_store.messages.list_by_chat(
            self._conversation_id(1003)
        )
        run = next(
            message.run_id
            for message in messages
            if message.role == "user"
        )
        input_snapshot = self.services.chat_store.run_inputs.get(run)
        self.assertEqual(1, len(documents))
        self.assertEqual("document:doc-alpha", documents[0].document_ref)
        self.assertEqual("alpha原名.pdf", documents[0].original_name)
        self.assertEqual("alpha原名.pdf", messages[0].files[0].original_name)
        self.assertIsNotNone(input_snapshot)
        assert input_snapshot is not None
        self.assertEqual("请总结", input_snapshot.message)
        self.assertEqual("document:doc-alpha", input_snapshot.files[0].document_ref)

    def test_new_empty_chat_uses_all_available_documents(self) -> None:
        self._save_document(
            file_name="hash-beta.pdf",
            original_name="Beta 原名.pdf",
            document_id="doc-beta",
        )
        self._save_document(
            file_name="hash-alpha.pdf",
            original_name="Alpha 原名.pdf",
            document_id="doc-alpha",
        )

        response = self._chat(
            chat_id=1012,
            file_names=[],
            message="请综合总结",
        )

        self.assertEqual(200, response.status_code)
        body = response.get_data(as_text=True)
        conversation_id = self._conversation_id(1012)
        messages = self.services.chat_store.messages.list_by_chat(conversation_id)
        run_input = self.services.chat_store.run_inputs.get(
            messages[0].run_id
        )
        bindings = (
            self.services.chat_store.document_bindings.list_current_by_chat(
                conversation_id
            )
        )
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(
            ("hash-alpha.pdf", "hash-beta.pdf"),
            tuple(item.file_name for item in run_input.files),
        )
        self.assertEqual((), messages[0].files)
        self.assertEqual(
            {"document:doc-alpha", "document:doc-beta"},
            {item.document_ref for item in bindings},
        )
        self.assertIn(
            'event: chatInfo\ndata: {"chatId": 1012, "isNewChat": true}',
            body,
        )

        # 公开路由必须只创建一个任务级 Chat Port，并让自动文件走与显式文件相同的
        # attach + stream_message(document_refs) 调用链。
        factory = self.services.chat_conversation_factory
        self.assertEqual(1, len(factory.ports))
        self.assertEqual(0, factory.active_leases)
        self.assertEqual(
            ("document:doc-alpha", "document:doc-beta"),
            tuple(
                item.document_ref
                for item in factory.ports[0].attach_document_calls[0][1]
            ),
        )
        self.assertEqual(
            ("document:doc-alpha", "document:doc-beta"),
            factory.ports[0].stream_message_calls[0][2],
        )

        history_response = self.client.get(
            "/llm/chat/history",
            query_string={"chatId": "1012"},
        )
        self.assertEqual(200, history_response.status_code)
        history = history_response.get_json()
        self.assertEqual([], history[0]["files"])
        leases = self.services.chat_store.resource_leases.list_by_chat(
            conversation_id
        )
        self.assertEqual(
            ["document_binding", "document_binding", "thread", "workspace"],
            sorted(item.resource_type for item in leases),
        )
        self.assertTrue(all(item.status == "active" for item in leases))

    def test_new_empty_chat_logs_requested_and_effective_counts_separately(
        self,
    ) -> None:
        """自动全量时，路由原始数量与事务最终数量不得混用。"""

        self._save_document(
            file_name="hash-beta.pdf",
            original_name="Beta 原名.pdf",
            document_id="doc-beta",
        )
        self._save_document(
            file_name="hash-alpha.pdf",
            original_name="Alpha 原名.pdf",
            document_id="doc-alpha",
        )

        with self.assertLogs(
            "app.blueprints.llm",
            level="INFO",
        ) as blueprint_logs, self.assertLogs(
            "app.modules.chat",
            level="INFO",
        ) as chat_logs:
            response = self._chat(
                chat_id=1016,
                file_names=[],
                message="请综合总结",
            )
            response.get_data()

        allocation_log = next(
            message
            for message in blueprint_logs.output
            if "文件对话运行已分配" in message
        )
        self.assertIn("runId=", allocation_log)
        self.assertIn("requested_file_count=0", allocation_log)

        selection_log = next(
            message
            for message in chat_logs.output
            if "受理事务已选择有效文档" in message
        )
        self.assertIn("run_id=", selection_log)
        self.assertIn("selection_mode=automatic_initial", selection_log)
        self.assertIn("session_created=True", selection_log)
        self.assertIn("default_candidate_count=2", selection_log)
        self.assertIn("effective_file_count=2", selection_log)
        self.assertNotIn("hash-alpha.pdf", selection_log)
        self.assertNotIn("custom-documents", selection_log)

        accepted_log = next(
            message
            for message in chat_logs.output
            if "运行已受理并冻结输入快照" in message
        )
        self.assertIn("effective_file_count=2", accepted_log)

    def test_new_empty_chat_over_limit_rolls_back_all_local_facts(self) -> None:
        for index in range(CHAT_MAX_FILES_PER_REQUEST + 1):
            self._save_document(
                file_name=f"hash-{index:03d}.pdf",
                original_name=f"原名-{index:03d}.pdf",
                document_id=f"doc-{index:03d}",
            )

        response = self._chat(
            chat_id=1013,
            file_names=[],
            message="请总结全部文件",
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "fileNames超过文件对话数量上限"},
            response.get_json(),
        )
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1013))
        self.assertEqual((), self.services.chat_store.sessions.list_all())

    def test_new_empty_chat_accepts_effective_count_equal_to_limit(self) -> None:
        """最终有效数量等于上限时必须正常受理，不能出现大于等于误判。"""

        for index in range(CHAT_MAX_FILES_PER_REQUEST):
            self._save_document(
                file_name=f"limit-{index:03d}.pdf",
                original_name=f"上限原名-{index:03d}.pdf",
                document_id=f"limit-doc-{index:03d}",
            )

        response = self._chat(
            chat_id=1016,
            file_names=[],
            message="请总结上限范围内的全部文件",
        )
        body = response.get_data(as_text=True)

        self.assertEqual(200, response.status_code)
        self.assertIn(
            'event: done\ndata: {"chatId": 1016}',
            body,
        )
        conversation_id = self._conversation_id(1016)
        messages = self.services.chat_store.messages.list_by_chat(conversation_id)
        run_input = self.services.chat_store.run_inputs.get(
            messages[0].run_id
        )
        self.assertIsNotNone(run_input)
        assert run_input is not None
        self.assertEqual(CHAT_MAX_FILES_PER_REQUEST, len(run_input.files))
        self.assertEqual(
            CHAT_MAX_FILES_PER_REQUEST,
            len(
                self.services.chat_store.document_bindings
                .list_current_by_chat(conversation_id)
            ),
        )

    def test_new_empty_chat_rejects_duplicate_business_file_name(self) -> None:
        self.kb_service.save_document_record(
            file_name="same.pdf",
            architecture_id=1,
            anything_doc_id="doc-one",
            doc_path="custom-documents/doc-one.json",
            original_name="同名一.pdf",
            ingested_file_name="one-same.pdf",
        )
        self.kb_service.save_document_record(
            file_name="same.pdf",
            architecture_id=2,
            anything_doc_id="doc-two",
            doc_path="custom-documents/doc-two.json",
            original_name="同名二.pdf",
            ingested_file_name="two-same.pdf",
        )

        response = self._chat(
            chat_id=1014,
            file_names=[],
            message="请总结全部文件",
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "全量文件范围存在重复fileName，无法用于对话"},
            response.get_json(),
        )
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1014))
        self.assertEqual(
            (),
            self.services.chat_conversation_factory.ports,
        )

    def test_new_empty_chat_rejects_duplicate_remote_identity_before_io(
        self,
    ) -> None:
        """重复远端身份必须整体拒绝，不能创建会话或进入供应商 Port。"""

        self.kb_service.save_document_record(
            file_name="remote-alpha.pdf",
            architecture_id=1,
            anything_doc_id="same-document",
            doc_path="custom-documents/remote-alpha.json",
            original_name="远端一.pdf",
            ingested_file_name="remote-alpha.pdf",
        )
        self.kb_service.save_document_record(
            file_name="remote-beta.pdf",
            architecture_id=2,
            anything_doc_id="same-document",
            doc_path="custom-documents/remote-beta.json",
            original_name="远端二.pdf",
            ingested_file_name="remote-beta.pdf",
        )

        response = self._chat(
            chat_id=1017,
            file_names=[],
            message="请总结全部文件",
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "全量文件范围存在重复文档引用，无法用于对话"},
            response.get_json(),
        )
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1017))
        self.assertEqual(
            (),
            self.services.chat_conversation_factory.ports,
        )

    def test_new_empty_chat_rejects_malformed_catalog_before_io(self) -> None:
        """损坏目录记录不得被跳过，也不得产生任何远端会话副作用。"""

        self.kb_service.save_document_record(
            file_name="broken.pdf",
            architecture_id=1,
            anything_doc_id="",
            doc_path="",
            original_name="损坏记录.pdf",
            ingested_file_name="broken.pdf",
        )

        response = self._chat(
            chat_id=1018,
            file_names=[],
            message="请总结全部文件",
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "文件 broken.pdf 缺少可用于对话的文档引用"},
            response.get_json(),
        )
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1018))
        self.assertEqual(
            (),
            self.services.chat_conversation_factory.ports,
        )

    def test_unresolved_document_is_404_without_creating_session_or_run(self) -> None:
        response = self._chat(
            chat_id=1004,
            file_names=["missing.pdf"],
            message="请总结",
        )

        self.assertEqual(404, response.status_code)
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1004))

    def test_active_chat_request_is_rejected_with_409(self) -> None:
        self.services.chat_commands.start_chat_run(
            identity=FileChatIdentity(chat_id=1005),
            user_message="first",
        )

        response = self._chat(
            chat_id=1005,
            file_names=[],
            message="second",
        )

        self.assertEqual(409, response.status_code)

    def test_sse_close_after_execution_starts_preserves_user_turn(self) -> None:
        """已领取执行权后连接关闭，用户轮次仍按失败语义保留。"""
        response = self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": 1006,
                    "fileNames": [],
                    "message": "执行已开始后保留",
                },
            },
            buffered=False,
        )

        response.close()

        conversation_id = self._conversation_id(1006)
        messages = self.services.chat_store.messages.list_by_chat(conversation_id)
        self.assertEqual(1, len(messages))
        self.assertEqual(MESSAGE_COMMITTED, messages[0].status)
        run = self.services.chat_store.runs.get(messages[0].run_id)
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(RUN_FAILED, run.status)
        self.assertEqual((), self.services.chat_store.runs.list_active(conversation_id))

    def test_global_stream_capacity_returns_429_before_run_acceptance(self) -> None:
        executor = self.services.chat_run_executor
        acquired = [executor.try_acquire_stream_slot() for _ in range(executor.max_concurrent_streams)]
        self.assertEqual([True] * executor.max_concurrent_streams, acquired)
        try:
            response = self._chat(
                chat_id=1007,
                file_names=[],
                message="queued?",
            )
        finally:
            for _ in range(sum(acquired)):
                executor.release_stream_slot()

        self.assertEqual(429, response.status_code)
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1007))

    def test_same_chat_admission_conflict_returns_409_before_global_429(
        self,
    ) -> None:
        """同一 chatId 已在准入窗口内时，即使全局容量已满也稳定返回 409。"""
        executor = self.services.chat_run_executor
        lease = self.services.chat_commands.reserve_chat_admission(
            identity=FileChatIdentity(chat_id=1017),
            scope_selector=ChatScopeSelector.for_files(()),
        )
        acquired = [
            executor.try_acquire_stream_slot()
            for _ in range(executor.max_concurrent_streams)
        ]
        self.assertEqual(
            [True] * executor.max_concurrent_streams,
            acquired,
        )
        try:
            response = self._chat(
                chat_id=1017,
                file_names=[],
                message="same chat must win",
            )
        finally:
            self.services.chat_commands.release_chat_admission(lease=lease)
            for _ in range(sum(acquired)):
                executor.release_stream_slot()

        self.assertEqual(409, response.status_code)
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1017))

    def test_stream_capacity_exception_releases_admission_guard(self) -> None:
        """容量适配器异常不得把同一 chatId 阻塞到 Guard 自然过期。"""
        executor = self.services.chat_run_executor
        with patch.object(
            executor,
            "try_acquire_stream_slot",
            side_effect=RuntimeError("forced capacity failure"),
        ):
            response = self._chat(
                chat_id=1020,
                file_names=[],
                message="capacity failure",
            )

        self.assertEqual(500, response.status_code)
        self.assertIsNone(_resolved_conversation_id(self.services.chat_store, 1020))
        retry = self.services.chat_commands.reserve_chat_admission(
            identity=FileChatIdentity(chat_id=1020),
            scope_selector=ChatScopeSelector.for_files(()),
        )
        self.services.chat_commands.release_chat_admission(lease=retry)

    def test_continue_chat_reuses_session_and_reports_not_new(self) -> None:
        first = self._chat(chat_id=1008, file_names=[], message="first")
        first.get_data()
        second = self._chat(chat_id=1008, file_names=[], message="second")

        self.assertEqual(200, second.status_code)
        self.assertIn('"isNewChat": false', second.get_data(as_text=True))
        self.assertEqual(
            4,
            len(
                self.services.chat_store.messages.list_by_chat(
                    self._conversation_id(1008)
                )
            ),
        )

    def test_later_empty_chat_does_not_absorb_new_knowledge_base_files(self) -> None:
        """后续空数组只复用既有 binding heads，不重新展开知识库全量目录。"""

        self._save_document(
            file_name="hash-alpha.pdf",
            original_name="Alpha 原名.pdf",
            document_id="doc-alpha",
        )
        first = self._chat(
            chat_id=1015,
            file_names=[],
            message="first",
        )
        first.get_data()
        self._save_document(
            file_name="hash-beta.pdf",
            original_name="Beta 原名.pdf",
            document_id="doc-beta",
        )

        second = self._chat(
            chat_id=1015,
            file_names=[],
            message="second",
        )
        second_body = second.get_data(as_text=True)

        self.assertEqual(200, second.status_code)
        self.assertIn('"isNewChat": false', second_body)
        factory = self.services.chat_conversation_factory
        self.assertEqual(2, len(factory.ports))
        self.assertEqual([], factory.ports[1].attach_document_calls)
        self.assertEqual(
            ("document:doc-alpha",),
            factory.ports[1].stream_message_calls[0][2],
        )
        self.assertEqual(
            ("hash-alpha.pdf",),
            tuple(
                item.file_name
                for item in (
                    self.services.chat_store.document_bindings
                    .list_current_by_chat(self._conversation_id(1015))
                )
            ),
        )
        user_messages = [
            item
            for item in self.services.chat_store.messages.list_by_chat(
                self._conversation_id(1015)
            )
            if item.role == "user"
        ]
        self.assertEqual(2, len(user_messages))
        self.assertEqual((), user_messages[0].files)
        self.assertEqual((), user_messages[1].files)

    def test_replaced_business_file_creates_a_new_document_binding_revision(self) -> None:
        self._save_document(document_id="doc-v1")
        first = self._chat(
            chat_id=1009,
            file_names=["hash-alpha.pdf"],
            message="first",
        )
        first.get_data()
        self._save_document(document_id="doc-v2")

        second = self._chat(
            chat_id=1009,
            file_names=["hash-alpha.pdf"],
            message="second",
        )
        second.get_data()

        conversation_id = self._conversation_id(1009)
        documents = self.services.chat_store.document_bindings.list_by_chat(
            conversation_id
        )
        document = self.services.chat_store.document_bindings.list_current_by_chat(
            conversation_id
        )[0]
        binding_leases = [
            lease
            for lease in self.services.chat_store.resource_leases.list_by_chat(
                conversation_id
            )
            if lease.resource_type == "document_binding"
        ]
        self.assertEqual("document:doc-v2", document.document_ref)
        self.assertEqual(2, len(documents))
        self.assertEqual(2, len(binding_leases))

    def test_delete_succeeds_for_leases_created_by_the_new_executor(self) -> None:
        response = self._chat(
            chat_id=1010,
            file_names=[],
            message="delete after this",
        )
        response.get_data()

        deleted = self.client.post(
            "/llm/chat/delete",
            json={
                "businessType": "chat",
                "params": {"chatId": 1010},
            },
        )

        self.assertEqual(200, deleted.status_code)
        self.assertTrue(deleted.get_json()["deleted"])
        self.assertEqual(1010, deleted.get_json()["chatId"])
        self.assertIsInstance(deleted.get_json()["chatId"], int)


class ChatDifferentSessionConcurrencyTests(unittest.TestCase):
    """验证 50 个不同会话在完整公开路由执行链中的隔离性。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.services = _build_test_services(
            self.tmp,
            max_concurrent_streams=50,
        )
        self.services.kb_service.save_document_record(
            file_name="shared.pdf",
            architecture_id=1,
            anything_doc_id="shared-document",
            doc_path="custom-documents/shared-document.json",
            original_name="共享原名.pdf",
            ingested_file_name="shared.pdf",
        )
        self.app = create_app(services=self.services)

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_fifty_different_new_chats_keep_all_facts_isolated(self) -> None:
        """流槽位配置为 50 时，不同 chatId 不得串写输入、资源、绑定或历史。"""

        worker_count = 50
        barrier = Barrier(worker_count)

        def send(index: int) -> tuple[int, int, str]:
            chat_id = 20000 + index
            # Flask test client 不是跨线程共享对象；每个 Worker 创建自己的客户端，
            # 只共享线程安全的应用服务和临时 SQLite，贴近多请求并发边界。
            with self.app.test_client() as client:
                barrier.wait()
                response = client.post(
                    "/llm/chat",
                    json={
                        "businessType": "chat",
                        "params": {
                            "chatId": chat_id,
                            "fileNames": [],
                            "message": f"question-{index}",
                        },
                    },
                )
                return chat_id, response.status_code, response.get_data(
                    as_text=True
                )

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(send, range(worker_count)))

        self.assertEqual(50, self.services.chat_run_executor.max_concurrent_streams)
        self.assertEqual(
            {},
            {
                chat_id: {
                    "status_code": status_code,
                    "body": body[:500],
                }
                for chat_id, status_code, body in results
                if status_code != 200
            },
        )
        self.assertEqual(
            {
                chat_id
                for chat_id, _, body in results
                if f'event: done\ndata: {{"chatId": {chat_id}}}' in body
            },
            {20000 + index for index in range(worker_count)},
        )

        workspace_refs: set[str] = set()
        thread_refs: set[str] = set()
        run_ids: set[str] = set()
        for index in range(worker_count):
            public_chat_id = 20000 + index
            conversation_id = _resolved_conversation_id(
                self.services.chat_store,
                public_chat_id,
            )
            self.assertIsNotNone(conversation_id)
            assert conversation_id is not None
            session = self.services.chat_store.sessions.get(conversation_id)
            self.assertIsNotNone(session)
            assert session is not None
            workspace_refs.add(session.workspace_ref)
            thread_refs.add(session.thread_ref)

            messages = self.services.chat_store.messages.list_by_chat(
                conversation_id
            )
            self.assertEqual(["user", "assistant"], [
                message.role for message in messages
            ])
            self.assertEqual(f"question-{index}", messages[0].content)
            self.assertEqual("第一段第二段", messages[1].content)
            self.assertEqual((), messages[0].files)
            run_ids.add(messages[0].run_id)

            run_input = self.services.chat_store.run_inputs.get(
                messages[0].run_id
            )
            self.assertIsNotNone(run_input)
            assert run_input is not None
            self.assertEqual(f"question-{index}", run_input.message)
            self.assertEqual(("shared.pdf",), tuple(
                item.file_name for item in run_input.files
            ))
            self.assertEqual(
                ("document:shared-document",),
                tuple(
                    item.document_ref
                    for item in (
                        self.services.chat_store.document_bindings
                        .list_current_by_chat(conversation_id)
                    )
                ),
            )
            self.assertEqual(
                (),
                self.services.chat_store.runs.list_active(conversation_id),
            )

        self.assertEqual(worker_count, len(workspace_refs))
        self.assertEqual(worker_count, len(thread_refs))
        self.assertEqual(worker_count, len(run_ids))
        factory = self.services.chat_conversation_factory
        self.assertEqual(worker_count, len(factory.ports))
        self.assertEqual(0, factory.active_leases)
        self.assertEqual(
            {f"question-{index}" for index in range(worker_count)},
            {
                port.stream_message_calls[0][1]
                for port in factory.ports
            },
        )
        self.assertTrue(all(
            port.stream_message_calls[0][2]
            == ("document:shared-document",)
            for port in factory.ports
        ))
        self.assertEqual(
            {f"chat-id{20000 + index}" for index in range(worker_count)},
            {
                port.open_conversation_calls[0][0]
                for port in factory.ports
            },
        )
        self.assertTrue(
            all(len(port.open_conversation_calls) == 1 for port in factory.ports)
        )


if __name__ == "__main__":
    unittest.main()
