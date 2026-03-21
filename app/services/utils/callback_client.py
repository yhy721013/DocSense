from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


def post_callback_payload(callback_url: str, payload: dict, timeout: float) -> bool:
    try:
        response = requests.post(callback_url, json=payload, timeout=timeout)
        return bool(response.ok)
    except requests.exceptions.RequestException as exc:
        logger.warning("回调请求失败 url=%s: %s", callback_url, exc)
        return False
