"""AnythingLLM 全局文档接口的原子客户端。

该客户端只负责文档上传和元数据更新，不负责把文档加入工作区、固定文档或创建会话。
重试范围严格限制为已识别的 Document Processor 暂时不可用错误，避免自动重放其他
可能产生副作用的失败请求。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
)
from app.integrations.anythingllm.models import AnythingLLMDocument, require_mapping
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
        upload_max_retries: int = 3,
        upload_retry_base_delay: float = 3.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """创建文档原子客户端并校验上传重试参数。

        ``upload_max_retries`` 表示首次请求之后允许的重试次数，因此默认最多发起四次
        上传。``sleep`` 可在测试中注入，保证指数退避测试不产生真实等待。
        """
        if upload_max_retries < 0:
            raise ValueError("upload_max_retries 不得小于 0")
        if upload_retry_base_delay < 0:
            raise ValueError("upload_retry_base_delay 不得小于 0")
        self._transport = transport
        self._upload_max_retries = upload_max_retries
        self._upload_retry_base_delay = upload_retry_base_delay
        self._sleep = sleep

    def upload_document(
        self,
        file_path: str,
        *,
        user_id: int | None = None,
    ) -> AnythingLLMDocument:
        """上传本地文件并返回包含真实 ID 和位置的统一文档 DTO。

        参数:
            file_path: 待上传的本地普通文件路径。
            user_id: 可选 AnythingLLM 用户标识。

        返回:
            由上传响应中真实 ``id/docId`` 和 ``location/docpath`` 构造的文档 DTO。

        异常:
            FileNotFoundError: 路径不存在或不是普通文件时抛出。
            AnythingLLMProtocolError: 响应缺少 documents、ID 或位置时抛出。
            AnythingLLMTransportError: HTTP 或网络请求失败时抛出对应子类。

        每次重试都会重新打开文件，确保文件游标从头开始。文件句柄由本方法拥有，并在
        单次请求结束后立即关闭；传输层只借用该句柄。
        """
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"待上传文件不存在或不是普通文件：{path}")

        file_size = path.stat().st_size
        logger.info(
            "开始上传 AnythingLLM 文档: file_name=%s file_size=%d "
            "max_attempts=%d has_user_context=%s",
            path.name,
            file_size,
            self._upload_max_retries + 1,
            user_id is not None,
        )

        for attempt in range(self._upload_max_retries + 1):
            try:
                with path.open("rb") as file_object:
                    body = self._transport.post_multipart(
                        "document/upload",
                        files={"file": (os.path.basename(path), file_object)},
                        user_id=user_id,
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

    def update_metadata(
        self,
        location: str,
        metadata: Mapping[str, Any],
        *,
        user_id: int | None = None,
    ) -> None:
        """更新全局文档元数据，并校验响应没有明确失败语义。

        空位置会被拒绝，防止元数据意外写入未知文档。空元数据允许发送，以兼容上游
        清空元数据的语义。成功时不返回供应商响应，避免原始字段泄漏到编排层。
        """
        normalized_location = str(location or "").strip()
        if not normalized_location:
            raise ValueError("文档 location 不能为空")
        body = self._transport.post_json(
            "document/meta",
            {"location": normalized_location, "metadata": dict(metadata)},
            user_id=user_id,
        )
        payload = require_mapping(body, context="文档元数据响应")
        if payload.get("error") or payload.get("success") is False:
            raise AnythingLLMProtocolError("AnythingLLM 明确拒绝文档元数据更新")
        logger.info(
            "AnythingLLM 文档元数据更新完成: location=%s metadata_keys=%s "
            "has_user_context=%s",
            normalized_location,
            sorted(str(key) for key in metadata.keys()),
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
