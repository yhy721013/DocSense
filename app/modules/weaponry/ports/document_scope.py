"""武器谱受理阶段的文档范围解析端口。

Port 只表达“以当前本地权威记录冻结范围”的业务能力，不暴露 SQLite 行、DatabaseService
方法或 AnythingLLM 响应。实现必须在一次逻辑只读快照中完成解析，且不得上传、绑定或修改
文档；生产 Repository 应使用文件名或 architectureId 索引过滤，而不是全表扫描。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.weaponry.domain import WeaponryDocumentScope


class WeaponryDocumentScopeError(ValueError):
    """请求指定的文档范围无法形成确定快照。"""


class WeaponryDocumentScopeNotFoundError(WeaponryDocumentScopeError):
    """显式文件尚未进入本地知识库映射。"""


class WeaponryDocumentScopeAmbiguityError(WeaponryDocumentScopeError):
    """显式文件名或外部文档引用不能唯一归属。"""


class WeaponryDocumentScopeIntegrityError(RuntimeError):
    """本地权威文档记录损坏；该错误不得伪装为成功受理。"""


@runtime_checkable
class WeaponryDocumentScopePort(Protocol):
    """在原子任务写入前解析并冻结一次文档范围。"""

    def resolve(
        self,
        *,
        architecture_id: int,
        requested_file_names: tuple[str, ...],
    ) -> WeaponryDocumentScope:
        ...


__all__ = [
    "WeaponryDocumentScopeAmbiguityError",
    "WeaponryDocumentScopeError",
    "WeaponryDocumentScopeIntegrityError",
    "WeaponryDocumentScopeNotFoundError",
    "WeaponryDocumentScopePort",
]
