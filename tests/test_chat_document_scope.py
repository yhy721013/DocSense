"""文件对话活动范围纯领域模型与状态机测试。"""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from app.modules.chat.domain.document_candidates import ChatDocumentCandidate
from app.modules.chat.domain.document_candidates import ChatArchitectureCandidates
from app.modules.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_ARCHITECTURE,
    CHAT_SCOPE_MODE_FILES,
    CHAT_SCOPE_SELECTION_ACTIVE_REUSE,
    CHAT_SCOPE_SELECTION_ARCHITECTURE_INITIAL,
    CHAT_SCOPE_SELECTION_ARCHITECTURE_REUSE,
    CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL,
    CHAT_SCOPE_SELECTION_EXPLICIT,
    CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
    CHAT_SCOPE_SOURCE_EXPLICIT,
    ChatArchitectureIdConflictError,
    ChatArchitectureScopeInvalidError,
    ChatArchitectureScopeNotFoundError,
    ChatScopeModeConflictError,
    ChatScopeSelector,
    ChatScopeDecision,
    ChatScopeHead,
    ChatScopeRevision,
    ChatSessionScopeBinding,
    decide_chat_architecture_scope,
    decide_chat_document_scope,
    decide_chat_session_scope_binding,
)


_CONTRACT_PATH = (
    Path(__file__).resolve().parent
    / "contracts"
    / "chat_scope_state_machine.json"
)


def _document(file_name: str) -> ChatDocumentCandidate:
    return ChatDocumentCandidate(
        file_name=file_name,
        original_name=f"{file_name}.original",
        document_ref=f"document:{file_name}",
        external_location=f"custom-documents/{file_name}.json",
    )


def _documents(file_names: list[str] | tuple[str, ...]):
    return tuple(_document(file_name) for file_name in file_names)


class ChatDocumentScopeDomainTests(unittest.TestCase):
    """验证状态机只依赖显式事务事实，且全部结果不可变。"""

    def test_state_machine_matches_stage_zero_contract_asset(self) -> None:
        contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

        for case in contract["cases"]:
            with self.subTest(case_id=case["id"]):
                current_scope = case["currentScope"]
                requested_files = case["requestedFiles"]
                decision = decide_chat_document_scope(
                    session_created=not case["sessionExists"],
                    requested_documents=_documents(requested_files),
                    # 显式请求路径不会读取全量目录；资产中的 initialCatalog
                    # 只是环境背景，不能伪装成同时存在的自动候选。
                    automatic_initial_documents=(
                        ()
                        if requested_files
                        else _documents(case["initialCatalog"])
                    ),
                    current_scope_documents=(
                        None
                        if current_scope is None
                        else _documents(current_scope)
                    ),
                )

                self.assertEqual(
                    case["expectedEffectiveFiles"],
                    [
                        item.file_name
                        for item in decision.effective_documents
                    ],
                )
                self.assertEqual(
                    case["expectedHistoryFiles"],
                    [
                        item.file_name
                        for item in decision.requested_files
                    ],
                )
                self.assertEqual(
                    case["expectedSelectionMode"],
                    decision.selection_mode,
                )
                self.assertEqual(
                    case["createsScopeRevision"],
                    decision.creates_scope_revision,
                )

    def test_explicit_selection_wholly_replaces_current_scope(self) -> None:
        decision = decide_chat_document_scope(
            session_created=False,
            requested_documents=_documents(["c.pdf"]),
            automatic_initial_documents=(),
            current_scope_documents=_documents(["a.pdf", "b.pdf"]),
        )

        self.assertEqual(CHAT_SCOPE_SELECTION_EXPLICIT, decision.selection_mode)
        self.assertEqual(CHAT_SCOPE_SOURCE_EXPLICIT, decision.scope_source_mode)
        self.assertEqual(
            ("c.pdf",),
            tuple(item.file_name for item in decision.effective_documents),
        )

    def test_empty_existing_session_requires_scope_head(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing active scope"):
            decide_chat_document_scope(
                session_created=False,
                requested_documents=(),
                automatic_initial_documents=(),
                current_scope_documents=None,
            )

    def test_empty_initial_scope_remains_a_real_revision(self) -> None:
        decision = decide_chat_document_scope(
            session_created=True,
            requested_documents=(),
            automatic_initial_documents=(),
            current_scope_documents=None,
        )

        self.assertEqual(
            CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL,
            decision.selection_mode,
        )
        self.assertEqual(
            CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
            decision.scope_source_mode,
        )
        self.assertTrue(decision.creates_scope_revision)
        self.assertEqual((), decision.effective_documents)

    def test_existing_empty_ignores_race_prepared_initial_candidates(self) -> None:
        decision = decide_chat_document_scope(
            session_created=False,
            requested_documents=(),
            automatic_initial_documents=_documents(["new.pdf"]),
            current_scope_documents=_documents(["stable.pdf"]),
        )

        self.assertEqual(
            CHAT_SCOPE_SELECTION_ACTIVE_REUSE,
            decision.selection_mode,
        )
        self.assertEqual(
            ("stable.pdf",),
            tuple(item.file_name for item in decision.effective_documents),
        )

    def test_duplicate_business_or_remote_identity_is_rejected(self) -> None:
        duplicate_file_name = (
            _document("a.pdf"),
            ChatDocumentCandidate(
                file_name="a.pdf",
                original_name="other.pdf",
                document_ref="document:other",
                external_location="custom-documents/other.json",
            ),
        )
        with self.assertRaisesRegex(ValueError, "duplicate file_name"):
            decide_chat_document_scope(
                session_created=True,
                requested_documents=(),
                automatic_initial_documents=duplicate_file_name,
                current_scope_documents=None,
            )

        duplicate_ref = (
            _document("a.pdf"),
            ChatDocumentCandidate(
                file_name="b.pdf",
                original_name="b.pdf",
                document_ref="document:a.pdf",
                external_location="custom-documents/b.pdf.json",
            ),
        )
        with self.assertRaisesRegex(ValueError, "duplicate document_ref"):
            decide_chat_document_scope(
                session_created=True,
                requested_documents=(),
                automatic_initial_documents=duplicate_ref,
                current_scope_documents=None,
            )

    def test_decision_schema_round_trip_and_unknown_data_fail_closed(self) -> None:
        decision = decide_chat_document_scope(
            session_created=False,
            requested_documents=_documents(["a.pdf"]),
            automatic_initial_documents=(),
            current_scope_documents=_documents(["old.pdf"]),
        )

        payload = decision.to_payload()
        restored = ChatScopeDecision.from_payload(payload)
        self.assertEqual(decision, restored)
        payload["requested_documents"][0]["file_name"] = "changed.pdf"
        self.assertEqual(
            "a.pdf",
            decision.requested_documents[0].file_name,
        )

        invalid_version = decision.to_payload()
        invalid_version["schema_version"] = 3
        with self.assertRaisesRegex(ValueError, "schema_version"):
            ChatScopeDecision.from_payload(invalid_version)

        unknown_field = decision.to_payload()
        unknown_field["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            ChatScopeDecision.from_payload(unknown_field)

    def test_revision_head_and_decision_are_frozen(self) -> None:
        revision = ChatScopeRevision(
            scope_revision_id="scope-1",
            conversation_id="chat-1",
            source_mode=CHAT_SCOPE_SOURCE_EXPLICIT,
            source_run_id="run-1",
            members=_documents(["a.pdf"]),
            created_at="2026-07-28T00:00:00+00:00",
        )
        head = ChatScopeHead(
            conversation_id="chat-1",
            scope_revision_id="scope-1",
            updated_at="2026-07-28T00:00:00+00:00",
        )

        with self.assertRaises(FrozenInstanceError):
            setattr(revision, "members", ())
        with self.assertRaises(FrozenInstanceError):
            setattr(head, "scope_revision_id", "scope-2")

    def test_selector_and_session_binding_are_immutable(self) -> None:
        selector = ChatScopeSelector.for_architecture(7)
        binding, created = decide_chat_session_scope_binding(
            conversation_id="chat-1",
            selector=selector,
            existing_binding=None,
            created_at="2026-07-28T00:00:00+00:00",
        )

        self.assertTrue(created)
        self.assertEqual(CHAT_SCOPE_MODE_ARCHITECTURE, binding.scope_mode)
        self.assertEqual(7, binding.architecture_id)
        reused, created = decide_chat_session_scope_binding(
            conversation_id="chat-1",
            selector=ChatScopeSelector.for_architecture(7),
            existing_binding=binding,
            created_at="ignored",
        )
        self.assertIs(binding, reused)
        self.assertFalse(created)
        with self.assertRaises(ChatArchitectureIdConflictError):
            decide_chat_session_scope_binding(
                conversation_id="chat-1",
                selector=ChatScopeSelector.for_architecture(8),
                existing_binding=binding,
                created_at="ignored",
            )
        with self.assertRaises(ChatScopeModeConflictError):
            decide_chat_session_scope_binding(
                conversation_id="chat-1",
                selector=ChatScopeSelector.for_files([]),
                existing_binding=binding,
                created_at="ignored",
            )

        files_binding = ChatSessionScopeBinding(
            conversation_id="chat-2",
            scope_mode=CHAT_SCOPE_MODE_FILES,
            architecture_id=None,
            created_at="2026-07-28T00:00:00+00:00",
        )
        self.assertIsNone(files_binding.architecture_id)

    def test_architecture_scope_initial_and_reuse_ignore_late_catalog_outcome(self) -> None:
        resolved = ChatArchitectureCandidates(
            architecture_id=7,
            resolution_outcome="resolved",
            documents=_documents(["a.pdf", "b.pdf"]),
        )
        initial = decide_chat_architecture_scope(
            session_created=True,
            requested_architecture_id=7,
            bound_architecture_id=7,
            architecture_candidates=resolved,
            current_scope_documents=None,
        )
        self.assertEqual(
            CHAT_SCOPE_SELECTION_ARCHITECTURE_INITIAL,
            initial.selection_mode,
        )
        self.assertEqual(7, initial.source_architecture_id)
        self.assertEqual(2, len(initial.effective_documents))

        now_empty = ChatArchitectureCandidates(
            architecture_id=7,
            resolution_outcome="not_found",
            error_code="architecture_catalog_not_found",
        )
        reused = decide_chat_architecture_scope(
            session_created=False,
            requested_architecture_id=7,
            bound_architecture_id=7,
            architecture_candidates=now_empty,
            current_scope_documents=initial.effective_documents,
        )
        self.assertEqual(
            CHAT_SCOPE_SELECTION_ARCHITECTURE_REUSE,
            reused.selection_mode,
        )
        self.assertEqual(initial.effective_documents, reused.effective_documents)
        self.assertFalse(reused.creates_scope_revision)

    def test_new_architecture_scope_defers_typed_candidate_failures(self) -> None:
        with self.assertRaises(ChatArchitectureScopeNotFoundError):
            decide_chat_architecture_scope(
                session_created=True,
                requested_architecture_id=7,
                bound_architecture_id=7,
                architecture_candidates=ChatArchitectureCandidates(
                    architecture_id=7,
                    resolution_outcome="not_found",
                    error_code="architecture_catalog_not_found",
                ),
                current_scope_documents=None,
            )
        with self.assertRaises(ChatArchitectureScopeInvalidError):
            decide_chat_architecture_scope(
                session_created=True,
                requested_architecture_id=7,
                bound_architecture_id=7,
                architecture_candidates=ChatArchitectureCandidates(
                    architecture_id=7,
                    resolution_outcome="invalid",
                    error_code="architecture_catalog_invalid",
                ),
                current_scope_documents=None,
            )


if __name__ == "__main__":
    unittest.main()
