"""阶段 1A-1：``/llm/check-task`` 已接线的公开契约测试。

当前路由已经采用批准的 HTTP 200 严格空成功体：参数校验与单项 404 保持既有错误
结构，批量缺失项继续处理其余任务，必要的回调恢复仍在本次请求内完成。任务状态、
进度、回调尝试结果均不得再通过成功体公开。

所有用例均注入临时 SQLite 和离线应用容器，回调发送使用 Mock，不会访问真实网络或
启动 ``run.py``。
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app import create_app
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


_TARGET_CONTRACT_PATH = (
    Path(__file__).with_name("contracts") / "stage0_contracts.json"
)
_CALLBACK_URL = "http://callback.invalid/llm/callback"


class CheckTaskRouteContractTests(unittest.TestCase):
    """验证已批准的 check-task 路由行为与内部恢复副作用。"""

    def setUp(self) -> None:
        self._tempdir = workspace_tempdir()
        self.runtime_directory = self._tempdir.__enter__()
        self.services = build_offline_application_services(self.runtime_directory)
        self.task_service = self.services.task_service
        self.client = create_app(services=self.services).test_client()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _client_with_callback_url(self, callback_url: str):
        """复用同一临时任务库，仅替换测试应用中的回调配置。"""

        configured_services = replace(
            self.services,
            llm_config=replace(
                self.services.llm_config,
                callback_url=callback_url,
            ),
        )
        return create_app(services=configured_services).test_client()

    def _complete_file_task(self, file_name: str) -> None:
        """建立可触发回调恢复的文件终态任务。"""

        self.task_service.create_file_task(
            file_name,
            {
                "businessType": "file",
                "params": [{"fileName": file_name}],
            },
        )
        self.task_service.mark_business_completed(
            "file",
            file_name,
            {
                "businessType": "file",
                "data": {"fileName": file_name, "status": "2"},
                "msg": "解析成功",
            },
            status="2",
        )

    def test_rejects_invalid_or_missing_business_type(self) -> None:
        """缺失或非法 businessType 必须保持 HTTP 400 与既有错误体。"""

        payloads = (
            {},
            {"businessType": "unknown", "params": [{}]},
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/llm/check-task", json=payload)
                self.assertEqual(400, response.status_code)
                self.assertEqual({"error": "businessType无效"}, response.get_json())

    def test_rejects_missing_empty_non_array_and_non_object_params(self) -> None:
        """params 必须是非空对象数组，混合元素不能再被静默过滤。"""

        invalid_params = (
            None,
            [],
            {},
            "not-an-array",
            [None],
            [1, "invalid"],
        )

        for params in invalid_params:
            with self.subTest(params=params):
                payload = {"businessType": "file"}
                if params is not None:
                    payload["params"] = params

                response = self.client.post("/llm/check-task", json=payload)
                self.assertEqual(400, response.status_code)
                self.assertEqual({"error": "params不能为空"}, response.get_json())

    def test_mixed_params_rejects_entire_request(self) -> None:
        """任一非对象元素必须拒绝整次请求，不能只处理其中的合法项。"""

        self.task_service.create_file_task(
            "valid.pdf",
            {"businessType": "file"},
            status="1",
        )

        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "file",
                "params": [None, {"fileName": "valid.pdf"}, "ignored"],
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual({"error": "params不能为空"}, response.get_json())

    def test_rejects_missing_business_key_for_each_current_type(self) -> None:
        """三类当前实现分别校验自己的业务键；weaponry 仅属内部兼容。"""

        cases = (
            ("file", "fileName不能为空"),
            ("report", "reportId不能为空"),
            ("weaponry", "architectureId不能为空"),
        )

        for business_type, error_message in cases:
            with self.subTest(business_type=business_type):
                response = self.client.post(
                    "/llm/check-task",
                    json={"businessType": business_type, "params": [{}]},
                )
                self.assertEqual(400, response.status_code)
                self.assertEqual({"error": error_message}, response.get_json())

    def test_single_missing_public_task_returns_404(self) -> None:
        """file/report 单项未命中继续返回既有 HTTP 404 JSON。"""

        cases = (
            ("file", {"fileName": "missing.pdf"}),
            ("report", {"reportId": 404}),
        )

        for business_type, params in cases:
            with self.subTest(business_type=business_type):
                response = self.client.post(
                    "/llm/check-task",
                    json={"businessType": business_type, "params": [params]},
                )
                self.assertEqual(404, response.status_code)
                self.assertEqual({"error": "任务不存在"}, response.get_json())

    def test_success_is_empty_body_for_all_public_business_types(self) -> None:
        """三类业务键均可检查，但成功体不能泄露任务状态或内部身份。"""

        self.task_service.create_file_task(
            "demo.pdf",
            {"businessType": "file", "params": [{"fileName": "demo.pdf"}]},
            status="1",
        )
        self.task_service.create_report_task(
            132,
            {"businessType": "report", "params": [{"reportId": 132}]},
        )
        self.task_service.create_weaponry_task(
            10502,
            {
                "businessType": "weaponry",
                "params": {"architectureId": 10502},
            },
        )

        cases = (
            ("file", {"fileName": "demo.pdf"}),
            ("report", {"reportId": "00132"}),
            ("weaponry", {"architectureId": 10502}),
        )

        for business_type, params in cases:
            with self.subTest(business_type=business_type):
                response = self.client.post(
                    "/llm/check-task",
                    json={"businessType": business_type, "params": [params]},
                )
                self.assertEqual(200, response.status_code)
                self.assertEqual(b"", response.data)

    def test_report_id_has_no_64_bit_range_limit(self) -> None:
        """超 64 位的兼容整数字符串可查找，但成功体仍严格为空。"""

        report_id = 10**80 + 132
        self.task_service.create_report_task(
            report_id,
            {
                "businessType": "report",
                "params": [{"reportId": report_id}],
            },
        )

        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "report",
                "params": [{"reportId": f"+000{report_id}"}],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)

    def test_weaponry_id_uses_same_canonical_rule_as_submission_and_progress(self) -> None:
        self.task_service.create_weaponry_task(
            10502,
            {
                "businessType": "weaponry",
                "params": {"architectureId": 10502},
            },
        )

        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "weaponry",
                "params": [{"architectureId": "00010502"}],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)

    def test_weaponry_id_rejects_unapproved_forms_with_400(self) -> None:
        for invalid in (True, 1.0, " 1 ", "+1", 0, -1, [], {}):
            with self.subTest(invalid=invalid):
                response = self.client.post(
                    "/llm/check-task",
                    json={
                        "businessType": "weaponry",
                        "params": [{"architectureId": invalid}],
                    },
                )

                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {
                        "error": "architectureId必须为1到9223372036854775807之间的正整数"
                    },
                    response.get_json(),
                )

    def test_report_id_rejects_non_integer_values_with_400(self) -> None:
        for invalid in (True, 132.0, "132.0", "not-an-integer", [], {}):
            with self.subTest(invalid=invalid):
                response = self.client.post(
                    "/llm/check-task",
                    json={
                        "businessType": "report",
                        "params": [{"reportId": invalid}],
                    },
                )

                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {"error": "reportId必须是整数或整数字符串"},
                    response.get_json(),
                )

    def test_report_id_rejects_more_than_128_digits_with_400(self) -> None:
        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "report",
                "params": [{"reportId": "9" * 129}],
            },
        )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "reportId不能超过128位十进制数字"},
            response.get_json(),
        )

    def test_batch_all_existing_returns_empty_success(self) -> None:
        """批量检查存在项后只返回空成功体，不公开数据库顺序或状态。"""

        self.task_service.create_file_task(
            "a.pdf",
            {"businessType": "file"},
            status="1",
        )
        self.task_service.create_file_task(
            "b.pdf",
            {"businessType": "file"},
            status="0",
        )

        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "file",
                "params": [{"fileName": "b.pdf"}, {"fileName": "a.pdf"}],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)

    def test_batch_partial_missing_keeps_existing_item_processing(self) -> None:
        """批量缺失项不终止其余检查，最终仍返回空成功体。"""

        self.task_service.create_file_task(
            "first.pdf",
            {"businessType": "file"},
            status="1",
        )
        self.task_service.create_file_task(
            "last.pdf",
            {"businessType": "file"},
            status="0",
        )

        response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "file",
                "params": [
                    {"fileName": "first.pdf"},
                    {"fileName": "missing.pdf"},
                    {"fileName": "last.pdf"},
                ],
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)

    def test_pending_callback_is_replayed_and_persisted_as_success(self) -> None:
        """终态 pending 回调在 check-task 中成功补发后必须持久化 success。"""

        self._complete_file_task("pending.pdf")
        client = self._client_with_callback_url(_CALLBACK_URL)

        with patch(
            "app.services.llm_service.task_service.post_callback_payload",
            return_value=True,
        ) as callback_sender:
            response = client.post(
                "/llm/check-task",
                json={
                    "businessType": "file",
                    "params": [{"fileName": "pending.pdf"}],
                },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)
        self.assertEqual(
            "success",
            self.task_service.get_task("file", "pending.pdf")["callback_status"],
        )
        callback_sender.assert_called_once()

    def test_failed_callback_is_replayed_and_persisted_as_success(self) -> None:
        """终态 failed 回调也允许由 check-task 显式恢复一次。"""

        self._complete_file_task("failed-then-success.pdf")
        self.task_service.mark_callback_failed(
            "file",
            "failed-then-success.pdf",
            "timeout",
        )
        client = self._client_with_callback_url(_CALLBACK_URL)

        with patch(
            "app.services.llm_service.task_service.post_callback_payload",
            return_value=True,
        ) as callback_sender:
            response = client.post(
                "/llm/check-task",
                json={
                    "businessType": "file",
                    "params": [{"fileName": "failed-then-success.pdf"}],
                },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)
        self.assertEqual(
            "success",
            self.task_service.get_task("file", "failed-then-success.pdf")[
                "callback_status"
            ],
        )
        callback_sender.assert_called_once()

    def test_failed_replay_remains_failed(self) -> None:
        """补发返回失败时不得伪装成功，内部 callbackStatus 必须仍为 failed。"""

        self._complete_file_task("still-failed.pdf")
        self.task_service.mark_callback_failed("file", "still-failed.pdf", "timeout")
        client = self._client_with_callback_url(_CALLBACK_URL)

        with patch(
            "app.services.llm_service.task_service.post_callback_payload",
            return_value=False,
        ) as callback_sender:
            response = client.post(
                "/llm/check-task",
                json={
                    "businessType": "file",
                    "params": [{"fileName": "still-failed.pdf"}],
                },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)
        self.assertEqual(
            "failed",
            self.task_service.get_task("file", "still-failed.pdf")["callback_status"],
        )
        callback_sender.assert_called_once()

    def test_missing_callback_url_moves_pending_to_skipped_without_replay(self) -> None:
        """无接收方时 pending 应幂等收敛为 skipped，且不制造网络尝试。"""

        self._complete_file_task("no-callback.pdf")

        with patch(
            "app.services.llm_service.task_service.post_callback_payload"
        ) as callback_sender:
            response = self.client.post(
                "/llm/check-task",
                json={
                    "businessType": "file",
                    "params": [{"fileName": "no-callback.pdf"}],
                },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)
        self.assertEqual(
            "skipped",
            self.task_service.get_task("file", "no-callback.pdf")["callback_status"],
        )
        callback_sender.assert_not_called()

    def test_skipped_callback_is_not_replayed_after_url_is_configured(self) -> None:
        """skipped 是明确终态，后续补配 URL 不能追补历史任务。"""

        self._complete_file_task("already-skipped.pdf")
        initial_response = self.client.post(
            "/llm/check-task",
            json={
                "businessType": "file",
                "params": [{"fileName": "already-skipped.pdf"}],
            },
        )
        self.assertEqual(200, initial_response.status_code)
        self.assertEqual(b"", initial_response.data)

        client = self._client_with_callback_url(_CALLBACK_URL)
        with patch(
            "app.services.llm_service.task_service.post_callback_payload"
        ) as callback_sender:
            response = client.post(
                "/llm/check-task",
                json={
                    "businessType": "file",
                    "params": [{"fileName": "already-skipped.pdf"}],
                },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.data)
        callback_sender.assert_not_called()


class CheckTaskContractAssetTests(unittest.TestCase):
    """确认黄金资产已经从目标状态推进为当前已实现状态。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            _TARGET_CONTRACT_PATH.read_text(encoding="utf-8")
        )["checkTask"]

    def test_success_is_http_200_empty_body_and_recovery_side_effect_remains(self) -> None:
        """删除成功 JSON 不能误删同步回调恢复这一内部副作用。"""

        self.assertEqual(200, self.contract["success"]["status"])
        self.assertEqual("", self.contract["success"]["body"])
        self.assertEqual(
            "may_recover_terminal_callback",
            self.contract["success"]["sideEffect"],
        )
        self.assertEqual("implemented", self.contract["implementationStatus"])
        self.assertEqual("1B", self.contract["implementationWave"])

    def test_error_and_batch_missing_policies_remain_frozen(self) -> None:
        """1B 按已批准规则收紧元素校验，其他 400/404 与批量缺失策略保持。"""

        self.assertEqual(400, self.contract["invalidRequestStatus"])
        self.assertEqual(
            {
                "requiredType": "object",
                "onInvalid": "reject_entire_request",
                "status": 400,
            },
            self.contract["paramsElementPolicy"],
        )
        self.assertEqual(404, self.contract["singleMissing"]["status"])
        self.assertEqual(
            "continue_existing_items_and_return_empty_success",
            self.contract["batchMissingPolicy"],
        )
        self.assertEqual(
            ["file", "report", "weaponry"],
            self.contract["documentedBusinessTypes"],
        )


if __name__ == "__main__":
    unittest.main()
