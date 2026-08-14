from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from typing import Mapping

import requests

from app.infrastructure.observability.callback_history import (
    CALLBACK_HISTORY_DIR as _SHARED_CALLBACK_HISTORY_DIR,
    build_callback_history_stem as _build_callback_history_stem,
    save_callback_history_payload as _save_callback_history_payload,
)

logger = logging.getLogger(__name__)

# 兼容旧测试与调用方对模块常量的补丁；真正的默认值与物理 Writer 均归属共享基础设施。
CALLBACK_HISTORY_DIR = _SHARED_CALLBACK_HISTORY_DIR


def build_callback_history_stem(
    payload: Mapping[str, Any],
    callback_context: Mapping[str, Any] | None = None,
    *,
    timestamp: datetime | None = None,
) -> str:
    """兼容旧导入路径；文件名规则由共享 History Writer 统一维护。"""

    return _build_callback_history_stem(
        payload,
        callback_context,
        timestamp=timestamp,
    )


def save_callback_history_payload(
    payload: dict,
    *,
    callback_context: Mapping[str, Any] | None = None,
    history_dir: Path | None = None,
    timestamp: datetime | None = None,
) -> Path:
    """兼容旧导入路径；显式传递旧常量以保留既有补丁与部署覆盖方式。"""

    return _save_callback_history_payload(
        payload,
        callback_context=callback_context,
        history_dir=history_dir or CALLBACK_HISTORY_DIR,
        timestamp=timestamp,
    )


def post_callback_payload(
    callback_url: str,
    payload: dict,
    timeout: float,
    *,
    callback_context: Mapping[str, Any] | None = None,
) -> bool:
    try:
        dump_path = save_callback_history_payload(payload, callback_context=callback_context)
        logger.debug("回调历史数据已保存: file_name=%s", dump_path.name)
    except Exception as e:
        logger.warning("保存回调历史数据失败: error_type=%s", type(e).__name__)

    response = None
    try:
        response = requests.post(callback_url, json=payload, timeout=timeout)
        # requests.Response.ok 会把 3xx 也视为 True；外部回调契约只接受 2xx。
        return 200 <= response.status_code < 300
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "向外部回调地址发送请求失败: timeout_seconds=%s error_type=%s",
            timeout,
            type(exc).__name__,
        )
        return False
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                # 响应清理异常不能覆盖甲方已经返回的 HTTP 状态；连接池故障单独记录。
                logger.warning("关闭外部回调响应失败", exc_info=True)
