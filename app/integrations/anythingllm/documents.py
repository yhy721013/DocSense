"""AnythingLLM 全局文档接口的原子客户端。

该客户端只负责全局文档上传和永久删除，不负责把文档加入工作区、固定文档、创建会话或
调用当前部署不支持的上传后元数据更新端点。重试范围严格限制为已识别的 Document
Processor 暂时不可用错误，避免自动重放其他可能产生副作用的失败请求。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

from app.integrations.anythingllm.errors import (
    AnythingLLMCleanupUncertainError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTransportError,
    AnythingLLMUploadRejectedError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    first_text,
    normalize_document_location_key,
    normalize_document_path,
    parse_xlsx_sheet_location,
    require_mapping,
    require_sequence,
)
from app.integrations.anythingllm.policies import (
    DEFAULT_UPLOAD_RETRIES,
    DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
    validate_upload_max_retries,
    validate_upload_retry_base_delay,
)
from app.integrations.anythingllm.transport import AnythingLLMTransport


logger = logging.getLogger(__name__)


_XLSX_FOLDER_CLEANUP_TOKEN_VERSION = 1


@dataclass(frozen=True)
class _XlsxFolderCleanupScope:
    """严格解析后的单次 XLSX Collector 目录所有权快照。"""

    folder_name: str
    member_locations: tuple[str, ...]


@dataclass(frozen=True)
class XlsxFolderCleanupToken:
    """只供 AnythingLLM 集成层恢复使用的 opaque 文件夹清理凭据。"""

    value: str

    @classmethod
    def issue(cls, locations: Sequence[str]) -> "XlsxFolderCleanupToken":
        """从同一次可信上传响应签发成员集合固定的清理 token。"""
        normalized_locations: list[str] = []
        folder_name = ""
        for location in tuple(locations):
            parsed = parse_xlsx_sheet_location(location)
            if parsed is None:
                raise ValueError("XLSX 清理成员位置不符合受控 Sheet 结构")
            normalized_location, current_folder, _sheet_file = parsed
            if folder_name and current_folder != folder_name:
                raise ValueError("XLSX 清理成员不属于同一顶层目录")
            folder_name = current_folder
            normalized_locations.append(normalized_location)
        if not normalized_locations:
            raise ValueError("XLSX 清理成员不能为空")
        if len(set(normalized_locations)) != len(normalized_locations):
            raise ValueError("XLSX 清理成员包含重复位置")
        ordered_locations = tuple(sorted(normalized_locations))
        payload = {
            "folder_name": folder_name,
            "member_locations": list(ordered_locations),
            "version": _XLSX_FOLDER_CLEANUP_TOKEN_VERSION,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).decode("ascii")
        return cls(f"v{_XLSX_FOLDER_CLEANUP_TOKEN_VERSION}.{encoded}")

    def parse(self) -> _XlsxFolderCleanupScope:
        """重新校验 token 结构、路径语法与规范编码，拒绝调用方伪造范围。"""
        prefix = f"v{_XLSX_FOLDER_CLEANUP_TOKEN_VERSION}."
        if not isinstance(self.value, str) or not self.value.startswith(prefix):
            raise ValueError("XLSX 清理 token 版本无效")
        try:
            raw = base64.b64decode(
                self.value[len(prefix) :].encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("XLSX 清理 token 无法解码") from exc
        expected_keys = {"folder_name", "member_locations", "version"}
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise ValueError("XLSX 清理 token 结构无效")
        if payload["version"] != _XLSX_FOLDER_CLEANUP_TOKEN_VERSION:
            raise ValueError("XLSX 清理 token 版本冲突")
        folder_name = payload["folder_name"]
        member_locations = payload["member_locations"]
        if not isinstance(folder_name, str) or not isinstance(member_locations, list):
            raise ValueError("XLSX 清理 token 字段类型无效")
        if any(not isinstance(item, str) for item in member_locations):
            raise ValueError("XLSX 清理 token 成员类型无效")
        canonical = self.issue(member_locations)
        if canonical.value != self.value:
            raise ValueError("XLSX 清理 token 不是规范编码")
        return _XlsxFolderCleanupScope(
            folder_name=folder_name,
            member_locations=tuple(member_locations),
        )


@dataclass(frozen=True)
class XlsxFolderInventoryItem:
    """只读库存中的一个 XLSX Collector Folder 及其完整成员快照。

    该 DTO 只表达远端当前事实，不代表调用方拥有删除权。只有上传响应边界签发并在
    删除前重新核对成员集合的 ``XlsxFolderCleanupToken`` 才能授权破坏性操作。
    """

    folder_name: str
    member_locations: tuple[str, ...]

    def __post_init__(self) -> None:
        token_scope = XlsxFolderCleanupToken.issue(self.member_locations).parse()
        if token_scope.folder_name != self.folder_name:
            raise ValueError("XLSX 库存 Folder 与成员位置不一致")


class AnythingLLMDocumentClient:
    """封装 AnythingLLM 全局文档 API 的无状态原子操作。

    客户端不拥有传输对象的生命周期，同一任务中的其他原子客户端可以共享同一个
    ``AnythingLLMTransport``。调用方必须由更外层的 Factory 或 Facade 统一关闭传输对象。
    """

    _PROCESSOR_OFFLINE_MARKERS = (
        "Document processing API is not online",
        "fetch failed",
    )
    def __init__(
        self,
        transport: AnythingLLMTransport,
        *,
        upload_max_retries: int = DEFAULT_UPLOAD_RETRIES,
        upload_retry_base_delay: float = DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """创建文档原子客户端并校验上传重试参数。

        ``upload_max_retries`` 表示首次请求之后允许的重试次数，因此默认最多发起四次
        上传。``sleep`` 可在测试中注入，保证指数退避测试不产生真实等待。
        """
        validated_upload_max_retries = validate_upload_max_retries(
            upload_max_retries
        )
        validated_retry_base_delay = validate_upload_retry_base_delay(
            upload_retry_base_delay
        )
        self._transport = transport
        self._upload_max_retries = validated_upload_max_retries
        self._upload_retry_base_delay = validated_retry_base_delay
        self._sleep = sleep

    def upload_document(
        self,
        file_path: str,
        *,
        user_id: int | None = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> AnythingLLMDocument:
        """上传本地文件及可选元数据，并返回真实 ID 和位置。

        参数:
            file_path: 待上传的本地普通文件路径。
            user_id: 可选 AnythingLLM 用户标识。
            metadata: 随 multipart 请求提交的文档元数据。AnythingLLM 要求 multipart 中的
                ``metadata`` 是 JSON 字符串；本方法在第一次请求前完成独立拷贝和序列化，
                后续有限重试复用同一不可变字符串，避免调用方并发修改 Mapping 导致一次
                逻辑上传在不同尝试中携带不同身份信息。

        返回:
            由上传响应中真实 ``id/docId`` 和 ``location/docpath`` 构造的文档 DTO。

        异常:
            FileNotFoundError: 路径不存在或不是普通文件时抛出。
            TypeError: metadata 不是 Mapping 时抛出。
            ValueError: metadata 包含无法 JSON 序列化的值时抛出。
            AnythingLLMProtocolError: 响应缺少 documents、ID 或位置时抛出。
            AnythingLLMTransportError: HTTP 或网络请求失败时抛出对应子类。

        每次重试都会重新打开文件，确保文件游标从头开始。文件句柄由本方法拥有，并在
        单次请求结束后立即关闭；传输层只借用该句柄。
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"待上传文件不存在或不是普通文件：{path}")

        serialized_metadata, metadata_keys = self._serialize_upload_metadata(metadata)

        file_size = path.stat().st_size
        logger.info(
            "开始上传 AnythingLLM 文档: file_name=%s file_size_bytes=%d "
            "max_attempts=%d metadata_key_count=%d has_user_context=%s",
            path.name,
            file_size,
            self._upload_max_retries + 1,
            len(metadata_keys),
            user_id is not None,
        )

        for attempt in range(self._upload_max_retries + 1):
            try:
                with path.open("rb") as file_object:
                    request_kwargs: dict[str, Any] = {
                        "files": {"file": (os.path.basename(path), file_object)},
                        "user_id": user_id,
                    }
                    if serialized_metadata is not None:
                        request_kwargs["data"] = {"metadata": serialized_metadata}
                    body = self._transport.post_multipart(
                        "document/upload",
                        **request_kwargs,
                    )
                document = self._parse_upload_response(body, user_id=user_id)
                logger.info(
                    "AnythingLLM 文档上传完成: file_name=%s has_document_id=%s "
                    "has_document_location=%s has_document_ref=%s attempt=%d",
                    path.name,
                    bool(document.id),
                    bool(document.location),
                    bool(document.document_ref),
                    attempt + 1,
                )
                return document
            except AnythingLLMHTTPError as exc:
                if not self._can_retry_processor_error(exc, attempt=attempt):
                    raise
                delay = self._upload_retry_base_delay * (2**attempt)
                logger.warning(
                    "AnythingLLM 文档处理服务暂时不可用，准备重试上传: "
                    "file_name=%s attempt=%d/%d delay_seconds=%.1f status_code=%s",
                    path.name,
                    attempt + 1,
                    self._upload_max_retries + 1,
                    delay,
                    exc.status_code,
                )
                self._sleep(delay)

        # 循环的最后一次失败必定在 except 分支重新抛出，该分支仅用于类型检查完整性。
        raise AssertionError("上传重试循环异常结束")

    @staticmethod
    def _serialize_upload_metadata(
        metadata: Optional[Mapping[str, Any]],
    ) -> tuple[Optional[str], tuple[str, ...]]:
        """防御性复制并序列化上传元数据，且不在日志中暴露元数据值。

        空 Mapping 与 ``None`` 都表示不发送 multipart ``metadata`` 字段，以保持旧调用方
        的请求结构不变。键必须是非空字符串，禁止把不同类型的键静默转成同名字符串；
        日志只记录排序后的键名，值保持原始 JSON 类型。序列化失败在打开文件和发起 HTTP
        前抛出，确保配置错误不会产生外部上传副作用。
        """
        if metadata is None:
            return None, ()
        if not isinstance(metadata, Mapping):
            raise TypeError("metadata 必须是 Mapping 或 None")

        metadata_copy: dict[str, Any] = {}
        for key, value in metadata.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("metadata 的键必须是非空字符串")
            metadata_copy[key] = value
        if not metadata_copy:
            return None, ()
        try:
            serialized = json.dumps(
                metadata_copy,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("metadata 必须只包含可 JSON 序列化的值") from exc
        return serialized, tuple(sorted(metadata_copy))

    def delete_document(
        self,
        location: str,
        *,
        user_id: int | None = None,
    ) -> None:
        """永久删除一次上传产生的全局文档及其所有关联。

        AnythingLLM 开发者 API 的 ``DELETE system/remove-documents`` 接收 ``names`` 数组。
        上游 ``purgeDocument`` 会删除源文档、向量缓存，并从全部 Workspace 移除该文档。
        因此本方法具有全局破坏性，只接受上传接口返回且可归一化到 ``custom-documents``
        的位置。部分部署会返回包含宿主前缀的绝对路径，本方法会先剥离该前缀；归一化后
        仍不允许任意目录、父目录片段、查询串或控制字符。

        本操作不自动重试。虽然上游删除设计为幂等，网络超时仍无法证明服务器是否已经
        执行成功；重试或补偿决策必须由持有完整业务上下文的 Gateway 明确控制。
        """
        normalized_location = normalize_document_path(location)
        path_parts = tuple(part for part in normalized_location.split("/") if part)
        has_control_character = any(ord(char) < 32 for char in normalized_location)
        if (
            not normalized_location.startswith("custom-documents/")
            or any(part in {".", ".."} for part in path_parts)
            or len(path_parts) < 2
            or has_control_character
            or "?" in normalized_location
            or "#" in normalized_location
        ):
            raise ValueError("只能删除有效的 custom-documents 全局文档位置")

        logger.info(
            "开始永久删除 AnythingLLM 全局文档: has_document_location=%s "
            "has_user_context=%s",
            bool(normalized_location),
            user_id is not None,
        )
        body = self._transport.delete_json(
            "system/remove-documents",
            {"names": [normalized_location]},
            user_id=user_id,
        )
        payload = require_mapping(body, context="永久删除文档响应")
        if payload.get("error") or payload.get("success") is not True:
            raise AnythingLLMProtocolError("AnythingLLM 未确认全局文档已永久删除")
        logger.info(
            "AnythingLLM 全局文档永久删除完成: has_document_location=%s has_user_context=%s",
            bool(normalized_location),
            user_id is not None,
        )

    def delete_document_artifact(
        self,
        location: str,
        *,
        user_id: int | None = None,
    ) -> None:
        """按上传产物类型安全删除一个业务文件拥有的全局 Artifact。

        普通 ``custom-documents`` 完全复用既有 ``delete_document`` 合同。严格匹配的
        AnythingLLM 1.15.0 单 Sheet XLSX location 则转换为成员集合只有该 Sheet 的 opaque
        token，并在删除前通过 folder-list 核对目录没有成员漂移。
        """
        xlsx_location = parse_xlsx_sheet_location(location)
        if xlsx_location is None:
            self.delete_document(location, user_id=user_id)
            return
        token = XlsxFolderCleanupToken.issue((xlsx_location[0],))
        self.delete_xlsx_folder(token, user_id=user_id)

    def delete_xlsx_folder(
        self,
        token: XlsxFolderCleanupToken,
        *,
        user_id: int | None = None,
    ) -> None:
        """使用严格 opaque token 删除一次 XLSX 上传产生的完整 Collector 目录。

        该方法拒绝原始路径字符串。删除前必须由 folder-list 返回与 token 完全相同的成员
        集合；若目标已不存在，则再通过根 documents 列表确认目录确实缺失。端点缺失、成员
        漂移、网络结果不明或非 ``success=true`` 都按不确定清理失败处理。
        """
        if not isinstance(token, XlsxFolderCleanupToken):
            raise TypeError("token 必须是 XlsxFolderCleanupToken")
        scope = token.parse()
        try:
            current_members = self._list_xlsx_folder_members(
                scope.folder_name,
                user_id=user_id,
            )
        except AnythingLLMHTTPError as exc:
            if exc.status_code != 404:
                raise
            try:
                folder_absent = self._folder_absent_from_root(
                    scope.folder_name,
                    user_id=user_id,
                )
            except Exception as reconcile_exc:
                raise AnythingLLMCleanupUncertainError(
                    "AnythingLLM XLSX 文件夹缺失状态无法确认"
                ) from reconcile_exc
            if folder_absent:
                logger.info(
                    "AnythingLLM XLSX 上传目录已不存在: cleanup_status=already_absent "
                    "has_user_context=%s",
                    user_id is not None,
                )
                return
            raise AnythingLLMCleanupUncertainError(
                "AnythingLLM folder-list 能力不可用，拒绝执行 XLSX 文件夹删除"
            ) from exc
        if current_members != scope.member_locations:
            raise AnythingLLMCleanupUncertainError(
                "AnythingLLM XLSX 文件夹成员集合已变化，拒绝执行破坏性删除"
            )

        logger.info(
            "开始删除 AnythingLLM XLSX 上传目录: member_count=%d has_user_context=%s",
            len(scope.member_locations),
            user_id is not None,
        )
        try:
            body = self._transport.delete_json(
                "document/remove-folder",
                {"name": scope.folder_name},
                user_id=user_id,
            )
        except AnythingLLMTransportError as exc:
            # 删除请求可能已到达上游。只有重新读取根目录能证明目标已消失时，才把结果提升
            # 为确定成功；任何二次读取失败或目录仍在都必须保留“不确定”语义。
            try:
                absent = self._folder_absent_from_root(
                    scope.folder_name,
                    user_id=user_id,
                )
            except Exception:
                absent = False
            if absent:
                logger.warning(
                    "AnythingLLM XLSX 文件夹删除响应不确定，但已确认目录不存在: "
                    "cleanup_status=succeeded_after_reconcile"
                )
                return
            raise AnythingLLMCleanupUncertainError(
                "AnythingLLM XLSX 文件夹删除结果未确认"
            ) from exc
        payload = require_mapping(body, context="删除 XLSX 文件夹响应")
        if payload.get("error") or payload.get("success") is not True:
            raise AnythingLLMCleanupUncertainError(
                "AnythingLLM 未确认 XLSX 文件夹已永久删除"
            )
        logger.info(
            "AnythingLLM XLSX 上传目录删除完成: member_count=%d "
            "has_user_context=%s",
            len(scope.member_locations),
            user_id is not None,
        )

    def list_xlsx_folder_inventory(
        self,
        *,
        user_id: int | None = None,
    ) -> tuple[XlsxFolderInventoryItem, ...]:
        """只读列出所有符合受控结构的 XLSX Collector Folder。

        本方法只调用 ``GET documents`` 与 ``GET documents/folder/<name>``，不会上传、
        绑定、删除或签发清理授权。根目录中与 XLSX 结构无关的普通文档会被忽略；一旦
        名称符合 XLSX Folder 结构，其类型、成员和重复身份必须全部通过严格校验。
        """

        body = self._transport.get_json("documents", user_id=user_id)
        payload = require_mapping(body, context="documents 根列表响应")
        local_files = require_mapping(
            payload.get("localFiles"),
            context="documents 根列表 localFiles 字段",
        )
        items = require_sequence(
            local_files.get("items"),
            context="documents 根列表 items 字段",
        )

        folder_names: list[str] = []
        normalized_keys: set[str] = set()
        for index, item in enumerate(items):
            record = require_mapping(item, context=f"documents 根列表成员 {index}")
            folder_name = first_text(record, "name")
            parsed = parse_xlsx_sheet_location(
                f"{folder_name}/sheet-placeholder.json"
            )
            if parsed is None:
                continue
            if first_text(record, "type").casefold() != "folder":
                raise AnythingLLMProtocolError(
                    "AnythingLLM XLSX 根目录成员类型不是 folder"
                )
            normalized_key = parsed[1].casefold()
            if normalized_key in normalized_keys:
                raise AnythingLLMProtocolError(
                    "AnythingLLM documents 根列表包含重复 XLSX Folder"
                )
            normalized_keys.add(normalized_key)
            folder_names.append(parsed[1])

        inventory = tuple(
            XlsxFolderInventoryItem(
                folder_name=folder_name,
                member_locations=self._list_xlsx_folder_members(
                    folder_name,
                    user_id=user_id,
                ),
            )
            for folder_name in sorted(folder_names, key=str.casefold)
        )
        logger.info(
            "AnythingLLM XLSX Folder 只读库存完成: folder_count=%d "
            "member_count=%d has_user_context=%s remote_mutation=false",
            len(inventory),
            sum(len(item.member_locations) for item in inventory),
            user_id is not None,
        )
        return inventory

    def _list_xlsx_folder_members(
        self,
        folder_name: str,
        *,
        user_id: int | None,
    ) -> tuple[str, ...]:
        """读取并严格规范化 folder-list 返回的直接 Sheet 成员。"""
        requested_scope = XlsxFolderCleanupToken.issue(
            (f"{folder_name}/sheet-placeholder.json",)
        ).parse()
        body = self._transport.get_json(
            "documents/folder/"
            f"{quote(requested_scope.folder_name, safe='')}",
            user_id=user_id,
        )
        payload = require_mapping(body, context="XLSX folder-list 响应")
        returned_folder = first_text(payload, "folder")
        if returned_folder != requested_scope.folder_name or payload.get("error"):
            raise AnythingLLMProtocolError(
                "AnythingLLM XLSX folder-list 返回了不一致的目录"
            )
        documents = require_sequence(
            payload.get("documents"),
            context="XLSX folder-list documents 字段",
        )
        members: list[str] = []
        for index, item in enumerate(documents):
            record = require_mapping(item, context=f"XLSX folder-list 成员 {index}")
            location = first_text(record, "location", "docpath", "docPath")
            name = first_text(record, "name", "filename", "fileName")
            if location:
                parsed = parse_xlsx_sheet_location(location)
            elif name:
                parsed = parse_xlsx_sheet_location(
                    f"{requested_scope.folder_name}/{name}"
                )
            else:
                parsed = None
            if parsed is None or parsed[1] != requested_scope.folder_name:
                raise AnythingLLMProtocolError(
                    "AnythingLLM XLSX folder-list 包含无效成员"
                )
            if location and name and parsed[2] != name:
                raise AnythingLLMProtocolError(
                    "AnythingLLM XLSX folder-list 成员名称与位置冲突"
                )
            members.append(parsed[0])
        if len(set(members)) != len(members):
            raise AnythingLLMProtocolError(
                "AnythingLLM XLSX folder-list 包含重复成员"
            )
        return tuple(sorted(members))

    def _folder_absent_from_root(
        self,
        folder_name: str,
        *,
        user_id: int | None,
    ) -> bool:
        """通过根 documents 列表确认目标顶层目录是否已经不存在。"""
        requested_scope = XlsxFolderCleanupToken.issue(
            (f"{folder_name}/sheet-placeholder.json",)
        ).parse()
        body = self._transport.get_json("documents", user_id=user_id)
        payload = require_mapping(body, context="documents 根列表响应")
        local_files = require_mapping(
            payload.get("localFiles"),
            context="documents 根列表 localFiles 字段",
        )
        items = require_sequence(
            local_files.get("items"),
            context="documents 根列表 items 字段",
        )
        for index, item in enumerate(items):
            record = require_mapping(item, context=f"documents 根列表成员 {index}")
            if (
                first_text(record, "name").casefold()
                == requested_scope.folder_name.casefold()
            ):
                return False
        return True

    def _parse_upload_response(
        self,
        value: Any,
        *,
        user_id: int | None,
    ) -> AnythingLLMDocument:
        """完整解析上传响应，并把 XLSX 单 Sheet 边界收口在原子客户端。"""
        payload = require_mapping(value, context="文档上传响应")
        documents = payload.get("documents")
        if not isinstance(documents, list) or not documents:
            raise AnythingLLMProtocolError(
                "AnythingLLM 文档上传响应缺少非空 documents 数组"
            )

        # 先冻结全部成员和 location，再解析需要 id 等字段的 DTO。只要响应已经精确证明
        # 所有非重复成员属于同一受控 XLSX Collector 目录，就必须先签发恢复 token；
        # 否则后续某个成员缺少 id 时会丢失唯一安全的目录清理边界。
        records: list[Mapping[str, Any]] = []
        raw_locations: list[str] = []
        try:
            for index, item in enumerate(documents):
                record = require_mapping(item, context=f"文档上传成员 {index}")
                records.append(record)
                raw_locations.append(
                    first_text(record, "location", "docpath", "docPath")
                )
        except AnythingLLMProtocolError as exc:
            raise AnythingLLMUploadRejectedError(
                "AnythingLLM 文档上传响应包含空或畸形成员"
            ) from exc

        location_keys = tuple(
            normalize_document_location_key(location)
            for location in raw_locations
        )
        if any(not location for location in location_keys):
            raise AnythingLLMUploadRejectedError(
                "AnythingLLM 文档上传响应包含空位置"
            )
        if len(set(location_keys)) != len(location_keys):
            raise AnythingLLMUploadRejectedError(
                "AnythingLLM 文档上传响应包含重复位置"
            )

        xlsx_locations = tuple(
            parse_xlsx_sheet_location(location) for location in raw_locations
        )
        trusted_xlsx_cleanup_token: XlsxFolderCleanupToken | None = None
        if all(item is not None for item in xlsx_locations):
            try:
                trusted_xlsx_cleanup_token = XlsxFolderCleanupToken.issue(
                    raw_locations
                )
            except ValueError:
                # 全部成员虽然各自符合 Sheet 路径语法，但不属于同一顶层目录。此时不能
                # 猜测任一目录拥有完整上传结果，后续保持 fail-closed 且不发起删除。
                trusted_xlsx_cleanup_token = None

        parsed_documents: list[AnythingLLMDocument] = []
        try:
            parsed_documents.extend(
                AnythingLLMDocument.from_payload(record) for record in records
            )
        except AnythingLLMProtocolError as exc:
            if trusted_xlsx_cleanup_token is None:
                raise AnythingLLMUploadRejectedError(
                    "AnythingLLM 文档上传响应包含空或畸形成员"
                ) from exc
            try:
                self.delete_xlsx_folder(
                    trusted_xlsx_cleanup_token,
                    user_id=user_id,
                )
            except Exception as cleanup_exc:
                logger.error(
                    "AnythingLLM XLSX 上传响应成员畸形，且整批清理结果未确认: "
                    "sheet_count=%d cleanup_attempted=true "
                    "cleanup_confirmed=false error_type=%s",
                    len(records),
                    type(cleanup_exc).__name__,
                )
                raise AnythingLLMUploadRejectedError(
                    "AnythingLLM XLSX 上传响应包含畸形成员，且整批清理结果未确认",
                    cleanup_attempted=True,
                    cleanup_confirmed=False,
                    folder_cleanup_token=trusted_xlsx_cleanup_token.value,
                ) from cleanup_exc
            logger.warning(
                "AnythingLLM XLSX 上传响应成员畸形，已完成整批清理: "
                "sheet_count=%d cleanup_attempted=true cleanup_confirmed=true",
                len(records),
            )
            raise AnythingLLMUploadRejectedError(
                "AnythingLLM XLSX 上传响应包含畸形成员",
                cleanup_attempted=True,
                cleanup_confirmed=True,
                folder_cleanup_token=trusted_xlsx_cleanup_token.value,
            ) from exc

        custom_documents = tuple(
            location.startswith("custom-documents/") for location in location_keys
        )
        if all(custom_documents):
            if len(parsed_documents) != 1:
                raise AnythingLLMUploadRejectedError(
                    "AnythingLLM 普通文档上传返回了多个文档成员"
                )
            return parsed_documents[0]
        if not all(item is not None for item in xlsx_locations):
            raise AnythingLLMUploadRejectedError(
                "AnythingLLM 文档上传响应混合了不同产物结构"
            )

        token = trusted_xlsx_cleanup_token
        if token is None:
            raise AnythingLLMUploadRejectedError(
                "AnythingLLM XLSX 上传响应包含混合顶层目录"
            )
        if len(parsed_documents) == 1:
            return parsed_documents[0]

        try:
            self.delete_xlsx_folder(token, user_id=user_id)
        except Exception as exc:
            logger.error(
                "AnythingLLM 多 Sheet XLSX 已拒绝，但整批清理结果未确认: "
                "sheet_count=%d cleanup_attempted=true cleanup_confirmed=false "
                "error_type=%s",
                len(parsed_documents),
                type(exc).__name__,
            )
            raise AnythingLLMUploadRejectedError(
                "当前仅支持单 Sheet XLSX，且多 Sheet 上传清理结果未确认",
                cleanup_attempted=True,
                cleanup_confirmed=False,
                folder_cleanup_token=token.value,
            ) from exc
        logger.warning(
            "AnythingLLM 多 Sheet XLSX 已拒绝并完成整批清理: "
            "sheet_count=%d cleanup_attempted=true cleanup_confirmed=true",
            len(parsed_documents),
        )
        raise AnythingLLMUploadRejectedError(
            "当前仅支持单 Sheet XLSX",
            cleanup_attempted=True,
            cleanup_confirmed=True,
            folder_cleanup_token=token.value,
        )

    def _can_retry_processor_error(
        self,
        error: AnythingLLMHTTPError,
        *,
        attempt: int,
    ) -> bool:
        """判断错误是否属于已知且仍有配额的 Document Processor 临时故障。"""
        if attempt >= self._upload_max_retries or error.status_code != 500:
            return False
        summary = error.response_summary.casefold()
        return any(marker.casefold() in summary for marker in self._PROCESSOR_OFFLINE_MARKERS)
