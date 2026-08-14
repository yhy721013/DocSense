"""Weaponry 终态 Callback 结果快照 Port。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskId, require_persisted_utc
from app.modules.weaponry.domain import WeaponryCallbackPayload


@dataclass(frozen=True, slots=True)
class WeaponryResultSnapshot:
    task_id: TaskId
    business_ref: TaskBusinessRef
    payload: WeaponryCallbackPayload
    result_digest: str
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if (
            not isinstance(self.business_ref, TaskBusinessRef)
            or self.business_ref.business_type != "weaponry"
        ):
            raise TypeError("business_ref 必须是 Weaponry TaskBusinessRef")
        if not isinstance(self.payload, WeaponryCallbackPayload):
            raise TypeError("payload 必须是 WeaponryCallbackPayload")
        if str(self.payload.architecture_id) != self.business_ref.business_key:
            raise ValueError("payload 与 business_ref 身份不一致")
        if (
            not isinstance(self.result_digest, str)
            or len(self.result_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.result_digest)
        ):
            raise ValueError("result_digest 必须是 64 位小写 SHA-256")
        object.__setattr__(
            self,
            "created_at",
            require_persisted_utc(self.created_at, name="created_at"),
        )


@runtime_checkable
class WeaponryResultSnapshotStorePort(Protocol):
    def save(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        payload: WeaponryCallbackPayload,
        created_at: str,
    ) -> WeaponryResultSnapshot: ...

    def get(self, task_id: TaskId) -> WeaponryResultSnapshot | None: ...


__all__ = ["WeaponryResultSnapshot", "WeaponryResultSnapshotStorePort"]
