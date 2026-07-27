"""文件对话内部文档候选 DTO 的离线契约测试。"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from app.services.chat import (
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

    def test_payload_round_trip_uses_only_primitive_schema_v1_values(self) -> None:
        candidates = ChatDocumentSelectionCandidates(
            new_session_default_documents=(
                _document("alpha.pdf"),
                _document("beta.pdf"),
            )
        )

        payload = candidates.to_payload()
        restored = ChatDocumentSelectionCandidates.from_payload(payload)

        self.assertEqual(1, payload["schema_version"])
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
        invalid_version["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "schema_version"):
            ChatDocumentSelectionCandidates.from_payload(invalid_version)

        unknown_field = dict(valid)
        unknown_field["selection_mode"] = "explicit"
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            ChatDocumentSelectionCandidates.from_payload(unknown_field)

        partial_document = dict(valid)
        partial_document["explicit_documents"] = [{"file_name": "alpha.pdf"}]
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            ChatDocumentSelectionCandidates.from_payload(partial_document)


if __name__ == "__main__":
    unittest.main()
