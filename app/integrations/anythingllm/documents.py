"""AnythingLLM 全局文档接口的原子客户端。

该客户端只负责全局文档上传和永久删除，不负责把文档加入工作区、固定文档、创建会话或
调用当前部署不支持的上传后元数据更新端点。重试范围严格限制为已识别的 Document
Processor 暂时不可用错误，避免自动重放其他可能产生副作用的失败请求。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
)
from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    normalize_document_path,
    require_mapping,
)
from app.integrations.anythingllm.policies import (
    DEFAULT_UPLOAD_RETRIES,
    DEFAULT_UPLOAD_RETRY_BASE_DELAY_SECONDS,
    validate_upload_max_retries,
    validate_upload_retry_base_delay,
)
from app.integrations.anythingllm.transport import AnythingLLMTransport


logger = logging.getLogger(__name__)


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
            "开始上传 AnythingLLM 文档: file_name=%s file_size=%d "
            "max_attempts=%d metadata_keys=%s has_user_context=%s",
            path.name,
            file_size,
            self._upload_max_retries + 1,
            metadata_keys,
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
                document = self._parse_upload_response(body)
                logger.info(
                    "AnythingLLM 文档上传完成: file_name=%s document_id=%s "
                    "location=%s document_ref=%s attempt=%d",
                    path.name,
                    document.id,
                    document.location,
                    document.document_ref,
                    attempt + 1,
                )
                return document
            except AnythingLLMHTTPError as exc:
                if not self._can_retry_processor_error(exc, attempt=attempt):
                    raise
                delay = self._upload_retry_base_delay * (2**attempt)
                logger.warning(
                    "AnythingLLM Document Processor 暂时不可用，准备重试上传: "
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
            "开始永久删除 AnythingLLM 全局文档: location=%s "
            "has_user_context=%s",
            normalized_location,
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
            "AnythingLLM 全局文档永久删除完成: location=%s has_user_context=%s",
            normalized_location,
            user_id is not None,
        )

    def _parse_upload_response(self, value: Any) -> AnythingLLMDocument:
        """严格解析上传响应，不允许根据文件名猜测缺失的内部位置。"""
        payload = require_mapping(value, context="文档上传响应")
        documents = payload.get("documents")
        if not isinstance(documents, list) or not documents:
            raise AnythingLLMProtocolError(
                "AnythingLLM 文档上传响应缺少非空 documents 数组"
            )
        return AnythingLLMDocument.from_payload(documents[0])

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
