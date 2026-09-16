"""分类节点变更的请求级 AnythingLLM 原子 Client 工厂。

本模块只组装项目既有的 Transport 和 Workspace Client，不实现业务编排。每次 ``create``
都会新建一个 Transport，并在离开上下文时关闭；因此单个同步 Operation 的不同 HTTP 调用
不会共享 ``requests.Session``、Cookie 或连接级可变状态。
"""

from __future__ import annotations

import logging
import math
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Protocol, runtime_checkable

from app.integrations.anythingllm import (
    AnythingLLMTransport,
    AnythingLLMWorkspaceClient,
)
from app.services.core.config import AnythingLLMConfig


logger = logging.getLogger(__name__)


def _positive_finite_timeout(value: object) -> float:
    """校验经 deadline 裁剪后的单次 HTTP 超时，拒绝底层隐式转换。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds 必须是正有限秒数")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError("timeout_seconds 必须是正有限秒数")
    return normalized


@dataclass(frozen=True)
class ReassignmentAnythingLLMClients:
    """一次 HTTP 租约内共享同一 Transport 的最小原子 Client 集合。"""

    workspaces: AnythingLLMWorkspaceClient


@runtime_checkable
class ReassignmentAnythingLLMClientFactoryProtocol(Protocol):
    """供生产 Adapter 与离线 Fake 共享的请求级 Client Factory 协议。"""

    def create(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractContextManager[ReassignmentAnythingLLMClients]:
        ...


class AnythingLLMReassignmentClientFactory:
    """按一次 HTTP 调用创建独立 Transport 的无状态工厂。

    通用 ``AnythingLLMConfig.timeout`` 不在这里直接使用。分类节点变更必须经过
    ``ReassignmentExecutionDeadline`` 计算剩余预算后，显式传入 ``timeout_seconds``；这样
    超时不会突破同步请求为探测/补偿保留的窗口。
    """

    def __init__(
        self,
        config: AnythingLLMConfig,
        *,
        transport_factory: Callable[..., AnythingLLMTransport] = AnythingLLMTransport,
    ) -> None:
        if not isinstance(config, AnythingLLMConfig):
            raise TypeError("config 必须是 AnythingLLMConfig")
        if not callable(transport_factory):
            raise TypeError("transport_factory 必须可调用")
        self._config = config
        self._transport_factory = transport_factory

    def create(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractContextManager[ReassignmentAnythingLLMClients]:
        """返回只覆盖一次原子调用的 Client 租约。"""

        return self._create_lease(timeout_seconds=_positive_finite_timeout(timeout_seconds))

    @contextmanager
    def _create_lease(
        self,
        *,
        timeout_seconds: float,
    ) -> Iterator[ReassignmentAnythingLLMClients]:
        transport: AnythingLLMTransport | None = None
        action_failed = False
        try:
            transport = self._transport_factory(
                base_url=self._config.base_url,
                api_key=self._config.api_key,
                timeout=timeout_seconds,
            )
            yield ReassignmentAnythingLLMClients(
                workspaces=AnythingLLMWorkspaceClient(transport),
            )
        except BaseException:
            action_failed = True
            raise
        finally:
            if transport is not None:
                try:
                    transport.close()
                    logger.debug(
                        "分类节点变更请求级 AnythingLLM Transport 已关闭: "
                        "timeout_seconds=%.3f",
                        timeout_seconds,
                    )
                except Exception as close_error:
                    # 已有业务异常时，关闭失败只能作为诊断保留，不能覆盖真正决定 Saga 事实的
                    # 远端调用异常；这里不能使用 logger.exception，否则 traceback 会把供应商
                    # 异常正文、URL 或其他敏感上下文带入日志。正常路径关闭失败仍必须上抛，
                    # 交由 Adapter 保守归类。
                    if action_failed:
                        logger.error(
                            "关闭分类节点变更 AnythingLLM Transport 失败，"
                            "保留原始业务异常: close_error_type=%s",
                            type(close_error).__name__,
                        )
                    else:
                        raise


__all__ = [
    "AnythingLLMReassignmentClientFactory",
    "ReassignmentAnythingLLMClientFactoryProtocol",
    "ReassignmentAnythingLLMClients",
]
