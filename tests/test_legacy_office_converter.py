from __future__ import annotations

import os
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.services.utils.legacy_office_converter import (
    LegacyOfficeConversionError,
    _find_soffice,
    _soffice_candidates,
    convert_legacy_office_file,
)
from tests import workspace_tempdir


class LegacyOfficeConverterTests(unittest.TestCase):
    @patch("app.services.utils.legacy_office_converter._find_soffice")
    def test_non_legacy_format_is_unchanged(self, mock_find_soffice):
        result = convert_legacy_office_file("example.docx")

        self.assertEqual(result, "example.docx")
        mock_find_soffice.assert_not_called()

    def test_configured_soffice_path_has_priority(self):
        with workspace_tempdir() as tmp:
            executable = Path(tmp) / "soffice"
            executable.write_text("test", encoding="utf-8")
            executable.chmod(0o755)

            with patch.dict(os.environ, {"SOFFICE_PATH": str(executable)}, clear=False):
                result = _find_soffice()

        self.assertEqual(result, executable)

    def test_platform_candidates_cover_supported_deployments(self):
        expected_candidates = {
            "Windows": "LibreOffice/program/soffice.exe",
            "Darwin": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "Linux": "/usr/lib64/libreoffice/program/soffice",
        }
        environment = {
            "PROGRAMFILES": "C:/Program Files",
            "PROGRAMFILES(X86)": "",
            "LOCALAPPDATA": "",
            "SOFFICE_PATH": "",
        }
        for system_name, expected in expected_candidates.items():
            with (
                self.subTest(system_name=system_name),
                patch.dict(os.environ, environment, clear=False),
                patch(
                    "app.services.utils.legacy_office_converter.platform.system",
                    return_value=system_name,
                ),
                patch(
                    "app.services.utils.legacy_office_converter.shutil.which",
                    return_value=None,
                ),
            ):
                candidates = [str(path).replace("\\", "/") for path in _soffice_candidates(None)]

            self.assertTrue(any(candidate.endswith(expected) for candidate in candidates))

    def test_doc_xls_and_ppt_are_converted_and_validated(self):
        formats = {
            ".doc": (".docx", "word/document.xml"),
            ".xls": (".xlsx", "xl/workbook.xml"),
            ".ppt": (".pptx", "ppt/presentation.xml"),
        }
        for source_suffix, (target_suffix, required_member) in formats.items():
            with self.subTest(source_suffix=source_suffix), workspace_tempdir() as tmp:
                source = Path(tmp) / f"legacy{source_suffix}"
                source.write_bytes(b"legacy office")

                def fake_run(command, **_kwargs):
                    output_dir = Path(command[command.index("--outdir") + 1])
                    generated = output_dir / f"legacy{target_suffix}"
                    with zipfile.ZipFile(generated, "w") as archive:
                        archive.writestr("[Content_Types].xml", "<Types />")
                        archive.writestr(required_member, "<document />")
                    return subprocess.CompletedProcess(command, 0, stdout="converted")

                with (
                    patch(
                        "app.services.utils.legacy_office_converter._find_soffice",
                        return_value=Path("/test/soffice"),
                    ),
                    patch(
                        "app.services.utils.legacy_office_converter.subprocess.run",
                        side_effect=fake_run,
                    ) as mock_run,
                ):
                    result = Path(convert_legacy_office_file(str(source)))

                self.assertEqual(result, source.with_suffix(target_suffix))
                self.assertTrue(result.is_file())
                self.assertTrue(source.is_file())
                command = mock_run.call_args.args[0]
                self.assertIn("--headless", command)
                self.assertIn("-env:UserInstallation=", " ".join(command))

    @patch(
        "app.services.utils.legacy_office_converter._find_soffice",
        return_value=Path("/test/soffice"),
    )
    @patch("app.services.utils.legacy_office_converter.subprocess.run")
    def test_failed_conversion_raises_instead_of_parsing_legacy_file(
        self,
        mock_run,
        _mock_find_soffice,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            ["soffice"],
            1,
            stdout="conversion failed",
        )
        with workspace_tempdir() as tmp:
            source = Path(tmp) / "legacy.doc"
            source.write_bytes(b"legacy office")

            with self.assertRaisesRegex(
                LegacyOfficeConversionError,
                "conversion failed",
            ):
                convert_legacy_office_file(str(source))


if __name__ == "__main__":
    unittest.main()
