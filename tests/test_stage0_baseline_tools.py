"""阶段 0 容量工具的纯离线安全与统计测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.stage0_load_baseline import (
    assert_safe_base_url,
    build_endpoint_url,
    load_workload,
    percentile,
    select_scenarios,
)


_WORKLOAD_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "stage0_workloads.example.json"
)


class Stage0BaselineToolTests(unittest.TestCase):
    """保证压测工具默认不会越权连接或误触发重型服务。"""

    def test_percentile_uses_stable_linear_interpolation(self) -> None:
        self.assertEqual(0.0, percentile([], 0.95))
        self.assertEqual(2.5, percentile([1.0, 2.0, 3.0, 4.0], 0.5))
        self.assertAlmostEqual(3.85, percentile([1.0, 2.0, 3.0, 4.0], 0.95))

    def test_url_safety_defaults_to_loopback_and_rejects_credentials(self) -> None:
        assert_safe_base_url("http://127.0.0.1:5001", allow_non_loopback=False)
        assert_safe_base_url("http://localhost:5001", allow_non_loopback=False)
        with self.assertRaisesRegex(ValueError, "默认只允许"):
            assert_safe_base_url("http://10.0.0.8:5001", allow_non_loopback=False)
        with self.assertRaisesRegex(ValueError, "不得内嵌"):
            assert_safe_base_url(
                "http://user:secret@localhost:5001",
                allow_non_loopback=False,
            )

    def test_endpoint_path_cannot_override_checked_host(self) -> None:
        self.assertEqual(
            "ws://localhost:5001/llm/progress",
            build_endpoint_url(
                "http://localhost:5001",
                "/llm/progress",
                websocket=True,
            ),
        )
        with self.assertRaisesRegex(ValueError, "站内路径"):
            build_endpoint_url(
                "http://localhost:5001",
                "https://other.example/llm/progress",
            )

    def test_example_contains_two_enabled_50_concurrency_safe_scenarios(self) -> None:
        workload = load_workload(_WORKLOAD_PATH)
        selected = select_scenarios(
            workload,
            names=None,
            allow_heavy_services=False,
        )
        self.assertEqual(
            {"short_history_50", "progress_connections_50"},
            {scenario["name"] for scenario in selected},
        )
        self.assertTrue(all(scenario["concurrency"] >= 50 for scenario in selected))

        websocket = next(item for item in selected if item["type"] == "websocket")
        self.assertNotIn("action", websocket["message"])

    def test_heavy_scenarios_remain_disabled_even_when_heavy_flag_is_allowed(self) -> None:
        workload = json.loads(_WORKLOAD_PATH.read_text(encoding="utf-8"))
        selected = select_scenarios(
            workload,
            names=None,
            allow_heavy_services=True,
        )
        self.assertNotIn("chat_sse_50", {item["name"] for item in selected})
        self.assertNotIn("inflight_tasks_50", {item["name"] for item in selected})

        chat_sse = next(
            item for item in workload["scenarios"] if item["name"] == "chat_sse_50"
        )
        self.assertTrue(chat_sse["templateOnly"])
        self.assertEqual("chat", chat_sse["json"]["businessType"])
        self.assertIn("message", chat_sse["json"]["params"])

        inflight = next(
            item
            for item in workload["scenarios"]
            if item["name"] == "inflight_tasks_50"
        )
        self.assertTrue(inflight["templateOnly"])


if __name__ == "__main__":
    unittest.main()
