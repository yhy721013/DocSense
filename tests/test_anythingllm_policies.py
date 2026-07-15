"""AnythingLLM 共享执行策略的纯内存契约测试。"""

from __future__ import annotations

import unittest

from app.integrations.anythingllm.policies import (
    DEFAULT_EMBEDDING_ATTEMPTS,
    DEFAULT_UPLOAD_RETRIES,
    MAX_EMBEDDING_ATTEMPTS,
    MAX_UPLOAD_RETRIES,
    analysis_rag_workspace_settings,
    chat_workspace_settings,
    document_rag_workspace_settings,
    knowledge_index_workspace_settings,
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

    def test_analysis_and_knowledge_history_policies_are_explicitly_separated(self) -> None:
        """临时两阶段分析关闭历史，永久知识库保留原有一轮历史。"""
        analysis_settings = analysis_rag_workspace_settings()
        knowledge_settings = knowledge_index_workspace_settings()

        self.assertEqual(0, analysis_settings["openAiHistory"])
        self.assertEqual(1, knowledge_settings["openAiHistory"])
        self.assertEqual(
            {key: value for key, value in analysis_settings.items() if key != "openAiHistory"},
            {key: value for key, value in knowledge_settings.items() if key != "openAiHistory"},
        )

    def test_legacy_and_chat_workspace_policies_remain_compatible(self) -> None:
        """旧混合 Facade 与文件对话不得被临时 analysis 的历史隔离误改。"""
        self.assertEqual(1, document_rag_workspace_settings()["openAiHistory"])
        self.assertEqual(20, chat_workspace_settings()["openAiHistory"])

    def test_workspace_policy_calls_return_independent_dicts(self) -> None:
        """调用方修改一次策略副本不得污染后续任务。"""
        first = analysis_rag_workspace_settings()
        first["openAiHistory"] = 99

        self.assertEqual(0, analysis_rag_workspace_settings()["openAiHistory"])


if __name__ == "__main__":
    unittest.main()
