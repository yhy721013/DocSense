from __future__ import annotations

import requests


def post_callback_payload(callback_url: str, payload: dict, timeout: float) -> bool:
    response = requests.post(callback_url, json=payload, timeout=timeout)
    return bool(response.ok)
