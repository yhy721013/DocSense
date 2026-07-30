"""Legacy Office 旧导入路径的兼容 Facade。

唯一转换实现已经迁入 ``adapters.libreoffice.engine``。现有 Analysis、Report、Container
和第三方部署代码可继续从本模块导入公开类型，但不得在这里新增第二套转换逻辑。
"""

from .adapters.libreoffice.engine import (
    LibreOfficeLegacyOfficePreparer,
    discover_libreoffice_executable,
    is_legacy_office_path,
)

__all__ = [
    "LibreOfficeLegacyOfficePreparer",
    "discover_libreoffice_executable",
    "is_legacy_office_path",
]
