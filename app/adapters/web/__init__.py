"""HTTP、SSE 与 WebSocket 入站适配器命名空间。"""

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
    "ARCHITECTURE_ID_ERROR",
    "ArchitectureIdValidationError",
    "MAX_REPORT_ID_DIGITS",
    "NormalizedArchitectureId",
    "NormalizedReportId",
    "ReportIdValidationError",
    "normalize_architecture_id",
    "normalize_report_id",
]
