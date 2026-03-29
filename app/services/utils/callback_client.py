from __future__ import annotations

import logging
import json
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def post_callback_payload(callback_url: str, payload: dict, timeout: float) -> bool:
    # 将回调结果的 JSON 保存到本地文件供调试/审计
    try:
        runtime_dir = Path(".runtime")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        dump_path = runtime_dir / "call_back.json"
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.debug("回调数据已保存至 %s", dump_path)
    except Exception as e:
        logger.warning("保存回调数据失败: %s", e)

    try:
        response = requests.post(callback_url, json=payload, timeout=timeout)
        return bool(response.ok)
    except requests.exceptions.RequestException as exc:
        logger.warning("回调请求失败 url=%s: %s", callback_url, exc)
        return False
