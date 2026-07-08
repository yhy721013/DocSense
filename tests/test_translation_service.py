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

    @patch("app.services.llm_service.translation_service.DocumentTranslator")
    def test_ensure_document_translator_recovers_half_initialized_state(self, mock_document_translator_cls):
        service = LLMTranslationService()
        translator = Mock()
        service._translator = translator

        service._ensure_document_translator()

        mock_document_translator_cls.assert_called_once_with(translator)
        self.assertIs(service._document_translator, mock_document_translator_cls.return_value)

    def test_hymt_translate_text_defaults_to_machine_translation(self):
        translator = HYMTTranslator.__new__(HYMTTranslator)
        translator._translate_with_argos = Mock(return_value="translated")
        translator._translate_with_llm = Mock(return_value="llm")

        result = translator.translate_text("hello", "Chinese")

        self.assertEqual(result, "translated")
        translator._translate_with_argos.assert_called_once_with("hello", "Chinese")
        translator._translate_with_llm.assert_not_called()

    def test_argos_source_language_detection_for_supported_languages(self):
        samples = {
            "en": "The aircraft carrier can launch aircraft in bad weather.",
            "ja": "空母は悪天候でも航空機を発進させることができます。",
            "ru": "Авианосец может запускать самолеты в плохую погоду.",
            "ko": "항공모함은 악천후에도 항공기를 발진시킬 수 있습니다.",
            "fr": "Le porte-avions peut lancer des avions par mauvais temps.",
            "de": "Der Flugzeugträger kann bei schlechtem Wetter Flugzeuge starten.",
            "it": "La portaerei può lanciare aerei con il maltempo.",
            "zh": "航空母舰可以在恶劣天气下起降飞机。",
        }

        for expected_code, text in samples.items():
            with self.subTest(expected_code=expected_code):
                self.assertEqual(expected_code, HYMTTranslator._detect_argos_source_language(text))

    def test_argos_source_language_detection_keeps_common_french_from_italian(self):
        text = "Aujourd'hui il fait beau. Nous testons la traduction."

        self.assertEqual("fr", HYMTTranslator._detect_argos_source_language(text))

    def test_argos_uses_english_pivot_when_direct_chinese_package_missing(self):
        translator = HYMTTranslator.__new__(HYMTTranslator)
        languages = [
            _FakeArgosLanguage("fr"),
            _FakeArgosLanguage("en"),
            _FakeArgosLanguage("zh"),
        ]
        languages[0].translations["en"] = _FakeArgosTranslation("english text")
        languages[1].translations["zh"] = _FakeArgosTranslation("中文文本")

        result = translator._translate_argos_path(
            text="texte francais",
            installed_languages=languages,
            from_lang_code="fr",
            to_lang_code="zh",
        )

        self.assertEqual("中文文本", result)
        self.assertEqual("texte francais", languages[0].translations["en"].last_text)
        self.assertEqual("english text", languages[1].translations["zh"].last_text)

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


class _FakeArgosLanguage:
    def __init__(self, code):
        self.code = code
        self.translations = {}

    def get_translation(self, to_lang):
        return self.translations.get(to_lang.code)


class _FakeArgosTranslation:
    def __init__(self, translated_text):
        self.translated_text = translated_text
        self.last_text = None

    def translate(self, text):
        self.last_text = text
        return self.translated_text


if __name__ == "__main__":
    unittest.main()
