from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

from app.services.core.architecture_tree import (
    MAX_SIGNED_INT64,
    ArchitectureTreeIndexCache,
    ArchitectureTreeValidationError,
    architecture_tree_fingerprint,
    build_architecture_tree_index,
)


def _tree_with_label(label: str) -> list[dict[str, object]]:
    return [
        {
            "id": 1,
            "name": label,
            "parentId": 0,
            "path": "1",
            "pathName": label,
            "remark": f"{label}备注",
        }
    ]


class ArchitectureTreeNormalizationTests(unittest.TestCase):
    def test_accepts_signed_int64_ids_and_numeric_strings(self):
        index = build_architecture_tree_index(
            [
                {"id": "1", "name": "根", "parentId": "0"},
                {
                    "id": str(MAX_SIGNED_INT64),
                    "name": "叶子",
                    "parentId": "1",
                },
            ]
        )

        self.assertEqual(index.root_ids, (1,))
        self.assertEqual(index.leaf_ids, (MAX_SIGNED_INT64,))
        self.assertEqual(index.require(MAX_SIGNED_INT64).parent_id, 1)

    def test_rejects_non_positive_non_integer_boolean_unicode_and_overflow_ids(self):
        invalid_ids = (
            0,
            -1,
            True,
            1.0,
            "1.0",
            "²",
            "１２３",
            MAX_SIGNED_INT64 + 1,
        )
        for invalid_id in invalid_ids:
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaises(ArchitectureTreeValidationError):
                    build_architecture_tree_index(
                        [{"id": invalid_id, "name": "非法节点"}]
                    )

    def test_rejects_invalid_parent_ids(self):
        invalid_parent_ids = (-1, False, 1.0, "²", "１２３", MAX_SIGNED_INT64 + 1)
        for parent_id in invalid_parent_ids:
            with self.subTest(parent_id=parent_id):
                with self.assertRaises(ArchitectureTreeValidationError):
                    build_architecture_tree_index(
                        [{"id": 1, "name": "节点", "parentId": parent_id}]
                    )

    def test_rejects_duplicate_ids_after_normalization(self):
        with self.assertRaisesRegex(
            ArchitectureTreeValidationError,
            "重复 id: 1",
        ):
            build_architecture_tree_index(
                [
                    {"id": 1, "name": "节点一"},
                    {"id": "01", "name": "节点二"},
                ]
            )

    def test_normalizes_zero_none_missing_and_blank_parent_as_roots(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "零父节点", "parentId": 0},
                {"id": 2, "name": "空父节点", "parentId": None},
                {"id": 3, "name": "缺失父节点"},
                {"id": 4, "name": "空字符串父节点", "parentId": "  "},
            ]
        )

        self.assertEqual(index.root_ids, (1, 2, 3, 4))
        self.assertTrue(all(node.parent_id is None for node in index.nodes))
        self.assertEqual(index.leaf_ids, (1, 2, 3, 4))

    def test_orphan_is_a_legal_finite_tree_boundary(self):
        index = build_architecture_tree_index(
            [
                {
                    "id": 20,
                    "name": "有限树节点",
                    "parentId": 999,
                    "path": "1/999/20",
                },
                {"id": 21, "name": "可见子节点", "parentId": 20},
            ]
        )

        orphan = index.require(20)
        self.assertEqual(orphan.parent_id, 999)
        self.assertEqual(orphan.root_id, 20)
        self.assertEqual(orphan.depth, 1)
        self.assertEqual(orphan.source_path, "1/999/20")
        self.assertEqual(orphan.semantic_path, "有限树节点")
        self.assertEqual(index.root_ids, (20,))
        self.assertEqual(index.ancestors_by_id[21], (20,))
        self.assertEqual(index.leaf_ids, (21,))

    def test_rejects_self_and_multi_node_parent_cycles(self):
        cycle_cases = (
            [{"id": 1, "name": "自环", "parentId": 1}],
            [
                {"id": 1, "name": "节点一", "parentId": 3},
                {"id": 2, "name": "节点二", "parentId": 1},
                {"id": 3, "name": "节点三", "parentId": 2},
            ],
        )
        for nodes in cycle_cases:
            with self.subTest(nodes=nodes):
                with self.assertRaisesRegex(
                    ArchitectureTreeValidationError,
                    "父链存在环",
                ):
                    build_architecture_tree_index(nodes)


class ArchitectureTreeTopologyTests(unittest.TestCase):
    def test_computes_relative_leaves_ancestors_descendants_and_siblings(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "根"},
                {"id": 2, "name": "分支甲", "parentId": 1},
                {"id": 3, "name": "分支乙", "parentId": 1},
                {"id": 4, "name": "叶子甲", "parentId": 2},
                {"id": 5, "name": "叶子乙", "parentId": 2},
            ]
        )

        self.assertEqual(index.children_by_id[1], (2, 3))
        self.assertEqual(index.children_by_id[2], (4, 5))
        self.assertEqual(index.leaf_ids, (3, 4, 5))
        self.assertEqual(index.ancestors_by_id[5], (1, 2))
        self.assertEqual(index.leaf_descendants_by_id[1], (4, 5, 3))
        self.assertEqual(index.leaf_descendants_by_id[2], (4, 5))
        self.assertEqual(index.siblings_by_id[2], (3,))
        self.assertEqual(index.siblings_by_id[4], (5,))
        self.assertEqual(index.require(5).depth, 3)

    def test_missing_path_name_is_rebuilt_and_detail_prefix_is_compressed(self):
        index = build_architecture_tree_index(
            [
                {
                    "id": 1,
                    "name": "装备目标",
                    "path": "1",
                    "pathName": "权威/装备目标",
                },
                {"id": 2, "name": "CVN-78", "parentId": 1},
                {"id": 3, "name": "CVN-78-战技指标", "parentId": 2},
            ]
        )

        self.assertEqual(index.require(2).semantic_path, "权威/装备目标/CVN-78")
        self.assertEqual(
            index.require(3).semantic_path,
            "权威/装备目标/CVN-78/战技指标",
        )
        self.assertEqual(index.require(3).source_path, "1/2/3")

    def test_non_empty_path_name_is_opaque_and_never_used_as_topology(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "真实根", "pathName": "另一个/展示/路径"},
                {
                    "id": 2,
                    "name": "F/A-18E/F",
                    "parentId": 1,
                    "pathName": "装备目标/空中装备/F/A-18E/F",
                },
            ]
        )

        child = index.require(2)
        self.assertEqual(child.semantic_path, "装备目标/空中装备/F/A-18E/F")
        self.assertEqual(child.path_name, child.semantic_path)
        self.assertEqual(child.root_id, 1)
        self.assertEqual(child.depth, 2)
        self.assertEqual(index.ancestors_by_id[2], (1,))

    def test_profiles_and_index_mappings_are_immutable(self):
        index = build_architecture_tree_index(_tree_with_label("不可变"))

        with self.assertRaises(FrozenInstanceError):
            index.nodes[0].name = "被篡改"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            index.nodes_by_id[2] = index.nodes[0]  # type: ignore[index]
        with self.assertRaises(TypeError):
            index.children_by_id[1] = (2,)  # type: ignore[index]

    def test_alias_index_keeps_collisions_in_request_order(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "CVN-78"},
                {"id": 2, "name": "CVN 78"},
            ]
        )

        self.assertEqual(index.alias_to_ids["cvn78"], (1, 2))


class ArchitectureTreeFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_for_equivalent_root_and_numeric_forms(self):
        first = [
            {
                "id": "001",
                "name": " 根 ",
                "parentId": "0",
                "path": " 1 ",
                "pathName": " 根 ",
                "remark": " 完整备注 ",
            }
        ]
        second = [
            {
                "id": 1,
                "name": "根",
                "parentId": None,
                "path": "1",
                "pathName": "根",
                "remark": "完整备注",
            }
        ]

        self.assertEqual(
            architecture_tree_fingerprint(first),
            architecture_tree_fingerprint(second),
        )

    def test_fingerprint_is_sensitive_to_order_and_every_contract_field(self):
        base = [
            {
                "id": 1,
                "name": "根",
                "parentId": 0,
                "path": "1",
                "pathName": "根",
                "remark": "a" * 2_000 + "尾部甲",
            },
            {
                "id": 2,
                "name": "叶",
                "parentId": 1,
                "path": "1/2",
                "pathName": "根/叶",
                "remark": "叶备注",
            },
        ]
        base_fingerprint = architecture_tree_fingerprint(base)

        variants: list[list[dict[str, object]]] = []
        variants.append([dict(base[1]), dict(base[0])])
        for field, value in (
            ("id", 3),
            ("parentId", 999),
            ("name", "新叶"),
            ("pathName", "根/新叶"),
            ("remark", "叶备注变更"),
        ):
            variant = [dict(item) for item in base]
            variant[1][field] = value
            variants.append(variant)
        long_remark_variant = [dict(item) for item in base]
        long_remark_variant[0]["remark"] = "a" * 2_000 + "尾部乙"
        variants.append(long_remark_variant)

        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    architecture_tree_fingerprint(variant),
                    base_fingerprint,
                )

    def test_fingerprint_explicitly_ignores_source_path(self):
        first = [
            {
                "id": 1,
                "name": "根",
                "parentId": 0,
                "path": "1",
                "pathName": "根",
                "remark": "备注",
            }
        ]
        second = [
            {
                "id": 1,
                "name": "根",
                "parentId": None,
                "path": "999/1",
                "pathName": "根",
                "remark": "备注",
            }
        ]

        self.assertEqual(
            architecture_tree_fingerprint(first),
            architecture_tree_fingerprint(second),
        )

    def test_index_fingerprint_matches_standalone_function(self):
        nodes = _tree_with_label("一致")

        self.assertEqual(
            build_architecture_tree_index(nodes).fingerprint,
            architecture_tree_fingerprint(nodes),
        )


class ArchitectureTreeIndexCacheTests(unittest.TestCase):
    def test_cache_returns_same_index_instance_and_uses_lru_eviction(self):
        calls: list[str] = []

        def builder(nodes):
            index = build_architecture_tree_index(nodes)
            calls.append(index.fingerprint)
            return index

        cache = ArchitectureTreeIndexCache(capacity=2, builder=builder)
        tree_a = _tree_with_label("A")
        tree_b = _tree_with_label("B")
        tree_c = _tree_with_label("C")

        first_a = cache.get_or_build(tree_a)
        self.assertIs(cache.get_or_build(tree_a), first_a)
        cache.get_or_build(tree_b)
        cache.get_or_build(tree_a)  # A 成为最近使用项。
        cache.get_or_build(tree_c)

        self.assertEqual(
            cache.cached_fingerprints,
            (
                architecture_tree_fingerprint(tree_a),
                architecture_tree_fingerprint(tree_c),
            ),
        )
        cache.get_or_build(tree_b)
        self.assertEqual(calls.count(architecture_tree_fingerprint(tree_b)), 2)
        self.assertEqual(len(cache), 2)

    def test_same_fingerprint_has_only_one_concurrent_cold_build(self):
        build_started = threading.Event()
        allow_build_to_finish = threading.Event()
        call_lock = threading.Lock()
        build_calls = 0

        def builder(nodes):
            nonlocal build_calls
            with call_lock:
                build_calls += 1
            build_started.set()
            self.assertTrue(allow_build_to_finish.wait(timeout=5))
            return build_architecture_tree_index(nodes)

        cache = ArchitectureTreeIndexCache(builder=builder)
        tree = _tree_with_label("并发")
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(cache.get_or_build, tree) for _ in range(8)]
            self.assertTrue(build_started.wait(timeout=5))
            time.sleep(0.05)
            allow_build_to_finish.set()
            indexes = [future.result(timeout=5) for future in futures]

        self.assertEqual(build_calls, 1)
        self.assertTrue(all(index is indexes[0] for index in indexes))

    def test_failed_build_is_not_cached(self):
        attempts = 0

        def builder(nodes):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ArchitectureTreeValidationError("模拟冷构建失败")
            return build_architecture_tree_index(nodes)

        cache = ArchitectureTreeIndexCache(builder=builder)
        tree = _tree_with_label("重试")
        with self.assertRaisesRegex(
            ArchitectureTreeValidationError,
            "模拟冷构建失败",
        ):
            cache.get_or_build(tree)

        index = cache.get_or_build(tree)
        self.assertEqual(index.fingerprint, architecture_tree_fingerprint(tree))
        self.assertEqual(attempts, 2)
        self.assertEqual(len(cache), 1)

    def test_rejects_invalid_capacity(self):
        for capacity in (0, -1, True, 1.5):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    ArchitectureTreeIndexCache(capacity=capacity)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
