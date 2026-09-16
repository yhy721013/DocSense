"""阶段 1H-0 当前行为、依赖边界与隔离基线。

这些测试不是在认可当前耦合，而是把迁移前事实显式冻结。后续波次移除某条临时依赖时，
必须同步更新黄金资产和对应阶段执行记录，不能让依赖变化以偶然测试漂移的方式发生。
"""

from __future__ import annotations

import ast
import hashlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.modules.analysis.adapters.legacy_files import (
    LocalAnalysisTaskWorkspaceAdapter,
)
from app.modules.analysis.ports.common import AnalysisExecutionRef
from app.modules.report.adapters.local_artifacts import LocalReportArtifactAdapter
from app.modules.tasks.domain import TaskId
from app.services.translator.document_handler import DocumentTranslator
from app.services.utils.mhtml_normalizer import (
    extract_retrieval_text_from_mhtml,
    extract_text_from_mhtml,
)
from tests import workspace_tempdir


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_ASSET_ROOT = (
    Path(__file__).resolve().parent / "assets" / "document_processing"
)
_BASELINE_PATH = _ASSET_ROOT / "stage1h_baseline.json"


def _sha256(path: Path) -> str:
    """以流式读取计算文档摘要，避免测试把大文档一次性读入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as file_object:
        for chunk in iter(lambda: file_object.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _absolute_import(source: Path, node: ast.ImportFrom) -> str:
    """把相对导入换算为仓库内绝对模块名，供架构资产稳定比较。"""

    module_parts = node.module.split(".") if node.module else []
    if node.level == 0:
        return ".".join(module_parts)

    relative_source = source.relative_to(_REPOSITORY_ROOT)
    package_parts = list(relative_source.with_suffix("").parts[:-1])
    parent_count = node.level - 1
    if parent_count > len(package_parts):
        return ""
    if parent_count:
        package_parts = package_parts[:-parent_count]
    return ".".join(package_parts + module_parts)


def _import_targets(source: Path) -> set[str]:
    """提取单个 Python 文件的静态导入，不执行任何生产模块副作用。"""

    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _absolute_import(source, node)
            if target:
                targets.add(target)
        elif isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
    return targets


class _ProgressTrackerFake:
    """只记录重置次数，避免基线分派测试初始化真实翻译模型。"""

    def __init__(self) -> None:
        self.reset_count = 0

    def reset(self) -> None:
        self.reset_count += 1


class _TranslatorFake:
    def __init__(self) -> None:
        self.tracker = _ProgressTrackerFake()

    def get_progress_tracker(self) -> _ProgressTrackerFake:
        return self.tracker


class _HandlerFake:
    """记录 DocumentTranslator 的格式分派，不执行 MinerU、浏览器或模型。"""

    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def convert_to_markdown(self, **_: object) -> str:
        self._calls.append(f"{self._name}.convert_to_markdown")
        return "prepared.md"

    def process(self, **_: object) -> str:
        self._calls.append(f"{self._name}.process")
        return f"{self._name}-result"


class Stage1HDocumentProcessingBaselineTests(unittest.TestCase):
    """只读冻结公开契约、当前耦合、格式行为和任务目录隔离。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))

    def test_public_contract_documents_match_readonly_stage1h_hashes(self) -> None:
        """1H 未经确认不得顺手修改公开合同文档。"""

        expected = self.baseline["publicContractHashes"]
        actual = {
            relative_path: _sha256(_REPOSITORY_ROOT / relative_path)
            for relative_path in expected
        }
        self.assertEqual(expected, actual)

    def test_temporary_conversion_couplings_match_inventory(self) -> None:
        """尚未迁移的反向依赖必须与阶段资产完全一致，禁止继续扩散。"""

        expected_edges = {
            (item["source"], item["target"])
            for item in self.baseline["temporaryConversionCouplings"]
        }
        legacy_targets = {target for _, target in expected_edges}
        legacy_targets.update(
            self.baseline.get("forbiddenConversionTargets", ())
        )
        source_files = {
            path
            for path in (_REPOSITORY_ROOT / "app").rglob("*.py")
            if "__pycache__" not in path.parts
        }
        actual_edges: set[tuple[str, str]] = set()
        for source in source_files:
            relative_source = source.relative_to(_REPOSITORY_ROOT).as_posix()
            for target in _import_targets(source):
                if target in legacy_targets:
                    actual_edges.add((relative_source, target))

        self.assertEqual(expected_edges, actual_edges)

    def test_committed_mhtml_corpus_freezes_current_text_rules(self) -> None:
        """使用仓库自建语料固定普通正文和检索正文的当前清洗规则。"""

        for case in self.baseline["mhtmlCases"]:
            with self.subTest(file=case["file"], mode=case["mode"]):
                source = _ASSET_ROOT / case["file"]
                if case["mode"] == "retrieval":
                    text = extract_retrieval_text_from_mhtml(str(source))
                else:
                    text = extract_text_from_mhtml(str(source))

                for expected_text in case["contains"]:
                    self.assertIn(expected_text, text)
                for excluded_text in case["excludes"]:
                    self.assertNotIn(excluded_text, text)
                for value, expected_count in case.get(
                    "exactCounts",
                    {},
                ).items():
                    self.assertEqual(expected_count, text.count(value))

    def test_document_translator_format_dispatch_matches_baseline(self) -> None:
        """冻结当前格式选择，后续只改变所有权，不擅自改变支持格式。"""

        for suffix, expected_handler in self.baseline[
            "translationDispatch"
        ].items():
            with self.subTest(suffix=suffix):
                calls: list[str] = []
                translator = _TranslatorFake()
                document_translator = DocumentTranslator.__new__(
                    DocumentTranslator
                )
                document_translator.translator = translator
                document_translator.txt_handler = _HandlerFake("txt", calls)
                document_translator.markdown_handler = _HandlerFake(
                    "markdown",
                    calls,
                )
                document_translator.mhtml_handler = _HandlerFake(
                    "mhtml",
                    calls,
                )

                result = document_translator.process_file(
                    f"baseline{suffix}",
                    output_path="translated.txt",
                )

                self.assertEqual(1, translator.tracker.reset_count)
                self.assertEqual(
                    f"{expected_handler}-result",
                    result,
                )
                self.assertEqual(
                    f"{expected_handler}.process",
                    calls[-1],
                )
                self.assertEqual(1, calls.count(f"{expected_handler}.process"))

    def test_fifty_task_namespaces_are_isolated_in_current_adapters(self) -> None:
        """固定 Analysis 目录与 Report Artifact 前缀已有的 50 任务隔离基础。"""

        task_count = 50
        barrier = threading.Barrier(task_count)
        with workspace_tempdir() as temporary_root:
            analysis_adapter = LocalAnalysisTaskWorkspaceAdapter(
                str(Path(temporary_root) / "analysis")
            )
            report_adapter = LocalReportArtifactAdapter(
                Path(temporary_root) / "report"
            )

            def create_workspace(index: int) -> tuple[str, str]:
                execution = AnalysisExecutionRef(
                    task_id=TaskId(f"stage1h-baseline-{index:02d}"),
                    file_name=f"baseline-{index:02d}.txt",
                    batch_id=f"{index + 1:032x}",
                    batch_sequence=1,
                )
                barrier.wait(timeout=10)
                workspace = analysis_adapter.create(execution)
                report_scope = report_adapter.begin(execution.task_id)
                return (
                    str(Path(workspace.root_path).resolve()),
                    report_scope.namespace,
                )

            with ThreadPoolExecutor(max_workers=task_count) as executor:
                identities = tuple(executor.map(create_workspace, range(task_count)))

        analysis_roots = {analysis_root for analysis_root, _ in identities}
        report_namespaces = {namespace for _, namespace in identities}
        self.assertEqual(task_count, len(analysis_roots))
        self.assertEqual(task_count, len(report_namespaces))
        self.assertTrue(
            all(namespace.startswith("report/") for namespace in report_namespaces)
        )


if __name__ == "__main__":
    unittest.main()
