"""进程内 Progress 最新投影与发布订阅 Hub。

该 Hub 仍保留旧业务代码使用的字典接口，同时为任务模块的类型化 Adapter 提供带
执行身份和序号的内部事件。它只是单实例内存通知设施，不具备跨进程广播、持久化、
重放或可靠队列能力；多实例部署前必须由 Redis 等共享通知实现替换。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, DefaultDict, Dict, List, Tuple

from app.services.core.progress import normalize_progress_payload


logger = logging.getLogger(__name__)

Subscriber = Callable[[Dict[str, Any]], None]


@dataclass(frozen=True)
class ProgressHubEvent:
    """Hub 内部使用的不可变事件信封，不属于 WebSocket 公开消息。

    ``payload`` 仍是迁移期旧字典，因此调用方不得修改。Hub 在公开读取和旧回调边界
    都会复制字典，避免一个订阅者污染最新投影或其他连接。
    """

    business_type: str
    business_key: str
    task_id: str
    sequence_no: int
    updated_at: str
    payload: Dict[str, Any]


EventSubscriber = Callable[[ProgressHubEvent], None]


class LLMProgressHub:
    """线程安全的单实例 Progress Hub，并兼容旧调用接口。

    所有共享状态均由 ``_lock`` 保护；发布时先在锁内更新 latest 并复制订阅者，随后
    在锁外调用回调。这样慢订阅者或异常订阅者既不会长期占用 Hub 锁，也不会阻断
    同一进程内的其他任务发布。
    """

    _LEGACY_TASK_PREFIX = "legacy-progress"

    def __init__(self) -> None:
        self._lock = RLock()
        self._subscribers: DefaultDict[
            Tuple[str, str],
            List[Subscriber],
        ] = defaultdict(list)
        self._event_subscribers: DefaultDict[
            Tuple[str, str],
            List[EventSubscriber],
        ] = defaultdict(list)
        self._latest: Dict[Tuple[str, str], ProgressHubEvent] = {}

    def subscribe(
        self,
        business_type: str,
        business_key: str,
        callback: Subscriber,
        *,
        replay_latest: bool = True,
    ) -> None:
        """注册旧字典回调；默认保持“订阅后立即重放 latest”的既有行为。"""

        if not callable(callback):
            raise TypeError("callback 必须可调用")
        key = (business_type, business_key)
        with self._lock:
            self._subscribers[key].append(callback)
            latest = self._latest.get(key) if replay_latest else None

        if latest is not None:
            self._invoke_legacy_subscriber(callback, latest, replay=True)

    def unsubscribe(
        self,
        business_type: str,
        business_key: str,
        callback: Subscriber,
    ) -> None:
        """幂等释放旧字典回调。"""

        key = (business_type, business_key)
        with self._lock:
            listeners = self._subscribers.get(key)
            if not listeners:
                return
            remaining = [listener for listener in listeners if listener is not callback]
            if remaining:
                self._subscribers[key] = remaining
            else:
                self._subscribers.pop(key, None)

    def subscribe_event(
        self,
        business_type: str,
        business_key: str,
        callback: EventSubscriber,
        *,
        replay_latest: bool = True,
    ) -> None:
        """注册类型化 Adapter 使用的内部事件回调。

        1B-2 的应用服务会先建立订阅、再显式读取当前快照，因此生产 Adapter 传入
        ``replay_latest=False``，避免同一 latest 被“订阅重放 + 当前读取”发送两次。
        """

        if not callable(callback):
            raise TypeError("callback 必须可调用")
        key = (business_type, business_key)
        with self._lock:
            self._event_subscribers[key].append(callback)
            latest = self._latest.get(key) if replay_latest else None

        if latest is not None:
            self._invoke_event_subscriber(callback, latest, replay=True)

    def unsubscribe_event(
        self,
        business_type: str,
        business_key: str,
        callback: EventSubscriber,
    ) -> None:
        """幂等释放内部事件回调。"""

        key = (business_type, business_key)
        with self._lock:
            listeners = self._event_subscribers.get(key)
            if not listeners:
                return
            remaining = [listener for listener in listeners if listener is not callback]
            if remaining:
                self._event_subscribers[key] = remaining
            else:
                self._event_subscribers.pop(key, None)

    def get_latest(
        self,
        business_type: str,
        business_key: str,
    ) -> Dict[str, Any] | None:
        """返回旧接口所需的 latest 副本，禁止调用方修改 Hub 内部投影。"""

        with self._lock:
            latest = self._latest.get((business_type, business_key))
            return deepcopy(latest.payload) if latest is not None else None

    def get_latest_event(
        self,
        business_type: str,
        business_key: str,
    ) -> ProgressHubEvent | None:
        """返回任务模块 Adapter 使用的 latest 事件副本。"""

        with self._lock:
            latest = self._latest.get((business_type, business_key))
            return self._copy_event(latest) if latest is not None else None

    def publish(
        self,
        business_type: str,
        business_key: str,
        payload: Dict[str, Any],
        *,
        task_id: str = "",
    ) -> None:
        """更新 latest 后在锁外通知全部订阅者。

        ``task_id`` 是内部可选参数，不进入公开消息。任务受理入口会传入真实
        ``execution_id``；同一执行的后续旧发布调用可以省略并自动沿用。旧测试或
        尚未迁移的孤立发布没有执行身份时，Hub 使用仅限本进程的兼容身份。
        """

        if not isinstance(payload, dict):
            raise TypeError("payload 必须是 dict")
        key = (business_type, business_key)
        normalized_payload = deepcopy(normalize_progress_payload(deepcopy(payload)))
        requested_task_id = str(task_id or "").strip()

        with self._lock:
            previous = self._latest.get(key)
            effective_task_id = (
                requested_task_id
                or (previous.task_id if previous is not None else "")
                or self._legacy_task_id(business_type, business_key)
            )
            sequence_no = (
                previous.sequence_no + 1
                if previous is not None and previous.task_id == effective_task_id
                else 1
            )
            event = ProgressHubEvent(
                business_type=business_type,
                business_key=business_key,
                task_id=effective_task_id,
                sequence_no=sequence_no,
                updated_at=datetime.now(timezone.utc).isoformat(),
                payload=normalized_payload,
            )
            self._latest[key] = event
            legacy_subscribers = tuple(self._subscribers.get(key, ()))
            event_subscribers = tuple(self._event_subscribers.get(key, ()))

        logger.debug(
            "发布 Progress 内存事件: business_type=%s business_key=%s "
            "sequence_no=%s legacy_subscriber_count=%s event_subscriber_count=%s",
            business_type,
            business_key,
            sequence_no,
            len(legacy_subscribers),
            len(event_subscribers),
        )
        for callback in legacy_subscribers:
            self._invoke_legacy_subscriber(callback, event, replay=False)
        for callback in event_subscribers:
            self._invoke_event_subscriber(callback, event, replay=False)

    @classmethod
    def _legacy_task_id(cls, business_type: str, business_key: str) -> str:
        """生成不公开的兼容身份；新执行的首次发布应始终传真实 TaskId。"""

        return f"{cls._LEGACY_TASK_PREFIX}:{business_type}:{business_key}"

    @staticmethod
    def _copy_event(event: ProgressHubEvent) -> ProgressHubEvent:
        return ProgressHubEvent(
            business_type=event.business_type,
            business_key=event.business_key,
            task_id=event.task_id,
            sequence_no=event.sequence_no,
            updated_at=event.updated_at,
            payload=deepcopy(event.payload),
        )

    def _invoke_legacy_subscriber(
        self,
        callback: Subscriber,
        event: ProgressHubEvent,
        *,
        replay: bool,
    ) -> None:
        try:
            callback(deepcopy(event.payload))
        except Exception:
            logger.exception(
                "Progress 旧订阅者执行失败，已隔离: business_type=%s "
                "business_key=%s sequence_no=%s replay=%s",
                event.business_type,
                event.business_key,
                event.sequence_no,
                replay,
            )

    def _invoke_event_subscriber(
        self,
        callback: EventSubscriber,
        event: ProgressHubEvent,
        *,
        replay: bool,
    ) -> None:
        try:
            # 即使内部订阅者误改 payload，也不能污染 Hub latest 或后续订阅者。
            callback(self._copy_event(event))
        except Exception:
            logger.exception(
                "Progress 类型化订阅者执行失败，已隔离: business_type=%s "
                "business_key=%s task_id=%s sequence_no=%s replay=%s",
                event.business_type,
                event.business_key,
                event.task_id,
                event.sequence_no,
                replay,
            )


__all__ = ["LLMProgressHub", "ProgressHubEvent"]
