import unittest
from unittest.mock import Mock, patch

from app.services.llm_service.translation_service import LLMTranslationService
from app.services.translator.core import HYMTTranslator


class LLMTranslationServiceTests(unittest.TestCase):
    def test_translate_text_only_defaults_to_machine_translation(self):
        service = LLMTranslationService()
        translator = Mock()
        translator.translate_text.return_value = "translated"
        service._translator = translator

        result = service.translate_text_only("hello", as_html=False)

        self.assertEqual(result, "translated")
        translator.translate_text.assert_called_once_with("hello", "Chinese", fast_translate=True)

    def test_hymt_translate_text_defaults_to_machine_translation(self):
        translator = HYMTTranslator.__new__(HYMTTranslator)
        translator._translate_with_argos = Mock(return_value="translated")
        translator._translate_with_llm = Mock(return_value="llm")

        result = translator.translate_text("hello", "Chinese")

        self.assertEqual(result, "translated")
        translator._translate_with_argos.assert_called_once_with("hello", "Chinese")
        translator._translate_with_llm.assert_not_called()

    @patch("app.services.llm_service.translation_service.DocumentTranslator")
    @patch("app.services.llm_service.translation_service.HYMTTranslator")
    def test_ensure_translator_skips_ollama_check_for_default_machine_translation(
            self,
            mock_translator_cls,
            _mock_document_translator_cls,
    ):
        service = LLMTranslationService()

        service._ensure_translator()

        self.assertFalse(mock_translator_cls.call_args.kwargs["check_ollama"])


if __name__ == "__main__":
    unittest.main()
