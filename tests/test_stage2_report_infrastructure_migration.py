"""阶段 2-4 第 3 步中可独立验证的基础设施等价迁移测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
import zipfile

from app.modules.report.adapters.docx_template import extract_docx_template_text
from app.modules.report.adapters.runtime_config import (
    ReportExecutionCapabilityConfig,
    ReportRuntimeConfigurationError,
    load_report_execution_capability_config,
    load_report_runtime_config,
)
from app.modules.report.adapters.execution_profile_factory import (
    build_report_execution_profile,
)
from app.services.core.config import load_report_infrastructure_config
from tests import workspace_tempdir
from tests.document_processing_fixtures import build_test_document_preparer


class ReportRuntimeConfigMigrationTests(unittest.TestCase):
    def test_defaults_equal_legacy_config(self) -> None:
        """迁移前后十一项默认值必须逐字段一致。"""

        legacy = load_report_infrastructure_config()
        migrated = load_report_runtime_config({})

        for name in migrated.__dataclass_fields__:
            self.assertEqual(getattr(legacy, name), getattr(migrated, name), name)
        self.assertEqual(64, len(migrated.fingerprint))

    def test_existing_environment_keys_keep_their_values(self) -> None:
        environment = {
            "DOCSENSE_REPORT_RUNTIME_MODE": "single_instance",
            "DOCSENSE_REPORT_SCAN_INTERVAL_SECONDS": "2.5",
            "DOCSENSE_REPORT_ACCEPTED_BATCH_SIZE": "12",
            "DOCSENSE_REPORT_DISPATCH_FAILURE_RETRY_SECONDS": "31",
            "DOCSENSE_REPORT_RESOURCE_SWEEP_INTERVAL_SECONDS": "32",
            "DOCSENSE_REPORT_RESOURCE_SWEEP_LIMIT": "13",
            "DOCSENSE_REPORT_RUNNING_SAMPLE_LIMIT": "14",
            "DOCSENSE_REPORT_STOP_TIMEOUT_SECONDS": "6",
            "DOCSENSE_REPORT_CLEANUP_HTTP_TIMEOUT_SECONDS": "20",
            "DOCSENSE_REPORT_CLEANUP_LEASE_SECONDS": "50",
            "DOCSENSE_REPORT_MAX_DOWNLOAD_BYTES": "4096",
        }

        config = load_report_runtime_config(environment)

        self.assertEqual(2.5, config.scan_interval_seconds)
        self.assertEqual(12, config.accepted_batch_size)
        self.assertEqual(4096, config.max_download_bytes)

    def test_invalid_values_fail_closed(self) -> None:
        with self.assertRaises(ReportRuntimeConfigurationError):
            load_report_runtime_config(
                {"DOCSENSE_REPORT_ACCEPTED_BATCH_SIZE": "12.5"}
            )
        with self.assertRaises(ReportRuntimeConfigurationError):
            load_report_runtime_config(
                {"DOCSENSE_REPORT_CLEANUP_HTTP_TIMEOUT_SECONDS": "inf"}
            )

    def test_v2_deployment_fingerprints_are_required_and_canonical(self) -> None:
        with self.assertRaises(ReportRuntimeConfigurationError):
            load_report_execution_capability_config({})

        capabilities = load_report_execution_capability_config(
            {
                "DOCSENSE_REPORT_RAG_PROVIDER_FINGERPRINT": "A" * 64,
                "DOCSENSE_REPORT_RAG_MODEL_FINGERPRINT": "b" * 64,
            }
        )

        self.assertEqual("a" * 64, capabilities.rag_provider_fingerprint)
        self.assertEqual("b" * 64, capabilities.rag_model_fingerprint)

    def test_execution_profile_uses_actual_document_pipeline_identity(self) -> None:
        with workspace_tempdir() as first_tmp, workspace_tempdir() as second_tmp:
            first_preparer = build_test_document_preparer(Path(first_tmp) / "dp")
            second_preparer = build_test_document_preparer(Path(second_tmp) / "dp")
            capabilities = ReportExecutionCapabilityConfig("a" * 64, "b" * 64)

            first = build_report_execution_profile(
                runtime_config=load_report_runtime_config({}),
                capabilities=capabilities,
                document_preparer=first_preparer,
            )
            second = build_report_execution_profile(
                runtime_config=load_report_runtime_config({}),
                capabilities=capabilities,
                document_preparer=second_preparer,
            )

        self.assertEqual(first, second)
        self.assertEqual(first_preparer.execution_profile_id, first.document_processing_profile_id)
        self.assertEqual(
            first_preparer.execution_profile_fingerprint,
            first.document_processing_fingerprint,
        )
        self.assertEqual(64, len(first.rag_workspace_settings_fingerprint))
        self.assertEqual(64, len(first.rag_upload_policy_fingerprint))


class ReportDocxAdapterMigrationTests(unittest.TestCase):
    def test_docx_body_table_header_and_footer_order_is_stable(self) -> None:
        with workspace_tempdir() as tmp:
            path = Path(tmp) / "template.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "word/document.xml",
                    """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>""",
                )
                archive.writestr(
                    "word/header1.xml",
                    """<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉</w:t></w:r></w:p></w:hdr>""",
                )
                archive.writestr(
                    "word/footer1.xml",
                    """<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页脚</w:t></w:r></w:p></w:ftr>""",
                )

            result = extract_docx_template_text(str(path))

        self.assertEqual("正文\n表格\n页脚\n页眉", result)


if __name__ == "__main__":
    unittest.main()
