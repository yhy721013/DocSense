"""使用持久化 latest execution 保护进度通知的通用 Adapter。"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import (
    GuardedProgressPublisherPort,
    ProgressPublication,
    ProgressPublisherPort,
    TaskCommandPort,
)


logger = logging.getLogger(__name__)


class LatestTaskProgressPublisherAdapter:
    """在通知 Hub 前复核 TaskId 仍是持久化 latest owner。

    数据库终态提交与内存通知无法组成同一事务。该复核保证：若新 execution 已经提交，
    迟到的旧初始/运行/终态 Progress 会被跳过；若复核后才提交新 execution，则新提交的
    accepted 通知按程序顺序最终覆盖旧快照。底层 Hub 还会拒绝非 accepted 的跨 TaskId
    覆盖，两层共同避免进程调度竞态让旧任务成为最终 latest。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[Any, Any, Any],
        delegate: ProgressPublisherPort,
    ) -> None:
        if not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(delegate, ProgressPublisherPort):
            raise TypeError("delegate 必须实现 ProgressPublisherPort")
        if not isinstance(delegate, GuardedProgressPublisherPort):
            raise TypeError(
                "delegate 必须实现原子 latest Guard 发布能力"
            )
        self._task_commands = task_commands
        self._delegate = delegate

    def publish(self, publication: ProgressPublication) -> None:
        if not isinstance(publication, ProgressPublication):
            raise TypeError("publication 必须是 ProgressPublication")
        business_ref = TaskBusinessRef(
            publication.key.business_type,
            publication.key.business_key,
        )
        # 不能在这里先查数据库、随后再普通 publish：两步之间新任务可能提交。底层
        # Guarded Port 会在 Hub 同键投影锁内调用该只读判断，使 owner 复核与 latest 更新
        # 对同一业务键原子化，同时不阻塞其他业务键的发布。
        published = self._delegate.publish_guarded(
            publication,
            is_current=lambda: self._task_commands.is_latest(
                publication.expected_task_id,
                business_ref,
            ),
        )
        if not isinstance(published, bool):
            raise TypeError("GuardedProgressPublisherPort 必须返回 bool")
        if not published:
            logger.warning(
                "持久化 latest 已变化，跳过旧执行 Progress: business_type=%s "
                "business_key=%s task_id=%s internal_state=%s",
                publication.key.business_type,
                publication.key.business_key,
                publication.expected_task_id,
                publication.internal_state,
            )

    def publish_guarded(
        self,
        publication: ProgressPublication,
        *,
        is_current: Callable[[], bool],
    ) -> bool:
        """把业务 Application 的 owner 复核与持久化 latest 复核合并到同一 Hub 临界区。

        Analysis Worker 已在自己的用例层提供 ``is_current``，而容器还必须复用本 Adapter
        的 ``TaskCommandPort.is_latest`` 保护，防止未来调用者遗漏持久化 latest 条件。
        两个判断都只读且不持有写事务；委托给底层 Guarded Progress Port 后，二者与同一
        业务键的投影更新处于同一原子临界区，不能退化为“先查后普通 publish”。
        """

        if not isinstance(publication, ProgressPublication):
            raise TypeError("publication 必须是 ProgressPublication")
        if not callable(is_current):
            raise TypeError("is_current 必须可调用")
        business_ref = TaskBusinessRef(
            publication.key.business_type,
            publication.key.business_key,
        )

        def guarded_current() -> bool:
            caller_current = is_current()
            if not isinstance(caller_current, bool):
                raise TypeError("Application Progress is_current 必须返回 bool")
            if not caller_current:
                return False
            return self._task_commands.is_latest(
                publication.expected_task_id,
                business_ref,
            )

        published = self._delegate.publish_guarded(
            publication,
            is_current=guarded_current,
        )
        if not isinstance(published, bool):
            raise TypeError("GuardedProgressPublisherPort 必须返回 bool")
        if not published:
            logger.warning(
                "Guarded Progress owner 已变化，跳过旧执行投影: business_type=%s "
                "business_key=%s task_id=%s internal_state=%s",
                publication.key.business_type,
                publication.key.business_key,
                publication.expected_task_id,
                publication.internal_state,
            )
        return published


__all__ = ["LatestTaskProgressPublisherAdapter"]
