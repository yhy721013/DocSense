from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.container import create_application_services
from app.modules.document_processing import (
    LegacyOfficeConfig,
    LegacyOfficeConversionError,
)
from app.services.core.config import (
    LegacyOfficeConfigurationError,
    load_legacy_office_config,
)
from app.services.core.settings import RUNTIME_DIR


class _StopAfterLegacyOfficePreflight(RuntimeError):
    pass


class LegacyOfficeConfigTests(unittest.TestCase):
    def test_defaults_are_disabled_and_use_fixed_runtime_jobs_root(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = load_legacy_office_config()

        self.assertFalse(config.enabled)
        self.assertIsNone(config.executable)
        self.assertEqual("26.2", config.allowed_version_series)
        self.assertEqual(120.0, config.timeout_seconds)
        self.assertEqual(1, config.max_concurrency)
        self.assertEqual(512 * 1024 * 1024, config.max_input_bytes)
        self.assertEqual(1024 * 1024 * 1024, config.max_output_bytes)
        self.assertEqual(
            RUNTIME_DIR / "office_conversion" / "jobs",
            config.jobs_root,
        )

    def test_loader_preserves_approved_internal_overrides(self) -> None:
        executable = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
        with patch.dict(
            os.environ,
            {
                "DOCSENSE_LEGACY_OFFICE_ENABLED": "true",
                "DOCSENSE_LIBREOFFICE_EXECUTABLE": executable,
                "DOCSENSE_LIBREOFFICE_ALLOWED_VERSION_SERIES": "26.2",
                "DOCSENSE_LEGACY_OFFICE_TIMEOUT_SECONDS": "180",
                "DOCSENSE_LEGACY_OFFICE_MAX_CONCURRENCY": "2",
                "DOCSENSE_LEGACY_OFFICE_MAX_INPUT_BYTES": "4096",
                "DOCSENSE_LEGACY_OFFICE_MAX_OUTPUT_BYTES": "8192",
            },
            clear=True,
        ):
            config = load_legacy_office_config()

        self.assertTrue(config.enabled)
        self.assertEqual(executable, config.executable)
        self.assertEqual(180.0, config.timeout_seconds)
        self.assertEqual(2, config.max_concurrency)
        self.assertEqual(4096, config.max_input_bytes)
        self.assertEqual(8192, config.max_output_bytes)

    def test_loader_rejects_ambiguous_or_unsafe_values(self) -> None:
        invalid_cases = (
            ("DOCSENSE_LEGACY_OFFICE_ENABLED", "perhaps"),
            ("DOCSENSE_LIBREOFFICE_EXECUTABLE", "relative/soffice"),
            ("DOCSENSE_LIBREOFFICE_ALLOWED_VERSION_SERIES", ""),
            ("DOCSENSE_LEGACY_OFFICE_TIMEOUT_SECONDS", "infinite"),
            ("DOCSENSE_LEGACY_OFFICE_MAX_CONCURRENCY", "0"),
            ("DOCSENSE_LEGACY_OFFICE_MAX_INPUT_BYTES", "1.5"),
            ("DOCSENSE_LEGACY_OFFICE_MAX_OUTPUT_BYTES", "-1"),
        )
        for name, value in invalid_cases:
            with self.subTest(name=name, value=value), patch.dict(
                os.environ,
                {name: value},
                clear=True,
            ):
                with self.assertRaises(LegacyOfficeConfigurationError):
                    load_legacy_office_config()

    def test_disabled_container_does_not_run_version_probe(self) -> None:
        preparer = MagicMock()
        preparer.sweep_stale_jobs.return_value = 0
        config = LegacyOfficeConfig.disabled(
            jobs_root=Path("/tmp/docsense-office-disabled"),
        )

        with (
            patch(
                "app.container.load_legacy_office_config",
                return_value=config,
            ),
            patch(
                "app.container.LibreOfficeLegacyOfficePreparer",
                return_value=preparer,
            ),
            patch(
                "app.container.load_weaponry_infrastructure_config",
                return_value=MagicMock(),
            ),
            patch(
                "app.container.load_reassignment_infrastructure_config",
                return_value=MagicMock(),
            ),
            patch(
                "app.container.load_anythingllm_config",
                side_effect=_StopAfterLegacyOfficePreflight,
            ),
            self.assertRaises(_StopAfterLegacyOfficePreflight),
        ):
            create_application_services()

        preparer.sweep_stale_jobs.assert_called_once_with()
        preparer.preflight.assert_not_called()

    def test_enabled_container_runs_version_gate_before_external_config(self) -> None:
        preparer = MagicMock()
        preparer.sweep_stale_jobs.return_value = 2
        preparer.preflight.return_value = "LibreOffice 26.2.5.2"
        config = LegacyOfficeConfig(
            enabled=True,
            executable="/Applications/LibreOffice.app/Contents/MacOS/soffice",
            jobs_root=Path("/tmp/docsense-office-enabled"),
        )
        load_anythingllm = MagicMock(
            side_effect=_StopAfterLegacyOfficePreflight
        )

        with (
            patch(
                "app.container.load_legacy_office_config",
                return_value=config,
            ),
            patch(
                "app.container.LibreOfficeLegacyOfficePreparer",
                return_value=preparer,
            ),
            patch(
                "app.container.load_weaponry_infrastructure_config",
                return_value=MagicMock(),
            ),
            patch(
                "app.container.load_reassignment_infrastructure_config",
                return_value=MagicMock(),
            ),
            patch(
                "app.container.load_anythingllm_config",
                load_anythingllm,
            ),
            self.assertRaises(_StopAfterLegacyOfficePreflight),
        ):
            create_application_services()

        preparer.sweep_stale_jobs.assert_called_once_with()
        preparer.preflight.assert_called_once_with()
        load_anythingllm.assert_called_once_with()

    def test_enabled_container_rejects_preflight_failures_before_external_config(
        self,
    ) -> None:
        cases = (
            "executable_not_found",
            "development_version_rejected",
            "version_not_allowed",
        )
        for error_code in cases:
            with self.subTest(error_code=error_code):
                preparer = MagicMock()
                preparer.sweep_stale_jobs.return_value = 0
                preparer.preflight.side_effect = LegacyOfficeConversionError(
                    error_code,
                    diagnostic="sanitized diagnostic",
                )
                config = LegacyOfficeConfig(
                    enabled=True,
                    executable="/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    jobs_root=Path("/tmp/docsense-office-rejected"),
                )
                load_anythingllm = MagicMock()

                with (
                    patch(
                        "app.container.load_legacy_office_config",
                        return_value=config,
                    ),
                    patch(
                        "app.container.LibreOfficeLegacyOfficePreparer",
                        return_value=preparer,
                    ),
                    patch(
                        "app.container.load_weaponry_infrastructure_config",
                        return_value=MagicMock(),
                    ),
                    patch(
                        "app.container.load_reassignment_infrastructure_config",
                        return_value=MagicMock(),
                    ),
                    patch(
                        "app.container.load_anythingllm_config",
                        load_anythingllm,
                    ),
                    self.assertRaises(LegacyOfficeConversionError) as captured,
                ):
                    create_application_services()

                self.assertEqual(error_code, captured.exception.code)
                self.assertEqual(
                    "Legacy Office 文件本地转换失败",
                    str(captured.exception),
                )
                self.assertIsNone(captured.exception.__cause__)
                preparer.preflight.assert_called_once_with()
                load_anythingllm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
