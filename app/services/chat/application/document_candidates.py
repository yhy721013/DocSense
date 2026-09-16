"""兼容导出文件对话内部文档候选 DTO。

DTO 的实现位于纯 Domain 模块，避免 SQLite 协调器经由 Application 模块反向依赖
DatabaseService 或文档 Resolver。保留本模块是为了兼容阶段 2 已建立的内部导入路径。
"""

from app.services.chat.domain.document_candidates import (
    ChatArchitectureCandidates,
    ChatDocumentCandidate,
    ChatDocumentSelectionCandidates,
)

__all__ = [
    "ChatArchitectureCandidates",
    "ChatDocumentCandidate",
    "ChatDocumentSelectionCandidates",
]
