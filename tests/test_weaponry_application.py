"""阶段 1D-4：武器谱 Submit/Run Application 与严格 Fake 验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Event
import unittest
from unittest.mock import patch

from app.adapters.web.flask.weaponry_requests import parse_weaponry_request
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskClaimOutcome, TaskSubmissionOutcome
from app.modules.weaponry.application import (
    RunWeaponryOutcome,
    RunWeaponryTask,
    SubmitWeaponryTask,
    WeaponryAuditError,
    WeaponryFieldExecutor,
    WeaponryTaskConflictError,
    WeaponryTaskPersistenceError,
)
from app.modules.weaponry.adapters import (
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
    WeaponryTaskCommandCodec,
)
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    DOCUMENT_SCOPE_CATEGORY,
    DOCUMENT_SCOPE_EXPLICIT,
    EVIDENCE_SCORE_MODE_RANK,
    EVIDENCE_SCORE_MODE_SCORE,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    EXTRACTION_PROMPT_VERSION,
    FILE_AGGREGATE_STRATEGY,
    MAX_TABLE_ROWS,
    TABLE_MERGE_POLICY_VERSION,
    WEAPONRY_STATUS_FAILED,
    WEAPONRY_STATUS_SUCCEEDED,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceCandidate,
    EvidenceSelectionPolicy,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryExecutionPolicySnapshot,
    WeaponrySubmission,
    WeaponryResult,
)
from app.modules.weaponry.ports import (
    ExtractionAnswer,
    ExtractionValidationOutcome,
    SearchTargetEvidence,
    TargetEvidenceScope,
    TargetEvidenceSearchResult,
    WeaponryAuditOutcome,
    WeaponryAuditReserveOutcome,
    WeaponryAuditReserveResult,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCallIdentity,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryOperation,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponrySourceBoundaryError,
    WeaponryTrackedResource,
    WeaponryTranslationOutcome,
    WeaponryTranslationResult,
)
from app.services.llm_service.task_service import LLMTaskService
from tests.fakes import (
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryCallbackPort,
    FakeWeaponryDispatcherPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryTaskCommandPort,
    FakeWeaponryTranslationPort,
    WeaponryInvocationRecorder,
)
from tests import workspace_tempdir


_ASSET_PATH = (
    Path(__file__).resolve().parent
    / "contracts"
    / "stage1d4_weaponry_application.json"
)
_PROFILE_ID = "stage1d4-test-profile-v1"
_PROVIDER_FINGERPRINT = "stage1d4-provider-v1"
_EMBEDDING_FINGERPRINT = "stage1d4-embedding-v1"

_EVIDENCE_A1 = "甲舰是甲级首舰，承担远洋警戒、防空指挥和编队协同等多项任务。"
_EVIDENCE_A2 = "甲级舰艇采用远洋任务设计，并具备持续部署和综合保障能力。"
_EVIDENCE_B1 = "乙舰是乙级二号舰，主要承担近海巡逻、护航和区域防御等任务。"


def _profile() -> EvidenceSelectionPolicy:
    """构造只用于离线 Application 验收、绝不代表生产阈值的策略快照。"""

    return EvidenceSelectionPolicy(
        profile_id=_PROFILE_ID,
        provider_fingerprint=_PROVIDER_FINGERPRINT,
        embedding_fingerprint=_EMBEDDING_FINGERPRINT,
        document_processing_fingerprint="stage1d4-processing-v1",
        input_candidate_top_n=12,
        table_candidate_top_n=20,
        reject_reference_like=True,
    )


def _execution_policy() -> WeaponryExecutionPolicySnapshot:
    return WeaponryExecutionPolicySnapshot(
        extraction_strategy=FILE_AGGREGATE_STRATEGY,
        extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
        extraction_context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
        extraction_model_fingerprint="stage1d4-extraction-model-v1",
        table_merge_policy_version=TABLE_MERGE_POLICY_VERSION,
        max_table_rows=MAX_TABLE_ROWS,
    )


def _submission(
    *,
    table: bool = False,
    document_count: int = 2,
    architecture_id: int = 10502,
    field_override: dict[str, object] | None = None,
) -> WeaponrySubmission:
    """构造包含真实公开投影、冻结文档范围和内部策略的提交命令。"""

    if document_count not in {1, 2}:
        raise ValueError("测试 document_count 只支持 1 或 2")
    if table:
        field = {
            "templateClassifyId": 1772442376645741,
            "fieldName": "雷达设备",
            "fieldType": "TABLE",
            "fieldDescription": "按雷达型号逐行提取，不合并不同设备",
            "tableFieldList": [
                [
                    {
                        "fieldName": "型号",
                        "fieldType": "INPUT",
                        "fieldDescription": "雷达的正式型号",
                        "analyseData": "",
                        "analyseDataSource": [],
                    },
                    {
                        "fieldName": "用途",
                        "fieldType": "INPUT",
                        "fieldDescription": "搜索、跟踪或火控用途",
                        "analyseData": "",
                        "analyseDataSource": [],
                    },
                ]
            ],
        }
    else:
        field = {
            "templateClassifyId": 1772442376645740,
            "fieldName": "舰级名称",
            "fieldType": "INPUT",
            "fieldDescription": "提取装备所属舰级的正式名称，不要与单舰名称混淆",
            "analyseData": "",
            "analyseDataSource": [],
        }

    if field_override is not None:
        field = field_override

    file_names = ("a-hash.pdf", "b-hash.pdf")[:document_count]
    payload = {
        "businessType": "weaponry",
        "params": {
            "architectureId": f"{architecture_id:06d}",
            "filePathList": [
                f"http://files.local/{file_name}" for file_name in file_names
            ],
            "weaponryTemplateFieldList": [field],
        },
    }
    parsed = parse_weaponry_request(payload)
    source_names = ("甲舰资料.pdf", "乙舰资料.pdf")
    documents = tuple(
        WeaponryDocumentSnapshot(
            sequence_no=index,
            document_key=f"doc-{'a' if index == 1 else 'b'}",
            file_name=file_name,
            original_name=source_names[index - 1],
            ingested_file_name=f"normalized-{file_name}",
            source_architecture_id=20000 + index,
            external_document_ref=f"custom-documents/doc-{index}.json",
            anything_document_id=f"anything-doc-{index}",
        )
        for index, file_name in enumerate(file_names, start=1)
    )
    return parsed.to_submission(
        document_scope=WeaponryDocumentScope(
            mode=DOCUMENT_SCOPE_EXPLICIT,
            requested_file_names=file_names,
            documents=documents,
        ),
        evidence_selection_policy=_profile(),
        execution_policy=_execution_policy(),
        auxiliary_guidance_policy=AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_NONE,
            catalog_fingerprint="",
            top_n=0,
            max_context_chars=0,
        ),
        trace_id=f"trace-stage1d4-application-{architecture_id}",
    )


def _forced_input_field(field_name: str) -> dict[str, object]:
    return {
        "templateClassifyId": 1772442376645750,
        "fieldName": field_name,
        "fieldType": "INPUT",
        "fieldDescription": "该字段由甲方维护，模型不得生成",
        "futureExtension": {"keep": "input"},
        "analyseData": "",
        "analyseDataSource": [],
    }


def _forced_table_field(*, mixed: bool) -> dict[str, object]:
    column_names = [
        "装备编号",
        "一级分类",
        "二级分类",
        "三级分类",
        "四级分类",
    ]
    if mixed:
        column_names[1:1] = ["型号"]
        column_names.append("用途")
    return {
        "templateClassifyId": 1772442376645751,
        "fieldName": "装备明细",
        "fieldType": "TABLE",
        "fieldDescription": "按装备逐行提取普通业务字段",
        "futureExtension": {"keep": "table"},
        "tableFieldList": [
            [
                {
                    "fieldName": field_name,
                    "fieldType": "INPUT",
                    "fieldDescription": f"{field_name}说明",
                    "futureColumnKey": index,
                    "analyseData": "",
                    "analyseDataSource": [],
                }
                for index, field_name in enumerate(column_names, start=1)
            ]
        ],
    }


def _candidate(
    candidate_id: str,
    document_key: str,
    text: str,
    *,
    rank: object,
    score: object = 0.9,
    score_present: bool = True,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=candidate_id,
        document_key=document_key,
        text=text,
        provider_rank=rank,
        provider_score=score,
        provider_score_present=score_present,
        score_profile_id=_PROFILE_ID,
    )


def _empty_category_submission() -> WeaponrySubmission:
    """构造已批准的“空类别范围仍受理、随后异步失败”命令。"""

    base = _submission(document_count=1)
    projection = base.request_projection.to_dict()
    projection["params"]["filePathList"] = []
    parsed = parse_weaponry_request(projection)
    return parsed.to_submission(
        document_scope=WeaponryDocumentScope(
            mode=DOCUMENT_SCOPE_CATEGORY,
            requested_file_names=(),
            documents=(),
        ),
        evidence_selection_policy=base.evidence_selection_policy,
        execution_policy=base.execution_policy,
        auxiliary_guidance_policy=base.auxiliary_guidance_policy,
        trace_id="trace-stage1d4-empty-category",
    )


class _WeaponryHarness:
    """为每个用例建立无数据库、文件、网络和供应商进程的隔离装配。"""

    def __init__(self, submission: WeaponrySubmission | None = None) -> None:
        self.submission = submission or _submission()
        self.recorder = WeaponryInvocationRecorder()
        self.tasks = FakeWeaponryTaskCommandPort(self.recorder)
        self.progress = FakeWeaponryProgressPublisherPort(self.recorder)
        self.dispatcher = FakeWeaponryDispatcherPort(self.recorder)
        self.retrieval = FakeTargetEvidenceRetrievalPort(self.recorder)
        self.extraction = FakeEvidenceExtractionPort(self.recorder)
        self.guidance = FakeAuxiliaryGuidancePort(self.recorder)
        self.translation = FakeWeaponryTranslationPort(self.recorder)
        self.audit = FakeWeaponryInteractionAuditPort(self.recorder)
        self.callbacks = FakeWeaponryCallbackPort(self.recorder)
        self.resources = FakeWeaponryResourceStorePort(self.recorder)
        self.submit_service = SubmitWeaponryTask(
            task_commands=self.tasks,
            progress_publisher=self.progress,
            dispatcher=self.dispatcher,
        )
        self.submit_result = self.submit_service.execute(self.submission)
        self.task_id = self.submit_result.task_id
        self.callbacks.set_latest(
            self.task_id,
            self.submission.architecture_id,
        )
        self.callbacks.delivery_results[self.task_id] = (
            WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.SUCCESS
            )
        )
        self.field_executor = WeaponryFieldExecutor(
            retrieval=self.retrieval,
            extraction=self.extraction,
            guidance=self.guidance,
            translation=self.translation,
            audit=self.audit,
        )
        self.run_service = RunWeaponryTask(
            task_commands=self.tasks,
            progress_publisher=self.progress,
            retrieval=self.retrieval,
            field_executor=self.field_executor,
            callbacks=self.callbacks,
            resources=self.resources,
        )
        # Run 用例不混入受理阶段的事件；Submit 顺序由独立用例冻结。
        self.recorder.clear()
        self.progress.publications.clear()

    @property
    def scope_ref(self) -> str:
        return f"fake-retrieval-scope:{self.task_id.value}"

    def call(
        self,
        operation: WeaponryOperation,
        *,
        document_sequence: int | None = None,
        attempt_no: int = 1,
        item_sequence: int | None = None,
    ) -> WeaponryCallIdentity:
        return WeaponryCallIdentity(
            task_id=self.task_id,
            field_sequence=1,
            document_sequence=document_sequence,
            operation=operation,
            attempt_no=attempt_no,
            item_sequence=item_sequence,
        )

    def configure_retrieval(
        self,
        candidates: tuple[EvidenceCandidate, ...],
        *,
        score_mode: str = EVIDENCE_SCORE_MODE_SCORE,
    ) -> WeaponryCallIdentity:
        call = self.call(WeaponryOperation.TARGET_RETRIEVAL)
        self.retrieval.search_results[call.attempt_key] = (
            TargetEvidenceSearchResult(
                scope_ref=self.scope_ref,
                call=call,
                candidates=candidates,
                score_mode=score_mode,
                provider_fingerprint=_PROVIDER_FINGERPRINT,
                embedding_fingerprint=_EMBEDDING_FINGERPRINT,
            )
        )
        return call

    def configure_answer(
        self,
        *,
        document_sequence: int,
        evidence_ids: tuple[str, ...],
        text: str,
        attempt_no: int = 1,
    ) -> WeaponryCallIdentity:
        call = self.call(
            WeaponryOperation.EVIDENCE_EXTRACTION,
            document_sequence=document_sequence,
            attempt_no=attempt_no,
        )
        raw = text.encode("utf-8")
        self.extraction.results[call.attempt_key] = ExtractionAnswer(
            call=call,
            text=text,
            raw_response_digest=hashlib.sha256(raw).hexdigest(),
            raw_response_chars=len(text),
            evidence_ids=evidence_ids,
            sources=(),
            validation_outcome=(
                ExtractionValidationOutcome.MATCHED
                if text
                else ExtractionValidationOutcome.EMPTY_ANSWER
            ),
        )
        return call

    def configure_translation(
        self,
        *,
        document_sequence: int,
        item_sequence: int,
        translated_text: str = "",
    ) -> WeaponryCallIdentity:
        call = self.call(
            WeaponryOperation.TRANSLATION,
            document_sequence=document_sequence,
            item_sequence=item_sequence,
        )
        if translated_text:
            result = WeaponryTranslationResult(
                call=call,
                text=translated_text,
                outcome=WeaponryTranslationOutcome.SUCCEEDED,
            )
        else:
            result = WeaponryTranslationResult(
                call=call,
                text="",
                outcome=WeaponryTranslationOutcome.FAILED,
                error_code="translation_unavailable",
            )
        self.translation.results[call.attempt_key] = result
        return call

    def configure_golden_input(self) -> None:
        self.configure_retrieval(
            (
                _candidate("b-1", "doc-b", _EVIDENCE_B1, rank=1, score=0.99),
                _candidate("a-1", "doc-a", _EVIDENCE_A1, rank=2, score=0.96),
                _candidate("a-2", "doc-a", _EVIDENCE_A2, rank=3, score=0.95),
            )
        )
        self.configure_answer(
            document_sequence=1,
            evidence_ids=("a-1", "a-2"),
            text="该舰属于甲级",
        )
        self.configure_answer(
            document_sequence=2,
            evidence_ids=("b-1",),
            text="该舰属于乙级",
        )
        # 既有协议允许翻译为空；翻译失败是字段内降级，不改变成功终态。
        self.configure_translation(document_sequence=1, item_sequence=1)
        self.configure_translation(document_sequence=2, item_sequence=1)


class _BlockingTargetEvidenceRetrievalPort(FakeTargetEvidenceRetrievalPort):
    """用 Event 精确暂停慢检索，不依赖随机 sleep 推测锁状态。"""

    def __init__(self, recorder: WeaponryInvocationRecorder) -> None:
        # 本测试使用真实 SQLite Audit/Resource Adapter；它们不会向 Fake recorder
        # 写事件，因此这里关闭只属于严格 Fake 组合的前置事件检查。
        super().__init__(recorder, enforce_call_order=False)
        self.entered = Event()
        self.release = Event()

    def search_target(
        self,
        command: SearchTargetEvidence,
    ) -> TargetEvidenceSearchResult:
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("测试未及时释放慢检索")
        return super().search_target(command)


class SubmitWeaponryApplicationTests(unittest.TestCase):
    """冻结“持久事实先于通知”和 409 冲突语义。"""

    def test_submit_persists_before_progress_and_dispatch(self) -> None:
        recorder = WeaponryInvocationRecorder()
        tasks = FakeWeaponryTaskCommandPort(recorder)
        progress = FakeWeaponryProgressPublisherPort(recorder)
        dispatcher = FakeWeaponryDispatcherPort(recorder)
        service = SubmitWeaponryTask(
            task_commands=tasks,
            progress_publisher=progress,
            dispatcher=dispatcher,
        )

        result = service.execute(_submission(document_count=1))

        self.assertEqual(
            ["task.create", "progress.publish", "dispatcher.dispatch"],
            [event.operation for event in recorder.events],
        )
        self.assertTrue(result.progress_notified)
        self.assertTrue(result.dispatcher_notified)
        self.assertEqual("weaponry", tasks.submission_calls[0].task_type)
        self.assertEqual(
            TaskBusinessRef("weaponry", "10502"),
            tasks.submission_calls[0].business_ref,
        )
        self.assertEqual("accepted", tasks.executions[result.task_id].execution_state)

    def test_post_commit_notifications_can_fail_without_losing_acceptance(self) -> None:
        recorder = WeaponryInvocationRecorder()
        tasks = FakeWeaponryTaskCommandPort(recorder)
        progress = FakeWeaponryProgressPublisherPort(recorder)
        dispatcher = FakeWeaponryDispatcherPort(recorder)
        progress.error = RuntimeError("progress offline")
        expected_task_id = TaskId("weaponry-task-0001")
        dispatcher.dispatch_errors[expected_task_id] = RuntimeError("wake failed")
        service = SubmitWeaponryTask(
            task_commands=tasks,
            progress_publisher=progress,
            dispatcher=dispatcher,
        )

        with self.assertLogs(
            "app.modules.weaponry.application.submit_weaponry",
            level="ERROR",
        ):
            result = service.execute(_submission(document_count=1))

        self.assertFalse(result.progress_notified)
        self.assertFalse(result.dispatcher_notified)
        self.assertIn(result.task_id, tasks.executions)

    def test_every_non_accepted_outcome_maps_to_same_conflict(self) -> None:
        for outcome in (
            TaskSubmissionOutcome.ACTIVE_CONFLICT,
            TaskSubmissionOutcome.CALLBACK_SENDING,
            TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
        ):
            with self.subTest(outcome=outcome):
                recorder = WeaponryInvocationRecorder()
                tasks = FakeWeaponryTaskCommandPort(recorder)
                tasks.submission_outcome_sequence.append(outcome)
                service = SubmitWeaponryTask(
                    task_commands=tasks,
                    progress_publisher=FakeWeaponryProgressPublisherPort(recorder),
                    dispatcher=FakeWeaponryDispatcherPort(recorder),
                )

                with self.assertRaisesRegex(
                    WeaponryTaskConflictError,
                    "任务正在处理中",
                ):
                    service.execute(_submission(document_count=1))

                self.assertEqual(["task.create"], [e.operation for e in recorder.events])

    def test_same_business_key_is_rejected_while_first_execution_is_active(self) -> None:
        recorder = WeaponryInvocationRecorder()
        tasks = FakeWeaponryTaskCommandPort(recorder)
        progress = FakeWeaponryProgressPublisherPort(recorder)
        dispatcher = FakeWeaponryDispatcherPort(recorder)
        service = SubmitWeaponryTask(
            task_commands=tasks,
            progress_publisher=progress,
            dispatcher=dispatcher,
        )

        first = service.execute(_submission(document_count=1))
        with self.assertRaises(WeaponryTaskConflictError):
            service.execute(_submission(document_count=1))

        self.assertEqual(1, len(tasks.executions))
        self.assertEqual(first.task_id, next(iter(tasks.executions)))
        self.assertEqual(1, len(progress.publications))
        self.assertEqual((first.task_id,), tuple(dispatcher.dispatched))


class RunWeaponryApplicationTests(unittest.TestCase):
    """验证字段执行、审计、latest、Callback 与资源收敛。"""

    def test_all_forced_input_names_short_circuit_every_field_interaction(
        self,
    ) -> None:
        empty_source = {
            "content": "",
            "source": "",
            "time": "",
            "fileName": "",
            "rows": [],
            "translate": "",
        }
        for field_name in (
            "装备编号",
            "一级分类",
            "二级分类",
            "三级分类",
            "四级分类",
        ):
            with self.subTest(field_name=field_name):
                harness = _WeaponryHarness(
                    _submission(
                        document_count=1,
                        field_override=_forced_input_field(
                            f" \t{field_name}\n"
                        ),
                    )
                )

                result = harness.run_service.execute(harness.task_id)
                public_field = (
                    harness.tasks.completion_calls[-1]
                    .result.fields[0]
                    .to_public_dict()
                )

                self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
                self.assertEqual(0, result.selected_evidence_count)
                self.assertEqual(0, result.model_call_count)
                self.assertEqual("", public_field["analyseData"])
                self.assertEqual(
                    [empty_source],
                    public_field["analyseDataSource"],
                )
                self.assertTrue(harness.tasks.latest_calls)
                self.assertEqual([], harness.guidance.calls)
                self.assertEqual([], harness.retrieval.search_calls)
                self.assertEqual([], harness.extraction.calls)
                self.assertEqual([], harness.translation.calls)
                self.assertEqual((), harness.audit.pending)
                self.assertEqual((), harness.audit.completions)
                self.assertNotIn(
                    "audit.reserve",
                    tuple(
                        event.operation for event in harness.recorder.events
                    ),
                )

    def test_forced_input_checks_latest_before_returning_empty_result(self) -> None:
        harness = _WeaponryHarness(
            _submission(
                document_count=1,
                field_override=_forced_input_field("装备编号"),
            )
        )
        harness.tasks.latest_results.append(False)

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.STALE, result.outcome)
        self.assertEqual(1, len(harness.tasks.latest_calls))
        self.assertEqual([], harness.tasks.completion_calls)
        self.assertEqual([], harness.guidance.calls)
        self.assertEqual([], harness.retrieval.search_calls)
        self.assertEqual([], harness.extraction.calls)
        self.assertEqual([], harness.translation.calls)
        self.assertEqual((), harness.audit.completions)

    def test_similar_input_name_continues_normal_extraction(self) -> None:
        harness = _WeaponryHarness(
            _submission(
                document_count=1,
                field_override=_forced_input_field("装备编号说明"),
            )
        )
        harness.configure_retrieval(
            (_candidate("normal-near-match", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("normal-near-match",),
            text="这是正常字段说明",
        )
        harness.configure_translation(
            document_sequence=1,
            item_sequence=1,
            translated_text="normal field description",
        )

        result = harness.run_service.execute(harness.task_id)
        field = harness.tasks.completion_calls[-1].result.fields[0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("这是正常字段说明", field.analyse_data)
        self.assertEqual(1, len(harness.guidance.calls))
        self.assertEqual(1, len(harness.retrieval.search_calls))
        self.assertEqual(1, len(harness.extraction.calls))
        self.assertEqual(1, len(harness.translation.calls))

    def test_all_forced_table_columns_return_one_complete_empty_row_without_calls(
        self,
    ) -> None:
        harness = _WeaponryHarness(
            _submission(
                document_count=1,
                field_override=_forced_table_field(mixed=False),
            )
        )

        result = harness.run_service.execute(harness.task_id)
        public_field = (
            harness.tasks.completion_calls[-1]
            .result.fields[0]
            .to_public_dict()
        )
        cells = public_field["tableFieldList"][0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(0, result.selected_evidence_count)
        self.assertEqual(0, result.model_call_count)
        self.assertEqual(
            [
                "装备编号",
                "一级分类",
                "二级分类",
                "三级分类",
                "四级分类",
            ],
            [cell["fieldName"] for cell in cells],
        )
        self.assertEqual(list(range(1, 6)), [
            cell["futureColumnKey"] for cell in cells
        ])
        for cell in cells:
            self.assertEqual("", cell["analyseData"])
            self.assertEqual(
                [
                    {
                        "content": "",
                        "source": "",
                        "time": "",
                        "fileName": "",
                        "rows": [],
                        "translate": "",
                    }
                ],
                cell["analyseDataSource"],
            )
        self.assertEqual([], harness.guidance.calls)
        self.assertEqual([], harness.retrieval.search_calls)
        self.assertEqual([], harness.extraction.calls)
        self.assertEqual([], harness.translation.calls)
        self.assertEqual((), harness.audit.completions)

    def test_mixed_table_exposes_only_normal_columns_and_forces_pollution_empty(
        self,
    ) -> None:
        harness = _WeaponryHarness(
            _submission(
                document_count=1,
                field_override=_forced_table_field(mixed=True),
            )
        )
        harness.configure_retrieval(
            (_candidate("mixed-table-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("mixed-table-a",),
            text=json.dumps(
                [
                    {
                        "装备编号": "MALICIOUS-001",
                        "型号": "AN/SPY-1",
                        "一级分类": "MALICIOUS-L1",
                        "二级分类": "MALICIOUS-L2",
                        "三级分类": "MALICIOUS-L3",
                        "四级分类": "MALICIOUS-L4",
                        "用途": "搜索与跟踪",
                    }
                ],
                ensure_ascii=False,
            ),
        )
        harness.configure_translation(
            document_sequence=1,
            item_sequence=1,
            translated_text="AN/SPY-1",
        )
        harness.configure_translation(
            document_sequence=1,
            item_sequence=2,
            translated_text="search and tracking",
        )

        result = harness.run_service.execute(harness.task_id)
        field = harness.tasks.completion_calls[-1].result.fields[0]
        public_field = field.to_public_dict()
        cells = public_field["tableFieldList"][0]
        cells_by_name = {cell["fieldName"]: cell for cell in cells}
        normal_names = ("型号", "用途")
        forced_names = (
            "装备编号",
            "一级分类",
            "二级分类",
            "三级分类",
            "四级分类",
        )

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(1, result.model_call_count)
        self.assertEqual(1, len(field.table_rows))
        self.assertEqual(
            [
                "装备编号",
                "型号",
                "一级分类",
                "二级分类",
                "三级分类",
                "四级分类",
                "用途",
            ],
            [cell["fieldName"] for cell in cells],
        )
        self.assertEqual({"keep": "table"}, public_field["futureExtension"])
        for field_name in forced_names:
            with self.subTest(field_name=field_name):
                self.assertEqual("", cells_by_name[field_name]["analyseData"])
                self.assertEqual(
                    "",
                    cells_by_name[field_name]["analyseDataSource"][0][
                        "content"
                    ],
                )
                self.assertEqual(
                    [],
                    cells_by_name[field_name]["analyseDataSource"][0]["rows"],
                )
        self.assertEqual(
            "AN/SPY-1",
            cells_by_name["型号"]["analyseData"],
        )
        self.assertEqual(
            "搜索与跟踪",
            cells_by_name["用途"]["analyseData"],
        )

        guidance_field = harness.guidance.calls[0].field
        retrieval_call = harness.retrieval.search_calls[0]
        extraction_request = harness.extraction.calls[0]
        self.assertEqual(
            normal_names,
            tuple(column.field_name for column in guidance_field.columns),
        )
        self.assertEqual(
            normal_names,
            tuple(
                column.field_name
                for column in extraction_request.field.columns
            ),
        )
        self.assertEqual(
            [list(normal_names)],
            [
                [cell["fieldName"] for cell in row]
                for row in extraction_request.field.template.to_dict()[
                    "tableFieldList"
                ]
            ],
        )
        for forced_name in forced_names:
            self.assertNotIn(forced_name, retrieval_call.query.text)
            self.assertNotIn(forced_name, extraction_request.prompt.text)
        for normal_name in normal_names:
            self.assertIn(normal_name, retrieval_call.query.text)
            self.assertIn(normal_name, extraction_request.prompt.text)
        self.assertEqual(
            ["AN/SPY-1", "搜索与跟踪"],
            [request.text for request in harness.translation.calls],
        )
        self.assertEqual(5, len(harness.audit.completions))

    def test_mixed_table_without_selected_evidence_returns_complete_empty_row(
        self,
    ) -> None:
        harness = _WeaponryHarness(
            _submission(
                document_count=1,
                field_override=_forced_table_field(mixed=True),
            )
        )
        harness.configure_retrieval(
            (
                _candidate(
                    "mixed-too-short",
                    "doc-a",
                    "内容过短",
                    rank=1,
                    score=0.99,
                ),
            )
        )

        result = harness.run_service.execute(harness.task_id)
        cells = (
            harness.tasks.completion_calls[-1]
            .result.fields[0]
            .to_public_dict()["tableFieldList"][0]
        )

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(0, result.selected_evidence_count)
        self.assertEqual(0, result.model_call_count)
        self.assertEqual([], harness.extraction.calls)
        self.assertEqual([], harness.translation.calls)
        self._assert_mixed_empty_fallback_cells(cells)

    def test_mixed_table_without_valid_model_rows_returns_complete_empty_row(
        self,
    ) -> None:
        harness = _WeaponryHarness(
            _submission(
                document_count=1,
                field_override=_forced_table_field(mixed=True),
            )
        )
        harness.configure_retrieval(
            (_candidate("mixed-no-row", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("mixed-no-row",),
            text=json.dumps(
                [{"装备编号": "MALICIOUS-001"}],
                ensure_ascii=False,
            ),
        )

        result = harness.run_service.execute(harness.task_id)
        cells = (
            harness.tasks.completion_calls[-1]
            .result.fields[0]
            .to_public_dict()["tableFieldList"][0]
        )

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(1, result.selected_evidence_count)
        self.assertEqual(1, result.model_call_count)
        self.assertEqual(1, len(harness.extraction.calls))
        self.assertEqual([], harness.translation.calls)
        self._assert_mixed_empty_fallback_cells(cells)

    def _assert_mixed_empty_fallback_cells(
        self,
        cells: list[dict[str, object]],
    ) -> None:
        self.assertEqual(
            [
                "装备编号",
                "型号",
                "一级分类",
                "二级分类",
                "三级分类",
                "四级分类",
                "用途",
            ],
            [cell["fieldName"] for cell in cells],
        )
        forced_names = {
            "装备编号",
            "一级分类",
            "二级分类",
            "三级分类",
            "四级分类",
        }
        for cell in cells:
            self.assertEqual("", cell["analyseData"])
            if cell["fieldName"] in forced_names:
                self.assertEqual(
                    [
                        {
                            "content": "",
                            "source": "",
                            "time": "",
                            "fileName": "",
                            "rows": [],
                            "translate": "",
                        }
                    ],
                    cell["analyseDataSource"],
                )
            else:
                self.assertEqual([], cell["analyseDataSource"])

    def test_success_matches_golden_and_calls_documents_in_frozen_order(self) -> None:
        harness = _WeaponryHarness()
        harness.configure_golden_input()

        result = harness.run_service.execute(harness.task_id)
        callback = harness.tasks.completion_calls[-1].result.to_callback()
        golden = json.loads(_ASSET_PATH.read_text(encoding="utf-8"))[
            "goldenCallbacks"
        ]["inputSuccess"]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(3, result.selected_evidence_count)
        self.assertEqual(2, result.model_call_count)
        self.assertEqual(
            ("translation_unavailable",),
            result.diagnostic_error_codes,
        )
        self.assertEqual(golden, callback.to_public_dict())
        # Provider 排名让 doc-b 先展示，但慢模型调用仍严格遵循冻结的 doc-a/doc-b 顺序。
        self.assertEqual(
            [1, 2],
            [request.document.sequence_no for request in harness.extraction.calls],
        )
        self.assertEqual(
            ["doc-a", "doc-b"],
            [request.document.document_key for request in harness.extraction.calls],
        )
        self.assertEqual("cleanup_pending", result.cleanup_state)
        self.assertEqual((), harness.retrieval.active_scope_refs)
        self.assertEqual((), harness.audit.pending)
        self.assertEqual(
            WeaponryResourceRecordState.CLEANUP_PENDING,
            harness.resources.records[0].state,
        )
        operations = [event.operation for event in harness.recorder.events]
        self.assertLess(operations.index("task.finish"), operations.index("callback.deliver"))
        self.assertLess(operations.index("callback.complete"), operations.index("retrieval.close"))
        self.assertLess(
            operations.index("resource.prepare_cleanup"),
            operations.index("retrieval.close"),
        )

    def test_all_rejected_is_success_with_empty_source_and_zero_model_calls(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (
                _candidate(
                    "too-short",
                    "doc-a",
                    "内容过短",
                    rank=1,
                    score=0.99,
                ),
            )
        )

        result = harness.run_service.execute(harness.task_id)
        callback = harness.tasks.completion_calls[-1].result.to_callback().to_public_dict()
        field = callback["data"]["weaponryTemplateFieldList"][0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(0, result.selected_evidence_count)
        self.assertEqual(0, result.model_call_count)
        self.assertEqual([], harness.extraction.calls)
        self.assertEqual([], harness.translation.calls)
        self.assertEqual("", field["analyseData"])
        self.assertEqual(
            {
                "content": "",
                "source": "",
                "time": "",
                "fileName": "",
                "rows": [],
                "translate": "",
            },
            field["analyseDataSource"][0],
        )

    def test_retrieval_capacity_degradation_is_not_lost_as_business_zero(self) -> None:
        """字段可按既有成功契约返回空，但内部必须保留供应商容量事实。"""

        harness = _WeaponryHarness(_submission(document_count=1))
        retrieval_call = harness.call(WeaponryOperation.TARGET_RETRIEVAL)
        harness.retrieval.search_errors[retrieval_call.attempt_key] = (
            WeaponryExternalOperationError(
                "provider_payload_too_large",
                "测试注入供应商请求体容量限制",
                outcome=WeaponryExternalOutcome.DEFINITELY_FAILED,
            )
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(0, result.selected_evidence_count)
        self.assertEqual(0, result.model_call_count)
        self.assertEqual(
            ("provider_payload_too_large",),
            result.diagnostic_error_codes,
        )
        self.assertEqual([], harness.extraction.calls)

    def test_exhausted_extraction_capacity_error_reaches_task_diagnostics(self) -> None:
        """两次抽取均被限流时仍成功空回调，但不可伪装为无业务数据。"""

        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("capacity-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        for attempt_no in (1, 2):
            extraction_call = harness.call(
                WeaponryOperation.EVIDENCE_EXTRACTION,
                document_sequence=1,
                attempt_no=attempt_no,
            )
            harness.extraction.errors[extraction_call.attempt_key] = (
                WeaponryExternalOperationError(
                    "provider_rate_limited",
                    "测试注入供应商限流",
                    outcome=WeaponryExternalOutcome.DEFINITELY_FAILED,
                )
            )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(1, result.selected_evidence_count)
        self.assertEqual(2, result.model_call_count)
        self.assertEqual(
            ("provider_rate_limited",),
            result.diagnostic_error_codes,
        )
        self.assertEqual(2, len(harness.extraction.calls))

    def test_auxiliary_guidance_degradation_reaches_task_diagnostics(self) -> None:
        """可选术语辅助失败仍按成功空结果兼容，但内部不能丢失失败类别。"""

        harness = _WeaponryHarness(_submission(document_count=1))
        guidance_call = harness.call(WeaponryOperation.AUXILIARY_GUIDANCE)
        harness.guidance.errors[guidance_call.attempt_key] = (
            WeaponryExternalOperationError(
                "terms_provider_unavailable",
                "测试注入术语辅助不可用",
                outcome=WeaponryExternalOutcome.DEFINITELY_FAILED,
            )
        )
        harness.configure_retrieval(())

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(
            ("terms_provider_unavailable",),
            result.diagnostic_error_codes,
        )

    def test_accepted_empty_category_scope_converges_to_failure_callback(self) -> None:
        harness = _WeaponryHarness(_empty_category_submission())

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
        self.assertEqual("weaponry_document_scope_empty", result.error_code)
        self.assertEqual([], harness.retrieval.search_calls)
        self.assertEqual("cleaned", result.cleanup_state)
        self.assertEqual(
            WEAPONRY_STATUS_FAILED,
            harness.tasks.completion_calls[-1].result.status,
        )
        self.assertEqual(
            WeaponryResourceRecordState.CLEANED,
            harness.resources.records[0].state,
        )

    def test_mismatched_retrieval_scope_fingerprint_is_rejected_and_quarantined(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.retrieval.forced_open_results[harness.task_id.value] = (
            TargetEvidenceScope(
                task_id=harness.task_id,
                scope_ref="mismatched-fingerprint-scope",
                allowed_document_keys=("doc-a",),
                selection_profile_id=_PROFILE_ID,
                provider_fingerprint="wrong-provider-fingerprint",
                embedding_fingerprint=_EMBEDDING_FINGERPRINT,
            )
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
        self.assertEqual("weaponry_port_contract_error", result.error_code)
        self.assertEqual("quarantined", result.cleanup_state)
        self.assertEqual([], harness.retrieval.search_calls)
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            harness.resources.records[0].state,
        )

    def test_invalid_duplicate_rank_rejects_batch_without_model_call(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (
                _candidate("rank-a", "doc-a", _EVIDENCE_A1, rank=1, score=0.9),
                _candidate("rank-b", "doc-a", _EVIDENCE_A2, rank=1, score=0.8),
            )
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(0, result.model_call_count)
        retrieval_completion = next(
            command
            for command in harness.audit.completions
            if command.reservation.call.operation
            is WeaponryOperation.TARGET_RETRIEVAL
        )
        self.assertEqual(WeaponryAuditOutcome.REJECTED, retrieval_completion.outcome)
        self.assertEqual(
            ("duplicate-provider-rank", "duplicate-provider-rank"),
            retrieval_completion.rejection_reasons,
        )

    def test_rank_only_batch_without_absolute_score_is_supported(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (
                _candidate(
                    "rank-only-a",
                    "doc-a",
                    _EVIDENCE_A1,
                    rank=1,
                    score=None,
                    score_present=False,
                ),
            ),
            score_mode=EVIDENCE_SCORE_MODE_RANK,
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("rank-only-a",),
            text="该舰属于甲级",
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(1, result.model_call_count)
        self.assertEqual(1, result.selected_evidence_count)

    def test_empty_first_source_does_not_contaminate_or_stop_second_source(self) -> None:
        harness = _WeaponryHarness()
        harness.configure_retrieval(
            (
                _candidate("a-empty", "doc-a", _EVIDENCE_A1, rank=1),
                _candidate("b-valid", "doc-b", _EVIDENCE_B1, rank=2, score=0.8),
            )
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("a-empty",),
            text="",
        )
        harness.configure_answer(
            document_sequence=2,
            evidence_ids=("b-valid",),
            text="该舰属于乙级",
        )
        harness.configure_translation(document_sequence=2, item_sequence=1)

        result = harness.run_service.execute(harness.task_id)
        field = harness.tasks.completion_calls[-1].result.fields[0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(2, result.model_call_count)
        self.assertEqual("该舰属于乙级", field.analyse_data)
        self.assertEqual(("乙舰资料.pdf",), tuple(item.source for item in field.sources))

    def test_only_exact_input_no_information_sentinel_is_discarded(self) -> None:
        exact = _WeaponryHarness(_submission(document_count=1))
        exact.configure_retrieval(
            (_candidate("exact-sentinel", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        exact.configure_answer(
            document_sequence=1,
            evidence_ids=("exact-sentinel",),
            text="  未找到  ",
        )

        exact_result = exact.run_service.execute(exact.task_id)
        exact_field = exact.tasks.completion_calls[-1].result.fields[0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, exact_result.outcome)
        self.assertEqual("", exact_field.analyse_data)
        self.assertEqual((), exact_field.sources)
        self.assertEqual([], exact.translation.calls)

        contained = _WeaponryHarness(_submission(document_count=1))
        contained.configure_retrieval(
            (_candidate("contained-sentinel", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        contained.configure_answer(
            document_sequence=1,
            evidence_ids=("contained-sentinel",),
            text="未找到号",
        )
        contained.configure_translation(document_sequence=1, item_sequence=1)

        contained_result = contained.run_service.execute(contained.task_id)
        contained_field = contained.tasks.completion_calls[-1].result.fields[0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, contained_result.outcome)
        self.assertEqual("未找到号", contained_field.analyse_data)
        self.assertEqual("未找到号", contained_field.sources[0].content)

    def test_table_cell_containing_no_information_text_is_not_discarded(self) -> None:
        harness = _WeaponryHarness(_submission(table=True, document_count=1))
        harness.configure_retrieval(
            (_candidate("table-sentinel", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("table-sentinel",),
            text=json.dumps(
                [{"型号": "未找到", "用途": "状态说明"}],
                ensure_ascii=False,
            ),
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)
        harness.configure_translation(document_sequence=1, item_sequence=2)

        result = harness.run_service.execute(harness.task_id)
        field = harness.tasks.completion_calls[-1].result.fields[0]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(1, len(field.table_rows))
        # TABLE 既有纯规则会把精确“未找到”单元格归一为空，但不得因此丢掉整行及
        # 其他有效单元格；本轮只修正“任意位置包含哨兵即丢弃整份回答”的错误。
        self.assertEqual("", field.table_rows[0][0].analyse_data)
        self.assertEqual("状态说明", field.table_rows[0][1].analyse_data)

    def test_source_boundary_failure_has_bounded_retry_then_empty_success(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("boundary-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        for attempt_no in (1, 2):
            call = harness.call(
                WeaponryOperation.EVIDENCE_EXTRACTION,
                document_sequence=1,
                attempt_no=attempt_no,
            )
            harness.extraction.errors[call.attempt_key] = WeaponrySourceBoundaryError(
                "extraction_source_out_of_scope",
                "测试注入来源越界",
            )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(2, result.model_call_count)
        self.assertEqual(2, len(harness.extraction.calls))
        self.assertEqual("", harness.tasks.completion_calls[-1].result.fields[0].analyse_data)

    def test_table_translation_subitems_have_unique_audit_identity(self) -> None:
        harness = _WeaponryHarness(_submission(table=True, document_count=1))
        evidence = "AN/SPY-1和AN/SPY-2雷达分别承担搜索跟踪与火控任务，均有明确设备记录。"
        harness.configure_retrieval(
            (_candidate("radar-a", "doc-a", evidence, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("radar-a",),
            text=json.dumps(
                [
                    {"型号": "AN/SPY-1", "用途": "搜索与跟踪"},
                    {"型号": "AN/SPY-2", "用途": "火控"},
                ],
                ensure_ascii=False,
            ),
        )
        translations = (
            "AN/SPY-1",
            "search and track",
            "AN/SPY-2",
            "fire control",
        )
        for item_sequence, translated_text in enumerate(translations, start=1):
            harness.configure_translation(
                document_sequence=1,
                item_sequence=item_sequence,
                translated_text=translated_text,
            )

        result = harness.run_service.execute(harness.task_id)
        field = harness.tasks.completion_calls[-1].result.fields[0]
        callback = harness.tasks.completion_calls[-1].result.to_callback()
        golden = json.loads(_ASSET_PATH.read_text(encoding="utf-8"))[
            "goldenCallbacks"
        ]["tableSuccess"]
        translation_call_ids = tuple(
            request.call.call_id for request in harness.translation.calls
        )

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(1, result.model_call_count)
        self.assertEqual(2, len(field.table_rows))
        self.assertEqual(4, len(translation_call_ids))
        self.assertEqual(4, len(set(translation_call_ids)))
        self.assertTrue(translation_call_ids[0].endswith("translation:i1"))
        self.assertTrue(translation_call_ids[-1].endswith("translation:i4"))
        self.assertEqual(golden, callback.to_public_dict())

    def test_audit_completion_failure_blocks_success_and_quarantines_scene(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        retrieval_call = harness.configure_retrieval(
            (_candidate("audit-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.audit.complete_errors[retrieval_call.attempt_key] = RuntimeError(
            "audit commit failed"
        )

        with self.assertLogs(
            "app.modules.weaponry.application",
            level="CRITICAL",
        ):
            result = harness.run_service.execute(harness.task_id)

        callback = harness.tasks.completion_calls[-1].result.to_callback().to_public_dict()
        golden = json.loads(_ASSET_PATH.read_text(encoding="utf-8"))[
            "goldenCallbacks"
        ]["failure"]
        self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
        self.assertEqual(WeaponryAuditError.code, result.error_code)
        self.assertEqual(golden, callback)
        self.assertEqual("quarantined", result.cleanup_state)
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            harness.resources.records[0].state,
        )
        self.assertEqual((harness.scope_ref,), harness.retrieval.active_scope_refs)

    def test_historical_audit_attempt_blocks_external_replay_and_quarantines(self) -> None:
        for audit_outcome, expected_error_code in (
            (
                WeaponryAuditReserveOutcome.PENDING,
                "weaponry_audit_attempt_pending",
            ),
            (
                WeaponryAuditReserveOutcome.COMPLETED,
                "weaponry_audit_attempt_completed",
            ),
        ):
            with self.subTest(audit_outcome=audit_outcome.value):
                harness = _WeaponryHarness(_submission(document_count=1))
                harness.configure_retrieval(
                    (_candidate("historical-audit", "doc-a", _EVIDENCE_A1, rank=1),)
                )
                original_reserve = harness.audit.reserve

                def classify_as_historical(command):
                    first = original_reserve(command)
                    return WeaponryAuditReserveResult(
                        audit_outcome,
                        first.reservation,
                    )

                with patch.object(
                    harness.audit,
                    "reserve",
                    side_effect=classify_as_historical,
                ):
                    result = harness.run_service.execute(harness.task_id)

                self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
                self.assertEqual(expected_error_code, result.error_code)
                self.assertEqual([], harness.guidance.calls)
                self.assertEqual([], harness.retrieval.search_calls)
                self.assertEqual([], harness.extraction.calls)
                self.assertEqual([], harness.translation.calls)
                self.assertEqual("quarantined", result.cleanup_state)
                self.assertEqual(
                    (harness.scope_ref,),
                    harness.retrieval.active_scope_refs,
                )

    def test_terminal_cas_lost_sends_no_terminal_progress_or_callback(self) -> None:
        harness = _WeaponryHarness()
        harness.configure_golden_input()
        harness.tasks.finish_results.append(False)

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.STALE, result.outcome)
        self.assertEqual([], [p for p in harness.progress.publications if p.progress == 1.0])
        self.assertFalse(
            any(event.operation == "callback.deliver" for event in harness.recorder.events)
        )
        self.assertEqual("cleanup_pending", result.cleanup_state)

    def test_stale_after_retrieval_stops_model_terminal_and_callback(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("stale-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        # guidance 慢调用返回后仍 current，retrieval 慢调用返回后已经 stale。
        harness.tasks.latest_results.extend((True, False))

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.STALE, result.outcome)
        self.assertEqual([], harness.extraction.calls)
        self.assertEqual([], harness.tasks.completion_calls)
        self.assertFalse(
            any(event.operation.startswith("callback.") for event in harness.recorder.events)
        )
        self.assertEqual("cleanup_pending", result.cleanup_state)

    def test_unknown_external_scope_creation_quarantines_resources(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.retrieval.open_errors[harness.task_id.value] = (
            WeaponryExternalOperationError(
                "retrieval_scope_outcome_unknown",
                "测试注入结果未知",
                outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
            )
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
        self.assertEqual("retrieval_scope_outcome_unknown", result.error_code)
        self.assertEqual("quarantined", result.cleanup_state)
        self.assertEqual(
            WEAPONRY_STATUS_FAILED,
            harness.tasks.completion_calls[-1].result.status,
        )

    def test_unknown_extraction_side_effect_fails_once_and_preserves_scene(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("unknown-extract", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        extraction_call = harness.call(
            WeaponryOperation.EVIDENCE_EXTRACTION,
            document_sequence=1,
        )
        harness.extraction.errors[extraction_call.attempt_key] = (
            WeaponryExternalOperationError(
                "extraction_context_untracked_resource_unknown",
                "测试注入抽取副作用结果未知",
                outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
            )
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
        self.assertEqual(
            "extraction_context_untracked_resource_unknown",
            result.error_code,
        )
        self.assertEqual(1, len(harness.extraction.calls))
        self.assertEqual("quarantined", result.cleanup_state)
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            harness.resources.records[0].state,
        )
        self.assertEqual((harness.scope_ref,), harness.retrieval.active_scope_refs)

    def test_cleanup_failure_keeps_recoverable_pending_record(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("cleanup-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("cleanup-a",),
            text="该舰属于甲级",
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)
        harness.retrieval.close_error_codes[harness.scope_ref] = (
            "retrieval_scope_close_failed"
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("cleanup_pending", result.cleanup_state)
        self.assertEqual((harness.scope_ref,), harness.retrieval.active_scope_refs)
        self.assertEqual((harness.task_id,), harness.resources.list_recoverable(limit=10))

    def test_cleanup_intent_persistence_failure_prevents_remote_delete(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("cleanup-intent", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("cleanup-intent",),
            text="该舰属于甲级",
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)
        harness.resources.failures["prepare_cleanup"] = RuntimeError(
            "测试注入清理意图持久化失败"
        )

        result = harness.run_service.execute(harness.task_id)
        operations = [event.operation for event in harness.recorder.events]

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("port_error", result.cleanup_state)
        self.assertIn("resource.prepare_cleanup", operations)
        self.assertNotIn("retrieval.close", operations)
        self.assertEqual((harness.scope_ref,), harness.retrieval.active_scope_refs)
        self.assertEqual(
            WeaponryResourceRecordState.TRACKING,
            harness.resources.records[0].state,
        )

    def test_callback_outcome_unknown_does_not_rewrite_business_terminal(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("callback-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("callback-a",),
            text="该舰属于甲级",
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)
        harness.callbacks.delivery_results[harness.task_id] = (
            WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN
            )
        )

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(
            WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN.value,
            result.callback_outcome,
        )
        self.assertEqual(
            WEAPONRY_STATUS_SUCCEEDED,
            harness.tasks.completion_calls[-1].result.status,
        )
        harness.tasks.submission_outcomes[
            TaskBusinessRef("weaponry", harness.submission.business_key)
        ] = TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN
        with self.assertRaisesRegex(WeaponryTaskConflictError, "任务正在处理中"):
            harness.submit_service.execute(harness.submission)

    def test_callback_projection_error_after_terminal_never_writes_second_terminal(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("callback-projection", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("callback-projection",),
            text="该舰属于甲级",
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)

        with patch.object(
            WeaponryResult,
            "to_callback",
            side_effect=RuntimeError("projection failed"),
        ), self.assertLogs(
            "app.modules.weaponry.application.run_weaponry",
            level="ERROR",
        ):
            result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("port_error", result.callback_outcome)
        self.assertEqual(1, len(harness.tasks.completion_calls))
        self.assertEqual(
            WEAPONRY_STATUS_SUCCEEDED,
            harness.tasks.completion_calls[0].result.status,
        )
        self.assertFalse(
            any(event.operation == "callback.acquire" for event in harness.recorder.events)
        )

    def test_task_persistence_unknown_never_writes_second_terminal(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        harness.configure_retrieval(
            (_candidate("persist-a", "doc-a", _EVIDENCE_A1, rank=1),)
        )
        harness.configure_answer(
            document_sequence=1,
            evidence_ids=("persist-a",),
            text="该舰属于甲级",
        )
        harness.configure_translation(document_sequence=1, item_sequence=1)
        harness.tasks.errors["finish"] = RuntimeError("commit outcome unknown")

        with self.assertRaises(WeaponryTaskPersistenceError):
            harness.run_service.execute(harness.task_id)

        self.assertEqual(1, len(harness.tasks.completion_calls))
        self.assertFalse(
            any(event.operation == "callback.deliver" for event in harness.recorder.events)
        )
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            harness.resources.records[0].state,
        )

    def test_missing_and_non_claimed_dispatches_are_idempotent(self) -> None:
        harness = _WeaponryHarness(_submission(document_count=1))
        missing = harness.run_service.execute(TaskId("missing-weaponry-task"))
        harness.tasks.claim_outcomes[harness.task_id] = TaskClaimOutcome.ALREADY_RUNNING
        not_claimed = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.MISSING, missing.outcome)
        self.assertEqual(RunWeaponryOutcome.NOT_CLAIMED, not_claimed.outcome)
        self.assertEqual([], harness.retrieval.search_calls)

    def test_duplicate_dispatch_after_terminal_has_no_second_side_effect(self) -> None:
        harness = _WeaponryHarness()
        harness.configure_golden_input()

        first = harness.run_service.execute(harness.task_id)
        extraction_count = len(harness.extraction.calls)
        completion_count = len(harness.tasks.completion_calls)
        callback_count = sum(
            event.operation == "callback.deliver"
            for event in harness.recorder.events
        )
        second = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.SUCCEEDED, first.outcome)
        self.assertEqual(RunWeaponryOutcome.NOT_CLAIMED, second.outcome)
        self.assertEqual(extraction_count, len(harness.extraction.calls))
        self.assertEqual(completion_count, len(harness.tasks.completion_calls))
        self.assertEqual(
            callback_count,
            sum(
                event.operation == "callback.deliver"
                for event in harness.recorder.events
            ),
        )

    def test_preexisting_nonempty_resource_scene_is_quarantined_without_external_io(self) -> None:
        """崩溃遗留资源不得被新一轮 Run 覆盖或当作空现场继续执行。"""

        harness = _WeaponryHarness(_submission(document_count=1))
        harness.resources.create(
            WeaponryResourceRecord(
                task_id=harness.task_id,
                business_ref=TaskBusinessRef(
                    "weaponry",
                    str(harness.submission.architecture_id),
                ),
                resources=(
                    WeaponryTrackedResource(
                        resource_id="preexisting-scope",
                        kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                        external_ref="opaque-preexisting-scope",
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key=(
                            f"weaponry:{harness.task_id.value}:preexisting-scope"
                        ),
                    ),
                ),
            )
        )
        harness.recorder.clear()

        result = harness.run_service.execute(harness.task_id)

        self.assertEqual(RunWeaponryOutcome.FAILED, result.outcome)
        self.assertEqual(
            "weaponry_resource_record_preexisting",
            result.error_code,
        )
        self.assertEqual("quarantined", result.cleanup_state)
        self.assertFalse(
            any(
                event.operation == "retrieval.open"
                for event in harness.recorder.events
            )
        )
        self.assertEqual([], harness.extraction.calls)
        self.assertEqual(1, len(harness.tasks.completion_calls))
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            harness.resources.records[0].state,
        )

    def test_fifty_concurrent_executions_remain_task_isolated(self) -> None:
        """共享同一组线程安全 Port，证明 50 个在途 Application execution 不串状态。"""

        recorder = WeaponryInvocationRecorder()
        tasks = FakeWeaponryTaskCommandPort(recorder)
        progress = FakeWeaponryProgressPublisherPort(recorder)
        dispatcher = FakeWeaponryDispatcherPort(recorder)
        retrieval = FakeTargetEvidenceRetrievalPort(recorder)
        extraction = FakeEvidenceExtractionPort(recorder)
        guidance = FakeAuxiliaryGuidancePort(recorder)
        translation = FakeWeaponryTranslationPort(recorder)
        audit = FakeWeaponryInteractionAuditPort(recorder)
        callbacks = FakeWeaponryCallbackPort(recorder)
        resources = FakeWeaponryResourceStorePort(recorder)
        submit = SubmitWeaponryTask(
            task_commands=tasks,
            progress_publisher=progress,
            dispatcher=dispatcher,
        )
        runner = RunWeaponryTask(
            task_commands=tasks,
            progress_publisher=progress,
            retrieval=retrieval,
            field_executor=WeaponryFieldExecutor(
                retrieval=retrieval,
                extraction=extraction,
                guidance=guidance,
                translation=translation,
                audit=audit,
            ),
            callbacks=callbacks,
            resources=resources,
        )
        task_ids: list[TaskId] = []
        for index in range(50):
            architecture_id = 30001 + index
            accepted = submit.execute(
                _submission(
                    document_count=1,
                    architecture_id=architecture_id,
                )
            )
            task_id = accepted.task_id
            task_ids.append(task_id)
            callbacks.set_latest(task_id, architecture_id)
            callbacks.delivery_results[task_id] = WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.SUCCESS
            )
            retrieval_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=None,
                operation=WeaponryOperation.TARGET_RETRIEVAL,
            )
            candidate_id = f"concurrent-candidate-{index:02d}"
            retrieval.search_results[retrieval_call.attempt_key] = (
                TargetEvidenceSearchResult(
                    scope_ref=f"fake-retrieval-scope:{task_id.value}",
                    call=retrieval_call,
                    candidates=(
                        _candidate(
                            candidate_id,
                            "doc-a",
                            f"并发任务{index:02d}拥有独立的甲级舰艇证据正文，"
                            "用于验证五十个任务不会互相读取模型结果。",
                            rank=1,
                        ),
                    ),
                    score_mode=EVIDENCE_SCORE_MODE_SCORE,
                    provider_fingerprint=_PROVIDER_FINGERPRINT,
                    embedding_fingerprint=_EMBEDDING_FINGERPRINT,
                )
            )
            extraction_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.EVIDENCE_EXTRACTION,
            )
            answer_text = f"并发任务-{index:02d}-甲级"
            extraction.results[extraction_call.attempt_key] = ExtractionAnswer(
                call=extraction_call,
                text=answer_text,
                raw_response_digest=hashlib.sha256(
                    answer_text.encode("utf-8")
                ).hexdigest(),
                raw_response_chars=len(answer_text),
                evidence_ids=(candidate_id,),
                sources=(),
                validation_outcome=ExtractionValidationOutcome.MATCHED,
            )
            translation_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.TRANSLATION,
                item_sequence=1,
            )
            translation.results[translation_call.attempt_key] = (
                WeaponryTranslationResult(
                    call=translation_call,
                    text=f"translated-{index:02d}",
                    outcome=WeaponryTranslationOutcome.SUCCEEDED,
                )
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            results = tuple(executor.map(runner.execute, task_ids))

        self.assertTrue(
            all(result.outcome is RunWeaponryOutcome.SUCCEEDED for result in results)
        )
        self.assertEqual(50, len(tasks.completion_calls))
        self.assertEqual(50, len(extraction.calls))
        self.assertEqual(50, len(translation.calls))
        self.assertEqual((), audit.pending)
        self.assertEqual((), retrieval.active_scope_refs)
        self.assertTrue(
            all(
                record.state is WeaponryResourceRecordState.CLEANUP_PENDING
                for record in resources.records
            )
        )
        contents = {
            completion.expected_task_id: completion.result.fields[0].analyse_data
            for completion in tasks.completion_calls
        }
        for index, task_id in enumerate(task_ids):
            self.assertEqual(f"并发任务-{index:02d}-甲级", contents[task_id])

    def test_slow_retrieval_holds_no_sqlite_write_transaction_or_global_task_lock(self) -> None:
        """慢供应商调用期间，同库写事务和另一业务键受理都必须立即可用。"""

        with workspace_tempdir() as runtime_directory:
            db_path = str(Path(runtime_directory) / "tasks.sqlite3")
            task_commands = LegacyTaskCommandAdapter(
                LLMTaskService(db_path),
                WeaponryTaskCommandCodec(),
            )
            recorder = WeaponryInvocationRecorder()
            progress = FakeWeaponryProgressPublisherPort(recorder)
            dispatcher = FakeWeaponryDispatcherPort(recorder)
            submit = SubmitWeaponryTask(
                task_commands=task_commands,
                progress_publisher=progress,
                dispatcher=dispatcher,
            )
            submission = _submission(document_count=1, architecture_id=41001)
            task_id = submit.execute(submission).task_id
            retrieval = _BlockingTargetEvidenceRetrievalPort(recorder)
            extraction = FakeEvidenceExtractionPort(
                recorder,
                enforce_call_order=False,
            )
            guidance = FakeAuxiliaryGuidancePort(
                recorder,
                enforce_call_order=False,
            )
            translation = FakeWeaponryTranslationPort(
                recorder,
                enforce_call_order=False,
            )
            callbacks = FakeWeaponryCallbackPort(recorder)
            callbacks.set_latest(task_id, submission.architecture_id)
            callbacks.delivery_results[task_id] = WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.SUCCESS
            )
            retrieval_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=None,
                operation=WeaponryOperation.TARGET_RETRIEVAL,
            )
            retrieval.search_results[retrieval_call.attempt_key] = (
                TargetEvidenceSearchResult(
                    scope_ref=f"fake-retrieval-scope:{task_id.value}",
                    call=retrieval_call,
                    candidates=(
                        _candidate("slow-a", "doc-a", _EVIDENCE_A1, rank=1),
                    ),
                    score_mode=EVIDENCE_SCORE_MODE_SCORE,
                    provider_fingerprint=_PROVIDER_FINGERPRINT,
                    embedding_fingerprint=_EMBEDDING_FINGERPRINT,
                )
            )
            extraction_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.EVIDENCE_EXTRACTION,
            )
            answer_text = "该舰属于甲级"
            extraction.results[extraction_call.attempt_key] = ExtractionAnswer(
                call=extraction_call,
                text=answer_text,
                raw_response_digest=hashlib.sha256(
                    answer_text.encode("utf-8")
                ).hexdigest(),
                raw_response_chars=len(answer_text),
                evidence_ids=("slow-a",),
                sources=(),
                validation_outcome=ExtractionValidationOutcome.MATCHED,
            )
            translation_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.TRANSLATION,
                item_sequence=1,
            )
            translation.results[translation_call.attempt_key] = (
                WeaponryTranslationResult(
                    call=translation_call,
                    text="甲级舰艇",
                    outcome=WeaponryTranslationOutcome.SUCCEEDED,
                )
            )
            runner = RunWeaponryTask(
                task_commands=task_commands,
                progress_publisher=progress,
                retrieval=retrieval,
                field_executor=WeaponryFieldExecutor(
                    retrieval=retrieval,
                    extraction=extraction,
                    guidance=guidance,
                    translation=translation,
                    audit=SQLiteWeaponryInteractionAuditAdapter(db_path),
                ),
                callbacks=callbacks,
                resources=SQLiteWeaponryResourceStoreAdapter(db_path),
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(runner.execute, task_id)
                self.assertTrue(
                    retrieval.entered.wait(timeout=10),
                    "Worker 未进入慢检索观察点",
                )
                try:
                    # 如果 Application 或 Audit 把网络调用包在 BEGIN IMMEDIATE 中，
                    # 这里会在 500ms 内以 database is locked 失败。
                    connection = sqlite3.connect(
                        db_path,
                        timeout=0.5,
                        isolation_level=None,
                    )
                    try:
                        connection.execute("PRAGMA busy_timeout = 500")
                        connection.execute("BEGIN IMMEDIATE")
                        connection.rollback()
                    finally:
                        connection.close()

                    second = submit.execute(
                        _submission(
                            document_count=1,
                            architecture_id=41002,
                        )
                    )
                    self.assertIsNotNone(
                        task_commands.get_execution(second.task_id)
                    )
                finally:
                    retrieval.release.set()
                result = future.result(timeout=20)

            self.assertEqual(RunWeaponryOutcome.SUCCEEDED, result.outcome)


if __name__ == "__main__":
    unittest.main()
