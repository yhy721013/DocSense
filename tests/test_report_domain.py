"""阶段 1C-1 报告领域对象与纯规则的离线测试。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.modules.report.domain import (
    EMPTY_REPORT_HTML,
    REPORT_INPUT_SCHEMA_VERSION,
    REPORT_STATUS_FAILED,
    REPORT_STATUS_SUCCEEDED,
    ReportArtifactError,
    ReportAuditError,
    ReportCallbackError,
    ReportCleanupError,
    ReportCallbackPayload,
    ReportDomainValidationError,
    ReportError,
    ReportId,
    ReportInputError,
    ReportInputSnapshot,
    ReportRagError,
    ReportPortContractError,
    ReportSourceNormalizationError,
    ReportStaleExecutionError,
    ReportSubmission,
    ReportTaskConflictError,
    ReportTaskPersistenceError,
    ReportTemplateError,
    build_report_callback,
    build_report_context_name,
    build_report_conversation_name,
    build_report_prompt,
    build_report_result,
    ensure_report_html,
)


def _submission(*, report_id: int = 132) -> ReportSubmission:
    """构造不依赖 Flask、数据库或网络的标准报告命令。"""

    identity = ReportId.from_public_value(report_id)
    return ReportSubmission(
        report_id=identity,
        source_urls=(
            "http://files.invalid/source-a.pdf",
            "http://files.invalid/source-a.pdf",
        ),
        template_outline_url="http://files.invalid/template.docx",
        template_desc="模板说明",
        requirement="生成综合报告",
        trace_id="trace-report-132",
    )


class ReportIdentityAndInputTests(unittest.TestCase):
    """验证 128 位业务键、输入顺序和不可变快照。"""

    def test_report_id_is_not_limited_to_machine_integer_range(self) -> None:
        huge_value = -(10**120 + 132)

        report_id = ReportId.from_public_value(huge_value)

        self.assertEqual(huge_value, report_id.public_value)
        self.assertEqual(str(huge_value), report_id.business_key)

        with self.assertRaisesRegex(
            ReportDomainValidationError,
            "不能超过128位十进制数字",
        ):
            ReportId.from_public_value(10**128)

        # 超大内部整数也必须在调用 str() 前被领域门禁拒绝，不能泄漏 CPython 转换异常。
        with self.assertRaisesRegex(
            ReportDomainValidationError,
            "不能超过128位十进制数字",
        ):
            ReportId.from_public_value(10**5000)

    def test_report_id_rejects_bool_and_noncanonical_key(self) -> None:
        invalid_values = (
            lambda: ReportId.from_public_value(True),
            lambda: ReportId(public_value=132, business_key="00132"),
            lambda: ReportId(public_value=-2, business_key="-02"),
        )
        for build in invalid_values:
            with self.subTest(build=build):
                with self.assertRaises(ReportDomainValidationError):
                    build()

    def test_submission_copies_source_urls_and_preserves_duplicates(self) -> None:
        mutable_urls = [
            "http://files.invalid/a.pdf",
            "http://files.invalid/a.pdf",
        ]
        submission = ReportSubmission(
            report_id=ReportId.from_public_value(132),
            source_urls=mutable_urls,  # type: ignore[arg-type]
            template_outline_url="  http://files.invalid/template.docx  ",
            template_desc="",
            requirement="",
            trace_id=" trace-132 ",
        )

        mutable_urls.append("http://files.invalid/late.pdf")

        self.assertEqual(
            (
                "http://files.invalid/a.pdf",
                "http://files.invalid/a.pdf",
            ),
            submission.source_urls,
        )
        self.assertEqual(
            "http://files.invalid/template.docx",
            submission.template_outline_url,
        )
        self.assertEqual("trace-132", submission.trace_id)
        with self.assertRaises(FrozenInstanceError):
            submission.requirement = "不能修改"  # type: ignore[misc]

    def test_submission_rejects_invalid_internal_url_values(self) -> None:
        invalid_sources = (
            (),
            "not-a-sequence",
            {"http://files.invalid/a.pdf"},
            {"http://files.invalid/a.pdf": "被误当作 URL"},
            ("",),
            (123,),
        )
        for source_urls in invalid_sources:
            with self.subTest(source_urls=source_urls):
                with self.assertRaises(ReportDomainValidationError):
                    ReportSubmission(
                        report_id=ReportId.from_public_value(132),
                        source_urls=source_urls,  # type: ignore[arg-type]
                        template_outline_url="http://files.invalid/template.docx",
                        template_desc="",
                        requirement="",
                        trace_id="trace-132",
                    )

    def test_input_snapshot_is_recoverable_without_request_dict(self) -> None:
        submission = _submission()

        snapshot = ReportInputSnapshot.from_submission(
            submission,
            task_id="execution-001",
            accepted_at="2026-07-16T15:00:00+08:00",
        )

        self.assertEqual(REPORT_INPUT_SCHEMA_VERSION, snapshot.schema_version)
        self.assertEqual("execution-001", snapshot.task_id)
        self.assertEqual(submission.report_id, snapshot.report_id)
        self.assertEqual(submission.source_urls, snapshot.source_urls)
        self.assertEqual(submission.trace_id, snapshot.trace_id)


class ReportResultAndCallbackTests(unittest.TestCase):
    """验证 HTML、空结果成功语义及公开回调字段完全兼容。"""

    def test_existing_html_is_trimmed_but_not_wrapped(self) -> None:
        self.assertEqual(
            "<section>报告正文</section>",
            ensure_report_html("  <section>报告正文</section>  "),
        )

    def test_plain_text_is_escaped_and_wrapped(self) -> None:
        self.assertEqual(
            '<div class="report-content"><pre>A &lt; B &amp; C</pre></div>',
            ensure_report_html("A < B & C"),
        )

    def test_none_empty_and_whitespace_results_remain_success_eligible(self) -> None:
        report_id = ReportId.from_public_value(132)
        for content in (None, "", "  \r\n\t  "):
            with self.subTest(content=content):
                result = build_report_result(report_id, content)
                callback = build_report_callback(
                    report_id,
                    result.html_details,
                    status=REPORT_STATUS_SUCCEEDED,
                )

                self.assertTrue(result.empty_rag_result)
                self.assertEqual(EMPTY_REPORT_HTML, result.html_details)
                self.assertEqual(
                    {
                        "businessType": "report",
                        "data": {
                            "reportId": 132,
                            "status": "1",
                            "details": EMPTY_REPORT_HTML,
                        },
                        "msg": "生成成功",
                    },
                    callback.to_public_dict(),
                )

    def test_success_and_failure_callbacks_keep_fixed_messages(self) -> None:
        report_id = ReportId.from_public_value(132)

        success = build_report_callback(
            report_id,
            "<div>完成</div>",
            status=REPORT_STATUS_SUCCEEDED,
        )
        failure = build_report_callback(
            report_id,
            "",
            status=REPORT_STATUS_FAILED,
        )

        self.assertEqual("生成成功", success.message)
        self.assertEqual("生成失败", failure.message)
        self.assertEqual("2", failure.to_public_dict()["data"]["status"])  # type: ignore[index]

    def test_callback_rejects_unsupported_status_or_mismatched_message(self) -> None:
        report_id = ReportId.from_public_value(132)
        with self.assertRaises(ReportDomainValidationError):
            build_report_callback(report_id, "", status="0")
        with self.assertRaises(ReportDomainValidationError):
            build_report_callback(report_id, "", status=["1"])  # type: ignore[arg-type]
        with self.assertRaises(ReportDomainValidationError):
            ReportCallbackPayload(
                report_id=report_id,
                status=REPORT_STATUS_SUCCEEDED,
                details="",
                message="生成失败",
            )

    def test_public_payload_returns_independent_mutable_copies(self) -> None:
        callback = build_report_callback(
            ReportId.from_public_value(132),
            "<div>完成</div>",
            status=REPORT_STATUS_SUCCEEDED,
        )

        first = callback.to_public_dict()
        second = callback.to_public_dict()
        first["data"]["details"] = "被调用方修改"  # type: ignore[index]

        self.assertEqual("<div>完成</div>", second["data"]["details"])  # type: ignore[index]


class ReportNamingAndErrorTests(unittest.TestCase):
    """验证任务级外部名称和稳定错误分类。"""

    def test_context_and_conversation_names_are_deterministic(self) -> None:
        report_id = ReportId.from_public_value(-132)

        self.assertEqual(
            "llm-report--132-execution-001",
            build_report_context_name(report_id, " execution-001 "),
        )
        self.assertEqual(
            "report--132",
            build_report_conversation_name(report_id),
        )

    def test_maximum_report_id_uses_bounded_deterministic_provider_names(self) -> None:
        report_id = ReportId.from_public_value(int("9" * 128))
        other_report_id = ReportId.from_public_value(int("8" * 128))

        first = build_report_context_name(report_id, "execution-001")
        repeated = build_report_context_name(report_id, "execution-001")
        other_execution = build_report_context_name(report_id, "execution-002")
        conversation = build_report_conversation_name(report_id)

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_execution)
        self.assertNotEqual(
            first,
            build_report_context_name(other_report_id, "execution-001"),
        )
        self.assertLessEqual(len(first), 96)
        self.assertLessEqual(len(conversation), 64)

    def test_report_prompt_is_exactly_compatible_with_legacy_builder(self) -> None:
        """迁移为强类型参数后不得改动既有 Prompt 文本。"""

        from app.services.core.prompts import build_report_prompt as legacy_builder

        params = {
            "templateDesc": "模板说明",
            "templateOutline": "Word 提取大纲",
            "requirement": "业务要求",
        }
        self.assertEqual(
            legacy_builder(params),
            build_report_prompt(
                template_desc=params["templateDesc"],
                template_outline=params["templateOutline"],
                requirement=params["requirement"],
            ),
        )

    def test_error_codes_and_stages_are_stable(self) -> None:
        expected = {
            ReportTaskConflictError: ("report_task_conflict", "submission"),
            ReportStaleExecutionError: ("report_stale_execution", "execution"),
            ReportInputError: ("report_input_error", "input"),
            ReportTemplateError: ("report_template_error", "template"),
            ReportRagError: ("report_rag_error", "rag"),
            ReportArtifactError: ("report_artifact_error", "artifact"),
            ReportSourceNormalizationError: (
                "report_source_normalization_error",
                "normalization",
            ),
            ReportAuditError: ("report_audit_error", "audit"),
            ReportCallbackError: ("report_callback_error", "callback"),
            ReportCleanupError: ("report_cleanup_error", "cleanup"),
            ReportPortContractError: (
                "report_port_contract_error",
                "application",
            ),
            ReportTaskPersistenceError: (
                "report_task_persistence_error",
                "task_persistence",
            ),
        }
        for error_type, (code, stage) in expected.items():
            with self.subTest(error_type=error_type):
                error = error_type("测试错误")
                self.assertIsInstance(error, ReportError)
                self.assertEqual(code, error.code)
                self.assertEqual(stage, error.stage)


if __name__ == "__main__":
    unittest.main()
