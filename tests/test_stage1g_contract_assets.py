"""阶段 1G-0 的公开路由与 Debug 内部契约黄金测试。"""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import create_app
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _ROOT / "tests" / "contracts" / "stage1g_debug_contract.json"


class Stage1GContractAssetTests(unittest.TestCase):
    """使用完全离线容器验证路由和 Debug 响应，不启动后台线程。"""

    def setUp(self) -> None:
        self.contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
        self._tempdir = workspace_tempdir()
        self.root = Path(self._tempdir.__enter__())
        self.services = build_offline_application_services(
            self.root / "application"
        )
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.services.close()
        self._tempdir.__exit__(None, None, None)

    def test_contract_asset_has_explicit_authority_and_immutable_parameters(self) -> None:
        self.assertEqual(1, self.contract["schemaVersion"])
        self.assertEqual(
            "docs/接口文档/",
            self.contract["authority"]["publicContract"],
        )
        self.assertFalse(
            self.contract["authority"]["publicParametersMutable"]
        )
        self.assertEqual(
            "808f3a109c1e56df32ac7abfde8ef625b846a2dd",
            self.contract["implementationBaseline"]["stage1g0Commit"],
        )

    def test_flask_route_map_matches_frozen_stage1g_baseline(self) -> None:
        actual = sorted(
            {
                (method, rule.rule)
                for rule in self.app.url_map.iter_rules()
                if rule.rule != "/static/<path:filename>"
                for method in rule.methods
                if method not in {"HEAD", "OPTIONS"}
            }
        )
        expected = sorted(
            (item["method"], item["path"])
            for item in self.contract["routes"]
        )

        self.assertEqual(expected, actual)

    def test_empty_callback_debug_response_matches_frozen_shape(self) -> None:
        callback_dir = self.root / "callback"
        with patch(
            "app.modules.debug.adapters.callback_history.CALLBACK_HISTORY_DIR",
            callback_dir,
        ):
            response = self.client.get("/debug/api/callback")

        payload = response.get_json()
        contract = self.contract["callbackDebug"]
        self.assertEqual(contract["statusCode"], response.status_code)
        self.assertEqual(contract["topLevelFields"], sorted(payload))
        self.assertFalse(payload["ok"])
        self.assertEqual(contract["emptyMessage"], payload["message"])
        self.assertEqual([], payload["records"])
        self.assertIsNone(payload["payload"])
        self.assertIsNone(payload["selectedRecord"])

    def test_empty_chat_debug_response_matches_frozen_shape(self) -> None:
        response = self.client.get("/debug/api/chat/bootstrap")
        payload = response.get_json()
        contract = self.contract["chatDebug"]

        self.assertEqual(contract["statusCode"], response.status_code)
        self.assertEqual(contract["topLevelFields"], sorted(payload))
        self.assertEqual(contract["dataFields"], sorted(payload["data"]))
        self.assertEqual([], payload["data"]["sessions"])
        self.assertEqual([], payload["data"]["availableFiles"])

    def test_debug_pages_keep_frozen_api_dependencies(self) -> None:
        for route, markers in self.contract["pages"].items():
            with self.subTest(route=route):
                response = self.client.get(route)
                html = response.get_data(as_text=True)
                self.assertEqual(200, response.status_code)
                for marker in markers:
                    self.assertIn(marker, html)

    def test_debug_contract_does_not_introduce_unknown_query_fields(self) -> None:
        self.assertEqual(
            ["record"],
            self.contract["callbackDebug"]["queryFields"],
        )
        self.assertEqual([], self.contract["chatDebug"]["queryFields"])


if __name__ == "__main__":
    unittest.main()
