"""来源名称映射和 Evidence 完整正文规范化纯规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import WeaponryDomainValidationError


def normalize_source_name(value: object) -> str:
    """提取供应商来源标识中的文件名，不解析其业务含义。"""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise WeaponryDomainValidationError("source_name 必须是 str 或 None")
    normalized = value.replace("\\", "/").strip()
    if not normalized:
        return ""
    marker = "custom-documents/"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    return normalized.rsplit("/", 1)[-1]


def source_lookup_keys(value: object) -> tuple[str, ...]:
    """按稳定顺序生成兼容历史 AnythingLLM 标识的大小写无关键。"""

    if value is None:
        return ()
    if not isinstance(value, str):
        raise WeaponryDomainValidationError("source_name 必须是 str 或 None")
    raw = value.replace("\\", "/").strip()
    if not raw:
        return ()
    candidates = [raw]
    marker = "custom-documents/"
    if marker in raw:
        candidates.append(marker + raw.split(marker, 1)[1])
    normalized = normalize_source_name(raw)
    if normalized:
        candidates.append(normalized)
    basename = raw.rsplit("/", 1)[-1]
    if basename:
        candidates.append(basename)

    seen: set[str] = set()
    keys: list[str] = []
    for candidate in candidates:
        key = candidate.lower()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return tuple(keys)


def is_terms_source_name(value: object) -> bool:
    name = normalize_source_name(value).lower()
    return name.startswith("term_rule_") or (
        name.endswith(".md") and "term_rule_" in name
    )


@dataclass(frozen=True)
class SourceNameMapping:
    """一次 execution 内供应商别名到公开原名/哈希名的不可变映射。"""

    original_names: tuple[tuple[str, str], ...] = ()
    file_names: tuple[tuple[str, str], ...] = ()
    fallback_original_name: str = ""
    fallback_file_name: str = ""

    def __post_init__(self) -> None:
        for mapping_name in ("original_names", "file_names"):
            mapping = getattr(self, mapping_name)
            if not isinstance(mapping, (tuple, list)):
                raise WeaponryDomainValidationError(
                    f"{mapping_name} 必须是有序键值序列"
                )
            normalized: list[tuple[str, str]] = []
            seen: dict[str, str] = {}
            for item in mapping:
                if (
                    not isinstance(item, (tuple, list))
                    or len(item) != 2
                    or not isinstance(item[0], str)
                    or not isinstance(item[1], str)
                    or not item[0]
                    or not item[1]
                ):
                    raise WeaponryDomainValidationError(
                        f"{mapping_name} 只能包含非空 str 键值对"
                    )
                key = item[0].lower()
                if key in seen:
                    if seen[key] != item[1]:
                        raise WeaponryDomainValidationError(
                            f"{mapping_name} 的别名 {key!r} 映射到多个目标"
                        )
                    continue
                seen[key] = item[1]
                normalized.append((key, item[1]))
            object.__setattr__(self, mapping_name, tuple(normalized))
        for name in ("fallback_original_name", "fallback_file_name"):
            if not isinstance(getattr(self, name), str):
                raise WeaponryDomainValidationError(f"{name} 必须是 str")

    @classmethod
    def from_aliases(
        cls,
        *,
        original_aliases: Iterable[tuple[str, str]],
        file_aliases: Iterable[tuple[str, str]],
        fallback_original_name: str = "",
        fallback_file_name: str = "",
    ) -> "SourceNameMapping":
        """展开供应商别名，使后续解析不再依赖可变字典。"""

        def expand(items: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
            expanded: list[tuple[str, str]] = []
            seen: dict[str, str] = {}
            for alias, target in items:
                if not isinstance(alias, str) or not isinstance(target, str):
                    raise WeaponryDomainValidationError("来源别名和目标名必须是 str")
                if not target:
                    continue
                for key in source_lookup_keys(alias):
                    if key in seen:
                        if seen[key] != target:
                            raise WeaponryDomainValidationError(
                                f"来源别名 {key!r} 映射到多个目标"
                            )
                        continue
                    seen[key] = target
                    expanded.append((key, target))
            return tuple(expanded)

        return cls(
            original_names=expand(original_aliases),
            file_names=expand(file_aliases),
            fallback_original_name=fallback_original_name,
            fallback_file_name=fallback_file_name,
        )

    @staticmethod
    def _resolve(
        source_name: str,
        mapping: tuple[tuple[str, str], ...],
        fallback: str,
    ) -> str:
        normalized = normalize_source_name(source_name)
        if is_terms_source_name(normalized):
            return normalized
        if not source_name:
            return fallback
        lookup = dict(mapping)
        for key in source_lookup_keys(source_name):
            if key in lookup:
                return lookup[key]
        return normalized or source_name

    def resolve_original_name(self, source_name: str) -> str:
        return self._resolve(
            source_name,
            self.original_names,
            self.fallback_original_name,
        )

    def resolve_file_name(self, source_name: str) -> str:
        return self._resolve(
            source_name,
            self.file_names,
            self.fallback_file_name,
        )


def normalize_evidence_rows(rows: Iterable[str]) -> tuple[str, ...]:
    """保序清理 Evidence 行，但绝不截断正文或限制数量。

    该函数只移除空白行并去除每行首尾空白。候选召回批次由 Retrieval Port 控制；一旦
    Evidence 通过选择门禁，领域层必须把完整文本投影到 Prompt 和公开 ``rows``。
    """

    normalized: list[str] = []
    for row in rows:
        if not isinstance(row, str):
            raise WeaponryDomainValidationError("rows 只能包含 str")
        text = row.strip()
        if text:
            normalized.append(text)
    return tuple(normalized)


__all__ = [
    "SourceNameMapping",
    "is_terms_source_name",
    "normalize_evidence_rows",
    "normalize_source_name",
    "source_lookup_keys",
]
