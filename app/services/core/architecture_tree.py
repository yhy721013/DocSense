from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


MAX_SIGNED_INT64 = (1 << 63) - 1
MAX_ARCHITECTURE_NODE_COUNT = 10_000
MAX_ARCHITECTURE_DEPTH = 128
MAX_ARCHITECTURE_NAME_CHARS = 256
MAX_ARCHITECTURE_PATH_CHARS = 2_048
MAX_ARCHITECTURE_PATH_NAME_CHARS = 2_048
MAX_ARCHITECTURE_REMARK_CHARS = 4_096
MAX_ARCHITECTURE_TOTAL_TEXT_CHARS = 2_000_000
MAX_ARCHITECTURE_SERIALIZED_CHARS = 4_000_000

_DETAIL_SUFFIXES = frozenset(
    {
        "基础数据",
        "战技指标",
        "运用数据",
        "效能数据",
        "模型数据",
        "目特数据",
        "声像数据",
    }
)
_DETAIL_SEPARATORS = ("-", "－", "—", "–", "﹣")
_ALIAS_SEPARATOR_RE = re.compile(r"[\s\-－—–﹣]+")
_ASCII_UNSIGNED_INTEGER_RE = re.compile(r"[0-9]+\Z")


class ArchitectureTreeValidationError(ValueError):
    """领域树无法建立可靠索引时抛出的稳定合同异常。"""


class _SiblingMapping(Mapping[int, tuple[int, ...]]):
    """按访问惰性生成同胞列表，避免宽树为每个节点复制整个同胞族。"""

    def __init__(
        self,
        families_by_node_id: Mapping[int, tuple[int, ...]],
    ) -> None:
        self._families_by_node_id = MappingProxyType(
            dict(families_by_node_id)
        )

    def __getitem__(self, node_id: int) -> tuple[int, ...]:
        family = self._families_by_node_id[node_id]
        return tuple(member_id for member_id in family if member_id != node_id)

    def __iter__(self):
        return iter(self._families_by_node_id)

    def __len__(self) -> int:
        return len(self._families_by_node_id)


@dataclass(frozen=True, slots=True)
class ArchitectureNodeProfile:
    """规范化后的单个领域节点。

    ``parent_id`` 保留请求中的父节点 ID；当父节点没有出现在本次有限树中时，
    ``root_id`` 会指向当前可见边界的根，但不会篡改 ``parent_id``。
    """

    id: int
    parent_id: int | None
    name: str
    semantic_path: str
    source_path: str
    remark: str
    ordinal: int
    root_id: int
    depth: int
    is_leaf: bool
    aliases: tuple[str, ...]

    @property
    def path_name(self) -> str:
        """为请求字段 ``pathName`` 提供语义明确的只读别名。"""

        return self.semantic_path


@dataclass(frozen=True, slots=True)
class ArchitectureTreeIndex:
    """一棵请求有限树的不可变索引。"""

    fingerprint: str
    nodes: tuple[ArchitectureNodeProfile, ...]
    nodes_by_id: Mapping[int, ArchitectureNodeProfile]
    root_ids: tuple[int, ...]
    leaf_ids: tuple[int, ...]
    children_by_id: Mapping[int, tuple[int, ...]]
    ancestors_by_id: Mapping[int, tuple[int, ...]]
    leaf_descendants_by_id: Mapping[int, tuple[int, ...]]
    siblings_by_id: Mapping[int, tuple[int, ...]]
    alias_to_ids: Mapping[str, tuple[int, ...]]

    def get(self, node_id: int) -> ArchitectureNodeProfile | None:
        return self.nodes_by_id.get(node_id)

    def require(self, node_id: int) -> ArchitectureNodeProfile:
        try:
            return self.nodes_by_id[node_id]
        except KeyError as exc:
            raise ArchitectureTreeValidationError(
                f"领域树不包含节点 id: {node_id}"
            ) from exc

    @property
    def leaf_count(self) -> int:
        return len(self.leaf_ids)


@dataclass(frozen=True, slots=True)
class _RawArchitectureNode:
    id: int
    parent_id: int | None
    name: str
    source_path: str
    path_name: str
    remark: str
    ordinal: int


def _snapshot_nodes(nodes: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    if isinstance(nodes, (str, bytes)) or isinstance(nodes, Mapping):
        raise ArchitectureTreeValidationError("architectureList 必须是节点数组")

    try:
        iterator = iter(nodes)
    except TypeError as exc:
        raise ArchitectureTreeValidationError("architectureList 必须是节点数组") from exc

    snapshot: list[dict[str, Any]] = []
    for ordinal, item in enumerate(iterator):
        if ordinal >= MAX_ARCHITECTURE_NODE_COUNT:
            raise ArchitectureTreeValidationError(
                "architectureList 节点数不能超过 "
                f"{MAX_ARCHITECTURE_NODE_COUNT}"
            )
        if not isinstance(item, Mapping):
            raise ArchitectureTreeValidationError(
                f"architectureList[{ordinal}] 必须是对象"
            )
        snapshot.append(dict(item))
    if not snapshot:
        raise ArchitectureTreeValidationError("architectureList 不能为空")
    try:
        serialized = json.dumps(
            snapshot,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        # ``ensure_ascii=False`` 会把孤立 UTF-16 surrogate 保留在 Python
        # 字符串中；显式编码可在进入 fingerprint 或 SQLite 前稳定拒绝它。
        serialized.encode("utf-8")
    except (
        TypeError,
        ValueError,
        RecursionError,
        UnicodeEncodeError,
    ) as exc:
        raise ArchitectureTreeValidationError(
            "architectureList 必须是 JSON 可序列化节点数组"
        ) from exc
    if len(serialized) > MAX_ARCHITECTURE_SERIALIZED_CHARS:
        raise ArchitectureTreeValidationError(
            "architectureList 紧凑 JSON 长度不能超过 "
            f"{MAX_ARCHITECTURE_SERIALIZED_CHARS} 个字符"
        )
    return tuple(snapshot)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _normalize_bounded_text(
    value: Any,
    *,
    field: str,
    ordinal: int,
    max_chars: int,
) -> str:
    """规范化单个文本字段，并在进入指纹和索引前执行稳定长度门禁。"""
    normalized = _normalize_text(value)
    if len(normalized) > max_chars:
        raise ArchitectureTreeValidationError(
            f"architectureList[{ordinal}].{field} 长度不能超过 "
            f"{max_chars} 个字符"
        )
    return normalized


def _normalize_integer(
    value: Any,
    *,
    field: str,
    ordinal: int,
    allow_zero: bool,
) -> int:
    if isinstance(value, bool):
        raise ArchitectureTreeValidationError(
            f"architectureList[{ordinal}].{field} 必须是 64 位整数"
        )

    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str):
        stripped = value.strip()
        if (
            len(stripped) > len(str(MAX_SIGNED_INT64))
            or _ASCII_UNSIGNED_INTEGER_RE.fullmatch(stripped) is None
        ):
            raise ArchitectureTreeValidationError(
                f"architectureList[{ordinal}].{field} 必须是 64 位整数"
            )
        try:
            normalized = int(stripped, 10)
        except ValueError as exc:
            raise ArchitectureTreeValidationError(
                f"architectureList[{ordinal}].{field} 必须是 64 位整数"
            ) from exc
    else:
        raise ArchitectureTreeValidationError(
            f"architectureList[{ordinal}].{field} 必须是 64 位整数"
        )

    minimum = 0 if allow_zero else 1
    if normalized < minimum or normalized > MAX_SIGNED_INT64:
        range_description = "0 到 64 位有符号整数上限" if allow_zero else "正 64 位有符号整数"
        raise ArchitectureTreeValidationError(
            f"architectureList[{ordinal}].{field} 必须是{range_description}"
        )
    return normalized


def _normalize_parent_id(value: Any, *, ordinal: int) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    normalized = _normalize_integer(
        value,
        field="parentId",
        ordinal=ordinal,
        allow_zero=True,
    )
    return None if normalized == 0 else normalized


def _normalize_raw_nodes(
    snapshot: tuple[dict[str, Any], ...],
) -> tuple[_RawArchitectureNode, ...]:
    normalized_nodes: list[_RawArchitectureNode] = []
    seen_ids: set[int] = set()
    total_text_chars = 0

    for ordinal, item in enumerate(snapshot):
        node_id = _normalize_integer(
            item.get("id"),
            field="id",
            ordinal=ordinal,
            allow_zero=False,
        )
        if node_id in seen_ids:
            raise ArchitectureTreeValidationError(
                f"architectureList 包含重复 id: {node_id}"
            )
        seen_ids.add(node_id)

        name = _normalize_bounded_text(
            item.get("name"),
            field="name",
            ordinal=ordinal,
            max_chars=MAX_ARCHITECTURE_NAME_CHARS,
        )
        if not name:
            raise ArchitectureTreeValidationError(
                f"architectureList[{ordinal}].name 不能为空"
            )
        source_path = _normalize_bounded_text(
            item.get("path"),
            field="path",
            ordinal=ordinal,
            max_chars=MAX_ARCHITECTURE_PATH_CHARS,
        )
        path_name = _normalize_bounded_text(
            item.get("pathName"),
            field="pathName",
            ordinal=ordinal,
            max_chars=MAX_ARCHITECTURE_PATH_NAME_CHARS,
        )
        remark = _normalize_bounded_text(
            item.get("remark"),
            field="remark",
            ordinal=ordinal,
            max_chars=MAX_ARCHITECTURE_REMARK_CHARS,
        )
        total_text_chars += sum(
            len(value)
            for value in (name, source_path, path_name, remark)
        )
        if total_text_chars > MAX_ARCHITECTURE_TOTAL_TEXT_CHARS:
            raise ArchitectureTreeValidationError(
                "architectureList 文本字段累计长度不能超过 "
                f"{MAX_ARCHITECTURE_TOTAL_TEXT_CHARS} 个字符"
            )

        normalized_nodes.append(
            _RawArchitectureNode(
                id=node_id,
                parent_id=_normalize_parent_id(item.get("parentId"), ordinal=ordinal),
                name=name,
                source_path=source_path,
                path_name=path_name,
                remark=remark,
                ordinal=ordinal,
            )
        )

    return tuple(normalized_nodes)


def _fingerprint_normalized(nodes: tuple[_RawArchitectureNode, ...]) -> str:
    payload = [
        [
            node.id,
            node.parent_id,
            node.name,
            node.path_name,
            node.remark,
        ]
        for node in nodes
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def architecture_tree_fingerprint(nodes: Iterable[Mapping[str, Any]]) -> str:
    """按规范节点和请求顺序计算稳定 SHA-256 指纹。"""

    snapshot = _snapshot_nodes(nodes)
    return _fingerprint_normalized(_normalize_raw_nodes(snapshot))


def _validate_acyclic(
    raw_by_id: Mapping[int, _RawArchitectureNode],
    ordered_ids: tuple[int, ...],
) -> None:
    """迭代检查单父链环，避免畸形深树先触发 Python ``RecursionError``。"""
    resolved: set[int] = set()
    for start_id in ordered_ids:
        if start_id in resolved:
            continue
        chain: list[int] = []
        positions: dict[int, int] = {}
        node_id = start_id
        while node_id in raw_by_id and node_id not in resolved:
            cycle_start = positions.get(node_id)
            if cycle_start is not None:
                cycle = chain[cycle_start:] + [node_id]
                cycle_text = " -> ".join(str(value) for value in cycle)
                raise ArchitectureTreeValidationError(
                    f"领域树父链存在环: {cycle_text}"
                )
            positions[node_id] = len(chain)
            chain.append(node_id)
            parent_id = raw_by_id[node_id].parent_id
            if parent_id not in raw_by_id:
                break
            node_id = parent_id
        resolved.update(chain)


def _detail_path_segment(name: str, parent_name: str) -> str:
    """压缩“父装备名-明细类型”，避免重建路径重复装备名称。"""

    for separator in _DETAIL_SEPARATORS:
        prefix = f"{parent_name}{separator}"
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :].strip()
        if suffix in _DETAIL_SUFFIXES:
            return suffix
    return name


def _normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return _ALIAS_SEPARATOR_RE.sub("", normalized)


def _build_aliases(name: str, semantic_path: str) -> tuple[str, ...]:
    aliases: list[str] = []
    for value in (name, semantic_path):
        normalized = _normalize_alias(value)
        if normalized and normalized not in aliases:
            aliases.append(normalized)

        compact_identifier = re.sub(r"(?<=\w)[/_.](?=\w)", "", normalized)
        if compact_identifier and compact_identifier not in aliases:
            aliases.append(compact_identifier)

    return tuple(aliases)


def _build_index_from_normalized(
    raw_nodes: tuple[_RawArchitectureNode, ...],
) -> ArchitectureTreeIndex:
    ordered_ids = tuple(node.id for node in raw_nodes)
    raw_by_id = {node.id: node for node in raw_nodes}
    _validate_acyclic(raw_by_id, ordered_ids)

    children: dict[int, list[int]] = {node_id: [] for node_id in ordered_ids}
    root_ids: list[int] = []
    for node in raw_nodes:
        if node.parent_id in raw_by_id:
            children[node.parent_id].append(node.id)
        else:
            # 父节点未随有限候选树传入时，当前节点就是可见边界的根。
            root_ids.append(node.id)

    ancestors: dict[int, tuple[int, ...]] = {}
    semantic_paths: dict[int, str] = {}
    structural_paths: dict[int, str] = {}
    root_by_id: dict[int, int] = {}
    topology_order: list[int] = []
    pending = list(reversed(root_ids))
    while pending:
        node_id = pending.pop()
        node = raw_by_id[node_id]
        parent = raw_by_id.get(node.parent_id)
        if parent is None:
            ancestor_ids: tuple[int, ...] = ()
            root_id = node_id
            reconstructed_path = str(node_id)
            reconstructed_semantic_path = node.name
        else:
            ancestor_ids = ancestors[parent.id] + (parent.id,)
            root_id = root_by_id[parent.id]
            reconstructed_path = f"{structural_paths[parent.id]}/{node_id}"
            segment = _detail_path_segment(node.name, parent.name)
            reconstructed_semantic_path = f"{semantic_paths[parent.id]}/{segment}"

        depth = len(ancestor_ids) + 1
        if depth > MAX_ARCHITECTURE_DEPTH:
            raise ArchitectureTreeValidationError(
                f"领域树可见深度不能超过 {MAX_ARCHITECTURE_DEPTH}"
            )
        resolved_structural_path = node.source_path or reconstructed_path
        resolved_semantic_path = node.path_name or reconstructed_semantic_path
        if len(resolved_structural_path) > MAX_ARCHITECTURE_PATH_CHARS:
            raise ArchitectureTreeValidationError(
                "领域树重建 path 长度不能超过 "
                f"{MAX_ARCHITECTURE_PATH_CHARS} 个字符"
            )
        if len(resolved_semantic_path) > MAX_ARCHITECTURE_PATH_NAME_CHARS:
            raise ArchitectureTreeValidationError(
                "领域树重建 pathName 长度不能超过 "
                f"{MAX_ARCHITECTURE_PATH_NAME_CHARS} 个字符"
            )
        ancestors[node_id] = ancestor_ids
        root_by_id[node_id] = root_id
        structural_paths[node_id] = resolved_structural_path
        # 非空 pathName 是调用方提供的不透明语义字符串，绝不按“/”反推拓扑。
        semantic_paths[node_id] = resolved_semantic_path
        topology_order.append(node_id)
        pending.extend(reversed(children[node_id]))

    leaf_descendants: dict[int, tuple[int, ...]] = {}
    for node_id in reversed(topology_order):
        child_ids = children[node_id]
        if not child_ids:
            result = (node_id,)
        else:
            result = tuple(
                leaf_id
                for child_id in child_ids
                for leaf_id in leaf_descendants[child_id]
            )
        leaf_descendants[node_id] = result

    root_tuple = tuple(root_ids)
    frozen_children = {
        node_id: tuple(child_ids)
        for node_id, child_ids in children.items()
    }
    families_by_node_id: dict[int, tuple[int, ...]] = {}
    for node in raw_nodes:
        family = (
            frozen_children[node.parent_id]
            if node.parent_id in raw_by_id
            else root_tuple
        )
        families_by_node_id[node.id] = family

    profiles: list[ArchitectureNodeProfile] = []
    for raw_node in raw_nodes:
        profile = ArchitectureNodeProfile(
            id=raw_node.id,
            parent_id=raw_node.parent_id,
            name=raw_node.name,
            semantic_path=semantic_paths[raw_node.id],
            source_path=structural_paths[raw_node.id],
            remark=raw_node.remark,
            ordinal=raw_node.ordinal,
            root_id=root_by_id[raw_node.id],
            depth=len(ancestors[raw_node.id]) + 1,
            is_leaf=not children[raw_node.id],
            aliases=_build_aliases(raw_node.name, semantic_paths[raw_node.id]),
        )
        profiles.append(profile)

    profiles_tuple = tuple(profiles)
    profile_by_id = {profile.id: profile for profile in profiles_tuple}
    alias_index: defaultdict[str, list[int]] = defaultdict(list)
    for profile in profiles_tuple:
        for alias in profile.aliases:
            alias_index[alias].append(profile.id)

    return ArchitectureTreeIndex(
        fingerprint=_fingerprint_normalized(raw_nodes),
        nodes=profiles_tuple,
        nodes_by_id=MappingProxyType(profile_by_id),
        root_ids=root_tuple,
        leaf_ids=tuple(profile.id for profile in profiles_tuple if profile.is_leaf),
        children_by_id=MappingProxyType(frozen_children),
        ancestors_by_id=MappingProxyType(dict(ancestors)),
        leaf_descendants_by_id=MappingProxyType(dict(leaf_descendants)),
        siblings_by_id=_SiblingMapping(families_by_node_id),
        alias_to_ids=MappingProxyType(
            {alias: tuple(node_ids) for alias, node_ids in alias_index.items()}
        ),
    )


def build_architecture_tree_index(
    nodes: Iterable[Mapping[str, Any]],
) -> ArchitectureTreeIndex:
    """校验、规范化并建立一棵请求有限树的不可变索引。"""

    snapshot = _snapshot_nodes(nodes)
    return _build_index_from_normalized(_normalize_raw_nodes(snapshot))


ArchitectureTreeBuilder = Callable[
    [Iterable[Mapping[str, Any]]],
    ArchitectureTreeIndex,
]


class ArchitectureTreeIndexCache:
    """线程安全 LRU，并对相同 fingerprint 的冷构建执行 single-flight。"""

    def __init__(
        self,
        capacity: int = 4,
        *,
        builder: ArchitectureTreeBuilder | None = None,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("领域树索引缓存容量必须是正整数")
        self.capacity = capacity
        self._builder = builder or build_architecture_tree_index
        self._cache: OrderedDict[str, ArchitectureTreeIndex] = OrderedDict()
        self._inflight: dict[str, Future[ArchitectureTreeIndex]] = {}
        self._lock = threading.Lock()

    def get_or_build(
        self,
        nodes: Iterable[Mapping[str, Any]],
    ) -> ArchitectureTreeIndex:
        snapshot = _snapshot_nodes(nodes)
        fingerprint = _fingerprint_normalized(_normalize_raw_nodes(snapshot))

        with self._lock:
            cached = self._cache.get(fingerprint)
            if cached is not None:
                self._cache.move_to_end(fingerprint)
                return cached

            future = self._inflight.get(fingerprint)
            is_builder = future is None
            if future is None:
                future = Future()
                self._inflight[fingerprint] = future

        if not is_builder:
            result = future.result()
            with self._lock:
                if fingerprint in self._cache:
                    self._cache.move_to_end(fingerprint)
            return result

        try:
            result = self._builder(snapshot)
            if not isinstance(result, ArchitectureTreeIndex):
                raise TypeError("领域树索引构建器必须返回 ArchitectureTreeIndex")
            if result.fingerprint != fingerprint:
                raise ArchitectureTreeValidationError("领域树索引构建结果 fingerprint 不一致")
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(fingerprint, None)
            future.set_exception(exc)
            raise

        with self._lock:
            self._cache[fingerprint] = result
            self._cache.move_to_end(fingerprint)
            while len(self._cache) > self.capacity:
                self._cache.popitem(last=False)
            self._inflight.pop(fingerprint, None)
        future.set_result(result)
        return result

    def clear(self) -> None:
        """清除已完成缓存；正在构建的 single-flight 不受影响。"""

        with self._lock:
            self._cache.clear()

    @property
    def cached_fingerprints(self) -> tuple[str, ...]:
        """按 LRU 从旧到新返回已完成索引指纹。"""

        with self._lock:
            return tuple(self._cache)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)
