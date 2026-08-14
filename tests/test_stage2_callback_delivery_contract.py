"""阶段 2-4 Callback Delivery 机器契约与分层边界门禁。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = PROJECT_ROOT / "tests/contracts/stage2_callback_delivery_contract.json"


class Stage2CallbackDeliveryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_public_contract_and_unknown_policy_are_frozen(self) -> None:
        self.assertFalse(self.contract["publicContractChanged"])
        self.assertFalse(self.contract["states"]["expiredSendingMayAutoReacquire"])
        self.assertTrue(self.contract["recovery"]["sharesSameGuardWithInitialDelivery"])
        self.assertTrue(self.contract["recovery"]["backgroundUnknownRetryForbidden"])

    def test_terminal_and_delivery_atomic_groups_are_complete(self) -> None:
        groups = self.contract["atomicGroups"]
        self.assertIn("callback_eligibility_guard", groups["businessTerminal"])
        self.assertEqual(
            {
                "latest_owner_validation",
                "guard_lease",
                "execution_callback_sending",
                "latest_callback_sending",
                "callback_attempt_increment",
                "authorized_event",
            },
            set(groups["acquire"]),
        )
        self.assertEqual(
            {"guard_outcome", "execution_callback_outcome", "latest_callback_outcome", "completed_event"},
            set(groups["complete"]),
        )

    def test_admission_uses_guard_not_historical_projection(self) -> None:
        admission = self.contract["admission"]
        self.assertEqual("callback_delivery_guards", admission["conflictAuthority"])
        self.assertTrue(admission["manualReleasePreservesOldUnknownProjection"])
        self.assertTrue(admission["manualReleaseAllowsNewAdmission"])

    def test_control_store_has_no_network_or_history_writer_import(self) -> None:
        path = PROJECT_ROOT / "app/modules/tasks/adapters/sqlite/callback_control_store.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
        self.assertTrue(
            {"requests", "httpx", "urllib", "aiohttp", "socket"}.isdisjoint(imported_roots)
        )
        source = path.read_text(encoding="utf-8")
        self.assertNotIn("save_callback_history_payload", source)


if __name__ == "__main__":
    unittest.main()
