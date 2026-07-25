"""武器谱文档范围 Port 的严格、零 I/O 测试替身。"""

from __future__ import annotations

from threading import RLock

from app.modules.weaponry.domain import WeaponryDocumentScope

from .weaponry_control import (
    FakeWeaponryCallbackPort,
    FakeWeaponryDispatcherPort,
    FakeWeaponryExternalResourceCleanupPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryTaskCommandPort,
)
from .weaponry_processing import (
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryTranslationPort,
)
from .weaponry_support import WeaponryInvocation, WeaponryInvocationRecorder


class FakeWeaponryDocumentScopePort:
    """只返回测试显式配置的范围；未配置调用直接失败，避免 Fake 掩盖编排错误。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self.scopes: dict[tuple[int, tuple[str, ...]], WeaponryDocumentScope] = {}
        self.errors: dict[tuple[int, tuple[str, ...]], BaseException] = {}
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def resolve(
        self,
        *,
        architecture_id: int,
        requested_file_names: tuple[str, ...],
    ) -> WeaponryDocumentScope:
        if isinstance(architecture_id, bool) or not isinstance(architecture_id, int):
            raise TypeError("architecture_id 必须是 int")
        if not isinstance(requested_file_names, tuple) or any(
            not isinstance(item, str) for item in requested_file_names
        ):
            raise TypeError("requested_file_names 必须是字符串 tuple")
        key = (architecture_id, requested_file_names)
        with self._lock:
            self.calls.append(key)
            error = self.errors.get(key)
            if error is not None:
                raise error
            scope = self.scopes.get(key)
            if scope is None:
                raise AssertionError(
                    "FakeWeaponryDocumentScopePort 收到未配置调用: "
                    f"architecture_id={architecture_id} file_count={len(requested_file_names)}"
                )
            if scope.requested_file_names != requested_file_names:
                raise AssertionError("Fake 返回范围与请求文件名不一致")
            return scope


__all__ = [
    "FakeAuxiliaryGuidancePort",
    "FakeEvidenceExtractionPort",
    "FakeTargetEvidenceRetrievalPort",
    "FakeWeaponryCallbackPort",
    "FakeWeaponryDispatcherPort",
    "FakeWeaponryDocumentScopePort",
    "FakeWeaponryExternalResourceCleanupPort",
    "FakeWeaponryInteractionAuditPort",
    "FakeWeaponryProgressPublisherPort",
    "FakeWeaponryResourceStorePort",
    "FakeWeaponryTaskCommandPort",
    "FakeWeaponryTranslationPort",
    "WeaponryInvocation",
    "WeaponryInvocationRecorder",
]
