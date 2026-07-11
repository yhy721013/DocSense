"""AnythingLLM 文件对话的任务级工厂。"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager, contextmanager
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from app.integrations.anythingllm.chat_gateway import AnythingLLMChatGateway
from app.integrations.anythingllm.policies import chat_workspace_settings
from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.integrations.anythingllm.workspaces import AnythingLLMWorkspaceClient
from app.ports import ChatConversationFactory, ChatConversationPort
from app.services.core.config import AnythingLLMConfig


logger = logging.getLogger(__name__)


class AnythingLLMChatFactory(ChatConversationFactory):
    """为单个请求或后台任务创建相互隔离的 AnythingLLM 对话网关。"""

    def __init__(
        self,
        config: AnythingLLMConfig,
        *,
        user_id: Optional[int] = 1,
        workspace_settings: Optional[Mapping[str, Any]] = None,
        stream_mode: str = "query",
        standalone_mode: str = "chat",
        transport_factory: Callable[..., AnythingLLMTransport] = AnythingLLMTransport,
    ) -> None:
        """校验不可变工厂配置，但不在构造阶段创建网络会话。"""
        if not isinstance(config, AnythingLLMConfig):
            raise TypeError("config must be AnythingLLMConfig")
        if user_id is not None and (
            isinstance(user_id, bool)
            or not isinstance(user_id, int)
            or user_id < 1
        ):
            raise ValueError("user_id must be a positive integer or None")
        if not callable(transport_factory):
            raise TypeError("transport_factory must be callable")

        self._config = config
        self._user_id = user_id
        self._workspace_settings = MappingProxyType(
            dict(
                chat_workspace_settings()
                if workspace_settings is None
                else workspace_settings
            )
        )
        self._stream_mode = AnythingLLMChatGateway._normalize_mode(stream_mode)
        self._standalone_mode = AnythingLLMChatGateway._normalize_mode(
            standalone_mode
        )
        self._transport_factory = transport_factory

    def create(self) -> AbstractContextManager[ChatConversationPort]:
        """返回惰性租约；进入上下文时才创建具体网络对象。"""
        return self._create_lease()

    @contextmanager
    def _create_lease(self) -> Iterator[ChatConversationPort]:
        transport: AnythingLLMTransport | None = None
        task_failed = False
        try:
            transport = self._transport_factory(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout=self._config.timeout,
            )
            workspace_client = AnythingLLMWorkspaceClient(transport)
            thread_client = AnythingLLMThreadClient(transport)
            gateway = AnythingLLMChatGateway(
                workspace_client,
                thread_client,
                user_id=self._user_id,
                workspace_settings=self._workspace_settings,
                stream_mode=self._stream_mode,
                standalone_mode=self._standalone_mode,
            )
            logger.debug(
                "Created task-scoped AnythingLLM chat gateway: "
                "has_user_context=%s",
                self._user_id is not None,
            )
            yield gateway
        except BaseException:
            task_failed = True
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                    logger.debug("Closed task-scoped AnythingLLM chat transport")
                except Exception:
                    if task_failed:
                        logger.exception(
                            "Failed to close AnythingLLM chat transport; "
                            "preserving active task exception"
                        )
                    else:
                        raise
