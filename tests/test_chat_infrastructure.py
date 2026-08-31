"""阶段 12：文件对话基础设施能力边界的离线契约测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app.container import create_application_services
from app.modules.chat import (
    AbortNotificationCapabilities,
    ChatAbortService,
    ChatCleanupDispatchCapabilities,
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteService,
    ChatInfrastructureCapabilityError,
    ChatOutboxMessage,
    ChatRunCoordinator,
    ChatRunLeaseLostError,
    ChatRunLockService,
    ChatStore,
    RESOURCE_WORKSPACE,
    chat_workspace_lease_id,
)
from app.services.core.config import (
    CHAT_RUNTIME_MODE_SINGLE_INSTANCE,
    ChatInfrastructureConfig,
    ChatInfrastructureConfigurationError,
    load_chat_infrastructure_config,
)
from tests.fakes import FakeChatConversationFactory
from app.modules.chat.domain.identity import FileChatIdentity


class _RecordingAbortNotifier:
    """可观察的通知替身，用于证明 abort 的持久化与通知顺序。"""

    capabilities = AbortNotificationCapabilities(
        supports_single_instance=True,
        supports_shared_instances=False,
        supports_cross_instance_wakeup=False,
    )

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.notifications: list[tuple[str, str]] = []

    def notify_abort_requested(self, *, conversation_id: str, run_id: str) -> None:
        self.notifications.append((conversation_id, run_id))
        if self.fail:
            raise RuntimeError("notifier unavailable")


class _RecordingCleanupDispatcher:
    """同步调度替身，验证删除流程不绕过 cleanup dispatcher。"""

    capabilities = ChatCleanupDispatchCapabilities(
        supports_single_instance=True,
        reliable_delivery=False,
        supports_delayed_retry=False,
        supports_external_workers=False,
        supports_synchronous_completion=True,
    )

    def __init__(self, *, executor: ChatCleanupJobExecutor) -> None:
        self._executor = executor
        self.jobs = []

    def dispatch(self, *, job):
        self.jobs.append(job)
        return self._executor.execute_cleanup_job(job_id=job.job_id)


class ChatInfrastructureContractTests(unittest.TestCase):
    """验证阶段 12 只增加内部能力边界，不引入伪分布式行为。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.db_path = f"{self.tmp}/chat.sqlite3"
        self.store = ChatStore(self.db_path)
        self.commands = ChatCommandService(ChatRunLockService(self.db_path))

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def test_sqlite_store_declares_limits_and_refuses_uninstalled_outbox(self) -> None:
        """SQLite 可提供本地事务和条件更新，但不能被误用为可靠发件箱。"""
        capabilities = self.store.capabilities

        self.assertTrue(capabilities.supports_single_instance)
        self.assertFalse(capabilities.supports_shared_instances)
        self.assertTrue(capabilities.supports_atomic_transactions)
        self.assertTrue(capabilities.supports_conditional_updates)
        self.assertTrue(capabilities.supports_unique_constraints)
        self.assertTrue(capabilities.supports_event_ledger)
        self.assertFalse(capabilities.supports_transactional_outbox)
        self.assertFalse(self.store.outbox.enabled)

        with self.assertRaises(ChatInfrastructureCapabilityError):
            self.store.outbox.enqueue(
                ChatOutboxMessage(
                    message_id="outbox-1",
                    topic="chat.cleanup",
                    payload={"chat_id": "chat-1"},
                )
            )

    def test_single_instance_execution_lease_is_internal_but_not_fenced(self) -> None:
        """当前适配器保留未来租约签名，同时明确不声称跨实例 fencing。"""
        run = self.commands.start_chat_run(
            identity=FileChatIdentity(chat_id=20001),
            user_message="请总结",
        )
        lease = self.commands.issue_execution_lease(run_id=run.run_id)

        self.assertIsInstance(
            ChatRunLockService(self.db_path),
            ChatRunCoordinator,
        )
        self.assertEqual(run.run_id, lease.run_id)
        self.assertEqual(run.conversation_id, lease.conversation_id)
        self.assertFalse(lease.has_fencing)
        self.assertFalse(self.commands.lease_capabilities.supports_fencing)

        refreshed = self.commands.heartbeat_chat_run(
            run_id=run.run_id,
            execution_lease=lease,
        )
        self.assertEqual(run.run_id, refreshed.run_id)

        self.commands.fail_chat_run_with_user(
            run_id=run.run_id,
            user_message_id=f"{run.run_id}:user",
            error_message="offline test failure",
            execution_lease=lease,
        )
        with self.assertRaises(ChatRunLeaseLostError):
            self.commands.validate_execution_lease(lease=lease)

    def test_abort_persists_before_best_effort_notifier_and_keeps_success_on_notify_failure(
        self,
    ) -> None:
        """通知异常不能撤销已写入的 abort 标记或改变既有接口响应。"""
        notifier = _RecordingAbortNotifier(fail=True)
        service = ChatAbortService(
            store=self.store,
            chat_commands=self.commands,
            abort_notifier=notifier,
        )
        identity = FileChatIdentity(chat_id=20002)
        run = self.commands.start_chat_run(identity=identity)

        result = service.abort_chat(identity=identity)
        stored = self.store.runs.get(run.run_id)

        self.assertTrue(result.aborted)
        self.assertEqual(
            [(run.conversation_id, run.run_id)],
            notifier.notifications,
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertTrue(stored.abort_requested)

    def test_delete_routes_remote_compensation_through_cleanup_dispatcher(self) -> None:
        """重复删除/补偿可以替换调度器，而删除接口的 JSON 语义保持不变。"""
        factory = FakeChatConversationFactory()
        with factory.create() as port:
            refs = port.open_conversation(
                context_name="cleanup-context",
                conversation_name="cleanup-thread",
            )
        identity = FileChatIdentity(chat_id=20003)
        conversation_id = self.store.identities.create_conversation(
            identity
        ).conversation_id
        self.store.sessions.create_or_get(
            conversation_id=conversation_id,
            workspace_ref=refs.context_ref,
            thread_ref=refs.conversation_ref,
        )
        self.store.resource_leases.ensure_active(
            lease_id=chat_workspace_lease_id(conversation_id),
            conversation_id=conversation_id,
            resource_type=RESOURCE_WORKSPACE,
            external_ref=refs.context_ref,
        )
        cleanup_executor = ChatCleanupJobExecutor(
            store=self.store,
            conversation_factory=factory,
        )
        dispatcher = _RecordingCleanupDispatcher(executor=cleanup_executor)
        service = ChatDeleteService(
            store=self.store,
            chat_commands=self.commands,
            conversation_factory=factory,
            cleanup_dispatcher=dispatcher,
            cleanup_executor=cleanup_executor,
        )

        result = service.delete_chat(identity=identity)

        self.assertTrue(result.deleted)
        self.assertEqual(1, len(dispatcher.jobs))
        self.assertEqual(conversation_id, dispatcher.jobs[0].conversation_id)
        self.assertEqual("delete_chat", dispatcher.jobs[0].reason)

    def test_invalid_chat_runtime_mode_fails_before_external_configuration_load(self) -> None:
        """错误部署模式必须在容器读取 AnythingLLM 配置前 fail fast。"""
        with patch.dict(
            os.environ,
            {"DOCSENSE_CHAT_RUNTIME_MODE": "cluster"},
            clear=False,
        ):
            with patch("app.container.load_anythingllm_config") as load_anythingllm:
                with self.assertRaises(ChatInfrastructureConfigurationError):
                    create_application_services()
        load_anythingllm.assert_not_called()

    def test_single_instance_runtime_mode_is_the_only_accepted_default(self) -> None:
        """默认配置和显式单实例配置均保持同一个受支持模式。"""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCSENSE_CHAT_RUNTIME_MODE", None)
            configured = load_chat_infrastructure_config()

        explicit = ChatInfrastructureConfig.single_instance()
        self.assertEqual(CHAT_RUNTIME_MODE_SINGLE_INSTANCE, configured.runtime_mode)
        self.assertEqual(configured, explicit)


if __name__ == "__main__":
    unittest.main()
