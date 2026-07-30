"""兼容旧 MinerU 导入路径。

唯一实现已迁入 ``app.modules.document_processing.adapters.mineru``。本文件禁止再次
加入提交、轮询、解压或临时目录逻辑；旧调用方将在 1H-5/1H-6 继续迁移。
"""

from app.modules.document_processing.adapters.mineru import (
    SUPPORTED_INPUT_SUFFIXES,
    MinerUConverter,
)

__all__ = ["MinerUConverter", "SUPPORTED_INPUT_SUFFIXES"]
