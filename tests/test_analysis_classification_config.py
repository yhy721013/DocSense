from __future__ import annotations

import os
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from app.modules.analysis.adapters.runtime_config import (
    ANALYSIS_CLASSIFICATION_MODE_LEGACY,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
    ANALYSIS_DATA_STANDARD_MODE_LEGACY,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
    ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW,
    AnalysisClassificationConfig,
    AnalysisClassificationConfigurationError,
    load_analysis_classification_config,
)
from app.container import create_application_services


class AnalysisClassificationConfigTests(unittest.TestCase):
    ENV_NAME = "DOCSENSE_ANALYSIS_CLASSIFICATION_MODE"
    CONSTRAINT_ENV_NAME = "DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE"
    DATA_STANDARD_ENV_NAME = "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE"
    IDENTITY_RESELECT_ENV_NAME = "DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE"

    def test_missing_environment_uses_topk_two_stage_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.ENV_NAME, None)
            os.environ.pop(self.CONSTRAINT_ENV_NAME, None)
            os.environ.pop(self.DATA_STANDARD_ENV_NAME, None)
            os.environ.pop(self.IDENTITY_RESELECT_ENV_NAME, None)
            config = load_analysis_classification_config()

        self.assertEqual(ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE, config.mode)
        self.assertEqual(
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
            config.filename_constraint_mode,
        )
        self.assertEqual(
            ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
            config.data_standard_mode,
        )
        self.assertEqual(
            ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
            config.identity_reselect_mode,
        )
        self.assertEqual(128, config.model_candidate_limit)
        self.assertEqual(32_000, config.classification_prompt_char_limit)
        self.assertEqual(64, config.base_leaf_limit)
        self.assertEqual(16, config.parent_candidate_limit)
        self.assertEqual(AnalysisClassificationConfig.topk_two_stage(), config)

    def test_all_supported_modes_are_loaded(self) -> None:
        for mode in (
            ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
            ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
            ANALYSIS_CLASSIFICATION_MODE_LEGACY,
        ):
            with self.subTest(mode=mode):
                with patch.dict(
                    os.environ,
                    {
                        self.ENV_NAME: mode,
                        self.CONSTRAINT_ENV_NAME: (
                            ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
                        ),
                    },
                    clear=False,
                ):
                    self.assertEqual(mode, load_analysis_classification_config().mode)

    def test_all_supported_filename_constraint_modes_are_loaded(self) -> None:
        for mode in (
            ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
        ):
            with self.subTest(mode=mode):
                with patch.dict(
                    os.environ,
                    {
                        self.CONSTRAINT_ENV_NAME: mode,
                        self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                    },
                    clear=False,
                ):
                    self.assertEqual(
                        mode,
                        load_analysis_classification_config().filename_constraint_mode,
                    )

    def test_supported_mode_is_normalized(self) -> None:
        with patch.dict(
            os.environ,
            {
                self.ENV_NAME: "  TOPK_TWO_STAGE  ",
                self.CONSTRAINT_ENV_NAME: "  SCOPE_GUARD  ",
                self.DATA_STANDARD_ENV_NAME: "  SCOPE_GUARD  ",
                self.IDENTITY_RESELECT_ENV_NAME: "  SHADOW  ",
            },
            clear=False,
        ):
            config = load_analysis_classification_config()

        self.assertEqual(ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE, config.mode)
        self.assertEqual(
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
            config.filename_constraint_mode,
        )
        self.assertEqual(
            ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
            config.data_standard_mode,
        )
        self.assertEqual(
            ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW,
            config.identity_reselect_mode,
        )

    def test_all_supported_data_standard_modes_are_loaded(self) -> None:
        for mode in (
            ANALYSIS_DATA_STANDARD_MODE_LEGACY,
            ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
        ):
            with self.subTest(mode=mode):
                with patch.dict(
                    os.environ,
                    {
                        self.DATA_STANDARD_ENV_NAME: mode,
                        self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                        self.CONSTRAINT_ENV_NAME: (
                            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                        ),
                    },
                    clear=False,
                ):
                    self.assertEqual(
                        mode,
                        load_analysis_classification_config().data_standard_mode,
                    )

    def test_all_supported_identity_reselect_modes_are_loaded_with_other_modes(
        self,
    ) -> None:
        combinations = (
            (
                ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
                ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
                ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
            ),
            (
                ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW,
                ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
                ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
                ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
            ),
            (
                ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
                ANALYSIS_CLASSIFICATION_MODE_LEGACY,
                ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
                ANALYSIS_DATA_STANDARD_MODE_LEGACY,
            ),
        )
        for identity_mode, mode, filename_mode, data_standard_mode in combinations:
            with self.subTest(identity_mode=identity_mode):
                with patch.dict(
                    os.environ,
                    {
                        self.ENV_NAME: mode,
                        self.CONSTRAINT_ENV_NAME: filename_mode,
                        self.DATA_STANDARD_ENV_NAME: data_standard_mode,
                        self.IDENTITY_RESELECT_ENV_NAME: identity_mode,
                    },
                    clear=False,
                ):
                    config = load_analysis_classification_config()

                self.assertEqual(identity_mode, config.identity_reselect_mode)
                self.assertEqual(mode, config.mode)
                self.assertEqual(filename_mode, config.filename_constraint_mode)
                self.assertEqual(data_standard_mode, config.data_standard_mode)

    def test_explicit_blank_and_unknown_modes_are_rejected(self) -> None:
        for raw_mode in ("", "   ", "topk", "two_stage", "none"):
            with self.subTest(raw_mode=raw_mode):
                with patch.dict(
                    os.environ,
                    {
                        self.ENV_NAME: raw_mode,
                        self.CONSTRAINT_ENV_NAME: (
                            ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
                        ),
                    },
                    clear=False,
                ):
                    with self.assertRaises(AnalysisClassificationConfigurationError):
                        load_analysis_classification_config()

    def test_explicit_blank_and_unknown_filename_constraint_modes_are_rejected(
        self,
    ) -> None:
        for raw_mode in ("", "   ", "guard", "topk_two_stage", "none"):
            with self.subTest(raw_mode=raw_mode):
                with patch.dict(
                    os.environ,
                    {
                        self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                        self.CONSTRAINT_ENV_NAME: raw_mode,
                    },
                    clear=False,
                ):
                    with self.assertRaises(AnalysisClassificationConfigurationError):
                        load_analysis_classification_config()

    def test_explicit_blank_and_unknown_data_standard_modes_are_rejected(
        self,
    ) -> None:
        for raw_mode in ("", "   ", "guard", "topk_two_stage", "none"):
            with self.subTest(raw_mode=raw_mode):
                with patch.dict(
                    os.environ,
                    {
                        self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                        self.CONSTRAINT_ENV_NAME: (
                            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                        ),
                        self.DATA_STANDARD_ENV_NAME: raw_mode,
                    },
                    clear=False,
                ):
                    with self.assertRaises(AnalysisClassificationConfigurationError):
                        load_analysis_classification_config()

    def test_explicit_blank_and_unknown_identity_reselect_modes_are_rejected(
        self,
    ) -> None:
        for raw_mode in ("", "   ", "guard", "scope_guard", "none"):
            with self.subTest(raw_mode=raw_mode):
                with patch.dict(
                    os.environ,
                    {
                        self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                        self.CONSTRAINT_ENV_NAME: (
                            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                        ),
                        self.DATA_STANDARD_ENV_NAME: (
                            ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
                        ),
                        self.IDENTITY_RESELECT_ENV_NAME: raw_mode,
                    },
                    clear=False,
                ):
                    with self.assertRaises(AnalysisClassificationConfigurationError):
                        load_analysis_classification_config()

    def test_non_string_identity_reselect_mode_is_rejected(self) -> None:
        for raw_mode in (None, 1, True, object()):
            with self.subTest(raw_mode=raw_mode):
                with self.assertRaises(AnalysisClassificationConfigurationError):
                    AnalysisClassificationConfig(
                        identity_reselect_mode=raw_mode,  # type: ignore[arg-type]
                    )

    def test_config_is_immutable(self) -> None:
        config = AnalysisClassificationConfig.topk_two_stage()

        with self.assertRaises(FrozenInstanceError):
            config.mode = ANALYSIS_CLASSIFICATION_MODE_LEGACY  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            config.filename_constraint_mode = (  # type: ignore[misc]
                ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
            )
        with self.assertRaises(FrozenInstanceError):
            config.data_standard_mode = (  # type: ignore[misc]
                ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
            )
        with self.assertRaises(FrozenInstanceError):
            config.identity_reselect_mode = (  # type: ignore[misc]
                ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW
            )

    def test_limits_must_be_positive_integers_and_reject_bool(self) -> None:
        fields = (
            "model_candidate_limit",
            "classification_prompt_char_limit",
            "base_leaf_limit",
            "parent_candidate_limit",
        )
        for field_name in fields:
            for invalid_value in (True, False, 0, -1, 1.5, "1"):
                with self.subTest(field_name=field_name, value=invalid_value):
                    kwargs = {field_name: invalid_value}
                    with self.assertRaises(AnalysisClassificationConfigurationError):
                        AnalysisClassificationConfig(**kwargs)  # type: ignore[arg-type]

    def test_limits_accept_smaller_positive_values_within_hard_caps(self) -> None:
        config = AnalysisClassificationConfig(
            model_candidate_limit=10,
            classification_prompt_char_limit=2_000,
            base_leaf_limit=6,
            parent_candidate_limit=4,
        )

        self.assertEqual((10, 2_000, 6, 4), (
            config.model_candidate_limit,
            config.classification_prompt_char_limit,
            config.base_leaf_limit,
            config.parent_candidate_limit,
        ))

    def test_limits_cannot_exceed_contract_caps_or_candidate_total(self) -> None:
        for field_name, invalid_value in (
            ("model_candidate_limit", 129),
            ("classification_prompt_char_limit", 32_001),
            ("base_leaf_limit", 65),
            ("parent_candidate_limit", 17),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(AnalysisClassificationConfigurationError):
                    AnalysisClassificationConfig(**{field_name: invalid_value})

        with self.assertRaisesRegex(
            AnalysisClassificationConfigurationError,
            "之和",
        ):
            AnalysisClassificationConfig(
                model_candidate_limit=10,
                base_leaf_limit=8,
                parent_candidate_limit=3,
            )

    def test_invalid_mode_fails_during_container_startup_before_external_config(self) -> None:
        """运行模式误配必须在 AnythingLLM 配置和数据库初始化前阻断启动。"""
        with patch.dict(
            os.environ,
            {
                self.ENV_NAME: "unsupported",
                self.CONSTRAINT_ENV_NAME: ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
            },
            clear=False,
        ):
            with patch(
                "app.container.load_anythingllm_config"
            ) as load_anythingllm:
                with self.assertRaises(AnalysisClassificationConfigurationError):
                    create_application_services()

        load_anythingllm.assert_not_called()

    def test_invalid_filename_constraint_mode_fails_during_container_startup(
        self,
    ) -> None:
        """文件名约束误配必须在 AnythingLLM 配置和数据库初始化前阻断启动。"""
        with patch.dict(
            os.environ,
            {
                self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                self.CONSTRAINT_ENV_NAME: "unsupported",
            },
            clear=False,
        ):
            with patch(
                "app.container.load_anythingllm_config"
            ) as load_anythingllm:
                with self.assertRaises(AnalysisClassificationConfigurationError):
                    create_application_services()

        load_anythingllm.assert_not_called()

    def test_invalid_data_standard_mode_fails_during_container_startup(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                self.CONSTRAINT_ENV_NAME: (
                    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                ),
                self.DATA_STANDARD_ENV_NAME: "unsupported",
            },
            clear=False,
        ):
            with patch(
                "app.container.load_anythingllm_config"
            ) as load_anythingllm:
                with self.assertRaises(AnalysisClassificationConfigurationError):
                    create_application_services()

        load_anythingllm.assert_not_called()

    def test_invalid_identity_reselect_mode_fails_during_container_startup(
        self,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                self.ENV_NAME: ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
                self.CONSTRAINT_ENV_NAME: (
                    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                ),
                self.DATA_STANDARD_ENV_NAME: ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
                self.IDENTITY_RESELECT_ENV_NAME: "unsupported",
            },
            clear=False,
        ):
            with patch(
                "app.container.load_anythingllm_config"
            ) as load_anythingllm:
                with self.assertRaises(AnalysisClassificationConfigurationError):
                    create_application_services()

        load_anythingllm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
