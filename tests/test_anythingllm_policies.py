"""AnythingLLM 共享执行策略的纯内存契约测试。"""

from __future__ import annotations

import unittest

from app.integrations.anythingllm.policies import (
    DEFAULT_EMBEDDING_ATTEMPTS,
    DEFAULT_UPLOAD_RETRIES,
    MAX_EMBEDDING_ATTEMPTS,
    MAX_UPLOAD_RETRIES,
    validate_embedding_max_attempts,
    validate_upload_max_retries,
    validate_upload_retry_base_delay,
)


class AnythingLLMPolicyTests(unittest.TestCase):
    """验证默认值、硬上限以及 Python 特殊数值类型的边界处理。"""

    def test_defaults_do_not_exceed_hard_limits(self) -> None:
        """生产默认策略必须始终位于对应的资源保护硬上限之内。"""
        self.assertLessEqual(DEFAULT_UPLOAD_RETRIES, MAX_UPLOAD_RETRIES)
        self.assertLessEqual(
            DEFAULT_EMBEDDING_ATTEMPTS,
            MAX_EMBEDDING_ATTEMPTS,
        )

    def test_upload_retries_accept_only_bounded_integer(self) -> None:
        """上传额外重试次数不得接受布尔值、浮点数或越界整数。"""
        self.assertEqual(0, validate_upload_max_retries(0))
        self.assertEqual(
            MAX_UPLOAD_RETRIES,
            validate_upload_max_retries(MAX_UPLOAD_RETRIES),
        )
        for invalid_value in (True, 1.0, -1, MAX_UPLOAD_RETRIES + 1):
            with self.subTest(value=invalid_value):
                with self.assertRaises(ValueError):
                    validate_upload_max_retries(invalid_value)  # type: ignore[arg-type]

    def test_embedding_attempts_accept_only_positive_bounded_integer(self) -> None:
        """Embedding 总调用次数必须是硬上限内的正整数。"""
        self.assertEqual(1, validate_embedding_max_attempts(1))
        self.assertEqual(
            MAX_EMBEDDING_ATTEMPTS,
            validate_embedding_max_attempts(MAX_EMBEDDING_ATTEMPTS),
        )
        for invalid_value in (False, 1.5, 0, MAX_EMBEDDING_ATTEMPTS + 1):
            with self.subTest(value=invalid_value):
                with self.assertRaises(ValueError):
                    validate_embedding_max_attempts(  # type: ignore[arg-type]
                        invalid_value
                    )

    def test_retry_delay_rejects_boolean_and_negative_values(self) -> None:
        """退避秒数允许整数或浮点数，但不得把布尔值解释为秒数。"""
        self.assertEqual(0.0, validate_upload_retry_base_delay(0))
        self.assertEqual(1.5, validate_upload_retry_base_delay(1.5))
        for invalid_value in (True, -0.1, "1"):
            with self.subTest(value=invalid_value):
                with self.assertRaises(ValueError):
                    validate_upload_retry_base_delay(  # type: ignore[arg-type]
                        invalid_value
                    )


if __name__ == "__main__":
    unittest.main()
