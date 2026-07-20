from __future__ import annotations

import hashlib
import json
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.ports import RagPromptKind, RagSource
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service import analysis_service
from app.services.llm_service.architecture_recall_service import (
    ArchitectureRecallCandidate,
    ArchitectureRecallDecision,
    RecallChannelRanking,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes.knowledge_index import FakeKnowledgeIndexFactory
from tests.fakes.rag import FakeDocumentRagFactory, FakeRagOutcome


class AnalysisTwoStageTests(unittest.TestCase):
    SOURCE = RagSource(document_ref="document:two-stage", text="领域分类测试证据")

    @staticmethod
    def _tree() -> list[dict]:
        return [
            {"id": 1, "name": "装备型号", "parentId": None},
            {"id": 10, "name": "CVN-78", "parentId": 1},
            {"id": 11, "name": "CVN-78-基础数据", "parentId": 10},
            {"id": 12, "name": "CVN-78-战技指标", "parentId": 10},
        ]

    @staticmethod
    def _data_standard_tree(*, include_general: bool = True) -> list[dict]:
        nodes = [
            {"id": 100, "name": "数据标准", "parentId": None},
            {"id": 101, "name": "建模与仿真", "parentId": 100},
            {"id": 102, "name": "军用软件", "parentId": 100},
            {"id": 103, "name": "目标特性", "parentId": 100},
            {"id": 104, "name": "术语与定义", "parentId": 100},
            {"id": 106, "name": "元数据", "parentId": 100},
            {"id": 600, "name": "基地目标", "parentId": None},
            {"id": 654, "name": "海军", "parentId": 600},
            {"id": 655, "name": "海军基地", "parentId": 654},
        ]
        if include_general:
            nodes.insert(
                5,
                {"id": 105, "name": "通用要求标准", "parentId": 100},
            )
        return nodes

    @staticmethod
    def _gjb_body() -> str:
        return "\n".join(
            (
                "中华人民共和国国家军用标准",
                "FL 0106 GJB 9001C-2017",
                "质量管理体系要求",
                "2017-05-18 发布 2017-07-01 实施",
                "1 范围",
                "2 规范性引用文件",
                "3 术语和定义",
                "本标准起草单位包括海军装备研究院和海军驻地代表局。",
            )
        )

    @staticmethod
    def _request(file_name: str, tree: list[dict]) -> dict:
        return {
            "businessType": "file",
            "params": [
                {
                    "fileName": file_name,
                    "originalFileName": file_name,
                    "filePath": f"https://example.invalid/{file_name}",
                    "enableFullTranslation": False,
                    "architectureList": tree,
                    "architectureStandardList": [],
                }
            ],
        }

    @staticmethod
    def _extraction(file_name: str, *, architecture_id: int = 999999) -> str:
        return json.dumps(
            {
                # 抽取阶段即使越权输出分类，mapper 也必须由已确认 ID 覆盖。
                "architectureId": architecture_id,
                "country": "",
                "channel": "",
                "maturity": "",
                "security": "",
                "format": "",
                "fileDataItem": {
                    "fileName": file_name,
                    "summary": "两阶段抽取摘要",
                    "keyword": "航母, CVN-78",
                    "score": 55,
                    "source": "未明确数据来源",
                    "dataFormat": "",
                },
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _decision(index, candidate_ids=(11, 12, 10)) -> ArchitectureRecallDecision:
        candidates = []
        for rank, node_id in enumerate(candidate_ids, start=1):
            node = index.require(node_id)
            candidates.append(
                ArchitectureRecallCandidate(
                    architecture_id=node_id,
                    path_name=node.semantic_path,
                    node_type="leaf" if node.is_leaf else "parent",
                    remark=node.remark,
                    rank=rank,
                    rrf_score=1.0 / rank,
                    channel_ranks=(("tree", rank),),
                    protected_reasons=(),
                )
            )
        return ArchitectureRecallDecision(
            tree_fingerprint=index.fingerprint,
            query_digest=hashlib.sha256(b"two-stage-query").hexdigest(),
            base_leaf_ids=tuple(
                node_id for node_id in candidate_ids if index.require(node_id).is_leaf
            ),
            candidates=tuple(candidates),
            channel_rankings=(
                RecallChannelRanking(channel="tree", node_ids=tuple(candidate_ids)),
            ),
            rrf_scores=tuple((node_id, 1.0 / rank) for rank, node_id in enumerate(candidate_ids, 1)),
            protected_reasons=(),
            direct_exact_ids=(),
            direct_tree_ids=tuple(candidate_ids),
            candidate_projection_chars=128,
            prompt_chars=128,
            elapsed_ms=1.0,
        )

    def _run(
            self,
            *,
            tmp: str,
            request: dict,
            rag_factory: FakeDocumentRagFactory,
            knowledge_factory: FakeKnowledgeIndexFactory | None = None,
            recall_side_effect=None,
            mode: str = "topk_two_stage",
            filename_constraint_mode: str = "legacy",
            data_standard_mode: str = "legacy",
    ):
        file_name = request["params"][0]["fileName"]
        local_file = Path(tmp, file_name)
        if not local_file.exists():
            local_file.write_text("CVN-78 航空母舰\n第一章 概述\n正文证据", encoding="utf-8")
        task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
        task = task_service.create_file_task(file_name, request)
        knowledge_factory = knowledge_factory or FakeKnowledgeIndexFactory()
        recall_patch = (
            patch(
                "app.services.llm_service.analysis_service.recall_architecture_candidates",
                side_effect=recall_side_effect,
            )
            if recall_side_effect is not None
            else patch(
                "app.services.llm_service.analysis_service.recall_architecture_candidates",
                wraps=analysis_service.recall_architecture_candidates,
            )
        )
        with (
            patch(
                "app.services.llm_service.analysis_service.download_to_temp_file",
                return_value=str(local_file),
            ),
            patch(
                "app.services.llm_service.analysis_service.normalize_file_for_llm",
                side_effect=lambda path: path,
            ),
            patch(
                "app.services.llm_service.analysis_service.prepare_analysis_file_for_upload",
                side_effect=lambda path, *_args: path,
            ),
            patch(
                "app.services.llm_service.analysis_service.enrich_with_translations",
                side_effect=lambda result, *_args, **_kwargs: result,
            ),
            recall_patch,
        ):
            analysis_service.run_file_analysis_task(
                task_service=task_service,
                progress_hub=LLMProgressHub(),
                request_payload=request,
                download_root=tmp,
                callback_url="",
                callback_timeout=5,
                document_rag_factory=rag_factory,
                knowledge_index_factory=knowledge_factory,
                execution_id=task["execution_id"],
                analysis_classification_mode=mode,
                analysis_filename_constraint_mode=filename_constraint_mode,
                analysis_data_standard_mode=data_standard_mode,
            )
        return (
            task_service,
            task_service.get_task("file", file_name),
            task_service.get_architecture_recall_decision(task["execution_id"]),
            rag_factory,
            knowledge_factory,
        )

    def test_single_candidate_skips_classification_and_starts_with_extraction(self):
        with workspace_tempdir() as tmp:
            file_name = "single.txt"
            request = self._request(
                file_name,
                [{"id": 11, "name": "CVN-78-基础数据", "parentId": 10}],
            )
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    )
                ]
            )
            task_service, task, recall, rag_factory, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 11)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis_extraction"])
        self.assertEqual(recall["returned_architecture_id"], 11)
        self.assertEqual(recall["returned_rank"], 1)
        self.assertEqual(len(rag_factory.ports[0].sessions[0].trace.attempts), 1)

    def test_classification_and_extraction_are_isolated_and_resolved_id_wins(self):
        with workspace_tempdir() as tmp:
            file_name = "cvn78.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":11}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name, architecture_id=999999),
                        sources=(self.SOURCE,),
                    )
                ],
            )
            with (
                patch(
                    "app.services.llm_service.analysis_service.build_architecture_classification_prompt",
                    wraps=analysis_service.build_architecture_classification_prompt,
                ) as classification_prompt,
                patch(
                    "app.services.llm_service.analysis_service.build_file_extraction_prompt",
                    wraps=analysis_service.build_file_extraction_prompt,
                ) as extraction_prompt,
            ):
                task_service, task, recall, _rag, knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            ["architecture_classification", "analysis_extraction"],
        )
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 11)
        self.assertEqual(recall["returned_architecture_id"], 11)
        self.assertEqual(classification_prompt.call_count, 1)
        self.assertEqual(extraction_prompt.call_count, 1)
        self.assertNotIn("模型候选", interaction["prompt"])
        # 完整树仍用于 storage，基础数据叶子归并到装备父节点 10。
        self.assertIn(10, knowledge.ports[0]._collections_by_architecture)

    def test_two_stage_constraint_decision_is_computed_once_and_reused(self):
        with workspace_tempdir() as tmp:
            file_name = "constraint-once.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":11}',
                        sources=(self.SOURCE,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    )
                ],
            )
            with patch(
                "app.services.llm_service.analysis_service."
                "_decide_topk_deterministic_architecture_constraint",
                wraps=getattr(
                    analysis_service,
                    "_decide_topk_deterministic_architecture_constraint",
                ),
            ) as decide_constraint:
                _service, task, recall, _rag, _knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    recall_side_effect=(
                        lambda index, *_args, **_kwargs: self._decision(index)
                    ),
                )

        self.assertEqual(decide_constraint.call_count, 1)
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 11)
        self.assertEqual(recall["returned_architecture_id"], 11)

    def test_full_tree_root_outside_visible_candidates_is_repaired_with_identical_candidates(self):
        with workspace_tempdir() as tmp:
            file_name = "repair.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":1}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text='{"architectureId":12}', sources=(self.SOURCE,)),
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,)),
                ],
            )
            with patch(
                "app.services.llm_service.analysis_service.build_architecture_repair_prompt",
                wraps=analysis_service.build_architecture_repair_prompt,
            ) as repair_prompt:
                task_service, task, _recall, _rag, _knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 12)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            ["architecture_classification", "architecture_repair", "analysis_extraction"],
        )
        repaired_candidates = repair_prompt.call_args.args[1]
        self.assertEqual([item["id"] for item in repaired_candidates], [11, 12, 10])
        self.assertTrue(all("nodeType" in item for item in repaired_candidates))

    def test_visible_ordinary_parent_is_allowed_as_deepest_reliable_fallback(self):
        with workspace_tempdir() as tmp:
            file_name = "parent.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":10}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 10)
        self.assertEqual(recall["returned_rank"], 3)

    def test_gjb_null_classification_falls_back_to_general_requirement_leaf(self):
        tree = [
            {"id": 100, "name": "数据标准", "parentId": None},
            {"id": 107, "name": "其他标准", "parentId": 100},
            {"id": 101, "name": "建模与仿真", "parentId": 100},
            {"id": 102, "name": "军用软件", "parentId": 100},
            {"id": 103, "name": "目标特性", "parentId": 100},
            {"id": 104, "name": "术语与定义", "parentId": 100},
            {"id": 105, "name": "通用要求", "parentId": 100},
            {"id": 106, "name": "元数据", "parentId": 100},
        ]
        with workspace_tempdir() as tmp:
            file_name = "GJB-9001C.txt"
            request = self._request(file_name, tree)
            Path(tmp, file_name).write_text("GJB 9001C-2017 国家军用标准", encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":null}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, rag_factory, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (107, 101, 102, 103, 104, 105, 106),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 105)
        self.assertEqual(recall["returned_architecture_id"], 105)
        self.assertEqual(
            [attempt.prompt_kind for attempt in rag_factory.ports[0].sessions[0].trace.attempts],
            [RagPromptKind.ARCHITECTURE_CLASSIFICATION, RagPromptKind.ANALYSIS_EXTRACTION],
        )

    def test_data_standard_profile_requires_cover_confirmation_not_body_reference(self):
        confirmed = analysis_service._build_data_standard_classification_profile(
            file_name="technical-upload.txt",
            original_name="GJB 9001C-2017.pdf",
            original_text=self._gjb_body(),
        )
        reference_only = (
            analysis_service._build_data_standard_classification_profile(
                file_name="radar.txt",
                original_name="radar-equipment-overview.pdf",
                original_text=(
                    "雷达装备性能资料\n"
                    "该装备设计参考 GJB 9001C-2017，但本文不是标准正文。"
                ),
            )
        )
        same_name_reference_only = (
            analysis_service._build_data_standard_classification_profile(
                file_name="storage.txt",
                original_name="GJB 9001C-2017.pdf",
                original_text=(
                    "项目质量管理实施报告\n"
                    "本项目参考 GJB 9001C-2017 编制，但本文不是标准正文。"
                ),
            )
        )

        self.assertTrue(confirmed.identity_confirmed)
        self.assertEqual(confirmed.document_kind, "standard_body")
        self.assertEqual(confirmed.standard_number, "GJB 9001C-2017")
        self.assertEqual(confirmed.title, "质量管理体系要求")
        self.assertFalse(reference_only.identity_confirmed)
        self.assertEqual(reference_only.document_kind, "reference_only")
        self.assertFalse(same_name_reference_only.identity_confirmed)
        self.assertEqual(
            same_name_reference_only.document_kind,
            "reference_only",
        )

    def test_scope_guard_limits_gjb_candidates_to_six_standard_leaves(self):
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(file_name, self._data_standard_tree())
            Path(tmp, file_name).write_text(self._gjb_body(), encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":105}',
                        sources=(self.SOURCE,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    )
                ],
            )
            with patch(
                "app.services.llm_service.analysis_service."
                "build_data_standard_classification_prompt",
                wraps=analysis_service.build_data_standard_classification_prompt,
            ) as standard_prompt:
                task_service, task, recall, _rag, _knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    data_standard_mode="scope_guard",
                )
            interaction = task_service.get_llm_interactions(
                "file",
                file_name,
            )[0]
            attempts = task_service.get_llm_interaction_attempts(
                interaction["id"]
            )

        visible_ids = {
            item["id"] for item in recall["final_candidates"]
        }
        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 105)
        self.assertEqual(visible_ids, set(range(101, 107)))
        self.assertNotIn(654, visible_ids)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            ["architecture_classification", "analysis_extraction"],
        )
        standard_prompt.assert_called_once()
        prompt_candidates = standard_prompt.call_args.args[1]
        prompt_context = standard_prompt.call_args.kwargs["standard_context"]
        self.assertEqual(
            {item["id"] for item in prompt_candidates},
            set(range(101, 107)),
        )
        self.assertEqual(
            prompt_context["standardTitle"],
            "质量管理体系要求",
        )

    def test_scope_guard_keeps_topk_single_rollback_inside_six_standard_leaves(
        self,
    ):
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(file_name, self._data_standard_tree())
            Path(tmp, file_name).write_text(self._gjb_body(), encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(
                            file_name,
                            architecture_id=105,
                        ),
                        sources=(self.SOURCE,),
                    )
                ],
            )
            task_service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                mode="topk_single",
                data_standard_mode="scope_guard",
            )
            interaction = task_service.get_llm_interactions(
                "file",
                file_name,
            )[0]
            attempts = task_service.get_llm_interaction_attempts(
                interaction["id"]
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 105)
        self.assertEqual(
            {item["id"] for item in recall["final_candidates"]},
            set(range(101, 107)),
        )
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            ["analysis"],
        )
        self.assertIn("数据标准作用域分类补充规则", interaction["prompt"])

    def test_scope_guard_repairs_then_falls_back_to_general_requirement(self):
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(file_name, self._data_standard_tree())
            Path(tmp, file_name).write_text(self._gjb_body(), encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":null}',
                        sources=(self.SOURCE,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":null}',
                        sources=(self.SOURCE,),
                    ),
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    ),
                ],
            )
            task_service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                data_standard_mode="scope_guard",
            )
            interaction = task_service.get_llm_interactions(
                "file",
                file_name,
            )[0]
            attempts = task_service.get_llm_interaction_attempts(
                interaction["id"]
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 105)
        self.assertEqual(recall["returned_architecture_id"], 105)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [
                "architecture_classification",
                "architecture_repair",
                "analysis_extraction",
            ],
        )

    def test_scope_guard_uses_general_requirement_when_classification_budget_is_exhausted(
        self,
    ):
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(file_name, self._data_standard_tree())
            Path(tmp, file_name).write_text(self._gjb_body(), encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=None,
                        failure_stage="response",
                        error_message="首次分类暂态失败",
                    ),
                    FakeRagOutcome(
                        text='{"architectureId":null}',
                        sources=(self.SOURCE,),
                    ),
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    ),
                ],
            )
            task_service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                data_standard_mode="scope_guard",
            )
            interaction = task_service.get_llm_interactions(
                "file",
                file_name,
            )[0]
            attempts = task_service.get_llm_interaction_attempts(
                interaction["id"]
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 105)
        self.assertEqual(recall["returned_architecture_id"], 105)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [
                "architecture_classification",
                "architecture_classification",
                "analysis_extraction",
            ],
        )

    def test_scope_guard_without_general_requirement_fails_closed(self):
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(
                file_name,
                self._data_standard_tree(include_general=False),
            )
            Path(tmp, file_name).write_text(self._gjb_body(), encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":null}',
                        sources=(self.SOURCE,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":null}',
                        sources=(self.SOURCE,),
                    )
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                data_standard_mode="scope_guard",
            )

        self.assertEqual(task["status"], "3")
        self.assertEqual(task["result_payload"]["data"]["status"], "3")
        self.assertIsNone(recall["returned_architecture_id"])
        self.assertEqual(recall["failure_stage"], "architecture_contract")

    def test_repairs_share_phase_budgets_and_total_model_calls_are_four(self):
        with workspace_tempdir() as tmp:
            file_name = "four-calls.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":999}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text='{"architectureId":11}', sources=(self.SOURCE,)),
                    FakeRagOutcome(text="```json\n{bad}\n```", sources=(self.SOURCE,)),
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,)),
                ],
            )
            task_service, task, _recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [
                "architecture_classification",
                "architecture_repair",
                "analysis_extraction",
                "json_repair",
            ],
        )

    def test_supplier_retry_consumes_classification_budget_and_prevents_repair(self):
        with workspace_tempdir() as tmp:
            file_name = "retry-budget.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=None,
                        sources=(),
                        failure_stage="provider",
                        error_message="temporary",
                    ),
                    FakeRagOutcome(text='{"architectureId":999}', sources=(self.SOURCE,)),
                ],
            )
            task_service, task, recall, _rag, knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "3")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(recall["failure_stage"], "architecture_contract")
        self.assertEqual(len(knowledge.ports), 0)

    def test_recall_audit_failure_prevents_remote_session_creation(self):
        with workspace_tempdir() as tmp:
            file_name = "audit-block.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":11}', sources=(self.SOURCE,))
                ]
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = task_service.create_file_task(file_name, request)
            Path(tmp, file_name).write_text("CVN-78", encoding="utf-8")
            with (
                patch(
                    "app.services.llm_service.analysis_service.download_to_temp_file",
                    return_value=str(Path(tmp, file_name)),
                ),
                patch(
                    "app.services.llm_service.analysis_service.normalize_file_for_llm",
                    side_effect=lambda path: path,
                ),
                patch(
                    "app.services.llm_service.analysis_service.prepare_analysis_file_for_upload",
                    side_effect=lambda path, *_args: path,
                ),
                patch(
                    "app.services.llm_service.analysis_service.recall_architecture_candidates",
                    side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                ),
                patch.object(
                    task_service,
                    "upsert_architecture_recall_decision",
                    side_effect=OSError("recall audit unavailable"),
                ),
            ):
                analysis_service.run_file_analysis_task(
                    task_service=task_service,
                    progress_hub=LLMProgressHub(),
                    request_payload=request,
                    download_root=tmp,
                    callback_url="",
                    callback_timeout=5,
                    document_rag_factory=rag_factory,
                    knowledge_index_factory=FakeKnowledgeIndexFactory(),
                    execution_id=task["execution_id"],
                )
            task = task_service.get_task("file", file_name)

        self.assertEqual(task["status"], "3")
        self.assertEqual(len(rag_factory.ports), 0)

    def test_oversized_classification_prompt_fails_before_remote_session(self):
        with workspace_tempdir() as tmp:
            file_name = "prompt-budget.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory()
            with patch(
                "app.services.llm_service.analysis_service.build_architecture_classification_prompt",
                return_value="x" * 32_001,
            ):
                _service, task, recall, _rag, knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                )

        self.assertEqual(task["status"], "3")
        self.assertEqual(recall["failure_stage"], "architecture_prompt_budget")
        self.assertEqual(len(rag_factory.ports), 0)
        self.assertEqual(len(knowledge.ports), 0)

    def test_recall_finalize_failure_blocks_permanent_knowledge_store(self):
        with workspace_tempdir() as tmp:
            file_name = "finalize-block.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":11}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            knowledge = FakeKnowledgeIndexFactory()
            with patch.object(
                LLMTaskService,
                "finalize_architecture_recall_decision",
                side_effect=OSError("finalize unavailable"),
            ):
                _service, task, recall, _rag, knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    knowledge_factory=knowledge,
                    recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                )

        self.assertEqual(task["status"], "3")
        self.assertIsNone(recall["finalized_at"])
        self.assertEqual(len(knowledge.ports), 0)

    def test_topk_single_keeps_bounded_parent_projection_and_audit(self):
        with workspace_tempdir() as tmp:
            file_name = "topk-single.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name, architecture_id=10),
                        sources=(self.SOURCE,),
                    )
                ]
            )
            task_service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                mode="topk_single",
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 10)
        self.assertEqual(recall["returned_architecture_id"], 10)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])
        self.assertIn("topk_single 受限候选补充合同", interaction["prompt"])
        self.assertIn('"nodeType":"parent"', interaction["prompt"])

    def test_legacy_small_tree_is_audited_before_success(self):
        with workspace_tempdir() as tmp:
            file_name = "legacy-success.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name, architecture_id=11),
                        sources=(self.SOURCE,),
                    )
                ]
            )
            task_service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                mode="legacy",
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 11)
        self.assertEqual(recall["returned_architecture_id"], 11)
        self.assertEqual(recall["returned_rank"], 3)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])

    def test_legacy_rejects_root_from_initial_and_repair_results(self):
        with workspace_tempdir() as tmp:
            file_name = "legacy-root.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name, architecture_id=1),
                        sources=(self.SOURCE,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":1}',
                        sources=(self.SOURCE,),
                    )
                ],
            )
            task_service, task, recall, _rag, knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                mode="legacy",
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "3")
        self.assertEqual(recall["failure_stage"], "architecture_contract")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(knowledge.ports), 0)

    def test_legacy_tree_over_global_candidate_limit_fails_before_session(self):
        tree = [{"id": 1, "name": "root", "parentId": None}]
        tree.extend(
            {"id": index, "name": f"leaf-{index}", "parentId": 1}
            for index in range(2, 130)
        )
        with workspace_tempdir() as tmp:
            file_name = "legacy-too-many.txt"
            request = self._request(file_name, tree)
            rag_factory = FakeDocumentRagFactory()
            _service, task, recall, _rag, knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                mode="legacy",
            )

        self.assertEqual(task["status"], "3")
        self.assertEqual(recall["failure_stage"], "architecture_prompt_budget")
        self.assertEqual(len(rag_factory.ports), 0)
        self.assertEqual(len(knowledge.ports), 0)

    def test_factory_acquisition_failure_finalizes_existing_recall_audit(self):
        with workspace_tempdir() as tmp:
            file_name = "factory-failure.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory()
            knowledge = FakeKnowledgeIndexFactory()
            with patch.object(
                rag_factory,
                "create",
                side_effect=OSError("factory unavailable"),
            ):
                _service, task, recall, _rag, knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    knowledge_factory=knowledge,
                    recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                )

        self.assertEqual(task["status"], "3")
        self.assertEqual(recall["failure_stage"], "architecture_contract")
        self.assertIsNotNone(recall["finalized_at"])
        self.assertEqual(len(rag_factory.ports), 0)
        self.assertEqual(len(knowledge.ports), 0)

    def test_factory_exit_failure_does_not_overwrite_committed_success(self):
        with workspace_tempdir() as tmp:
            file_name = "factory-exit-failure.txt"
            request = self._request(
                file_name,
                [{"id": 11, "name": "CVN-78-基础数据", "parentId": 10}],
            )
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    )
                ]
            )
            original_create = rag_factory.create

            @contextmanager
            def create_with_exit_failure():
                with original_create() as port:
                    yield port
                raise OSError("transport close failed")

            with patch.object(rag_factory, "create", side_effect=create_with_exit_failure):
                _service, task, recall, _rag, knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 11)
        self.assertEqual(recall["returned_architecture_id"], 11)
        self.assertEqual(len(knowledge.ports), 1)

    def test_topk_single_json_failure_uses_analysis_extraction_stage(self):
        with workspace_tempdir() as tmp:
            file_name = "topk-single-json-failure.txt"
            request = self._request(file_name, self._tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text="{bad", sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text="still bad", sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(index),
                mode="topk_single",
            )

        self.assertEqual(task["status"], "3")
        self.assertEqual(recall["failure_stage"], "analysis_extraction")
        self.assertEqual(len(knowledge.ports), 0)

    def test_extraction_repair_prompt_budget_is_checked_before_second_call(self):
        with workspace_tempdir() as tmp:
            file_name = "extraction-repair-budget.txt"
            request = self._request(
                file_name,
                [{"id": 11, "name": "CVN-78-基础数据", "parentId": 10}],
            )
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text="{bad", sources=(self.SOURCE,))
                ]
            )
            with patch(
                "app.services.llm_service.analysis_service.build_json_repair_prompt",
                return_value="x" * 32_001,
            ):
                task_service, task, recall, _rag, knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "3")
        self.assertEqual(recall["failure_stage"], "architecture_prompt_budget")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(knowledge.ports), 0)

    def test_explicit_gjb_signal_overrides_valid_non_standard_model_choice(self):
        tree = [
            {"id": 100, "name": "数据标准", "parentId": None},
            {"id": 101, "name": "建模与仿真", "parentId": 100},
            {"id": 102, "name": "军用软件", "parentId": 100},
            {"id": 103, "name": "目标特性", "parentId": 100},
            {"id": 104, "name": "术语与定义", "parentId": 100},
            {"id": 105, "name": "通用要求", "parentId": 100},
            {"id": 106, "name": "元数据", "parentId": 100},
            {"id": 600, "name": "其他资料", "parentId": None},
            {"id": 654, "name": "普通资料叶", "parentId": 600},
        ]
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(file_name, tree)
            Path(tmp, file_name).write_text("GJB 9001C-2017 国家军用标准", encoding="utf-8")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":654}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (101, 102, 103, 104, 105, 106, 654),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 105)
        self.assertEqual(recall["returned_architecture_id"], 105)

    def test_strong_gjb_filename_keeps_model_selected_visible_standard_leaf(self):
        tree = [
            {"id": 100, "name": "数据标准", "parentId": None},
            {"id": 101, "name": "建模与仿真", "parentId": 100},
            {"id": 102, "name": "军用软件", "parentId": 100},
            {"id": 103, "name": "目标特性", "parentId": 100},
            {"id": 104, "name": "术语与定义", "parentId": 100},
            {"id": 105, "name": "通用要求", "parentId": 100},
            {"id": 106, "name": "元数据", "parentId": 100},
        ]
        with workspace_tempdir() as tmp:
            file_name = "GJB-Z 9001C-2017.txt"
            request = self._request(file_name, tree)
            request["params"][0]["originalFileName"] = "quality-system.pdf"
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":103}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (101, 102, 103, 104, 105, 106),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 103)
        self.assertEqual(recall["returned_architecture_id"], 103)

    def test_body_gjb_reference_does_not_override_valid_equipment_choice(self):
        tree = [
            {"id": 100, "name": "数据标准", "parentId": None},
            {"id": 101, "name": "建模与仿真", "parentId": 100},
            {"id": 102, "name": "军用软件", "parentId": 100},
            {"id": 103, "name": "目标特性", "parentId": 100},
            {"id": 104, "name": "术语与定义", "parentId": 100},
            {"id": 105, "name": "通用要求", "parentId": 100},
            {"id": 106, "name": "元数据", "parentId": 100},
            {"id": 600, "name": "装备资料", "parentId": None},
            {"id": 654, "name": "雷达装备叶", "parentId": 600},
        ]
        with workspace_tempdir() as tmp:
            file_name = "radar-equipment-overview.txt"
            request = self._request(file_name, tree)
            Path(tmp, file_name).write_text(
                "该装备设计参考 GJB 9001C-2017，但本文是装备性能资料。",
                encoding="utf-8",
            )
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":654}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (101, 102, 103, 104, 105, 106, 654),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 654)
        self.assertEqual(recall["returned_architecture_id"], 654)

    def test_strong_gjb_filename_does_not_select_invisible_standard_leaf(self):
        tree = [
            {"id": 100, "name": "数据标准", "parentId": None},
            {"id": 101, "name": "建模与仿真", "parentId": 100},
            {"id": 102, "name": "军用软件", "parentId": 100},
            {"id": 600, "name": "其他资料", "parentId": None},
            {"id": 654, "name": "普通资料甲", "parentId": 600},
            {"id": 655, "name": "普通资料乙", "parentId": 600},
        ]
        with workspace_tempdir() as tmp:
            file_name = "GJB 9001C-2017.txt"
            request = self._request(file_name, tree)
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":654}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (654, 655),
                ),
            )

        self.assertEqual(task["status"], "3")
        self.assertEqual(task["result_payload"]["data"]["status"], "3")
        self.assertIsNone(recall["returned_architecture_id"])
        self.assertEqual(recall["failure_stage"], "architecture_contract")

    @staticmethod
    def _two_equipment_tree() -> list[dict]:
        detail_kinds = (
            "基础数据",
            "战技指标",
            "运用数据",
            "效能数据",
            "模型数据",
            "目特数据",
            "声像数据",
        )
        nodes = [
            {"id": 1, "name": "装备型号", "parentId": None},
            {"id": 56, "name": "CVN-68", "parentId": 1},
            {"id": 67, "name": "CVN-78", "parentId": 1},
            {"id": 515, "name": "AN/SPS-48", "parentId": 1},
        ]
        for parent_id, parent_name, first_leaf_id in (
            (56, "CVN-68", 561),
            (67, "CVN-78", 671),
            (515, "AN/SPS-48", 516),
        ):
            nodes.extend(
                {
                    "id": first_leaf_id + offset,
                    "name": f"{parent_name}-{kind}",
                    "parentId": parent_id,
                }
                for offset, kind in enumerate(detail_kinds)
            )
        return nodes

    def test_unique_cvn68_filename_identifier_forces_cross_branch_choice_to_parent(self):
        with workspace_tempdir() as tmp:
            file_name = "e2e-topk-cvn68-20260715.txt"
            request = self._request(file_name, self._two_equipment_tree())
            request["params"][0]["originalFileName"] = (
                "Nimitz (CVN 68) class (CVNM) 16-Aug-2023.pdf"
            )
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":515}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (561, 562, 516, 56, 515),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 56)
        self.assertEqual(recall["returned_architecture_id"], 56)
        self.assertEqual(recall["returned_rank"], 4)

    def test_unique_cvn78_filename_identifier_keeps_correct_visible_descendant(self):
        with workspace_tempdir() as tmp:
            file_name = "Gerald R Ford (CVN 78) class 14-Jul-2023.txt"
            request = self._request(file_name, self._two_equipment_tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":671}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (671, 516, 67, 515),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 671)
        self.assertEqual(recall["returned_architecture_id"], 671)
        self.assertEqual(recall["returned_rank"], 1)

    def test_ambiguous_or_non_numeric_filename_does_not_force_equipment_branch(self):
        cases = (
            "CVN-68 + CVN-78 comparison.txt",
            "Nimitz carrier overview.txt",
            "Nimitz CVN-680 boundary.txt",
        )
        for file_name in cases:
            with self.subTest(file_name=file_name), workspace_tempdir() as tmp:
                request = self._request(file_name, self._two_equipment_tree())
                rag_factory = FakeDocumentRagFactory(
                    analyse_outcomes=[
                        FakeRagOutcome(text='{"architectureId":515}', sources=(self.SOURCE,))
                    ],
                    ask_outcomes=[
                        FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                    ],
                )
                _service, task, recall, _rag, _knowledge = self._run(
                    tmp=tmp,
                    request=request,
                    rag_factory=rag_factory,
                    recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                        index,
                        (561, 671, 516, 56, 67, 515),
                    ),
                )

            self.assertEqual(task["status"], "2")
            self.assertEqual(task["result_payload"]["data"]["architectureId"], 515)
            self.assertEqual(recall["returned_architecture_id"], 515)

    def test_filename_match_does_not_force_invisible_equipment_parent(self):
        with workspace_tempdir() as tmp:
            file_name = "Nimitz CVN-68 overview.txt"
            request = self._request(file_name, self._two_equipment_tree())
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":515}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                # 56 在完整树存在，但没有进入模型可见候选。
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (561, 516, 515),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 515)
        self.assertEqual(recall["returned_architecture_id"], 515)

    def test_duplicate_equipment_identifier_parents_do_not_force_branch(self):
        tree = self._two_equipment_tree()
        detail_kinds = (
            "基础数据", "战技指标", "运用数据", "效能数据",
            "模型数据", "目特数据", "声像数据",
        )
        tree.append({"id": 68, "name": "CVN-68", "parentId": 1})
        tree.extend(
            {
                "id": 681 + offset,
                "name": f"CVN-68-{kind}",
                "parentId": 68,
            }
            for offset, kind in enumerate(detail_kinds)
        )
        with workspace_tempdir() as tmp:
            file_name = "Nimitz CVN-68 duplicate.txt"
            request = self._request(file_name, tree)
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(text='{"architectureId":515}', sources=(self.SOURCE,))
                ],
                ask_outcomes=[
                    FakeRagOutcome(text=self._extraction(file_name), sources=(self.SOURCE,))
                ],
            )
            _service, task, recall, _rag, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
                recall_side_effect=lambda index, *_args, **_kwargs: self._decision(
                    index,
                    (561, 681, 516, 56, 68, 515),
                ),
            )

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 515)
        self.assertEqual(recall["returned_architecture_id"], 515)

    def test_docx_body_is_read_before_session_and_reused_by_mapper(self):
        with workspace_tempdir() as tmp:
            file_name = "single.docx"
            docx_path = Path(tmp, file_name)
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>CVN-78 Word 正文证据</w:t></w:r></w:p></w:body>'
                '</w:document>'
            )
            with zipfile.ZipFile(docx_path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            request = self._request(
                file_name,
                [{"id": 11, "name": "CVN-78-基础数据", "parentId": 10}],
            )
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._extraction(file_name),
                        sources=(self.SOURCE,),
                    )
                ]
            )
            _service, task, _recall, rag_factory, _knowledge = self._run(
                tmp=tmp,
                request=request,
                rag_factory=rag_factory,
            )

        self.assertEqual(task["status"], "2")
        self.assertIn(
            "CVN-78 Word 正文证据",
            task["result_payload"]["data"]["fileDataItem"]["originalText"],
        )
        self.assertEqual(len(rag_factory.ports), 1)


if __name__ == "__main__":
    unittest.main()
