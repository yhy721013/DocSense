"""阶段 1F-2/1F-5B：文件分析 Web Adapter 与路由结构门禁。"""

from __future__ import annotations

import ast
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.web.flask.analysis_requests import (
    AnalysisRequestValidationError,
    parse_analysis_flask_request,
    parse_analysis_payload,
)
from app.adapters.web.flask.analysis_submission import (
    AnalysisSubmissionResponsePresenter,
)
from app.modules.analysis.domain.task_inputs import AnalysisPolicySnapshot
from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisExecutionRef,
)
from app.modules.tasks.domain import TaskId


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "contracts" / "stage1f0_analysis_contracts.json"
LLM_ROUTE_PATH = ROOT / "app" / "blueprints" / "llm.py"
FLASK_ADAPTER_INIT_PATH = ROOT / "app" / "adapters" / "web" / "flask" / "__init__.py"


class _FakeFlaskRequest:
    """只实现解析器需要的 Flask 请求表面，避免测试构造真实应用。"""

    def __init__(
        self,
        *,
        content_length: int | None,
        payload: object = None,
        raw_body: bytes = b"",
    ) -> None:
        self.content_length = content_length
        self._payload = payload
        self.stream = io.BytesIO(raw_body)

    def get_json(self, *, silent: bool = False) -> object:
        if not silent:
            raise AssertionError("解析器必须保持 silent=True")
        return self._payload


def _valid_payload(file_name: str = "adapter-demo.txt") -> dict[str, object]:
    """构造最小合法公开请求，不包含任何内部任务或执行身份。"""

    return {
        "businessType": "file",
        "params": [
            {
                "fileName": file_name,
                "filePath": f"https://example.invalid/{file_name}",
            }
        ],
    }


class AnalysisRequestParserTests(unittest.TestCase):
    """逐项比对 1F-0 黄金资产的同步入站错误和快照语义。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))[
            "analysisSubmission"
        ]

    def test_payload_validation_errors_match_frozen_contract(self) -> None:
        presenter = AnalysisSubmissionResponsePresenter()
        for case in self.contract["validationCases"]:
            with self.subTest(case=case["id"]):
                with self.assertRaises(AnalysisRequestValidationError) as captured:
                    parse_analysis_payload(case["payload"])
                presented = presenter.present_validation_error(captured.exception)
                self.assertEqual(case["status"], presented.status_code)
                self.assertEqual(case["body"], presented.body)

        too_many = _valid_payload("too-many-0.txt")
        too_many["params"] = [
            {
                "fileName": f"too-many-{index}.txt",
                "filePath": f"https://example.invalid/too-many-{index}.txt",
            }
            for index in range(self.contract["limits"]["paramsMax"] + 1)
        ]
        with self.assertRaises(AnalysisRequestValidationError) as captured:
            parse_analysis_payload(too_many)
        presented = presenter.present_validation_error(captured.exception)
        self.assertEqual(self.contract["tooManyParams"]["status"], presented.status_code)
        self.assertEqual(self.contract["tooManyParams"]["body"], presented.body)

    def test_strict_json_validation_and_frozen_snapshot_preserve_unknown_fields(self) -> None:
        invalid_json_value = _valid_payload()
        invalid_json_value["params"][0]["notFinite"] = float("nan")  # type: ignore[index]
        with self.assertRaisesRegex(
            AnalysisRequestValidationError,
            "请求JSON包含非法数值或Unicode字符",
        ):
            parse_analysis_payload(invalid_json_value)

        payload = _valid_payload(" adapter-demo.txt")
        payload["params"][0]["unknownExtension"] = {  # type: ignore[index]
            "empty": "",
            "items": ["first", {"nested": "original"}],
        }
        parsed = parse_analysis_payload(payload)
        projection_params = parsed.request_projection.get("params")
        self.assertIs(parsed.params[0], projection_params.values[0])  # type: ignore[union-attr]
        payload["params"][0]["unknownExtension"]["items"][1]["nested"] = "mutated"  # type: ignore[index]

        self.assertEqual(" adapter-demo.txt", parsed.params[0].to_dict()["fileName"])
        self.assertEqual(
            "original",
            parsed.params[0].to_dict()["unknownExtension"]["items"][1]["nested"],  # type: ignore[index]
        )
        command = parsed.to_batch_command(
            policy_snapshot=AnalysisPolicySnapshot.default(),
            trace_id="analysis-web-adapter-trace",
        )
        self.assertEqual("adapter-demo.txt", command.submissions[0].file_name)
        self.assertIs(parsed.params[0], command.submissions[0].raw_params)
        self.assertEqual(
            "original",
            command.submissions[0].raw_params.to_dict()["unknownExtension"]["items"][1]["nested"],  # type: ignore[index]
        )

    def test_original_file_name_presence_and_raw_value_semantics_are_frozen(self) -> None:
        """缺失、显式空值和业务原值必须保持既有差异，不能被上传命名规则回写。"""

        cases = (
            ("missing", object(), False, ""),
            ("null", None, True, ""),
            ("empty", "", True, ""),
            ("blank", "   ", True, ""),
            (
                "business-value",
                " Nimitz (CVN 68) class.pdf",
                True,
                " Nimitz (CVN 68) class.pdf",
            ),
        )
        for case_id, original_name, expected_present, expected_value in cases:
            with self.subTest(case=case_id):
                payload = _valid_payload(f"{case_id}.txt")
                params = payload["params"][0]  # type: ignore[index]
                if case_id != "missing":
                    params["originalFileName"] = original_name
                parsed = parse_analysis_payload(payload)
                command = parsed.to_batch_command(
                    policy_snapshot=AnalysisPolicySnapshot.default(),
                    trace_id=f"analysis-original-name-{case_id}",
                )
                submission = command.submissions[0]

                self.assertEqual(expected_present, submission.original_file_name_present)
                self.assertEqual(expected_value, submission.original_file_name)
                raw_params = submission.raw_params.to_dict()
                if case_id == "missing":
                    self.assertNotIn("originalFileName", raw_params)
                else:
                    self.assertEqual(original_name, raw_params["originalFileName"])

    def test_flask_request_reader_preserves_bounded_and_malformed_json_boundaries(self) -> None:
        with self.assertRaises(AnalysisRequestValidationError) as captured:
            parse_analysis_flask_request(
                _FakeFlaskRequest(
                    content_length=self.contract["limits"]["requestBytes"] + 1,
                )
            )
        self.assertEqual(413, captured.exception.status_code)
        self.assertEqual("请求体过大", str(captured.exception))

        # 用较小阈值覆盖 chunked 分支，避免离线测试分配 64 MiB 以上无意义内存。
        with patch(
            "app.adapters.web.flask.analysis_requests.MAX_ANALYSIS_REQUEST_BYTES",
            5,
        ):
            with self.assertRaises(AnalysisRequestValidationError) as captured:
                parse_analysis_flask_request(
                    _FakeFlaskRequest(content_length=None, raw_body=b"123456")
                )
        self.assertEqual(413, captured.exception.status_code)

        # JSONDecodeError 在旧路由中会先降为 {}, 随后按 businessType 错误返回；适配器
        # 必须保留这个已有优先级，不能擅自替换为新的“JSON 格式错误”。
        with self.assertRaises(AnalysisRequestValidationError) as captured:
            parse_analysis_flask_request(
                _FakeFlaskRequest(content_length=None, raw_body=b"{")
            )
        self.assertEqual(400, captured.exception.status_code)
        self.assertEqual("businessType必须为file", str(captured.exception))

    def test_unexpected_freeze_failure_is_not_misreported_as_public_400(self) -> None:
        """只把已知合同错误映射为 400，编程错误必须穿透到统一异常边界。"""

        with patch(
            "app.adapters.web.flask.analysis_requests.FrozenJsonObject.from_mapping",
            side_effect=RuntimeError("unexpected-freeze-failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected-freeze-failure"):
                parse_analysis_payload(_valid_payload())


class AnalysisSubmissionPresenterTests(unittest.TestCase):
    """锁定内部受理结果到既有空 202、冲突和繁忙响应的映射。"""

    def setUp(self) -> None:
        self.presenter = AnalysisSubmissionResponsePresenter()
        self.execution = AnalysisExecutionRef(
            task_id=TaskId("analysis-web-presenter"),
            file_name="adapter-demo.txt",
            batch_id="2" * 32,
            batch_sequence=1,
        )

    def test_admission_presentation_matches_contract_without_internal_ids(self) -> None:
        cases = (
            (
                AnalysisBatchAdmissionOutcome.ACCEPTED,
                202,
                None,
            ),
            (
                AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE,
                409,
                {"error": "任务正在处理中"},
            ),
            (
                AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING,
                409,
                {"error": "上一次任务回调尚未结束"},
            ),
            (
                AnalysisBatchAdmissionOutcome.BUSY,
                503,
                {"error": "任务服务繁忙，请稍后重试"},
            ),
        )
        for outcome, expected_status, expected_body in cases:
            with self.subTest(outcome=outcome):
                executions = (self.execution,) if outcome is AnalysisBatchAdmissionOutcome.ACCEPTED else ()
                response = self.presenter.present_admission(
                    AnalysisBatchAdmission(outcome=outcome, executions=executions)
                )
                self.assertEqual(expected_status, response.status_code)
                self.assertEqual(expected_body, response.body)
                self.assertNotIn("task_id", response.body or {})
                self.assertNotIn("batch_id", response.body or {})

    def test_validation_and_unhandled_failure_are_not_reclassified(self) -> None:
        validation_response = self.presenter.present_validation_error(
            AnalysisRequestValidationError("请求体过大", status_code=413)
        )
        self.assertEqual(413, validation_response.status_code)
        self.assertEqual({"error": "请求体过大"}, validation_response.body)
        with self.assertRaisesRegex(RuntimeError, "legacy-unhandled"):
            self.presenter.raise_unhandled(RuntimeError("legacy-unhandled"))


class AnalysisRouteIsolationTests(unittest.TestCase):
    """确认 1F-5B 路由只保留新 Application 链路，不得回退旧执行器。"""

    def test_route_uses_parser_submit_presenter_without_legacy_execution(self) -> None:
        source = LLM_ROUTE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(LLM_ROUTE_PATH))
        route = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "llm_analysis"
        )
        route_text = ast.get_source_segment(source, route) or ""

        self.assertIn("parse_analysis_flask_request(request)", route_text)
        self.assertIn("analysis_submit.execute(command)", route_text)
        self.assertIn("presenter.present_admission(admission)", route_text)
        self.assertIn("_analysis_http_response", route_text)
        self.assertIn("AnalysisSubmissionResponsePresenter", source)

        # 公开路由不能重新成为任务协调和外部副作用 owner。这里既锁定旧线程入口，也
        # 锁定常见的“临时直接访问容器服务”回退，避免以后重构时悄悄绕过批量事务。
        for forbidden in (
            "threading.Thread",
            "task_service",
            "progress_hub",
            "document_rag_factory",
            "knowledge_index_factory",
            "run_file_analysis_task",
            "run_file_analysis_batch_task",
            "replay_callback_if_needed",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, route_text)
        self.assertNotIn("run_file_analysis_task", source)
        self.assertNotIn("run_file_analysis_batch_task", source)

        flask_init_source = FLASK_ADAPTER_INIT_PATH.read_text(encoding="utf-8")
        self.assertIn("analysis_requests", flask_init_source)
        self.assertIn("analysis_submission", flask_init_source)


if __name__ == "__main__":
    unittest.main()
