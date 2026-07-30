"""ArtifactContent 的本地实现。

本文件属于 Adapter 层，因此可以持有 ``Path``；Application 和领域对象只接触
``ArtifactContent.open_reader`` 返回的流。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO, ContextManager


class BytesArtifactContent:
    """适合小型内存结果和离线测试的不可变内容源。"""

    def __init__(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("payload 必须是 bytes")
        self._payload = payload

    def open_reader(self) -> ContextManager[BinaryIO]:
        return BytesIO(self._payload)


class FileArtifactContent:
    """由具体 Processor Adapter 私有持有的本地文件内容源。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def open_reader(self) -> ContextManager[BinaryIO]:
        return self._path.open("rb")


__all__ = ["BytesArtifactContent", "FileArtifactContent"]
