"""文件对话内部文档候选 DTO 的离线契约测试。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.modules.chat import (
    ChatArchitectureCandidates,
    ChatDocumentCandidate,
    ChatDocumentSelectionCandidates,
)


def _document(file_name: str) -> ChatDocumentCandidate:
    """构造不依赖数据库或网络的供应商无关文档快照。"""
    return ChatDocumentCandidate(
        file_name=file_name,
        original_name=f"{file_name}.original",
        document_ref=f"document:{file_name}",
        external_location=f"custom-documents/{file_name}.json",
    )


class ChatDocumentSelectionCandidatesTests(unittest.TestCase):
    """冻结候选互斥、不变性及未来持久化所需的严格 Schema。"""

    def test_candidate_groups_are_immutable_and_mutually_exclusive(self) -> None:
        explicit = [_document("explicit.pdf")]
        candidates = ChatDocumentSelectionCandidates(
            explicit_documents=explicit,  # type: ignore[arg-type]
        )
        explicit.append(_document("late.pdf"))

        self.assertEqual(
            ("explicit.pdf",),
            tuple(item.file_name for item in candidates.explicit_documents),
        )
        with self.assertRaises(FrozenInstanceError):
            setattr(candidates, "explicit_documents", ())
        with self.assertRaisesRegex(ValueError, "cannot both be non-empty"):
            ChatDocumentSelectionCandidates(
                explicit_documents=(_document("explicit.pdf"),),
                new_session_default_documents=(_document("default.pdf"),),
            )

    def test_payload_round_trip_uses_only_primitive_schema_v3_values(self) -> None:
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document("alpha.pdf"),
                _document("beta.pdf"),
            )
        )

        payload = candidates.to_payload()
        restored = ChatDocumentSelectionCandidates.from_payload(payload)

        self.assertEqual(3, payload["schema_version"])
        self.assertIsInstance(payload["new_session_default_documents"], list)
        self.assertEqual(candidates, restored)
        payload["new_session_default_documents"][0]["file_name"] = "changed.pdf"
        self.assertEqual(
            "alpha.pdf",
            candidates.new_session_default_documents[0].file_name,
        )

    def test_effective_documents_uses_only_transactional_session_fact(
        self,
    ) -> None:
        default = (_document("default.pdf"),)
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=default
        )

        self.assertEqual(
            default,
            candidates.effective_documents(session_created=True),
        )
        self.assertEqual(
            (),
            candidates.effective_documents(session_created=False),
        )

    def test_payload_rejects_unknown_version_fields_and_partial_documents(
        self,
    ) -> None:
        valid = ChatDocumentSelectionCandidates(
            explicit_documents=(_document("alpha.pdf"),)
        ).to_payload()

        invalid_version = dict(valid)
        invalid_version["schema_version"] = 4
        with self.assertRaisesRegex(ValueError, "schema_version"):
            ChatDocumentSelectionCandidates.from_payload(invalid_version)

        unknown_field = dict(valid)
        unknown_field["selection_mode"] = "explicit"
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            ChatDocumentSelectionCandidates.from_payload(unknown_field)

        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            ChatDocumentSelectionCandidates.from_payload({})

        partial_document = dict(valid)
        partial_document["explicit_documents"] = [{"file_name": "alpha.pdf"}]
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            ChatDocumentSelectionCandidates.from_payload(partial_document)

    def test_schema_v1_payload_remains_readable_during_internal_cutover(self) -> None:
        payload = ChatDocumentSelectionCandidates(
            explicit_documents=(_document("alpha.pdf"),)
        ).to_payload()
        payload["schema_version"] = 1
        payload.pop("architecture_candidates")

        restored = ChatDocumentSelectionCandidates.from_payload(payload)

        self.assertEqual(("alpha.pdf",), tuple(
            item.file_name for item in restored.explicit_documents
        ))
        self.assertIsNone(restored.architecture_candidates)

    def test_architecture_candidates_are_strict_and_deep_frozen(self) -> None:
        documents = [_document("alpha.pdf")]
        architecture = ChatArchitectureCandidates(
            architecture_id=7,
            resolution_outcome="resolved",
            documents=documents,  # type: ignore[arg-type]
        )
        candidates = ChatDocumentSelectionCandidates(
            architecture_candidates=architecture
        )
        documents.append(_document("late.pdf"))

        restored = ChatDocumentSelectionCandidates.from_payload(
            candidates.to_payload()
        )
        self.assertEqual(candidates, restored)
        self.assertEqual(
            ("alpha.pdf",),
            tuple(item.file_name for item in architecture.documents),
        )
        self.assertEqual(
            architecture.documents,
            candidates.effective_documents(session_created=True),
        )
        self.assertEqual((), candidates.effective_documents(session_created=False))
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            ChatDocumentSelectionCandidates(
                explicit_documents=(_document("explicit.pdf"),),
                architecture_candidates=architecture,
            )

    def test_architecture_outcomes_reject_partial_or_ambiguous_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            ChatArchitectureCandidates(
                architecture_id=7,
                resolution_outcome="resolved",
            )
        with self.assertRaisesRegex(ValueError, "cannot contain documents"):
            ChatArchitectureCandidates(
                architecture_id=7,
                resolution_outcome="not_found",
                documents=(_document("partial.pdf"),),
                error_code="architecture_catalog_not_found",
            )
        with self.assertRaisesRegex(ValueError, "error_code"):
            ChatArchitectureCandidates(
                architecture_id=7,
                resolution_outcome="invalid",
                error_code="wrong",
            )


if __name__ == "__main__":
    unittest.main()
