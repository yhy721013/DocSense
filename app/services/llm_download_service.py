from __future__ import annotations

from pathlib import Path

import requests


def download_to_temp_file(url: str, file_name: str, temp_root: str, timeout: float) -> str:
    response = requests.get(url, timeout=timeout)
    if not response.ok:
        raise RuntimeError(f"下载文件失败: {response.status_code}")

    temp_dir = Path(temp_root)
    temp_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file_name).name
    target_path = temp_dir / safe_name
    target_path.write_bytes(response.content)
    return str(target_path)
