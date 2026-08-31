from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.modules.analysis.domain.architecture_tree import build_architecture_tree_index
from app.modules.analysis.domain.architecture_recall import (
    build_document_architecture_signals,
    recall_architecture_candidates,
)
from scripts.benchmark_architecture_recall import (
    BenchmarkCase,
    load_architecture_nodes,
    parse_args,
    run_benchmark,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_architecture_recall.py"
DETAIL_KINDS = (
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
    "模型数据",
    "目特数据",
    "声像数据",
)


def _large_synthetic_tree(family_count: int = 850) -> tuple[list[dict], dict[str, int]]:
    """构造 6,800+ 节点的浅树，避免测试依赖真实甲方领域树。"""

    nodes: list[dict] = [
        {
            "id": 1,
            "parentId": 0,
            "name": "武器装备",
            "pathName": "武器装备",
        }
    ]
    targets: dict[str, int] = {}
    for family_index in range(family_count):
        parent_id = 10_000 + family_index
        if family_index == 0:
            family_name = "CVN-78"
        else:
            family_name = f"TYPE-{family_index:04d}"
        nodes.append(
            {
                "id": parent_id,
                "parentId": 1,
                "name": family_name,
                "pathName": f"武器装备/{family_name}",
            }
        )
        for detail_index, detail_kind in enumerate(DETAIL_KINDS):
            leaf_id = 1_000_000 + family_index * 10 + detail_index
            nodes.append(
                {
                    "id": leaf_id,
                    "parentId": parent_id,
                    "name": f"{family_name}-{detail_kind}",
                    "pathName": f"武器装备/{family_name}/{detail_kind}",
                }
            )
            if detail_index == 0:
                targets[family_name] = leaf_id
    return nodes, targets


class ArchitectureRecallBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.nodes, cls.targets = _large_synthetic_tree()
        cls.index = build_architecture_tree_index(cls.nodes)

    def _run_cli(
        self,
        cases: list[dict],
        *extra_args: str,
    ) -> subprocess.CompletedProcess[str]:
        request_payload = {
            "businessType": "file",
            "params": [{"architectureList": self.nodes}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            tree_path = Path(temp_dir) / "request.json"
            cases_path = Path(temp_dir) / "cases.json"
            tree_path.write_text(
                json.dumps(request_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path.write_text(
                json.dumps({"cases": cases}, ensure_ascii=False),
                encoding="utf-8",
            )
            return subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--tree-json",
                    str(tree_path),
                    "--cases-json",
                    str(cases_path),
                    *extra_args,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

    def test_large_tree_recall_is_stable_bounded_and_hits_top64(self) -> None:
        signals = build_document_architecture_signals(
            filename="Gerald R Ford (CVN 78) class.pdf",
            original_filename="CVN-78-programme.pdf",
            title="Gerald R. Ford class aircraft carrier",
            headings=["CVN 78 specifications", "Programme overview"],
            body="CVN-78 is the lead ship of the Gerald R. Ford class.",
        )

        first = recall_architecture_candidates(self.index, signals)
        second = recall_architecture_candidates(self.index, signals)

        self.assertGreaterEqual(len(self.index.nodes), 6_800)
        self.assertLessEqual(len(first.candidates), 128)
        self.assertLessEqual(first.prompt_chars, 32_000)
        target_id = self.targets["CVN-78"]
        self.assertIn(target_id, first.base_leaf_ids)
        self.assertLessEqual(first.base_leaf_ids.index(target_id) + 1, 64)
        self.assertEqual(first.final_candidate_ids, second.final_candidate_ids)
        self.assertEqual(first.base_leaf_ids, second.base_leaf_ids)
        self.assertEqual(first.channel_rankings, second.channel_rankings)
        self.assertEqual(first.rrf_scores, second.rrf_scores)
        self.assertEqual(first.query_digest, second.query_digest)
        self.assertEqual(first.prompt_chars, second.prompt_chars)

    def test_cli_reads_request_shape_and_emits_only_bounded_metrics(self) -> None:
        body_marker = "PRIVATE-BENCHMARK-BODY-MUST-NOT-BE-EMITTED"
        request_payload = {
            "businessType": "file",
            "params": [
                {
                    "fileName": "benchmark.pdf",
                    "architectureList": self.nodes,
                }
            ],
        }
        cases_payload = {
            "cases": [
                {
                    "name": "cvn78",
                    "filename": "Gerald R Ford CVN 78.pdf",
                    "originalFilename": "CVN-78.pdf",
                    "title": "Gerald R. Ford class",
                    "headings": ["CVN 78 specifications"],
                    "body": f"{body_marker} CVN-78 aircraft carrier",
                    "goldIds": [self.targets["CVN-78"]],
                },
                {
                    "name": "type-0001",
                    "filename": "TYPE 0001 data.pdf",
                    "identifiers": ["TYPE-0001"],
                    "body": f"{body_marker} TYPE-0001 technical data",
                    "gold_ids": [str(self.targets["TYPE-0001"])],
                },
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            tree_path = Path(temp_dir) / "request.json"
            cases_path = Path(temp_dir) / "cases.json"
            tree_path.write_text(
                json.dumps(request_payload, ensure_ascii=False), encoding="utf-8"
            )
            cases_path.write_text(
                json.dumps(cases_payload, ensure_ascii=False), encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--tree-json",
                    str(tree_path),
                    "--cases-json",
                    str(cases_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(body_marker, completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["nodeCount"], len(self.nodes))
        self.assertEqual(result["caseCount"], 2)
        self.assertEqual(result["summary"]["recallAt64"], 1.0)
        self.assertEqual(
            result["summary"]["finalCandidateRecall"],
            1.0,
        )
        self.assertLessEqual(result["summary"]["maxCandidateCount"], 128)
        self.assertLessEqual(result["summary"]["maxPromptChars"], 32_000)
        for metrics in result["cases"]:
            self.assertEqual(len(metrics["queryDigest"]), 64)
            self.assertIsNotNone(metrics["goldRank"])
            self.assertEqual(metrics["recallAt64"], 1.0)
            self.assertEqual(metrics["finalCandidateRecall"], 1.0)
            self.assertIn("elapsedMs", metrics)

    def test_parent_gold_uses_final_candidate_metric_not_base_leaf_metric(self):
        case = BenchmarkCase(
            name="cvn78-parent",
            filename="Gerald R Ford CVN 78.pdf",
            original_filename="CVN-78.pdf",
            title="Gerald R. Ford class",
            headings=(),
            identifiers=(),
            body="CVN-78 aircraft carrier",
            gold_ids=(10_000,),
        )

        result = run_benchmark(self.nodes, (case,))
        metrics = result["cases"][0]

        self.assertEqual(metrics["recallAt64"], 0.0)
        self.assertEqual(metrics["finalCandidateRecall"], 1.0)
        self.assertIsNone(metrics["goldRank"])
        self.assertIsNotNone(metrics["goldCandidateRank"])
        self.assertEqual(result["summary"]["recallAt64"], 0.0)
        self.assertEqual(
            result["summary"]["finalCandidateRecall"],
            1.0,
        )

    def test_final_candidate_gate_controls_exit_code_and_keeps_output_bounded(
        self,
    ):
        body_marker = "PRIVATE-QUALITY-GATE-BODY-MUST-NOT-BE-EMITTED"
        cases = [
            {
                "name": "parent-hit",
                "filename": "Gerald R Ford CVN 78.pdf",
                "body": f"{body_marker} CVN-78 aircraft carrier",
                "goldIds": [10_000],
            },
            {
                "name": "intentional-miss",
                "filename": "Gerald R Ford CVN 78.pdf",
                "body": f"{body_marker} CVN-78 aircraft carrier",
                "goldIds": [self.targets["TYPE-0849"]],
            },
        ]

        passing = self._run_cli(
            cases,
            "--min-final-candidate-recall",
            "0.5",
        )
        failing = self._run_cli(
            cases,
            "--min-final-candidate-recall",
            "0.75",
        )

        self.assertEqual(passing.returncode, 0, passing.stderr)
        passing_result = json.loads(passing.stdout)
        self.assertTrue(passing_result["qualityGate"]["passed"])
        self.assertEqual(
            passing_result["summary"]["finalCandidateRecall"],
            0.5,
        )
        self.assertEqual(failing.returncode, 1, failing.stderr)
        failing_result = json.loads(failing.stdout)
        self.assertFalse(failing_result["qualityGate"]["passed"])
        self.assertIn("质量门禁未通过", failing.stderr)
        for output in (
            passing.stdout,
            passing.stderr,
            failing.stdout,
            failing.stderr,
        ):
            self.assertNotIn(body_marker, output)

    def test_optional_base_leaf_gate_can_fail_when_final_parent_gate_passes(self):
        completed = self._run_cli(
            [
                {
                    "name": "parent-only",
                    "filename": "Gerald R Ford CVN 78.pdf",
                    "body": "CVN-78 aircraft carrier",
                    "goldIds": [10_000],
                }
            ],
            "--min-final-candidate-recall",
            "1",
            "--min-base-leaf-recall-at-64",
            "0.1",
        )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        result = json.loads(completed.stdout)
        checks = result["qualityGate"]["checks"]
        self.assertTrue(checks["finalCandidateRecall"]["passed"])
        self.assertFalse(checks["baseLeafRecallAt64"]["passed"])

    def test_enabled_gate_rejects_goldless_cases_without_body_leak(self):
        body_marker = "PRIVATE-GOLDLESS-BODY-MUST-NOT-BE-EMITTED"
        completed = self._run_cli(
            [
                {
                    "name": "goldless",
                    "filename": "CVN-78.pdf",
                    "body": body_marker,
                }
            ],
            "--min-final-candidate-recall",
            "1",
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("每个 case 都必须提供 gold ID", completed.stderr)
        self.assertNotIn(body_marker, completed.stderr)

    def test_report_only_mode_keeps_goldless_case_compatibility(self):
        completed = self._run_cli(
            [
                {
                    "name": "goldless-report",
                    "filename": "CVN-78.pdf",
                    "body": "CVN-78 aircraft carrier",
                }
            ]
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["summary"]["goldCaseCount"], 0)
        self.assertIsNone(result["summary"]["recallAt64"])
        self.assertIsNone(
            result["summary"]["finalCandidateRecall"]
        )
        self.assertNotIn("qualityGate", result)

    def test_gate_thresholds_reject_non_finite_and_out_of_range_values(self):
        for raw_value in ("nan", "inf", "-0.01", "1.01", "not-a-number"):
            with self.subTest(raw_value=raw_value):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        parse_args(
                            [
                                "--tree-json",
                                "unused.json",
                                "--min-final-candidate-recall",
                                raw_value,
                            ]
                        )

                self.assertEqual(raised.exception.code, 2)
                self.assertIn("0 到 1", stderr.getvalue())

    def test_tree_loader_accepts_direct_node_array(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tree_path = Path(temp_dir) / "tree.json"
            tree_path.write_text(
                json.dumps(self.nodes[:1], ensure_ascii=False), encoding="utf-8"
            )
            loaded = load_architecture_nodes(tree_path)

        self.assertEqual(loaded, self.nodes[:1])

    def test_gold_ids_reject_non_ascii_digit_strings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tree_path = Path(temp_dir) / "tree.json"
            cases_path = Path(temp_dir) / "cases.json"
            tree_path.write_text(
                json.dumps(self.nodes[:1], ensure_ascii=False), encoding="utf-8"
            )
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "name": "unicode-digit",
                                "filename": "sample.pdf",
                                "goldIds": ["\uff11"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--tree-json",
                    str(tree_path),
                    "--cases-json",
                    str(cases_path),
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("\u5fc5\u987b\u662f\u6b63\u6574\u6570", completed.stderr)


if __name__ == "__main__":
    unittest.main()
