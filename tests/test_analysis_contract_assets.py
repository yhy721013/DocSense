"""阶段 1F-0 文件分析契约、算法黄金与遗留现状的离线门禁。

本模块刻意使用显式注入的离线容器、临时 SQLite 和线程替身。它不启动 run.py，
不创建真实网络连接，也不触发真实回调，用于在 1F 后续拆分旧 Analysis 链路时冻结
当前公开行为和纯规则输出。
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.modules.tasks.domain import ProgressKey, ProgressSnapshot, TaskId
from app.presenters.task_progress import ProgressWebSocketPresenter
from app.services.core.architecture_tree import (
    MAX_ARCHITECTURE_DEPTH,
    MAX_ARCHITECTURE_NAME_CHARS,
    MAX_ARCHITECTURE_NODE_COUNT,
    MAX_ARCHITECTURE_PATH_CHARS,
    MAX_ARCHITECTURE_PATH_NAME_CHARS,
    MAX_ARCHITECTURE_REMARK_CHARS,
    MAX_ARCHITECTURE_TOTAL_TEXT_CHARS,
    architecture_tree_fingerprint,
    build_architecture_tree_index,
)
from app.services.core.config import load_analysis_classification_config
from app.services.core.prompts import (
    build_architecture_classification_prompt,
    build_architecture_reselect_prompt,
    build_file_extraction_prompt,
)
from app.services.llm_service.analysis_service import (
    MAX_ANALYSIS_MODEL_CALLS,
    MAX_ANALYSIS_PARAMS_PER_REQUEST,
    MAX_ANALYSIS_PHASE_CALLS,
    MAX_ANALYSIS_PROMPT_CHARS,
    MAX_ANALYSIS_REQUEST_BYTES,
    build_effective_analysis_ranges,
    build_file_callback_payload,
    map_analysis_result,
)
from app.services.llm_service.architecture_recall_service import (
    build_document_architecture_signals,
    recall_architecture_candidates,
)
from app.modules.analysis.ports import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
)
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = (
    Path(__file__).with_name("contracts") / "stage1f0_analysis_contracts.json"
)


def _canonical_json(value: object) -> str:
    """生成与黄金资产一致的稳定 JSON，避免字典顺序掩盖真实回归。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_text(value: str) -> str:
    """返回 UTF-8 文本摘要，便于冻结长 Prompt 和完整默认范围。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _interface_authority_sha256(raw_bytes: bytes) -> str:
    """计算跨平台稳定的接口权威文档摘要。

    Git 在 Windows ``core.autocrlf=true`` 环境会把仓库中的 LF 转成 CRLF；如果直接对工作树
    字节求摘要，同一份接口文档会在 Windows 与 Linux 得到不同结果。这里仅规范化 UTF-8 BOM
    和换行编码，不裁剪空白、不改写正文，确保任何真实文字变化仍会触发契约门禁。
    """

    if not isinstance(raw_bytes, bytes):
        raise TypeError("raw_bytes 必须是 bytes")
    canonical_text = (
        raw_bytes.decode("utf-8-sig")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest().upper()


class AnalysisContractAssetTests(unittest.TestCase):
    """覆盖 1F-0 不可改变的公开契约、纯规则黄金和遗留事实清单。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        """延迟创建路由夹具，纯规则黄金不得依赖 Flask 容器。"""

        self._tempdir = None
        self.runtime_directory = None
        self.services = None
        self.task_service = None
        self.app = None
        self.client = None

    def _ensure_route_runtime(self) -> None:
        """仅在 HTTP 契约用例中构建临时容器，减少算法黄金的基础设施耦合。"""

        if self.client is not None:
            return
        self._tempdir = workspace_tempdir()
        self.runtime_directory = self._tempdir.__enter__()
        self.services = build_offline_application_services(self.runtime_directory)
        self.task_service = self.services.task_service
        self.app = create_app(services=self.services)
        # Flask 默认 500 页面是当前未捕获内部异常的既有行为，需要在隔离测试中可观测。
        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        if self._tempdir is not None:
            self._tempdir.__exit__(None, None, None)

    @staticmethod
    def _valid_submission(file_name: str) -> dict[str, object]:
        """构造不包含内部运行标识的最小合法公开请求。"""

        return {
            "businessType": "file",
            "params": [
                {
                    "fileName": file_name,
                    "filePath": f"http://127.0.0.1:8000/{file_name}",
                }
            ],
        }

    def test_contract_asset_is_strict_json_and_matches_interface_authority(
        self,
    ) -> None:
        """接口文档是唯一权威；资产同步已确认语义，不替代或擅自改写文档。"""

        authority = self.contract["interfaceAuthority"]
        interface_path = _ROOT / authority["path"]
        observed_sha256 = _interface_authority_sha256(interface_path.read_bytes())

        self.assertEqual(1, self.contract["schemaVersion"])
        self.assertEqual("1F-0", self.contract["stage"])
        self.assertTrue(self.contract["publicContractChanged"])
        self.assertEqual("2026-07-29", authority["lastApprovedAt"])
        self.assertIn("file/report/weaponry outcome_unknown", authority["approvedChange"])
        self.assertEqual(authority["sha256"], observed_sha256)
        self.assertEqual(
            self.contract,
            json.loads(_canonical_json(self.contract)),
        )

    def test_interface_authority_hash_is_stable_across_platform_line_endings(
        self,
    ) -> None:
        """LF、CRLF 与 UTF-8 BOM 只能是存储差异，不能改变接口契约身份。"""

        lf_document = "# 接口文档\n\n字段：fileName\n".encode("utf-8")
        crlf_document = lf_document.replace(b"\n", b"\r\n")
        bom_crlf_document = b"\xef\xbb\xbf" + crlf_document

        expected = _interface_authority_sha256(lf_document)
        self.assertEqual(expected, _interface_authority_sha256(crlf_document))
        self.assertEqual(expected, _interface_authority_sha256(bom_crlf_document))

    def test_submission_validation_and_limit_responses_are_exact(self) -> None:
        """验证所有已冻结的同步校验错误均只保留既有 error 字段。"""

        self._ensure_route_runtime()
        submission = self.contract["analysisSubmission"]
        for case in submission["validationCases"]:
            with self.subTest(case=case["id"]):
                response = self.client.post(
                    submission["path"],
                    json=case["payload"],
                )
                self.assertEqual(case["status"], response.status_code)
                self.assertTrue(response.content_type.startswith("application/json"))
                self.assertEqual(case["body"], response.get_json())

        limit = submission["limits"]["paramsMax"]
        too_many_payload = {
            "businessType": "file",
            "params": [
                {
                    "fileName": f"too-many-{index}.txt",
                    "filePath": f"http://127.0.0.1:8000/too-many-{index}.txt",
                }
                for index in range(limit + 1)
            ],
        }
        response = self.client.post(submission["path"], json=too_many_payload)
        self.assertEqual(submission["tooManyParams"]["status"], response.status_code)
        self.assertEqual(submission["tooManyParams"]["body"], response.get_json())

        response = self.client.post(
            submission["path"],
            data=b"{}",
            content_type="application/json",
            environ_overrides={
                "CONTENT_LENGTH": str(submission["limits"]["requestBytes"] + 1)
            },
        )
        self.assertEqual(submission["oversizedRequest"]["status"], response.status_code)
        self.assertEqual(submission["oversizedRequest"]["body"], response.get_json())

    def test_submission_success_is_empty_and_does_not_leak_internal_identity(
        self,
    ) -> None:
        """成功受理只能返回 202 空体，内部 execution 身份只能保留在任务事实中。"""

        self._ensure_route_runtime()
        submission = self.contract["analysisSubmission"]
        with patch("threading.Thread") as thread_factory:
            response = self.client.post(
                submission["path"],
                json=self._valid_submission("golden-accepted.txt"),
            )

        self.assertEqual(submission["success"]["status"], response.status_code)
        self.assertEqual(submission["success"]["body"].encode("utf-8"), response.data)
        self.assertEqual(
            submission["success"]["body"],
            response.get_data(as_text=True),
        )
        self.assertIsNone(response.get_json(silent=True))
        # 显式注入的离线容器不启动 Dispatcher；新路由只可靠受理并发出有界唤醒，
        # 不得为每个 HTTP 请求创建后台线程。
        thread_factory.assert_not_called()
        for field_name in submission["forbiddenSuccessFields"]:
            self.assertNotIn(field_name, response.get_data(as_text=True))

        persisted_task = self.task_service.get_task("file", "golden-accepted.txt")
        self.assertIsNotNone(persisted_task)
        assert persisted_task is not None
        self.assertIn("execution_id", persisted_task)
        execution = self.task_service.get_task_execution(persisted_task["execution_id"])
        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertIsNotNone(execution["batch_id"])
        self.assertIsNotNone(execution["batch_sequence"])

    def test_submission_conflict_busy_and_unhandled_failure_are_preserved(
        self,
    ) -> None:
        """冲突、SQLite 忙和未捕获异常不能被后续内部适配误映射为成功。"""

        self._ensure_route_runtime()
        submission = self.contract["analysisSubmission"]
        active_case, callback_case = submission["conflicts"]

        self.task_service.create_file_task(
            "golden-active.txt",
            {"businessType": "file"},
            status="1",
        )
        response = self.client.post(
            submission["path"],
            json=self._valid_submission("golden-active.txt"),
        )
        self.assertEqual(active_case["status"], response.status_code)
        self.assertEqual(active_case["body"], response.get_json())

        previous = self.task_service.create_file_task(
            "golden-callback-pending.txt",
            {"businessType": "file"},
        )
        self.task_service.mark_business_result(
            "file",
            "golden-callback-pending.txt",
            {"status": "2"},
            status="2",
            execution_id=previous["execution_id"],
        )
        response = self.client.post(
            submission["path"],
            json=self._valid_submission("golden-callback-pending.txt"),
        )
        self.assertEqual(callback_case["status"], response.status_code)
        self.assertEqual(callback_case["body"], response.get_json())

        assert self.services is not None
        assert self.services.analysis_submit is not None
        with patch.object(
            self.services.analysis_submit,
            "execute",
            return_value=AnalysisBatchAdmission(
                AnalysisBatchAdmissionOutcome.BUSY
            ),
        ):
            response = self.client.post(
                submission["path"],
                json=self._valid_submission("golden-busy.txt"),
            )
        self.assertEqual(submission["databaseBusy"]["status"], response.status_code)
        self.assertEqual(submission["databaseBusy"]["body"], response.get_json())

        # 捕获 Flask 的错误日志，避免故障注入测试向完整回归输出无关堆栈。
        with self.assertLogs("app", level="ERROR"):
            with patch.object(
                self.services.analysis_submit,
                "execute",
                side_effect=RuntimeError("stage1f0-unhandled"),
            ):
                response = self.client.post(
                    submission["path"],
                    json=self._valid_submission("golden-unhandled.txt"),
                )
        unhandled = submission["unhandledFailure"]
        self.assertEqual(unhandled["status"], response.status_code)
        self.assertTrue(
            response.content_type.startswith(unhandled["contentTypePrefix"])
        )
        self.assertIn(unhandled["bodyContains"], response.get_data(as_text=True))

    def test_file_check_task_progress_and_callback_payloads_are_frozen(self) -> None:
        """file 的 200 空体、Progress 字段和成功/失败 callback 统一冻结。"""

        self._ensure_route_runtime()
        check_task = self.contract["fileCheckTask"]
        self.task_service.create_file_task(
            "golden-check.txt",
            {"businessType": "file"},
            status="1",
        )
        response = self.client.post(
            check_task["path"],
            json={
                "businessType": "file",
                "params": [{"fileName": "golden-check.txt"}],
            },
        )
        self.assertEqual(check_task["success"]["status"], response.status_code)
        self.assertEqual(check_task["success"]["body"].encode("utf-8"), response.data)
        self.assertEqual(
            "all-params-parse-and-normalize-before-any-callback",
            check_task["validationBeforeSideEffects"],
        )
        self.assertEqual(
            "normalized-business-key-first-occurrence",
            check_task["sameRequestDeduplication"],
        )
        self.assertEqual(
            "original-params-count-before-deduplication",
            check_task["responseCardinalityBasis"],
        )
        self.assertTrue(check_task["receiverIdempotencyRequired"])
        self.assertFalse(check_task["newPublicFields"])

        response = self.client.post(
            check_task["path"],
            json={
                "businessType": "file",
                "params": [{"fileName": "golden-missing.txt"}],
            },
        )
        self.assertEqual(check_task["singleMissing"]["status"], response.status_code)
        self.assertEqual(check_task["singleMissing"]["body"], response.get_json())

        progress_contract = self.contract["fileProgress"]["message"]
        progress_snapshot = ProgressSnapshot(
            key=ProgressKey("file", "golden-progress.pdf"),
            task_id=TaskId("stage1f0-progress"),
            progress=0.35,
            message="处理中",
            internal_state="running",
            sequence_no=1,
            updated_at="2026-07-26T00:00:00Z",
        )
        self.assertEqual(
            progress_contract,
            ProgressWebSocketPresenter().present_snapshot(progress_snapshot),
        )

        mapping = self.contract["algorithmGoldens"]["mapping"]
        mapped_result = map_analysis_result(
            mapping["parsedResult"],
            mapping["request"],
            original_text=mapping["originalText"],
        )
        callback_success = build_file_callback_payload(
            self.contract["fileCallbacks"]["success"]["fileName"],
            mapped_result,
            status=self.contract["fileCallbacks"]["success"]["status"],
        )
        self.assertEqual(
            self.contract["fileCallbacks"]["success"]["sha256"],
            _sha256_text(_canonical_json(callback_success)),
        )
        self.assertEqual(
            self.contract["fileCallbacks"]["failure"],
            build_file_callback_payload("golden.pdf", {}, status="3"),
        )

    def test_limits_defaults_and_tree_golden_are_equivalent(self) -> None:
        """范围默认值和树拓扑必须先等价，后续 Domain 迁移不得顺手改算法。"""

        submission_limits = self.contract["analysisSubmission"]["limits"]
        self.assertEqual(submission_limits["requestBytes"], MAX_ANALYSIS_REQUEST_BYTES)
        self.assertEqual(submission_limits["paramsMax"], MAX_ANALYSIS_PARAMS_PER_REQUEST)
        self.assertEqual(submission_limits["model"]["promptChars"], MAX_ANALYSIS_PROMPT_CHARS)
        self.assertEqual(submission_limits["model"]["phaseCalls"], MAX_ANALYSIS_PHASE_CALLS)
        self.assertEqual(submission_limits["model"]["totalCalls"], MAX_ANALYSIS_MODEL_CALLS)
        self.assertEqual(submission_limits["tree"]["maxNodeCount"], MAX_ARCHITECTURE_NODE_COUNT)
        self.assertEqual(submission_limits["tree"]["maxDepth"], MAX_ARCHITECTURE_DEPTH)
        self.assertEqual(submission_limits["tree"]["maxNameChars"], MAX_ARCHITECTURE_NAME_CHARS)
        self.assertEqual(submission_limits["tree"]["maxPathChars"], MAX_ARCHITECTURE_PATH_CHARS)
        self.assertEqual(
            submission_limits["tree"]["maxPathNameChars"],
            MAX_ARCHITECTURE_PATH_NAME_CHARS,
        )
        self.assertEqual(
            submission_limits["tree"]["maxRemarkChars"],
            MAX_ARCHITECTURE_REMARK_CHARS,
        )
        self.assertEqual(
            submission_limits["tree"]["maxTotalTextChars"],
            MAX_ARCHITECTURE_TOTAL_TEXT_CHARS,
        )

        ranges = self.contract["algorithmGoldens"]["ranges"]
        default_ranges = build_effective_analysis_ranges({})
        self.assertEqual(ranges["defaultSha256"], _sha256_text(_canonical_json(default_ranges)))
        self.assertEqual(
            ranges["defaultCounts"],
            {name: len(items) for name, items in default_ranges.items()},
        )
        self.assertEqual(
            ranges["defaultArchitectureIds"],
            [item["id"] for item in default_ranges["architectureList"]],
        )
        explicit_ranges = build_effective_analysis_ranges(ranges["explicitRequest"])
        self.assertEqual(ranges["explicitExpected"], explicit_ranges)
        self.assertEqual(
            ranges["explicitSha256"],
            _sha256_text(_canonical_json(explicit_ranges)),
        )

        tree = self.contract["algorithmGoldens"]["tree"]
        index = build_architecture_tree_index(tree["nodes"])
        self.assertEqual(tree["fingerprint"], architecture_tree_fingerprint(tree["nodes"]))
        self.assertEqual(tuple(tree["rootIds"]), index.root_ids)
        self.assertEqual(tuple(tree["leafIds"]), index.leaf_ids)
        self.assertEqual(tuple(tree["ancestorsOf11"]), index.ancestors_by_id[11])
        self.assertEqual(
            tuple(tree["leafDescendantsOf10"]),
            index.leaf_descendants_by_id[10],
        )
        self.assertEqual(tuple(tree["siblingsOf11"]), index.siblings_by_id[11])

    def test_recall_prompt_mapping_and_identity_reselect_goldens_are_equivalent(
        self,
    ) -> None:
        """冻结召回排序、分类/抽取 Prompt、身份重选上下文和字段回退结果。"""

        golden = self.contract["algorithmGoldens"]
        tree = golden["tree"]
        recall = golden["recall"]
        signals_input = recall["signalsInput"]
        signals = build_document_architecture_signals(
            filename=signals_input["filename"],
            original_filename=signals_input["originalFilename"],
            title=signals_input["title"],
            headings=signals_input["headings"],
            identifiers=signals_input["identifiers"],
            body=signals_input["body"],
        )
        expected_signals = recall["expectedSignals"]
        self.assertEqual(expected_signals["filename"], signals.filename)
        self.assertEqual(expected_signals["originalFilename"], signals.original_filename)
        self.assertEqual(expected_signals["title"], signals.title)
        self.assertEqual(tuple(expected_signals["headings"]), signals.headings)
        self.assertEqual(tuple(expected_signals["identifiers"]), signals.identifiers)
        self.assertEqual(expected_signals["bodyExcerpt"], signals.body_excerpt)

        decision = recall_architecture_candidates(
            build_architecture_tree_index(tree["nodes"]),
            signals,
        )
        expected_decision = recall["expectedDecision"]
        self.assertEqual(
            expected_decision["candidateIds"],
            [candidate.architecture_id for candidate in decision.candidates],
        )
        self.assertEqual(
            expected_decision["candidateNodeTypes"],
            [candidate.node_type for candidate in decision.candidates],
        )
        self.assertEqual(
            expected_decision["candidateRanks"],
            [candidate.rank for candidate in decision.candidates],
        )
        self.assertEqual(
            expected_decision["candidateChannelRanks"],
            [
                [list(rank) for rank in candidate.channel_ranks]
                for candidate in decision.candidates
            ],
        )
        self.assertEqual(tuple(expected_decision["directExactIds"]), decision.direct_exact_ids)
        self.assertEqual(tuple(expected_decision["directTreeIds"]), decision.direct_tree_ids)
        self.assertEqual(
            expected_decision["channelRankings"],
            [
                [ranking.channel, list(ranking.node_ids)]
                for ranking in decision.channel_rankings
            ],
        )
        self.assertEqual(
            expected_decision["candidateProjectionChars"],
            decision.candidate_projection_chars,
        )
        self.assertEqual(expected_decision["promptChars"], decision.prompt_chars)
        self.assertEqual(expected_decision["queryDigest"], decision.query_digest)

        prompts = golden["prompts"]
        classification = prompts["classification"]
        classification_prompt = build_architecture_classification_prompt(
            classification["request"],
            classification["candidates"],
        )
        self.assertEqual(classification["expectedLength"], len(classification_prompt))
        self.assertEqual(
            classification["sha256"],
            _sha256_text(classification_prompt),
        )
        self.assertEqual(
            classification["expectedProjection"],
            json.loads(classification_prompt.split("【模型候选】\n", 1)[1].strip()),
        )

        extraction = prompts["extraction"]
        extraction_prompt = build_file_extraction_prompt(
            extraction["request"],
            resolved_architecture_id=extraction["resolvedArchitectureId"],
            resolved_architecture_path_name=extraction[
                "resolvedArchitecturePathName"
            ],
            resolved_architecture_path_node_names=tuple(
                extraction["resolvedArchitecturePathNodeNames"]
            ),
            resolved_architecture_node_type=extraction[
                "resolvedArchitectureNodeType"
            ],
        )
        self.assertEqual(extraction["expectedLength"], len(extraction_prompt))
        self.assertEqual(extraction["sha256"], _sha256_text(extraction_prompt))

        reselect = prompts["identityReselect"]
        reselect_prompt = build_architecture_reselect_prompt(
            reselect["initialResult"],
            reselect["confirmedIdentityContext"],
            reselect["candidates"],
        )
        self.assertEqual(reselect["expectedLength"], len(reselect_prompt))
        self.assertEqual(reselect["sha256"], _sha256_text(reselect_prompt))
        self.assertEqual(
            reselect["expectedContext"],
            json.loads(
                reselect_prompt.split("【已确认身份上下文】\n", 1)[1].splitlines()[0]
            ),
        )
        self.assertEqual(
            reselect["expectedCandidateCount"],
            len(json.loads(reselect_prompt.split("【受限候选】\n", 1)[1].strip())),
        )

        mapping = golden["mapping"]
        self.assertEqual(
            mapping["expected"],
            map_analysis_result(
                mapping["parsedResult"],
                mapping["request"],
                original_text=mapping["originalText"],
            ),
        )

    def test_default_classification_policy_and_legacy_reference_inventory_are_frozen(
        self,
    ) -> None:
        """保留基线执行清单，并确认 1F-5B 已从公开路由切走旧 owner。"""

        policy = self.contract["algorithmGoldens"]["policyDefaults"]
        with patch.dict(os.environ, {}, clear=True):
            config = load_analysis_classification_config()
        self.assertEqual(policy["mode"], config.mode)
        self.assertEqual(
            policy["filenameConstraintMode"],
            config.filename_constraint_mode,
        )
        self.assertEqual(policy["dataStandardMode"], config.data_standard_mode)
        self.assertEqual(
            policy["identityReselectMode"],
            config.identity_reselect_mode,
        )

        inventory = self.contract["legacyInventory"]
        self.assertEqual(
            [
                "request_validation",
                "domain_tree_validation",
                "legacy_task_precheck",
                "atomic_task_acceptance",
                "initial_progress_publication",
                "route_thread_dispatch",
                "single_or_batch_execution",
                "task_state_and_progress",
                "document_download_and_preparation",
                "rag_classification_and_extraction",
                "knowledge_persistence",
                "translation",
                "callback_delivery",
            ],
            inventory["stageOrder"],
        )
        self.assertEqual(9, len(inventory["externalSideEffects"]))
        # 资产中的第一个条目是 1F-0 的历史路线记录；它不能因为 1F-5B 已切换而被
        # 重写。剩余兼容模块仍保留到 1G 物理删除前，故继续验证其导出没有被意外移除。
        for reference in inventory["references"][1:]:
            source = (_ROOT / reference["path"]).read_text(encoding="utf-8")
            for symbol in reference["symbols"]:
                with self.subTest(path=reference["path"], symbol=symbol):
                    self.assertIn(symbol, source)

        route_source = (_ROOT / "app" / "blueprints" / "llm.py").read_text(
            encoding="utf-8"
        )
        for forbidden in (
            "create_file_tasks_if_available",
            "threading.Thread",
            "run_file_analysis_task",
            "run_file_analysis_batch_task",
            "replay_callback_if_needed",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, route_source)


if __name__ == "__main__":
    unittest.main()
