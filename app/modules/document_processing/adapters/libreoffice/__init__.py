"""LibreOffice 运行内核、通用 Processor 与 profile 构造。"""

from .engine import (
    LibreOfficeLegacyOfficePreparer,
    discover_libreoffice_executable,
    is_legacy_office_path,
)
from .processor import LibreOfficeDocumentProcessorAdapter
from .profile import (
    LEGACY_OFFICE_PROCESSOR_ID,
    create_legacy_office_profile,
)

__all__ = [
    "LEGACY_OFFICE_PROCESSOR_ID",
    "LibreOfficeDocumentProcessorAdapter",
    "LibreOfficeLegacyOfficePreparer",
    "create_legacy_office_profile",
    "discover_libreoffice_executable",
    "is_legacy_office_path",
]
