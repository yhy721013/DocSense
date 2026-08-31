"""用于持久化文件对话运行归属的锁服务。"""

from app.modules.chat.adapters.sqlite.locking.lock_service import (
    DEFAULT_CHAT_ADMISSION_SECONDS,
    DEFAULT_STALE_RUN_SECONDS,
    ChatAdmissionBusyError,
    ChatRunBusyError,
    ChatRunInactiveError,
    ChatRunLockService,
)
from app.modules.chat.ports.coordination import (
    ChatAdmissionLease,
    ChatRunCoordinator,
    ChatRunLease,
    ChatRunLeaseCapabilities,
    ChatRunLeaseLostError,
    SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES,
)

__all__ = [
    "ChatAdmissionBusyError",
    "ChatAdmissionLease",
    "ChatRunBusyError",
    "ChatRunCoordinator",
    "ChatRunInactiveError",
    "ChatRunLease",
    "ChatRunLeaseCapabilities",
    "ChatRunLeaseLostError",
    "ChatRunLockService",
    "DEFAULT_CHAT_ADMISSION_SECONDS",
    "DEFAULT_STALE_RUN_SECONDS",
    "SINGLE_INSTANCE_CHAT_RUN_LEASE_CAPABILITIES",
]
