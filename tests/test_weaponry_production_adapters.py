"""阶段 1D-3B 生产 Adapter、Schema v2、故障注入与并发隔离验收。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event
from types import MappingProxyType

from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm import AnythingLLMHTTPError, AnythingLLMTimeoutError
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.adapters import (
    AnythingLLMProvidedEvidenceExtractionAdapter,
    AnythingLLMTargetEvidenceRetrievalAdapter,
    AnythingLLMWeaponryCreationIntentRecoveryAdapter,
    LLMTranslationServiceWeaponryAdapter,
    NoAuxiliaryGuidanceAdapter,
    SQLiteWeaponryCreationIntentStoreAdapter,
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
    StoreBackedWeaponryResourceRegistrar,
    TermsRuleChunk,
    TermsRuleGuidanceAdapter,
    WeaponryAnythingLLMClients,
    WeaponryProductionSelectionProfileConfig,
    build_weaponry_production_selection_policy,
)
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    AUXILIARY_GUIDANCE_TERMS_RULES_V1,
    EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    EXTRACTION_PROMPT_VERSION,
    FILE_AGGREGATE_STRATEGY,
    TABLE_MERGE_POLICY_VERSION,
    WEAPONRY_INPUT_SCHEMA_VERSION,
    AuxiliaryGuidancePolicySnapshot,
    SelectedEvidence,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryExecutionPolicySnapshot,
    WeaponryFieldSpecification,
    build_input_extraction_prompt,
    build_retrieval_query,
    RetrievalField,
    select_evidence,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCleanupLease,
    AuxiliaryGuidanceOutcome,
    AuxiliaryGuidanceRequest,
    CompleteWeaponryInteraction,
    CompleteWeaponryResourceCleanup,
    EvidenceExtractionRequest,
    OpenTargetEvidenceScope,
    PrepareWeaponryResourceCleanup,
    QuarantineWeaponryResources,
    RegisterWeaponryResource,
    ReserveWeaponryInteraction,
    SearchTargetEvidence,
    WeaponryAuditOutcome,
    WeaponryAuditReserveOutcome,
    WeaponryCallIdentity,
    WeaponryCreationIntent,
    WeaponryCreationIntentKind,
    WeaponryCreationIntentState,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryOperation,
    WeaponryPortStateError,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponrySourceBoundaryError,
    WeaponryTrackedResource,
    WeaponryTrackedResourceState,
    WeaponryTranslationOutcome,
    WeaponryTranslationRequest,
)


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "contracts" / "stage1d3b_weaponry_multi_document.json"


def _document(sequence: int, marker: str) -> WeaponryDocumentSnapshot:
    return WeaponryDocumentSnapshot(
        sequence_no=sequence,
        document_key=f"doc-{marker}",
        file_name=f"{marker.upper()}.pdf",
        original_name=f"{marker.upper()} 原始.pdf",
        ingested_file_name=f"{marker}.mhtml",
        source_architecture_id=7,
        external_document_ref=f"custom-documents/{marker}.json",
        anything_document_id=f"provider-{marker}",
    )


def _scope(*documents: WeaponryDocumentSnapshot) -> WeaponryDocumentScope:
    return WeaponryDocumentScope(
        mode="category",
        requested_file_names=(),
        documents=tuple(documents),
    )


def _policy():
    return build_weaponry_production_selection_policy(
        WeaponryProductionSelectionProfileConfig(
            provider_fingerprint="anythingllm:1.8:test",
            embedding_fingerprint="multilingual-e5:test",
            document_processing_fingerprint="legacy-normalized-artifact-v1:test",
        )
    )


def _resource_record(task_id: TaskId, business_key: str = "7") -> WeaponryResourceRecord:
    return WeaponryResourceRecord(
        task_id=task_id,
        business_ref=TaskBusinessRef("weaponry", business_key),
    )


def _resource_registrar(
    store: SQLiteWeaponryResourceStoreAdapter,
    db_path: str,
) -> StoreBackedWeaponryResourceRegistrar:
    """为测试创建共享同一 SQLite 文件的资源事实与创建意图适配器。"""

    return StoreBackedWeaponryResourceRegistrar(
        store,
        SQLiteWeaponryCreationIntentStoreAdapter(db_path),
    )


def _call(
    task_id: TaskId,
    operation: WeaponryOperation,
    *,
    document_sequence: int | None = None,
    attempt_no: int = 1,
    item_sequence: int | None = None,
) -> WeaponryCallIdentity:
    return WeaponryCallIdentity(
        task_id=task_id,
        field_sequence=1,
        document_sequence=document_sequence,
        operation=operation,
        attempt_no=attempt_no,
        item_sequence=item_sequence,
    )


class _FakeWorkspaceClient:
    def __init__(self, runtime: "_FakeAnythingRuntime") -> None:
        self.runtime = runtime
        self.slug = ""
        self.bound_locations: tuple[str, ...] = ()

    def create_workspace(self, name, *, settings=None, user_id=None):
        if self.runtime.create_workspace_commits_before_error:
            self.slug = name
            self.runtime.created_workspaces.append(name)
            self.runtime.workspace_locations.setdefault(name, ())
            if self.runtime.create_workspace_error is not None:
                raise self.runtime.create_workspace_error
        if self.runtime.create_workspace_error is not None:
            raise self.runtime.create_workspace_error
        self.slug = name
        self.runtime.created_workspaces.append(name)
        self.runtime.workspace_locations.setdefault(name, ())
        return AnythingLLMWorkspace(id=name, slug=name, name=name)

    def list_workspaces(self, *, user_id=None):
        self.runtime.list_workspaces_calls += 1
        return [
            AnythingLLMWorkspace(id=name, slug=name, name=name)
            for name in self.runtime.workspace_locations
        ]

    def update_embeddings(self, workspace_slug, *, adds=(), deletes=(), user_id=None):
        self.runtime.workspace_writes.append(workspace_slug)
        self.bound_locations = tuple(adds)
        self.runtime.workspace_locations[workspace_slug] = self.bound_locations
        return AnythingLLMWorkspace(
            id=workspace_slug,
            slug=workspace_slug,
            name=workspace_slug,
        )

    def list_documents(self, workspace_slug, *, user_id=None):
        bound_locations = self.runtime.workspace_locations.get(
            workspace_slug,
            self.bound_locations,
        )
        if self.runtime.force_non_empty_extraction_workspace and not bound_locations:
            return [
                AnythingLLMDocument(
                    id="pollution",
                    location="custom-documents/pollution.json",
                    title="pollution",
                    document_ref="document:pollution",
                )
            ]
        documents = [
            AnythingLLMDocument(
                id=self.runtime.reported_provider_ids[location],
                location=location,
                title=location.rsplit("/", 1)[-1],
                document_ref=(
                    f"document:{self.runtime.reported_provider_ids[location]}"
                ),
            )
            for location in bound_locations
        ]
        if self.runtime.duplicate_bound_document and documents:
            documents.append(documents[0])
        return documents

    def vector_search(
        self,
        workspace_slug,
        query,
        *,
        top_n=None,
        score_threshold=None,
        user_id=None,
    ):
        self.runtime.vector_calls.append(
            (workspace_slug, query, top_n, score_threshold)
        )
        return list(self.runtime.vector_sources)

    def delete_workspace(self, workspace_slug, *, user_id=None):
        if self.runtime.delete_error is not None:
            raise self.runtime.delete_error
        self.runtime.deleted_workspaces.append(workspace_slug)
        self.runtime.workspace_locations.pop(workspace_slug, None)


class _FakeThreadClient:
    def __init__(self, runtime: "_FakeAnythingRuntime") -> None:
        self.runtime = runtime

    def create_thread(self, workspace_slug, name, *, user_id=None):
        self.runtime.created_threads.append((workspace_slug, name))
        return AnythingLLMThread(id=name, slug=name)

    def ask(
        self,
        workspace_slug,
        thread_slug,
        prompt,
        *,
        mode,
        user_id=None,
        document_ids=None,
    ):
        self.runtime.ask_calls.append(
            (workspace_slug, thread_slug, prompt, mode, tuple(document_ids or ()))
        )
        return AnythingLLMAnswer(
            text=self.runtime.answer_text,
            raw_text=self.runtime.answer_text,
            sources=tuple(self.runtime.answer_sources),
        )


class _FakeAnythingRuntime:
    def __init__(self) -> None:
        self.provider_ids = {
            "custom-documents/a.json": "provider-a",
            "custom-documents/b.json": "provider-b",
        }
        self.reported_provider_ids = dict(self.provider_ids)
        self.duplicate_bound_document = False
        self.vector_sources: tuple[AnythingLLMSource, ...] = ()
        self.answer_text = "甲级"
        self.answer_sources: tuple[AnythingLLMSource, ...] = ()
        self.force_non_empty_extraction_workspace = False
        self.created_workspaces: list[str] = []
        self.list_workspaces_calls = 0
        self.workspace_locations: dict[str, tuple[str, ...]] = {}
        self.deleted_workspaces: list[str] = []
        self.workspace_writes: list[str] = []
        self.created_threads: list[tuple[str, str]] = []
        self.vector_calls: list[tuple[object, ...]] = []
        self.ask_calls: list[tuple[object, ...]] = []
        self.active_leases = 0
        self.closed_leases = 0
        self.block_lease_entry = False
        self.lease_entered = Event()
        self.release_lease = Event()
        self.delete_error: Exception | None = None
        self.create_workspace_error: Exception | None = None
        self.create_workspace_commits_before_error = False

    @contextmanager
    def create(self):
        self.active_leases += 1
        workspace_client = _FakeWorkspaceClient(self)
        try:
            if self.block_lease_entry:
                self.lease_entered.set()
                if not self.release_lease.wait(timeout=5):
                    raise RuntimeError("Fake AnythingLLM lease 等待测试释放超时")
            yield WeaponryAnythingLLMClients(
                documents=object(),  # 本组测试不执行上传。
                workspaces=workspace_client,
                threads=_FakeThreadClient(self),
            )
        finally:
            self.active_leases -= 1
            self.closed_leases += 1


class _FailingCreatedResourceRegistrar:
    """故障注入：创建前允许执行，但拒绝提交任何创建后资源事实。"""

    def ensure_ready(self, task_id: TaskId) -> None:
        return None

    def reserve_creation(self, intent):
        # 故障注入只关注“外部创建完成后资源登记失败”。创建意图保持首次预留语义，
        # 避免测试替身绕过生产 Adapter 新增的副作用前检查点。
        from app.modules.weaponry.ports import WeaponryCreationIntentReserveResult

        return WeaponryCreationIntentReserveResult(created=True, intent=intent)

    def resolve_creation(self, intent, *, external_ref):
        return intent

    def quarantine_creation(self, intent, *, error_code):
        return intent

    def register_created(self, **kwargs):
        raise WeaponryPortStateError(
            "injected_resource_registration_failure",
            "注入的资源登记失败",
        )


class _FakeTermsProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None

    def search(self, query: str, *, top_n: int):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return (
            TermsRuleChunk("terms-a", "术语 A 的含义和单位规则。", 1),
            TermsRuleChunk("terms-b", "术语 B 的别名和字段口径。", 2),
        )[:top_n]


class _FakeTranslator:
    def __init__(self, result: str = "译文", *, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls = 0

    def translate_text_only(self, text, target_lang="Chinese", fast_translate=None, as_html=True):
        self.calls += 1
        if self.fail:
            raise RuntimeError("translation failed")
        return self.result


class WeaponrySchemaV2ProfileTests(unittest.TestCase):
    def test_profile_is_deterministic_and_contains_no_threshold_or_reranker(self) -> None:
        first = _policy()
        second = _policy()
        self.assertEqual(2, WEAPONRY_INPUT_SCHEMA_VERSION)
        self.assertEqual(first, second)
        self.assertTrue(first.profile_id.startswith("weaponry-production-v2-"))
        self.assertFalse(hasattr(first, "minimum_provider_score"))
        self.assertFalse(hasattr(first, "reranker_fingerprint"))
        self.assertIn("score-or-stable-rank", first.score_protocol)

    def test_minimal_multi_document_golden_asset_freezes_structure_not_precision(self) -> None:
        asset = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        self.assertEqual(2, asset["schemaVersion"])
        self.assertEqual(["doc-a", "doc-b"], [
            item["documentKey"] for item in asset["documents"]
        ])
        self.assertFalse(asset["precisionClaim"])
        self.assertTrue(asset["expected"]["rowsMustEqualSelectedEvidence"])


class WeaponryAnythingLLMRetrievalAdapterTests(unittest.TestCase):
    def _adapter(self, db_path: str, runtime: _FakeAnythingRuntime, task_id: TaskId):
        store = SQLiteWeaponryResourceStoreAdapter(db_path)
        store.create(_resource_record(task_id))
        registrar = _resource_registrar(store, db_path)
        adapter = AnythingLLMTargetEvidenceRetrievalAdapter(
            runtime,
            registrar,
            provider_fingerprint=_policy().provider_fingerprint,
            embedding_fingerprint=_policy().embedding_fingerprint,
        )
        return adapter, store

    def test_missing_resource_record_rejects_before_external_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _FakeAnythingRuntime()
            store = SQLiteWeaponryResourceStoreAdapter(
                str(Path(directory) / "tasks.sqlite3")
            )
            adapter = AnythingLLMTargetEvidenceRetrievalAdapter(
                runtime,
                _resource_registrar(store, str(Path(directory) / "tasks.sqlite3")),
                provider_fingerprint=_policy().provider_fingerprint,
                embedding_fingerprint=_policy().embedding_fingerprint,
            )
            task_id = TaskId("retrieval-record-missing")

            with self.assertRaisesRegex(WeaponryPortStateError, "找不到任务资源记录"):
                adapter.open_scope(
                    OpenTargetEvidenceScope(
                        task_id,
                        _scope(_document(1, "a")),
                        _policy(),
                    )
                )

            self.assertEqual([], runtime.created_workspaces)
            self.assertEqual(0, runtime.active_leases)

    def test_same_task_concurrent_open_is_rejected_before_second_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-concurrent-open")
            runtime = _FakeAnythingRuntime()
            runtime.block_lease_entry = True
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"),
                runtime,
                task_id,
            )
            command = OpenTargetEvidenceScope(
                task_id,
                _scope(_document(1, "a")),
                _policy(),
            )

            with ThreadPoolExecutor(max_workers=1) as executor:
                opening = executor.submit(adapter.open_scope, command)
                self.assertTrue(runtime.lease_entered.wait(timeout=2))
                with self.assertRaisesRegex(WeaponryPortStateError, "正在创建"):
                    adapter.open_scope(command)
                runtime.release_lease.set()
                scope = opening.result(timeout=5)

            self.assertEqual(1, len(runtime.created_workspaces))
            adapter.close_scope(scope)

    def test_registration_failure_compensates_or_reports_unknown(self) -> None:
        task_id = TaskId("retrieval-registration-failure")
        command = OpenTargetEvidenceScope(
            task_id,
            _scope(_document(1, "a")),
            _policy(),
        )

        runtime = _FakeAnythingRuntime()
        adapter = AnythingLLMTargetEvidenceRetrievalAdapter(
            runtime,
            _FailingCreatedResourceRegistrar(),
            provider_fingerprint=_policy().provider_fingerprint,
            embedding_fingerprint=_policy().embedding_fingerprint,
        )
        with self.assertRaises(WeaponryPortStateError):
            adapter.open_scope(command)
        self.assertEqual(runtime.created_workspaces, runtime.deleted_workspaces)

        unknown_runtime = _FakeAnythingRuntime()
        unknown_runtime.delete_error = RuntimeError("delete outcome unknown")
        unknown_adapter = AnythingLLMTargetEvidenceRetrievalAdapter(
            unknown_runtime,
            _FailingCreatedResourceRegistrar(),
            provider_fingerprint=_policy().provider_fingerprint,
            embedding_fingerprint=_policy().embedding_fingerprint,
        )
        with self.assertRaises(WeaponryExternalOperationError) as captured:
            unknown_adapter.open_scope(command)
        self.assertEqual(WeaponryExternalOutcome.OUTCOME_UNKNOWN, captured.exception.outcome)
        self.assertEqual(
            "retrieval_scope_untracked_resource_unknown",
            captured.exception.error_code,
        )

    def test_create_timeout_without_unique_match_is_quarantined_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-create-timeout")
            runtime = _FakeAnythingRuntime()
            runtime.create_workspace_error = AnythingLLMTimeoutError(
                "injected create timeout"
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"),
                runtime,
                task_id,
            )
            command = OpenTargetEvidenceScope(
                task_id,
                _scope(_document(1, "a")),
                _policy(),
            )

            with self.assertRaises(WeaponryExternalOperationError) as captured:
                adapter.open_scope(command)
            self.assertEqual(
                WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                captured.exception.outcome,
            )
            with self.assertRaisesRegex(
                WeaponryPortStateError,
                "创建意图已经隔离",
            ):
                adapter.open_scope(command)
            self.assertEqual([], runtime.created_workspaces)

    def test_create_timeout_after_commit_is_reconciled_without_second_create(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-create-timeout-after-commit")
            runtime = _FakeAnythingRuntime()
            runtime.create_workspace_commits_before_error = True
            runtime.create_workspace_error = AnythingLLMTimeoutError(
                "injected timeout after provider commit"
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"),
                runtime,
                task_id,
            )
            command = OpenTargetEvidenceScope(
                task_id,
                _scope(_document(1, "a")),
                _policy(),
            )

            scope = adapter.open_scope(command)

            self.assertEqual(1, len(runtime.created_workspaces))
            self.assertEqual(task_id, scope.task_id)
            adapter.close_scope(scope)

    def test_deterministic_workspace_conflict_is_treated_as_unknown_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-create-conflict")
            runtime = _FakeAnythingRuntime()
            runtime.create_workspace_error = AnythingLLMHTTPError(
                "injected deterministic workspace conflict",
                method="POST",
                url="http://anythingllm.local/workspace/new",
                status_code=409,
                response_summary="workspace already exists",
            )
            adapter, store = self._adapter(
                str(Path(directory) / "tasks.sqlite3"),
                runtime,
                task_id,
            )

            with self.assertRaises(WeaponryExternalOperationError) as captured:
                adapter.open_scope(
                    OpenTargetEvidenceScope(
                        task_id,
                        _scope(_document(1, "a")),
                        _policy(),
                    )
                )

            self.assertIs(
                WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                captured.exception.outcome,
            )
            self.assertEqual([], runtime.created_workspaces)
            resource_record = store.get(task_id)
            self.assertIsNotNone(resource_record)
            self.assertIs(
                WeaponryResourceRecordState.TRACKING,
                resource_record.state,  # type: ignore[union-attr]
            )


    def test_retrieval_uses_task_workspace_strong_source_mapping_and_score_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-task")
            runtime = _FakeAnythingRuntime()
            runtime.vector_sources = (
                AnythingLLMSource(
                    document_ref="name:a.pdf",
                    text="A 文档中的完整舰级证据正文，长度满足质量门禁。",
                    id="chunk-not-authoritative",
                    score=0.91,
                    score_present=True,
                    score_valid=True,
                    metadata=MappingProxyType(
                        {"location": "custom-documents/a.json"}
                    ),
                ),
                AnythingLLMSource(
                    document_ref="name:b.pdf",
                    text="B 文档中的完整舰级证据正文，内容与 A 相互独立。",
                    score=0.83,
                    score_present=True,
                    score_valid=True,
                    metadata=MappingProxyType({"documentId": "provider-b"}),
                ),
            )
            adapter, store = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            documents = (_document(1, "a"), _document(2, "b"))
            policy = _policy()
            scope = adapter.open_scope(
                OpenTargetEvidenceScope(
                    task_id=task_id,
                    document_scope=_scope(*documents),
                    policy=policy,
                )
            )
            call = _call(task_id, WeaponryOperation.TARGET_RETRIEVAL)
            result = adapter.search_target(
                SearchTargetEvidence(
                    scope=scope,
                    call=call,
                    query=build_retrieval_query(RetrievalField("舰级名称")),
                    allowed_document_keys=("doc-a", "doc-b"),
                    candidate_top_n=8,
                )
            )
            selected = select_evidence(
                result.candidates,
                score_mode=result.score_mode,
                query=build_retrieval_query(RetrievalField("舰级名称")),
                profile=policy,
                provider_fingerprint=result.provider_fingerprint,
                embedding_fingerprint=result.embedding_fingerprint,
                expected_document_keys=("doc-a", "doc-b"),
            )
            retry_result = adapter.search_target(
                SearchTargetEvidence(
                    scope=scope,
                    call=_call(
                        task_id,
                        WeaponryOperation.TARGET_RETRIEVAL,
                        attempt_no=2,
                    ),
                    query=build_retrieval_query(RetrievalField("舰级名称")),
                    allowed_document_keys=("doc-a", "doc-b"),
                    candidate_top_n=8,
                )
            )

            self.assertEqual("score", result.score_mode)
            self.assertEqual(("doc-a", "doc-b"), tuple(
                item.document_key for item in selected.selected
            ))
            self.assertEqual(
                tuple(item.candidate_id for item in result.candidates),
                tuple(item.candidate_id for item in retry_result.candidates),
            )
            self.assertEqual(0.0, runtime.vector_calls[0][3])
            self.assertEqual([scope.scope_ref], runtime.workspace_writes)
            self.assertNotIn("permanent-source-workspace", runtime.workspace_writes)
            record = store.get(task_id)
            assert record is not None
            self.assertEqual(3, len(record.resources))
            self.assertTrue(adapter.close_scope(scope).success)
            self.assertEqual(0, runtime.active_leases)

    def test_retrieval_accepts_only_exact_frozen_location_from_source_url(self) -> None:
        """兼容真实 vector-search 的 metadata.url，但禁止退化为展示名或模糊匹配。"""

        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-source-url")
            runtime = _FakeAnythingRuntime()
            runtime.vector_sources = (
                AnythingLLMSource(
                    document_ref="name:not-authoritative.pdf",
                    text="由完整 URL 身份映射的候选证据正文，长度满足质量门禁。",
                    id="chunk-not-authoritative",
                    score=0.91,
                    score_present=True,
                    score_valid=True,
                    metadata=MappingProxyType(
                        {
                            "url": "custom-documents/a.json",
                            "title": "另一个展示名称.pdf",
                        }
                    ),
                ),
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            document = _document(1, "a")
            scope = adapter.open_scope(
                OpenTargetEvidenceScope(
                    task_id=task_id,
                    document_scope=_scope(document),
                    policy=_policy(),
                )
            )

            result = adapter.search_target(
                SearchTargetEvidence(
                    scope=scope,
                    call=_call(task_id, WeaponryOperation.TARGET_RETRIEVAL),
                    query=build_retrieval_query(RetrievalField("舰级名称")),
                    allowed_document_keys=(document.document_key,),
                    candidate_top_n=8,
                )
            )

            self.assertEqual(1, len(result.candidates))
            self.assertEqual(
                document.document_key,
                result.candidates[0].document_key,
            )
            self.assertTrue(adapter.close_scope(scope).success)

    def test_retrieval_accepts_exact_unique_ingested_name_from_structured_url(self) -> None:
        """真实 hotdir URL 只按冻结入库文件名精确映射，不读取展示 title。"""

        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-source-ingested-url")
            runtime = _FakeAnythingRuntime()
            runtime.vector_sources = (
                AnythingLLMSource(
                    document_ref="name:not-authoritative.pdf",
                    text="由入库文件 URL 身份映射的候选证据正文，长度满足质量门禁。",
                    score=0.91,
                    score_present=True,
                    score_valid=True,
                    metadata=MappingProxyType(
                        {
                            "url": "file:///app/server/storage/hotdir/a.mhtml",
                            "title": "伪造展示名称.pdf",
                        }
                    ),
                ),
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            document = _document(1, "a")
            scope = adapter.open_scope(
                OpenTargetEvidenceScope(
                    task_id=task_id,
                    document_scope=_scope(document),
                    policy=_policy(),
                )
            )

            result = adapter.search_target(
                SearchTargetEvidence(
                    scope=scope,
                    call=_call(task_id, WeaponryOperation.TARGET_RETRIEVAL),
                    query=build_retrieval_query(RetrievalField("舰级名称")),
                    allowed_document_keys=(document.document_key,),
                    candidate_top_n=8,
                )
            )

            self.assertEqual(document.document_key, result.candidates[0].document_key)
            self.assertTrue(adapter.close_scope(scope).success)

    def test_retrieval_rejects_duplicate_ingested_names_before_search(self) -> None:
        """即使业务文件名不同，来源 URL 无法唯一归属时也不得创建可搜索范围。"""

        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-ingested-name-conflict")
            runtime = _FakeAnythingRuntime()
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            first = _document(1, "a")
            second = WeaponryDocumentSnapshot(
                sequence_no=2,
                document_key="doc-b",
                file_name="different-business-name.pdf",
                original_name="另一份原始文件.pdf",
                ingested_file_name=first.ingested_file_name,
                source_architecture_id=7,
                external_document_ref="custom-documents/b.json",
                anything_document_id="provider-b",
            )

            with self.assertRaisesRegex(
                WeaponrySourceBoundaryError,
                "入库文件名无法唯一映射",
            ):
                adapter.open_scope(
                    OpenTargetEvidenceScope(
                        task_id=task_id,
                        document_scope=_scope(first, second),
                        policy=_policy(),
                    )
                )

    def test_retrieval_still_rejects_title_only_source_identity(self) -> None:
        """同名 title 不是权威位置，不能借 URL 兼容重新进入身份判定。"""

        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-title-rejected")
            runtime = _FakeAnythingRuntime()
            runtime.vector_sources = (
                AnythingLLMSource(
                    document_ref="name:a.pdf",
                    text="只有展示标题而没有完整位置的候选证据正文。",
                    score=0.91,
                    score_present=True,
                    score_valid=True,
                    metadata=MappingProxyType({"title": "a.pdf"}),
                ),
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            document = _document(1, "a")
            scope = adapter.open_scope(
                OpenTargetEvidenceScope(
                    task_id=task_id,
                    document_scope=_scope(document),
                    policy=_policy(),
                )
            )

            with self.assertRaisesRegex(
                WeaponrySourceBoundaryError,
                "无法唯一映射",
            ):
                adapter.search_target(
                    SearchTargetEvidence(
                        scope=scope,
                        call=_call(task_id, WeaponryOperation.TARGET_RETRIEVAL),
                        query=build_retrieval_query(RetrievalField("舰级名称")),
                        allowed_document_keys=(document.document_key,),
                        candidate_top_n=8,
                    )
                )
            self.assertTrue(adapter.close_scope(scope).success)

    def test_retrieval_rejects_bare_file_name_masquerading_as_url(self) -> None:
        """单独文件名没有 URL/路径结构，不得借字段名伪装成强来源身份。"""

        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-bare-url-rejected")
            runtime = _FakeAnythingRuntime()
            runtime.vector_sources = (
                AnythingLLMSource(
                    document_ref="name:a.mhtml",
                    text="只有裸文件名而没有结构化来源路径的候选证据正文。",
                    score=0.91,
                    score_present=True,
                    score_valid=True,
                    metadata=MappingProxyType({"url": "a.mhtml"}),
                ),
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            document = _document(1, "a")
            scope = adapter.open_scope(
                OpenTargetEvidenceScope(
                    task_id=task_id,
                    document_scope=_scope(document),
                    policy=_policy(),
                )
            )

            with self.assertRaisesRegex(WeaponrySourceBoundaryError, "无法唯一映射"):
                adapter.search_target(
                    SearchTargetEvidence(
                        scope=scope,
                        call=_call(task_id, WeaponryOperation.TARGET_RETRIEVAL),
                        query=build_retrieval_query(RetrievalField("舰级名称")),
                        allowed_document_keys=(document.document_key,),
                        candidate_top_n=8,
                    )
                )
            self.assertTrue(adapter.close_scope(scope).success)

    def test_mixed_score_batch_and_unresolved_source_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-mixed")
            runtime = _FakeAnythingRuntime()
            runtime.vector_sources = (
                AnythingLLMSource(
                    "",
                    "A 文档中的完整候选证据正文，长度足够。",
                    score=0.8,
                    score_present=True,
                    metadata=MappingProxyType({"location": "custom-documents/a.json"}),
                ),
                AnythingLLMSource(
                    "",
                    "B 文档中的完整候选证据正文，长度足够。",
                    score=None,
                    score_present=False,
                    metadata=MappingProxyType({"location": "custom-documents/b.json"}),
                ),
            )
            adapter, _ = self._adapter(
                str(Path(directory) / "tasks.sqlite3"), runtime, task_id
            )
            scope = adapter.open_scope(
                OpenTargetEvidenceScope(task_id, _scope(_document(1, "a"), _document(2, "b")), _policy())
            )
            with self.assertRaisesRegex(WeaponrySourceBoundaryError, "混合"):
                adapter.search_target(
                    SearchTargetEvidence(
                        scope,
                        _call(task_id, WeaponryOperation.TARGET_RETRIEVAL),
                        build_retrieval_query(RetrievalField("舰级")),
                        ("doc-a", "doc-b"),
                        8,
                    )
                )
            adapter.close_scope(scope)

    def test_duplicate_binding_and_provider_id_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("retrieval-duplicate-binding")
            runtime = _FakeAnythingRuntime()
            runtime.duplicate_bound_document = True
            adapter, _ = self._adapter(
                str(Path(directory) / "duplicate.sqlite3"),
                runtime,
                task_id,
            )
            with self.assertRaisesRegex(WeaponrySourceBoundaryError, "重复绑定"):
                adapter.open_scope(
                    OpenTargetEvidenceScope(
                        task_id,
                        _scope(_document(1, "a")),
                        _policy(),
                    )
                )
            self.assertEqual(0, runtime.active_leases)

            drift_task_id = TaskId("retrieval-provider-id-drift")
            drift_runtime = _FakeAnythingRuntime()
            drift_runtime.reported_provider_ids["custom-documents/a.json"] = (
                "unexpected-provider-id"
            )
            drift_adapter, _ = self._adapter(
                str(Path(directory) / "drift.sqlite3"),
                drift_runtime,
                drift_task_id,
            )
            with self.assertRaisesRegex(WeaponrySourceBoundaryError, "ID"):
                drift_adapter.open_scope(
                    OpenTargetEvidenceScope(
                        drift_task_id,
                        _scope(_document(1, "a")),
                        _policy(),
                    )
                )
            self.assertEqual(0, drift_runtime.active_leases)


class WeaponryCreationIntentRecoveryTests(unittest.TestCase):
    def test_crash_window_workspace_is_reconciled_and_scene_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            task_id = TaskId("creation-crash-window")
            resources = SQLiteWeaponryResourceStoreAdapter(db_path)
            resources.create(_resource_record(task_id))
            intents = SQLiteWeaponryCreationIntentStoreAdapter(db_path)
            intent = WeaponryCreationIntent(
                task_id=task_id,
                intent_id="retrieval-workspace",
                kind=WeaponryCreationIntentKind.RETRIEVAL_WORKSPACE,
                expected_name="docsense-weaponry-retrieval-crash-window",
                identity_digest=hashlib.sha256(b"scope").hexdigest(),
            )
            intents.reserve(intent)
            runtime = _FakeAnythingRuntime()
            runtime.workspace_locations[intent.expected_name] = ()
            recovery = AnythingLLMWeaponryCreationIntentRecoveryAdapter(
                runtime,
                intents,
                resources,
            )

            result = recovery.run_once(limit=10)
            stored_intent = intents.get(task_id, intent.intent_id)
            resource_record = resources.get(task_id)

        self.assertEqual(1, result.reconciled_count)
        self.assertEqual([], runtime.created_workspaces)
        self.assertEqual(WeaponryCreationIntentState.RESOLVED, stored_intent.state)
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            resource_record.state,
        )
        self.assertEqual(1, len(resource_record.resources))

    def test_missing_unique_match_is_quarantined_without_create_or_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            task_id = TaskId("creation-crash-no-match")
            resources = SQLiteWeaponryResourceStoreAdapter(db_path)
            resources.create(_resource_record(task_id))
            intents = SQLiteWeaponryCreationIntentStoreAdapter(db_path)
            intent = WeaponryCreationIntent(
                task_id=task_id,
                intent_id="retrieval-workspace",
                kind=WeaponryCreationIntentKind.RETRIEVAL_WORKSPACE,
                expected_name="docsense-weaponry-retrieval-no-match",
                identity_digest=hashlib.sha256(b"scope").hexdigest(),
            )
            intents.reserve(intent)
            runtime = _FakeAnythingRuntime()
            recovery = AnythingLLMWeaponryCreationIntentRecoveryAdapter(
                runtime,
                intents,
                resources,
            )

            result = recovery.run_once(limit=10)
            stored_intent = intents.get(task_id, intent.intent_id)

        self.assertEqual(1, result.quarantined_count)
        self.assertEqual(WeaponryCreationIntentState.QUARANTINED, stored_intent.state)
        self.assertEqual([], runtime.created_workspaces)
        self.assertEqual([], runtime.deleted_workspaces)

    def test_workspace_list_is_loaded_once_for_a_bounded_batch(self) -> None:
        """批量恢复只允许一次远端清单读取，避免积压量线性放大 HTTP I/O。"""

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            resources = SQLiteWeaponryResourceStoreAdapter(db_path)
            intents = SQLiteWeaponryCreationIntentStoreAdapter(db_path)
            runtime = _FakeAnythingRuntime()
            for index in range(2):
                task_id = TaskId(f"creation-batch-{index}")
                resources.create(_resource_record(task_id))
                intent = WeaponryCreationIntent(
                    task_id=task_id,
                    intent_id="retrieval-workspace",
                    kind=WeaponryCreationIntentKind.RETRIEVAL_WORKSPACE,
                    expected_name=f"docsense-weaponry-batch-{index}",
                    identity_digest=hashlib.sha256(
                        f"scope-{index}".encode("utf-8")
                    ).hexdigest(),
                )
                intents.reserve(intent)
                runtime.workspace_locations[intent.expected_name] = ()
            recovery = AnythingLLMWeaponryCreationIntentRecoveryAdapter(
                runtime,
                intents,
                resources,
            )

            result = recovery.run_once(limit=10)

        self.assertEqual(2, result.reconciled_count)
        self.assertEqual(1, runtime.list_workspaces_calls)


class WeaponryProvidedEvidenceExtractionAdapterTests(unittest.TestCase):
    @staticmethod
    def _request(task_id: TaskId, document: WeaponryDocumentSnapshot, text: str):
        selected = SelectedEvidence(
            candidate_id=f"evidence-{document.document_key}",
            document_key=document.document_key,
            text=text,
            provider_rank=1,
            provider_score=0.9,
            score_profile_id=_policy().profile_id,
            score_mode="score",
            original_index=0,
        )
        field = WeaponryFieldSpecification.from_mapping(
            {
                "templateClassifyId": 1,
                "fieldName": "舰级名称",
                "fieldType": "INPUT",
                "fieldDescription": "提取该来源明确记载的舰级",
            }
        )
        prompt = build_input_extraction_prompt(field, (selected,))
        return EvidenceExtractionRequest(
            call=_call(
                task_id,
                WeaponryOperation.EVIDENCE_EXTRACTION,
                document_sequence=document.sequence_no,
            ),
            document=document,
            field=field,
            evidence=(selected,),
            prompt=prompt,
            guidance=(),
            context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
            model_fingerprint="anythingllm-chat-model:test",
        )

    def test_each_source_uses_new_empty_context_and_only_its_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            runtime = _FakeAnythingRuntime()
            adapter = AnythingLLMProvidedEvidenceExtractionAdapter(
                runtime,
                _resource_registrar(store, db_path),
                model_fingerprint="anythingllm-chat-model:test",
            )
            task_a = TaskId("extract-a")
            task_b = TaskId("extract-b")
            store.create(_resource_record(task_a, "7"))
            store.create(_resource_record(task_b, "8"))
            request_a = self._request(
                task_a,
                _document(1, "a"),
                "A 文档只包含甲级舰艇的完整证据正文。",
            )
            request_b = self._request(
                task_b,
                _document(1, "b"),
                "B 文档只包含乙级舰艇的完整证据正文。",
            )
            answer_a = adapter.extract(request_a)
            answer_b = adapter.extract(request_b)

            self.assertEqual(answer_a.text, answer_b.text)
            prompt_a = str(runtime.ask_calls[0][2])
            prompt_b = str(runtime.ask_calls[1][2])
            self.assertIn(request_a.evidence[0].text, prompt_a)
            self.assertNotIn(request_b.evidence[0].text, prompt_a)
            self.assertIn(request_b.evidence[0].text, prompt_b)
            self.assertNotIn(request_a.evidence[0].text, prompt_b)
            self.assertEqual("chat", runtime.ask_calls[0][3])
            self.assertEqual((), runtime.ask_calls[0][4])
            self.assertNotEqual(runtime.created_workspaces[0], runtime.created_workspaces[1])
            self.assertEqual(0, runtime.active_leases)

    def test_source_pollution_and_uninstalled_context_strategy_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            runtime = _FakeAnythingRuntime()
            task_id = TaskId("extract-pollution")
            store.create(_resource_record(task_id))
            adapter = AnythingLLMProvidedEvidenceExtractionAdapter(
                runtime,
                _resource_registrar(store, db_path),
                model_fingerprint="anythingllm-chat-model:test",
            )
            request = self._request(
                task_id,
                _document(1, "a"),
                "A 文档中的完整证据正文用于污染测试。",
            )
            runtime.answer_sources = (
                AnythingLLMSource("name:foreign.pdf", "foreign source"),
            )
            with self.assertRaises(WeaponrySourceBoundaryError):
                adapter.extract(request)

            alternate = EvidenceExtractionRequest(
                **{
                    **request.__dict__,
                    "context_strategy": EXTRACTION_CONTEXT_EVIDENCE_ONLY_V1,
                }
            )
            calls_before = len(runtime.created_workspaces)
            with self.assertRaisesRegex(WeaponryPortStateError, "只安装"):
                adapter.extract(alternate)
            self.assertEqual(calls_before, len(runtime.created_workspaces))

    def test_missing_record_and_registration_failure_never_continue_to_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("extract-resource-guard")
            request = self._request(
                task_id,
                _document(1, "a"),
                "A 文档中的完整证据正文用于资源前置校验。",
            )
            runtime = _FakeAnythingRuntime()
            db_path = str(Path(directory) / "tasks.sqlite3")
            empty_store = SQLiteWeaponryResourceStoreAdapter(db_path)
            guarded_adapter = AnythingLLMProvidedEvidenceExtractionAdapter(
                runtime,
                _resource_registrar(empty_store, db_path),
                model_fingerprint="anythingllm-chat-model:test",
            )
            with self.assertRaises(WeaponryPortStateError):
                guarded_adapter.extract(request)
            self.assertEqual([], runtime.created_workspaces)
            self.assertEqual([], runtime.ask_calls)

            failed_runtime = _FakeAnythingRuntime()
            failed_adapter = AnythingLLMProvidedEvidenceExtractionAdapter(
                failed_runtime,
                _FailingCreatedResourceRegistrar(),
                model_fingerprint="anythingllm-chat-model:test",
            )
            with self.assertRaises(WeaponryPortStateError):
                failed_adapter.extract(request)
            self.assertEqual(
                failed_runtime.created_workspaces,
                failed_runtime.deleted_workspaces,
            )
            self.assertEqual([], failed_runtime.ask_calls)

            unknown_runtime = _FakeAnythingRuntime()
            unknown_runtime.delete_error = RuntimeError("delete outcome unknown")
            unknown_adapter = AnythingLLMProvidedEvidenceExtractionAdapter(
                unknown_runtime,
                _FailingCreatedResourceRegistrar(),
                model_fingerprint="anythingllm-chat-model:test",
            )
            with self.assertRaises(WeaponryExternalOperationError) as captured:
                unknown_adapter.extract(request)
            self.assertEqual(
                WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                captured.exception.outcome,
            )
            self.assertEqual([], unknown_runtime.ask_calls)

    def test_extraction_workspace_conflict_is_not_blindly_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task_id = TaskId("extract-create-conflict")
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            store.create(_resource_record(task_id))
            runtime = _FakeAnythingRuntime()
            runtime.create_workspace_error = AnythingLLMHTTPError(
                "injected deterministic extraction conflict",
                method="POST",
                url="http://anythingllm.local/workspace/new",
                status_code=409,
                response_summary="workspace already exists",
            )
            adapter = AnythingLLMProvidedEvidenceExtractionAdapter(
                runtime,
                _resource_registrar(store, db_path),
                model_fingerprint="anythingllm-chat-model:test",
            )

            with self.assertRaises(WeaponryExternalOperationError) as captured:
                adapter.extract(
                    self._request(
                        task_id,
                        _document(1, "a"),
                        "A 文档中的完整证据正文用于 409 结果未知测试。",
                    )
                )

            self.assertIs(
                WeaponryExternalOutcome.OUTCOME_UNKNOWN,
                captured.exception.outcome,
            )
            self.assertEqual([], runtime.ask_calls)


class WeaponryOptionalAdaptersTests(unittest.TestCase):
    def test_no_auxiliary_is_zero_io_and_terms_failure_degrades(self) -> None:
        task_id = TaskId("auxiliary-task")
        field = WeaponryFieldSpecification.from_mapping(
            {
                "templateClassifyId": 1,
                "fieldName": "雷达型号",
                "fieldType": "INPUT",
                "fieldDescription": "正式型号",
            }
        )
        no_request = AuxiliaryGuidanceRequest(
            call=_call(task_id, WeaponryOperation.AUXILIARY_GUIDANCE),
            field=field,
            policy=AuxiliaryGuidancePolicySnapshot(AUXILIARY_GUIDANCE_NONE, "", 0, 0),
        )
        self.assertEqual(
            AuxiliaryGuidanceOutcome.EMPTY,
            NoAuxiliaryGuidanceAdapter().load(no_request).outcome,
        )

        provider = _FakeTermsProvider()
        terms_request = AuxiliaryGuidanceRequest(
            call=_call(task_id, WeaponryOperation.AUXILIARY_GUIDANCE, attempt_no=2),
            field=field,
            policy=AuxiliaryGuidancePolicySnapshot(
                AUXILIARY_GUIDANCE_TERMS_RULES_V1,
                "terms-catalog:test",
                2,
                20,
            ),
        )
        terms = TermsRuleGuidanceAdapter(
            provider,
            catalog_fingerprint="terms-catalog:test",
        )
        result = terms.load(terms_request)
        self.assertEqual(AuxiliaryGuidanceOutcome.PROVIDED, result.outcome)
        self.assertLessEqual(sum(len(item.text) for item in result.guidance), 20)
        provider.error = RuntimeError("provider down")
        degraded = terms.load(terms_request)
        self.assertEqual(AuxiliaryGuidanceOutcome.DEGRADED, degraded.outcome)

    def test_translation_success_and_failure_keep_compatibility(self) -> None:
        request = WeaponryTranslationRequest(
            call=_call(
                TaskId("translation-task"),
                WeaponryOperation.TRANSLATION,
                document_sequence=1,
                item_sequence=1,
            ),
            text="source text",
            target_language="Chinese",
        )
        success = LLMTranslationServiceWeaponryAdapter(_FakeTranslator()).translate(request)
        self.assertEqual(WeaponryTranslationOutcome.SUCCEEDED, success.outcome)
        failed = LLMTranslationServiceWeaponryAdapter(
            _FakeTranslator(fail=True)
        ).translate(request)
        self.assertEqual(WeaponryTranslationOutcome.FAILED, failed.outcome)
        self.assertEqual("", failed.text)


class WeaponrySQLiteAuditAndResourceTests(unittest.TestCase):
    def test_audit_schema_adds_translation_item_sequence_to_existing_empty_table(self) -> None:
        """开发库旧空表可以无损加列；不承担历史 Worker/业务数据兼容。"""

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            with closing(sqlite3.connect(db_path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE weaponry_interaction_audits (
                        reservation_id TEXT PRIMARY KEY,
                        audit_id TEXT NOT NULL,
                        attempt_key TEXT NOT NULL UNIQUE,
                        task_id TEXT NOT NULL,
                        business_key TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        field_sequence INTEGER NOT NULL,
                        document_sequence INTEGER,
                        attempt_no INTEGER NOT NULL,
                        reserve_payload_json TEXT NOT NULL,
                        state TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        complete_payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """
                )
            adapter = SQLiteWeaponryInteractionAuditAdapter(db_path)
            with closing(sqlite3.connect(db_path)) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(weaponry_interaction_audits)"
                    ).fetchall()
                }
            self.assertIn("item_sequence", columns)

            task_id = TaskId("translation-migration-task")
            call = _call(
                task_id,
                WeaponryOperation.TRANSLATION,
                document_sequence=1,
                item_sequence=2,
            )
            reserve_result = adapter.reserve(
                ReserveWeaponryInteraction(
                    business_ref=TaskBusinessRef("weaponry", "7"),
                    call=call,
                    input_digest=hashlib.sha256(b"cell").hexdigest(),
                    input_chars=4,
                    allowed_document_keys=("doc-a",),
                )
            )
            self.assertIs(WeaponryAuditReserveOutcome.RESERVED, reserve_result.outcome)
            reservation = reserve_result.reservation
            self.assertEqual((reservation,), adapter.list_pending(task_id, limit=10))

    def test_audit_reserve_classifies_new_pending_and_completed_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = SQLiteWeaponryInteractionAuditAdapter(
                str(Path(directory) / "tasks.sqlite3")
            )
            task_id = TaskId("audit-task")
            call = _call(task_id, WeaponryOperation.TARGET_RETRIEVAL)
            reserve = ReserveWeaponryInteraction(
                business_ref=TaskBusinessRef("weaponry", "7"),
                call=call,
                input_digest=hashlib.sha256(b"input").hexdigest(),
                input_chars=5,
                allowed_document_keys=("doc-a",),
            )
            first = adapter.reserve(reserve)
            self.assertIs(WeaponryAuditReserveOutcome.RESERVED, first.outcome)
            reservation = first.reservation
            pending = adapter.reserve(reserve)
            self.assertIs(WeaponryAuditReserveOutcome.PENDING, pending.outcome)
            self.assertEqual(reservation, pending.reservation)
            self.assertEqual((reservation,), adapter.list_pending(task_id, limit=10))
            complete = CompleteWeaponryInteraction(
                reservation=reservation,
                outcome=WeaponryAuditOutcome.SUCCEEDED,
                output_digest=hashlib.sha256(b"output").hexdigest(),
                output_chars=6,
                candidate_count=1,
                selected_count=1,
            )
            receipt = adapter.complete(complete)
            self.assertEqual(receipt, adapter.complete(complete))
            self.assertEqual((), adapter.list_pending(task_id, limit=10))
            completed = adapter.reserve(reserve)
            self.assertIs(WeaponryAuditReserveOutcome.COMPLETED, completed.outcome)
            self.assertEqual(reservation, completed.reservation)

    def test_resource_store_cas_shared_guard_and_fencing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteWeaponryResourceStoreAdapter(
                str(Path(directory) / "tasks.sqlite3")
            )
            task_id = TaskId("resource-task")
            record = store.create(_resource_record(task_id))
            owned = WeaponryTrackedResource(
                resource_id="owned-scope",
                kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                external_ref="scope-1",
                ownership=WeaponryResourceOwnership.OWNED,
                idempotency_key="owned-key",
            )
            record = store.register(RegisterWeaponryResource(task_id, owned, record.version))
            with self.assertRaises(WeaponryPortStateError) as conflict:
                # 空记录可以幂等重放；一旦已经登记外部资源，同一 TaskId 就必须返回
                # 稳定冲突码，让 Run Application 隔离崩溃现场而不是盲目重建。
                store.create(_resource_record(task_id))
            self.assertEqual(
                "resource_record_exists",
                conflict.exception.error_code,
            )
            shared = WeaponryTrackedResource(
                resource_id="shared-mapping",
                kind=WeaponryResourceKind.SOURCE_MAPPING,
                external_ref="shared-map",
                ownership=WeaponryResourceOwnership.SHARED,
                idempotency_key="shared-key",
                document_key="doc-a",
            )
            record = store.register(RegisterWeaponryResource(task_id, shared, record.version))
            with self.assertRaisesRegex(WeaponryPortStateError, "版本"):
                store.register(
                    RegisterWeaponryResource(
                        task_id,
                        WeaponryTrackedResource(
                            "late",
                            WeaponryResourceKind.RETRIEVAL_SCOPE,
                            "late",
                            WeaponryResourceOwnership.OWNED,
                            "late-key",
                        ),
                        0,
                    )
                )
            record = store.prepare_cleanup(
                PrepareWeaponryResourceCleanup(task_id, record.version)
            )
            acquired = store.acquire_cleanup(
                AcquireWeaponryCleanupLease(task_id, record.version)
            )
            assert acquired.lease is not None
            current = store.get(task_id)
            assert current is not None
            with self.assertRaisesRegex(WeaponryPortStateError, "shared"):
                store.complete_cleanup(
                    CompleteWeaponryResourceCleanup(
                        task_id,
                        acquired.lease,
                        shared.resource_id,
                        WeaponryResourceCleanupOutcome.SUCCEEDED,
                        current.version,
                    )
                )
            current = store.complete_cleanup(
                CompleteWeaponryResourceCleanup(
                    task_id,
                    acquired.lease,
                    owned.resource_id,
                    WeaponryResourceCleanupOutcome.SUCCEEDED,
                    current.version,
                )
            )
            self.assertEqual(WeaponryResourceRecordState.CLEANED, current.state)

    def test_resource_quarantine_manual_retry_is_audited_and_recoverable(self) -> None:
        """远端对账后可重新进入清理循环，且首次隔离事实由追加审计保留。"""

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            task_id = TaskId("quarantine-retry")
            record = store.create(_resource_record(task_id))
            record = store.register(
                RegisterWeaponryResource(
                    task_id,
                    WeaponryTrackedResource(
                        resource_id="owned-workspace",
                        kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                        external_ref="temporary-workspace",
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key="owned-workspace-key",
                    ),
                    record.version,
                )
            )
            quarantined = store.quarantine(
                QuarantineWeaponryResources(
                    task_id=task_id,
                    expected_version=record.version,
                    error_code="cleanup_outcome_unknown",
                    reason="远端删除请求结果未知",
                )
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE llm_task_executions (
                        execution_id TEXT PRIMARY KEY,
                        business_type TEXT NOT NULL,
                        execution_state TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO llm_task_executions VALUES (?, 'weaponry', 'failed')",
                    (task_id.value,),
                )
                connection.commit()

            resolved = store.resolve_quarantine(
                task_id,
                action="retry_cleanup",
                resolved_by="operator-001",
                reason="已确认临时工作区仍存在，允许重试删除",
                external_state_confirmed=True,
            )
            audits = store.list_operator_audits(task_id)

        self.assertEqual(
            WeaponryResourceRecordState.CLEANUP_PENDING,
            resolved.state,
        )
        self.assertEqual(
            WeaponryTrackedResourceState.CLEANUP_PENDING,
            resolved.resources[0].state,
        )
        self.assertTrue(resolved.next_retry_at)
        self.assertEqual(quarantined.version + 1, resolved.version)
        self.assertEqual(1, len(audits))
        self.assertEqual("retry_cleanup", audits[0]["action"])
        self.assertEqual("operator-001", audits[0]["resolved_by"])
        self.assertEqual(
            "cleanup_outcome_unknown",
            audits[0]["previous_error_code"],
        )

    def test_resource_quarantine_confirmed_absent_is_cleaned_and_audited(self) -> None:
        """只有显式确认远端资源均不存在时，才允许直接把 owned 资源记为 cleaned。"""

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            task_id = TaskId("quarantine-absent")
            record = store.create(_resource_record(task_id))
            record = store.register(
                RegisterWeaponryResource(
                    task_id,
                    WeaponryTrackedResource(
                        resource_id="owned-thread",
                        kind=WeaponryResourceKind.EXTRACTION_CONTEXT,
                        external_ref="temporary-thread",
                        ownership=WeaponryResourceOwnership.OWNED,
                        idempotency_key="owned-thread-key",
                    ),
                    record.version,
                )
            )
            store.quarantine(
                QuarantineWeaponryResources(
                    task_id=task_id,
                    expected_version=record.version,
                    error_code="cleanup_outcome_unknown",
                    reason="远端删除请求结果未知",
                )
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE llm_task_executions (
                        execution_id TEXT PRIMARY KEY,
                        business_type TEXT NOT NULL,
                        execution_state TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO llm_task_executions VALUES (?, 'weaponry', 'failed')",
                    (task_id.value,),
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "确认远端资源状态"):
                store.resolve_quarantine(
                    task_id,
                    action="confirmed_absent",
                    resolved_by="operator-002",
                    reason="尚未完成对账",
                    external_state_confirmed=False,
                )
            resolved = store.resolve_quarantine(
                task_id,
                action="confirmed_absent",
                resolved_by="operator-002",
                reason="已从供应商后台确认全部临时资源不存在",
                external_state_confirmed=True,
            )

        self.assertEqual(WeaponryResourceRecordState.CLEANED, resolved.state)
        self.assertEqual(
            WeaponryTrackedResourceState.CLEANED,
            resolved.resources[0].state,
        )

    def test_resource_quarantine_resolution_rejects_active_execution(self) -> None:
        """人工命令不得与仍持有任务执行权的 Worker 竞争远端资源所有权。"""

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            task_id = TaskId("quarantine-active")
            record = store.create(_resource_record(task_id))
            quarantined = store.quarantine(
                QuarantineWeaponryResources(
                    task_id=task_id,
                    expected_version=record.version,
                    error_code="creation_outcome_unknown",
                    reason="远端创建结果未知",
                )
            )
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE llm_task_executions (
                        execution_id TEXT PRIMARY KEY,
                        business_type TEXT NOT NULL,
                        execution_state TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO llm_task_executions (
                        execution_id, business_type, execution_state
                    ) VALUES (?, 'weaponry', 'running')
                    """,
                    (task_id.value,),
                )
                connection.commit()

            with self.assertRaisesRegex(WeaponryPortStateError, "活跃 execution"):
                store.resolve_quarantine(
                    task_id,
                    action="confirmed_absent",
                    resolved_by="operator-003",
                    reason="不应在 Worker 活跃时执行",
                    external_state_confirmed=True,
                )
            self.assertEqual(quarantined, store.get(task_id))

    def test_resource_quarantine_resolution_fails_closed_without_execution_table(self) -> None:
        """运维误指向旧库时不能把无法核验活跃状态误当成 Worker 已停止。"""

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteWeaponryResourceStoreAdapter(
                str(Path(directory) / "tasks.sqlite3")
            )
            task_id = TaskId("quarantine-no-execution-table")
            record = store.create(_resource_record(task_id))
            store.quarantine(
                QuarantineWeaponryResources(
                    task_id=task_id,
                    expected_version=record.version,
                    error_code="creation_outcome_unknown",
                    reason="远端创建结果未知",
                )
            )

            with self.assertRaisesRegex(
                WeaponryPortStateError,
                "缺少 execution 权威表",
            ):
                store.resolve_quarantine(
                    task_id,
                    action="confirmed_absent",
                    resolved_by="operator-004",
                    reason="误指向旧数据库",
                    external_state_confirmed=True,
                )

    def test_terminal_execution_recovers_tracking_record_after_intent_crash_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            terminal_states = ("succeeded", "failed", "stale")
            terminal_task_ids = tuple(
                TaskId(f"terminal-{state}") for state in terminal_states
            )
            running_task_id = TaskId("still-running")
            orphan_task_id = TaskId("execution-row-missing")

            for task_id in (*terminal_task_ids, running_task_id, orphan_task_id):
                record = store.create(_resource_record(task_id))
                store.register(
                    RegisterWeaponryResource(
                        task_id,
                        WeaponryTrackedResource(
                            resource_id=f"scope-{task_id.value}",
                            kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                            external_ref=f"workspace-{task_id.value}",
                            ownership=WeaponryResourceOwnership.OWNED,
                            idempotency_key=f"resource-{task_id.value}",
                        ),
                        record.version,
                    )
                )

            with closing(sqlite3.connect(db_path)) as connection:
                # list_recoverable 只依赖这三个权威字段；使用最小表可精确验证资源 Adapter
                # 的联表语义，避免把任务 Repository 的受理流程混入本测试。
                connection.execute(
                    """
                    CREATE TABLE llm_task_executions (
                        execution_id TEXT PRIMARY KEY,
                        business_type TEXT NOT NULL,
                        execution_state TEXT NOT NULL
                    )
                    """
                )
                connection.executemany(
                    """
                    INSERT INTO llm_task_executions (
                        execution_id, business_type, execution_state
                    ) VALUES (?, 'weaponry', ?)
                    """,
                    tuple(
                        (task_id.value, state)
                        for task_id, state in zip(terminal_task_ids, terminal_states)
                    )
                    + ((running_task_id.value, "running"),),
                )
                connection.commit()

            recoverable = store.list_recoverable(limit=20)

            self.assertEqual(
                set((*terminal_task_ids, orphan_task_id)),
                set(recoverable),
            )
            self.assertNotIn(running_task_id, recoverable)

    def test_corrupt_persistence_is_reported_as_stable_state_error(self) -> None:
        """内部 SQLite 损坏不得把 JSON/枚举/转换异常泄漏成不可诊断的 Worker 500。"""

        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            audit = SQLiteWeaponryInteractionAuditAdapter(db_path)
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            task_id = TaskId("corrupt-persistence")
            store.create(_resource_record(task_id))
            audit.reserve(
                ReserveWeaponryInteraction(
                    business_ref=TaskBusinessRef("weaponry", "7"),
                    call=_call(task_id, WeaponryOperation.TARGET_RETRIEVAL),
                    input_digest=hashlib.sha256(b"input").hexdigest(),
                    input_chars=5,
                )
            )

            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE weaponry_interaction_audits SET operation = 'broken' "
                    "WHERE task_id = ?",
                    (task_id.value,),
                )
                connection.commit()
            with self.assertRaisesRegex(WeaponryPortStateError, "pending 审计记录"):
                audit.list_pending(task_id, limit=10)

            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "UPDATE weaponry_resource_records SET payload_json = '{}' "
                    "WHERE task_id = ?",
                    (task_id.value,),
                )
                connection.commit()
            with self.assertRaisesRegex(WeaponryPortStateError, "资源记录"):
                store.get(task_id)

    def test_fifty_concurrent_records_and_audits_remain_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(db_path)
            audit = SQLiteWeaponryInteractionAuditAdapter(db_path)
            registrar = _resource_registrar(store, db_path)
            task_ids = tuple(TaskId(f"concurrent-{index:02d}") for index in range(50))
            for index, task_id in enumerate(task_ids, start=1):
                store.create(_resource_record(task_id, str(index)))

            def execute(index_task):
                index, task_id = index_task
                registrar.register_created(
                    task_id=task_id,
                    resource_id=f"scope-{index}",
                    kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                    external_ref=f"workspace-{index}",
                    ownership=WeaponryResourceOwnership.OWNED,
                    idempotency_key=f"resource-{index}",
                )
                call = _call(task_id, WeaponryOperation.TARGET_RETRIEVAL)
                result = audit.reserve(
                    ReserveWeaponryInteraction(
                        business_ref=TaskBusinessRef("weaponry", str(index + 1)),
                        call=call,
                        input_digest=hashlib.sha256(task_id.value.encode()).hexdigest(),
                        input_chars=len(task_id.value),
                    )
                )
                if result.outcome is not WeaponryAuditReserveOutcome.RESERVED:
                    raise AssertionError(
                        f"并发用例首次预留失败: outcome={result.outcome.value}"
                    )
                return result.reservation

            with ThreadPoolExecutor(max_workers=50) as executor:
                reservations = tuple(executor.map(execute, enumerate(task_ids)))
            self.assertEqual(50, len({item.reservation_id for item in reservations}))
            self.assertEqual(
                50,
                len({store.get(task_id).resources[0].external_ref for task_id in task_ids}),  # type: ignore[union-attr]
            )


if __name__ == "__main__":
    unittest.main()
