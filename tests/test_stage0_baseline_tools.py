"""阶段 0 容量工具的纯离线安全与统计测试。"""

from __future__ import annotations

import json
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

from scripts.stage0_load_baseline import (
    SampleResult,
    _run_bounded_samples,
    _websocket_sample,
    assert_safe_base_url,
    build_endpoint_url,
    load_workload,
    percentile,
    select_scenarios,
    summarize_results,
    validate_workload,
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

    def test_workload_rejects_invalid_timeouts_events_and_statuses(self) -> None:
        """危险或类型错误的运行参数必须在发出请求前整体失败。"""

        base = {
            "name": "invalid",
            "type": "http",
            "enabled": True,
            "path": "/health",
            "concurrency": 1,
            "totalRequests": 1,
        }
        invalid_fields = (
            {"timeoutSeconds": 0},
            {"timeoutSeconds": float("inf")},
            {"expectedStatuses": []},
            {"expectedStatuses": [True]},
            {"expectedStatuses": [99]},
            {"enabled": "false"},
            {"method": "TRACE"},
        )
        for override in invalid_fields:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    validate_workload({"scenarios": [base | override]})

        invalid_sse = base | {
            "type": "sse",
            "maxEvents": 0,
        }
        with self.assertRaisesRegex(ValueError, "maxEvents"):
            validate_workload({"scenarios": [invalid_sse]})

        invalid_websocket = {
            "name": "invalid-websocket",
            "type": "websocket",
            "enabled": True,
            "path": "/llm/progress",
            "concurrency": 1,
            "holdSeconds": 0,
            "message": {
                "businessType": "file",
                "params": [{"fileName": "probe.pdf"}],
            },
        }
        with self.assertRaisesRegex(ValueError, "holdSeconds"):
            validate_workload({"scenarios": [invalid_websocket]})

        invalid_websocket["holdSeconds"] = 1
        invalid_websocket["message"]["params"] = ["probe.pdf"]
        with self.assertRaisesRegex(ValueError, "message.params"):
            validate_workload({"scenarios": [invalid_websocket]})

    def test_websocket_rejects_unrelated_or_malformed_initial_snapshot(self) -> None:
        class InvalidSnapshotClient:
            connected = True

            def send(self, _message: str) -> None:
                return None

            def receive(self, timeout: float | None = None) -> str:
                return json.dumps(
                    {
                        "businessType": "file",
                        "data": {
                            "fileName": "another.pdf",
                            "progress": 0.1,
                        },
                    }
                )

            def close(self) -> None:
                self.connected = False

        scenario = {
            "path": "/llm/progress",
            "message": {
                "businessType": "file",
                "params": [{"fileName": "probe.pdf"}],
            },
            "receiveTimeoutSeconds": 1,
            "holdSeconds": 0.1,
            "livenessProbeSeconds": 0.1,
        }
        with patch(
            "simple_websocket.Client.connect",
            return_value=InvalidSnapshotClient(),
        ):
            result = _websocket_sample("http://localhost:5001", scenario)

        self.assertFalse(result.succeeded)
        self.assertEqual("ValueError", result.error_type)

    def test_websocket_hold_detects_connection_closed_after_initial_snapshot(self) -> None:
        """收到首帧后立即断开的连接不得再被 sleep 误报为稳定。"""

        class ClosedDuringHoldClient:
            connected = True

            def send(self, _message: str) -> None:
                return None

            def receive(self, timeout: float | None = None) -> str | None:
                if self.connected and not hasattr(self, "initial_received"):
                    self.initial_received = True
                    return json.dumps(
                        {
                            "businessType": "file",
                            "data": {
                                "fileName": "probe.pdf",
                                "progress": 0.1,
                            },
                        }
                    )
                self.connected = False
                return None

            def close(self) -> None:
                self.connected = False

        client = ClosedDuringHoldClient()
        scenario = {
            "path": "/llm/progress",
            "message": {
                "businessType": "file",
                "params": [{"fileName": "probe.pdf"}],
            },
            "receiveTimeoutSeconds": 1,
            "holdSeconds": 0.1,
            "livenessProbeSeconds": 0.1,
        }
        with patch("simple_websocket.Client.connect", return_value=client) as connect:
            result = _websocket_sample("http://localhost:5001", scenario)

        self.assertFalse(result.succeeded)
        self.assertEqual("ConnectionError", result.error_type)
        self.assertIsNotNone(result.ready_latency_ms)
        self.assertEqual(0.1, connect.call_args.kwargs["ping_interval"])

    def test_websocket_hold_reports_ready_and_hold_metrics_separately(self) -> None:
        """稳定连接的首帧耗时不得再与保持窗口混成同一个 P95。"""

        class StableClient:
            connected = True

            def send(self, _message: str) -> None:
                return None

            def receive(self, timeout: float | None = None) -> str | None:
                if not hasattr(self, "initial_received"):
                    self.initial_received = True
                    return json.dumps(
                        {
                            "businessType": "file",
                            "data": {
                                "fileName": "probe.pdf",
                                "progress": 0.1,
                            },
                        }
                    )
                time.sleep(float(timeout or 0))
                return None

            def close(self) -> None:
                self.connected = False

        scenario = {
            "path": "/llm/progress",
            "message": {
                "businessType": "file",
                "params": [{"fileName": "probe.pdf"}],
            },
            "receiveTimeoutSeconds": 1,
            "holdSeconds": 0.1,
            "livenessProbeSeconds": 0.1,
        }
        with patch("simple_websocket.Client.connect", return_value=StableClient()):
            result = _websocket_sample("http://localhost:5001", scenario)

        self.assertTrue(result.succeeded)
        self.assertIsNotNone(result.ready_latency_ms)
        self.assertGreaterEqual(result.hold_duration_ms or 0.0, 90.0)

    def test_summary_separates_success_failure_ready_and_hold_latencies(self) -> None:
        results = (
            SampleResult(True, 100.0, 200),
            SampleResult(True, 200.0, 200),
            SampleResult(
                True,
                1100.0,
                101,
                ready_latency_ms=100.0,
                hold_duration_ms=1000.0,
            ),
            SampleResult(False, 9000.0, error_type="Timeout"),
        )

        summary = summarize_results(
            {"name": "mixed", "type": "http", "concurrency": 2},
            results,
            elapsed_seconds=2.0,
        )

        self.assertEqual(3, summary["latencyMs"]["samples"])
        self.assertEqual(1, summary["failedLatencyMs"]["samples"])
        self.assertEqual(1, summary["readyLatencyMs"]["samples"])
        self.assertEqual(1, summary["holdDurationMs"]["samples"])
        self.assertLess(summary["latencyMs"]["p95"], 9000.0)

    def test_sample_runner_limits_in_flight_future_window(self) -> None:
        class ImmediateExecutor:
            def submit(self, function, *args):  # type: ignore[no-untyped-def]
                future: Future[SampleResult] = Future()
                future.set_result(function(*args))
                return future

        observed_sizes: list[int] = []

        def complete_all(futures, *, return_when):  # type: ignore[no-untyped-def]
            self.assertIsNotNone(return_when)
            observed_sizes.append(len(futures))
            return set(futures), set()

        scenario = {
            "name": "bounded",
            "type": "http",
            "concurrency": 2,
        }
        with patch("scripts.stage0_load_baseline.wait", side_effect=complete_all):
            results = _run_bounded_samples(
                ImmediateExecutor(),  # type: ignore[arg-type]
                sample_function=lambda _base, _scenario: SampleResult(True, 1.0),
                base_url="http://localhost",
                scenario=scenario,
                sample_count=25,
            )

        self.assertEqual(25, len(results))
        self.assertTrue(observed_sizes)
        self.assertLessEqual(max(observed_sizes), 4)


if __name__ == "__main__":
    unittest.main()
