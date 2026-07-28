"""Local document preprocessing boundaries."""

from .domain import (
    LEGACY_OFFICE_SAFE_ERROR_MESSAGE,
    LegacyOfficeConfig,
    LegacyOfficeConversionError,
    LegacyOfficePreparationResult,
)
from .libreoffice import (
    LibreOfficeLegacyOfficePreparer,
    discover_libreoffice_executable,
    is_legacy_office_path,
)
from .ports import LegacyOfficePreparer

__all__ = [
    "LEGACY_OFFICE_SAFE_ERROR_MESSAGE",
    "LegacyOfficeConfig",
    "LegacyOfficeConversionError",
    "LegacyOfficePreparationResult",
    "LegacyOfficePreparer",
    "LibreOfficeLegacyOfficePreparer",
    "discover_libreoffice_executable",
    "is_legacy_office_path",
]
