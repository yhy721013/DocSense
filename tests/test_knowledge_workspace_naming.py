"""永久知识谱系 Workspace 共享命名规则测试。"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from app.shared.domain import (
    ARCHITECTURE_ID_MAX,
    ARCHITECTURE_ID_MIN,
    permanent_architecture_workspace_name,
)


class PermanentKnowledgeWorkspaceNamingTests(unittest.TestCase):
    """锁定精确前缀、整数边界和失败关闭行为。"""

    def test_positive_architecture_id_uses_exact_new_prefix(self) -> None:
        self.assertEqual("archId-10605", permanent_architecture_workspace_name(10605))

    def test_rule_is_owned_by_shared_domain_after_stage2_path_migration(self) -> None:
        self.assertEqual(
            permanent_architecture_workspace_name.__module__,
            "app.shared.domain.knowledge_workspace",
        )

    def test_signed_storage_projection_values_have_deterministic_names(self) -> None:
        self.assertEqual("archId-0", permanent_architecture_workspace_name(0))
        self.assertEqual("archId--12", permanent_architecture_workspace_name(-12))

    def test_signed_64_bit_boundaries_are_supported(self) -> None:
        self.assertEqual(
            f"archId-{ARCHITECTURE_ID_MIN}",
            permanent_architecture_workspace_name(ARCHITECTURE_ID_MIN),
        )
        self.assertEqual(
            f"archId-{ARCHITECTURE_ID_MAX}",
            permanent_architecture_workspace_name(ARCHITECTURE_ID_MAX),
        )

    def test_bool_and_non_integer_values_are_rejected(self) -> None:
        for value in (True, False, "12", 12.0, None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    permanent_architecture_workspace_name(value)  # type: ignore[arg-type]

    def test_out_of_range_values_are_rejected(self) -> None:
        for value in (ARCHITECTURE_ID_MIN - 1, ARCHITECTURE_ID_MAX + 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    permanent_architecture_workspace_name(value)

    def test_concurrent_distinct_architecture_ids_keep_unique_exact_names(self) -> None:
        """并发计算不得共享可变状态，50 个谱系 ID 必须得到 50 个精确名称。"""

        architecture_ids = list(range(10_000, 10_050))
        with ThreadPoolExecutor(max_workers=10) as executor:
            workspace_names = list(
                executor.map(
                    permanent_architecture_workspace_name,
                    architecture_ids,
                )
            )

        self.assertEqual(50, len(set(workspace_names)))
        self.assertEqual(
            [f"archId-{architecture_id}" for architecture_id in architecture_ids],
            workspace_names,
        )


if __name__ == "__main__":
    unittest.main()
