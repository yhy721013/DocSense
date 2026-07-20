"""外部资源创建后的立即持久化协调器。

I/O Adapter 不应了解资源记录的 JSON 或数据库实现，但资源一旦创建，就必须在下一次外部
副作用前写入 ``WeaponryResourceStorePort``。本协调器封装短 CAS 重试；它只执行本地短事务，
不把任何网络调用包进数据库事务。
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId
from app.modules.weaponry.ports import (
    QuarantineWeaponryCreationIntent,
    RegisterWeaponryResource,
    ResolveWeaponryCreationIntent,
    WeaponryCreationIntent,
    WeaponryCreationIntentReserveResult,
    WeaponryCreationIntentStorePort,
    WeaponryPortStateError,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecordState,
    WeaponryResourceStorePort,
    WeaponryTrackedResource,
)


logger = logging.getLogger(__name__)


@runtime_checkable
class WeaponryCreatedResourceRegistrarProtocol(Protocol):
    """Adapter 只依赖这一项“创建后立即登记”能力。"""

    def ensure_ready(self, task_id: TaskId) -> None:
        """在任何外部创建副作用前确认 tracking 资源记录已经提交。"""
        ...

    def register_created(
        self,
        *,
        task_id: TaskId,
        resource_id: str,
        kind: WeaponryResourceKind,
        external_ref: str,
        ownership: WeaponryResourceOwnership,
        idempotency_key: str,
        document_key: str = "",
        call_id: str = "",
    ) -> WeaponryTrackedResource:
        ...

    def reserve_creation(
        self, intent: WeaponryCreationIntent
    ) -> WeaponryCreationIntentReserveResult:
        """在外部 create 之前原子落地创建意图。"""
        ...

    def resolve_creation(
        self,
        intent: WeaponryCreationIntent,
        *,
        external_ref: str,
    ) -> WeaponryCreationIntent:
        """资源登记成功后把 pending 意图解析到唯一远端标识。"""
        ...

    def quarantine_creation(
        self,
        intent: WeaponryCreationIntent,
        *,
        error_code: str,
    ) -> WeaponryCreationIntent:
        """无法证明远端身份时冻结意图，禁止自动再次 create。"""
        ...


class StoreBackedWeaponryResourceRegistrar:
    """用 Resource Store 的版本 CAS 完成线程/进程并发安全登记。"""

    def __init__(
        self,
        store: WeaponryResourceStorePort,
        creation_intents: WeaponryCreationIntentStorePort,
        *,
        max_cas_attempts: int = 8,
    ) -> None:
        if not isinstance(store, WeaponryResourceStorePort):
            raise TypeError("store 必须实现 WeaponryResourceStorePort")
        if not isinstance(creation_intents, WeaponryCreationIntentStorePort):
            raise TypeError("creation_intents 必须实现 WeaponryCreationIntentStorePort")
        if (
            isinstance(max_cas_attempts, bool)
            or not isinstance(max_cas_attempts, int)
            or max_cas_attempts < 1
        ):
            raise ValueError("max_cas_attempts 必须是正整数")
        self._store = store
        self._creation_intents = creation_intents
        self._max_cas_attempts = max_cas_attempts

    def ensure_ready(self, task_id: TaskId) -> None:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        record = self._store.get(task_id)
        if record is None:
            raise WeaponryPortStateError(
                "resource_record_not_found",
                "外部资源创建前找不到任务资源记录",
            )
        if record.state is not WeaponryResourceRecordState.TRACKING:
            raise WeaponryPortStateError(
                "resource_record_not_tracking",
                "外部资源创建前资源记录已离开 tracking 状态",
            )

    def register_created(
        self,
        *,
        task_id: TaskId,
        resource_id: str,
        kind: WeaponryResourceKind,
        external_ref: str,
        ownership: WeaponryResourceOwnership,
        idempotency_key: str,
        document_key: str = "",
        call_id: str = "",
    ) -> WeaponryTrackedResource:
        resource = WeaponryTrackedResource(
            resource_id=resource_id,
            kind=kind,
            external_ref=external_ref,
            ownership=ownership,
            idempotency_key=idempotency_key,
            document_key=document_key,
            call_id=call_id,
        )
        for attempt in range(1, self._max_cas_attempts + 1):
            record = self._store.get(task_id)
            if record is None:
                raise WeaponryPortStateError(
                    "resource_record_not_found",
                    "外部资源创建后找不到任务资源记录",
                )
            try:
                self._store.register(
                    RegisterWeaponryResource(
                        task_id=task_id,
                        resource=resource,
                        expected_version=record.version,
                    )
                )
                logger.info(
                    "武器谱外部资源已登记: task_id=%s kind=%s ownership=%s "
                    "cas_attempt=%d",
                    task_id.value,
                    kind.value,
                    ownership.value,
                    attempt,
                )
                return resource
            except WeaponryPortStateError as exc:
                if exc.error_code != "resource_version_conflict":
                    raise
                logger.debug(
                    "武器谱资源登记发生版本竞争，准备重读: task_id=%s kind=%s "
                    "cas_attempt=%d",
                    task_id.value,
                    kind.value,
                    attempt,
                )
        raise WeaponryPortStateError(
            "resource_registration_cas_exhausted",
            "外部资源登记连续发生版本竞争",
        )

    def reserve_creation(
        self, intent: WeaponryCreationIntent
    ) -> WeaponryCreationIntentReserveResult:
        result = self._creation_intents.reserve(intent)
        if not isinstance(result, WeaponryCreationIntentReserveResult):
            raise TypeError("Creation Intent reserve 返回类型错误")
        logger.info(
            "武器谱外部创建意图已持久化: task_id=%s intent_id=%s kind=%s "
            "created=%s state=%s",
            intent.task_id.value,
            intent.intent_id,
            intent.kind.value,
            result.created,
            result.intent.state.value,
        )
        return result

    def resolve_creation(
        self,
        intent: WeaponryCreationIntent,
        *,
        external_ref: str,
    ) -> WeaponryCreationIntent:
        resolved = self._creation_intents.resolve(
            ResolveWeaponryCreationIntent(
                task_id=intent.task_id,
                intent_id=intent.intent_id,
                expected_version=intent.version,
                external_ref=external_ref,
            )
        )
        logger.info(
            "武器谱外部创建意图已解析: task_id=%s intent_id=%s kind=%s",
            intent.task_id.value,
            intent.intent_id,
            intent.kind.value,
        )
        return resolved

    def quarantine_creation(
        self,
        intent: WeaponryCreationIntent,
        *,
        error_code: str,
    ) -> WeaponryCreationIntent:
        quarantined = self._creation_intents.quarantine(
            QuarantineWeaponryCreationIntent(
                task_id=intent.task_id,
                intent_id=intent.intent_id,
                expected_version=intent.version,
                error_code=error_code,
            )
        )
        logger.critical(
            "武器谱外部创建意图已隔离: task_id=%s intent_id=%s kind=%s "
            "error_code=%s",
            intent.task_id.value,
            intent.intent_id,
            intent.kind.value,
            error_code,
        )
        return quarantined


__all__ = [
    "StoreBackedWeaponryResourceRegistrar",
    "WeaponryCreatedResourceRegistrarProtocol",
]
