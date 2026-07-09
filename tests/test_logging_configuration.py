"""应用日志格式和第三方库降噪策略的离线单元测试。"""

from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from app.services.core.logging import (
    LOG_DATE_FORMAT,
    LOG_FORMAT,
    apply_third_party_log_levels,
    setup_logging,
)


class LoggingConfigurationTests(unittest.TestCase):
    """验证紧凑时间格式以及可重复应用的第三方 logger 级别。"""

    @patch("app.services.core.logging.logging.info")
    @patch("app.services.core.logging.logging.basicConfig")
    def test_setup_logging_uses_compact_timestamp(
        self,
        basic_config_mock,
        _info_mock,
    ) -> None:
        """全局配置必须使用两位年份且不得追加毫秒。"""
        setup_logging()

        configuration = basic_config_mock.call_args.kwargs
        self.assertEqual(LOG_FORMAT, configuration["format"])
        self.assertEqual(LOG_DATE_FORMAT, configuration["datefmt"])

    def test_argos_level_can_be_reapplied_after_dependency_override(self) -> None:
        """依赖把 logger 改回 INFO 后，统一策略必须能够恢复 WARNING。"""
        argos_logger = logging.getLogger("argostranslate.utils")
        previous_level = argos_logger.level
        try:
            argos_logger.setLevel(logging.INFO)

            apply_third_party_log_levels()

            self.assertEqual(logging.WARNING, argos_logger.level)
        finally:
            argos_logger.setLevel(previous_level)


if __name__ == "__main__":
    unittest.main()
