"""基于现有 ``DatabaseService`` 的武器谱文档范围只读适配器。"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging

from app.integrations.anythingllm.models import normalize_document_location_key
from app.modules.weaponry.domain import (
    DOCUMENT_SCOPE_CATEGORY,
    DOCUMENT_SCOPE_EXPLICIT,
    MAX_ARCHITECTURE_ID,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
)
from app.modules.weaponry.ports import (
    WeaponryDocumentScopeAmbiguityError,
    WeaponryDocumentScopeError,
    WeaponryDocumentScopeIntegrityError,
    WeaponryDocumentScopeNotFoundError,
)
from app.services.core.database import DatabaseService


logger = logging.getLogger(__name__)


def _normalized_architecture_id(value: object, *, file_name: str) -> int:
    # ``documents.architecture_id`` 是 SQLite INTEGER 权威列。浮点数、数字字符串和布尔值
    # 均代表 Repository/测试替身破坏了返回契约，不能通过 int() 静默截断或转换。
    if isinstance(value, bool) or not isinstance(value, int):
        raise WeaponryDocumentScopeError(
            f"文件 {file_name} 的知识库分类记录无效"
        )
    normalized = value
    if normalized < 1 or normalized > MAX_ARCHITECTURE_ID:
        raise WeaponryDocumentScopeError(
            f"文件 {file_name} 的知识库分类记录无效"
        )
    return normalized


def _record_external_ref(record: Mapping[str, object], *, file_name: str) -> str:
    """读取不透明外部位置；兼容只有 AnythingLLM 文档 ID 的历史有效行。"""

    doc_path = str(record.get("doc_path") or "").strip()
    if doc_path:
        return doc_path.replace("\\", "/")
    anything_document_id = str(record.get("anything_doc_id") or "").strip()
    if anything_document_id:
        return f"custom-documents/{anything_document_id}.json"
    raise WeaponryDocumentScopeError(
        f"文件 {file_name} 缺少知识库文档位置"
    )


def _ingested_file_name(record: Mapping[str, object], *, file_name: str) -> str:
    """读取真实入库文件名，不从业务名或文档位置做不可靠反推。"""

    ingested = (
        str(record.get("ingested_file_name") or "")
        .replace("\\", "/")
        .rsplit("/", 1)[-1]
        .strip()
    )
    if not ingested or ingested in {".", ".."}:
        # 这是本地权威记录损坏，不属于调用方参数错误。未来公开路由必须让受理失败，
        # 但不能新增未经确认的 HTTP 400 文本，也不能用 doc_path 猜测转换后文件名。
        raise WeaponryDocumentScopeIntegrityError(
            f"知识库文档记录缺少有效 ingested_file_name: file_name={file_name}"
        )
    return ingested


def _document_key(
    *,
    sequence_no: int,
    file_name: str,
    source_architecture_id: int,
    external_document_ref: str,
    anything_document_id: str,
) -> str:
    """生成 execution 内确定、不可由展示名碰撞的文档键。"""

    identity = "\x1f".join(
        (
            str(sequence_no),
            file_name,
            str(source_architecture_id),
            external_document_ref,
            anything_document_id,
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"document-{sequence_no:04d}-{digest}"


class DatabaseServiceWeaponryDocumentScopeAdapter:
    """以一次本地 SQLite 只读查询冻结 explicit/category 文档范围。

    Adapter 不调用 AnythingLLM、不下载文件、不修改永久 workspace，也不写旧的
    ``weaponry_task_document_snapshots``。返回对象随后由任务 Codec 作为 execution 输入的一部分
    写入 ``llm_task_executions``。
    """

    def __init__(self, database: DatabaseService) -> None:
        if not isinstance(database, DatabaseService):
            raise TypeError("database 必须是 DatabaseService")
        self._database = database

    def resolve(
        self,
        *,
        architecture_id: int,
        requested_file_names: tuple[str, ...],
    ) -> WeaponryDocumentScope:
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or architecture_id < 1
            or architecture_id > MAX_ARCHITECTURE_ID
        ):
            raise ValueError("architecture_id 必须是合法正整数")
        if not isinstance(requested_file_names, tuple):
            raise TypeError("requested_file_names 必须是 tuple")
        normalized_names: list[str] = []
        seen_requested: set[str] = set()
        for item in requested_file_names:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("requested_file_names 只能包含非空字符串")
            normalized = item.strip()
            key = normalized.casefold()
            if key in seen_requested:
                raise ValueError("requested_file_names 不能包含重复文件名")
            seen_requested.add(key)
            normalized_names.append(normalized)

        records = self._database.list_document_records()
        if not isinstance(records, list) or any(
            not isinstance(item, Mapping) for item in records
        ):
            raise WeaponryDocumentScopeIntegrityError(
                "文档记录查询必须返回 Mapping 列表"
            )

        if normalized_names:
            mode = DOCUMENT_SCOPE_EXPLICIT
            selected_records = self._resolve_explicit_records(
                records,
                tuple(normalized_names),
            )
        else:
            mode = DOCUMENT_SCOPE_CATEGORY
            selected_records = self._resolve_category_records(
                records,
                architecture_id,
            )

        documents: list[WeaponryDocumentSnapshot] = []
        external_ref_owners: dict[str, str] = {}
        for sequence_no, record in enumerate(selected_records, start=1):
            file_name = str(record.get("file_name") or "").strip()
            source_architecture_id = _normalized_architecture_id(
                record.get("architecture_id"),
                file_name=file_name,
            )
            external_document_ref = _record_external_ref(
                record,
                file_name=file_name,
            )
            # 受理和 Retrieval 必须使用同一完整位置身份。仅比较原始字符串会让 NFKC、
            # URL 编码、重复斜杠等不同写法在受理时通过，执行绑定时才折叠为同一文档并
            # 异步失败。这里只规范身份键，快照仍保留原始不透明 ref 供供应商调用。
            external_identity = normalize_document_location_key(
                external_document_ref
            )
            if not external_identity:
                raise WeaponryDocumentScopeIntegrityError(
                    f"文件 {file_name} 的知识库文档位置无法形成稳定身份"
                )
            existing_owner = external_ref_owners.get(external_identity)
            if existing_owner is not None:
                if mode == DOCUMENT_SCOPE_EXPLICIT:
                    raise WeaponryDocumentScopeAmbiguityError(
                        "选中文件指向同一知识库文档位置，无法唯一溯源"
                    )
                raise WeaponryDocumentScopeIntegrityError(
                    "类别文档记录包含重复外部文档位置"
                )
            external_ref_owners[external_identity] = file_name
            anything_document_id = str(
                record.get("anything_doc_id") or ""
            ).strip()
            original_name_value = str(record.get("original_name") or "")
            original_name = (
                original_name_value
                if original_name_value.strip()
                else file_name
            )
            documents.append(
                WeaponryDocumentSnapshot(
                    sequence_no=sequence_no,
                    document_key=_document_key(
                        sequence_no=sequence_no,
                        file_name=file_name,
                        source_architecture_id=source_architecture_id,
                        external_document_ref=external_document_ref,
                        anything_document_id=anything_document_id,
                    ),
                    file_name=file_name,
                    original_name=original_name,
                    ingested_file_name=_ingested_file_name(
                        record,
                        file_name=file_name,
                    ),
                    source_architecture_id=source_architecture_id,
                    external_document_ref=external_document_ref,
                    anything_document_id=anything_document_id,
                )
            )

        scope = WeaponryDocumentScope(
            mode=mode,
            requested_file_names=(
                tuple(normalized_names)
                if mode == DOCUMENT_SCOPE_EXPLICIT
                else ()
            ),
            documents=tuple(documents),
        )
        logger.info(
            "武器谱文档范围已冻结: architecture_id=%s scope_mode=%s "
            "document_count=%d source_architecture_count=%d",
            architecture_id,
            mode,
            len(scope.documents),
            len({item.source_architecture_id for item in scope.documents}),
        )
        return scope

    @staticmethod
    def _resolve_explicit_records(
        records: list[Mapping[str, object]],
        requested_file_names: tuple[str, ...],
    ) -> tuple[Mapping[str, object], ...]:
        by_file_name: dict[str, list[Mapping[str, object]]] = {}
        for record in records:
            file_name = str(record.get("file_name") or "").strip()
            if file_name:
                by_file_name.setdefault(file_name, []).append(record)

        selected: list[Mapping[str, object]] = []
        for file_name in requested_file_names:
            candidates = by_file_name.get(file_name, [])
            if not candidates:
                raise WeaponryDocumentScopeNotFoundError(
                    f"文件 {file_name} 尚未解析，无法用于知识谱系解析"
                )
            if len(candidates) != 1:
                raise WeaponryDocumentScopeAmbiguityError(
                    f"文件 {file_name} 在多个知识库分类中存在记录，无法唯一确定引用版本"
                )
            selected.append(candidates[0])
        return tuple(selected)

    @staticmethod
    def _resolve_category_records(
        records: list[Mapping[str, object]],
        architecture_id: int,
    ) -> tuple[Mapping[str, object], ...]:
        selected: list[Mapping[str, object]] = []
        for record in records:
            raw_architecture_id = record.get("architecture_id")
            if isinstance(raw_architecture_id, bool) or not isinstance(
                raw_architecture_id,
                int,
            ):
                continue
            if raw_architecture_id == architecture_id:
                selected.append(record)
        # 不依赖具体 Repository 的默认排序；MySQL Adapter 上线后仍得到相同冻结顺序。
        selected.sort(
            key=lambda item: (
                str(item.get("file_name") or "").casefold(),
                str(item.get("file_name") or ""),
                str(item.get("doc_path") or ""),
            )
        )
        file_name_keys = [
            str(item.get("file_name") or "").strip().casefold()
            for item in selected
        ]
        if any(not item for item in file_name_keys) or len(set(file_name_keys)) != len(
            file_name_keys
        ):
            raise WeaponryDocumentScopeIntegrityError(
                "类别文档记录包含空文件名或大小写不敏感重复文件名"
            )
        return tuple(selected)


# 当前实现使用 SQLite DatabaseService；保留语义清晰的别名，未来替换 Repository 时组合根
# 只需更换 Adapter，不影响 Port、Application 或 Codec。
SQLiteWeaponryDocumentScopeAdapter = DatabaseServiceWeaponryDocumentScopeAdapter


__all__ = [
    "DatabaseServiceWeaponryDocumentScopeAdapter",
    "SQLiteWeaponryDocumentScopeAdapter",
]
