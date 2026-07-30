"""武器谱提取策略的单一领域口径。"""

from __future__ import annotations

from .errors import DeprecatedWeaponryModeError, WeaponryDomainValidationError


FILE_AGGREGATE_STRATEGY = "file-aggregate-v1"


def resolve_legacy_extraction_strategy(raw_mode: object | None) -> str:
    """把迁移期旧配置收敛到唯一文件聚合策略。

    缺省、空值和显式 ``2`` 都表示当前唯一合法策略。显式 ``1`` 不能静默回退，
    否则运维会误以为仍在使用按 Chunk 回调语义。其他值同样属于配置错误，防止拼写
    错误在生产中悄悄改变行为。
    """

    if raw_mode is None:
        return FILE_AGGREGATE_STRATEGY
    if not isinstance(raw_mode, str):
        raise WeaponryDomainValidationError("WEAPONRY_ANALYSE_MODE 必须是 str")
    normalized = raw_mode.strip()
    if normalized in {"", "2"}:
        return FILE_AGGREGATE_STRATEGY
    if normalized == "1":
        raise DeprecatedWeaponryModeError(
            "WEAPONRY_ANALYSE_MODE=1 已废弃，只允许按文件聚合模式 2"
        )
    raise WeaponryDomainValidationError(
        f"WEAPONRY_ANALYSE_MODE={normalized!r} 无效，只允许模式 2"
    )


__all__ = ["FILE_AGGREGATE_STRATEGY", "resolve_legacy_extraction_strategy"]
