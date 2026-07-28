"""HTTP、SSE 与 WebSocket 入站适配器命名空间。"""

from .chat_scope import (
    CHAT_ARCHITECTURE_ID_EMPTY_ERROR,
    CHAT_FILE_NAME_ITEM_ERROR,
    CHAT_FILE_NAMES_TYPE_ERROR,
    CHAT_SCOPE_SELECTOR_CONFLICT_ERROR,
    ChatScopeSelectorValidationError,
    parse_chat_scope_selector,
)
from .report_ids import (
    MAX_REPORT_ID_DIGITS,
    NormalizedReportId,
    ReportIdValidationError,
    normalize_report_id,
)
from .weaponry_ids import (
    ARCHITECTURE_ID_ERROR,
    ArchitectureIdValidationError,
    NormalizedArchitectureId,
    normalize_architecture_id,
)

__all__ = [
    "CHAT_ARCHITECTURE_ID_EMPTY_ERROR",
    "CHAT_FILE_NAME_ITEM_ERROR",
    "CHAT_FILE_NAMES_TYPE_ERROR",
    "CHAT_SCOPE_SELECTOR_CONFLICT_ERROR",
    "ChatScopeSelectorValidationError",
    "ARCHITECTURE_ID_ERROR",
    "ArchitectureIdValidationError",
    "MAX_REPORT_ID_DIGITS",
    "NormalizedArchitectureId",
    "NormalizedReportId",
    "ReportIdValidationError",
    "normalize_architecture_id",
    "normalize_report_id",
    "parse_chat_scope_selector",
]
