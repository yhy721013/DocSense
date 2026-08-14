"""带总时限、大小上限和原子发布的 HTTP Source 下载实现。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import time
from uuid import uuid4

import requests


logger = logging.getLogger(__name__)

DEFAULT_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_MAX_SINGLE_IO_WAIT_SECONDS = 10.0


def download_source_to_temp_file(
    url: str,
    file_name: str,
    temp_root: str,
    timeout: float,
    max_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
) -> str:
    """在总时限和大小上限内下载文件，并以原子替换发布完整结果。

    日志只记录安全文件名和字节计数，不记录可能携带签名、Token 或业务标识的 URL。
    ``requests`` 的超时仅限制单次连接/读取，因此另用 monotonic 总时限阻止慢响应长期
    占用唯一 Report Worker。
    """

    if not isinstance(url, str) or not url.strip():
        raise ValueError("下载 URL 不能为空")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("下载 timeout 必须是数字")
    total_timeout = float(timeout)
    if total_timeout != total_timeout or total_timeout in (float("inf"), float("-inf")) or total_timeout <= 0:
        raise ValueError("下载 timeout 必须是正有限数字")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("下载 max_bytes 必须是正整数")

    temp_dir = Path(temp_root)
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file_name).name
    if not safe_name or safe_name in {".", ".."}:
        raise ValueError("下载文件名无效")
    target_path = temp_dir / safe_name
    partial_path = target_path.with_name(f".{safe_name}.{uuid4().hex}.part")
    started_at = time.monotonic()
    io_timeout = min(total_timeout, _MAX_SINGLE_IO_WAIT_SECONDS)
    response = None
    downloaded_bytes = 0
    try:
        response = requests.get(
            url.strip(), stream=True, allow_redirects=True, timeout=(io_timeout, io_timeout)
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"下载文件失败: {response.status_code}")
        if time.monotonic() - started_at > total_timeout:
            raise TimeoutError("下载文件超过总传输时限")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_bytes = int(content_length)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("下载响应 Content-Length 无效") from exc
            if declared_bytes < 0:
                raise RuntimeError("下载响应 Content-Length 无效")
            if declared_bytes > max_bytes:
                raise RuntimeError("下载文件超过允许的大小上限")

        with partial_path.open("xb") as file_object:
            for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
                if time.monotonic() - started_at > total_timeout:
                    raise TimeoutError("下载文件超过总传输时限")
                if not chunk:
                    continue
                if not isinstance(chunk, (bytes, bytearray)):
                    raise RuntimeError("下载响应产生了非字节数据")
                downloaded_bytes += len(chunk)
                if downloaded_bytes > max_bytes:
                    raise RuntimeError("下载文件超过允许的大小上限")
                file_object.write(chunk)
            file_object.flush()
            os.fsync(file_object.fileno())
        if time.monotonic() - started_at > total_timeout:
            raise TimeoutError("下载文件超过总传输时限")
        os.replace(partial_path, target_path)
    except Exception:
        try:
            partial_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理下载部分文件失败: file_name=%s", safe_name, exc_info=True)
        logger.warning(
            "HTTP Source 下载失败: file_name=%s downloaded_bytes=%d max_bytes=%d",
            safe_name,
            downloaded_bytes,
            max_bytes,
            exc_info=True,
        )
        raise
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                logger.warning("关闭下载响应失败: file_name=%s", safe_name, exc_info=True)

    logger.info(
        "HTTP Source 下载完成: file_name=%s bytes=%d max_bytes=%d",
        safe_name,
        downloaded_bytes,
        max_bytes,
    )
    return str(target_path)


__all__ = ["DEFAULT_MAX_DOWNLOAD_BYTES", "download_source_to_temp_file"]
