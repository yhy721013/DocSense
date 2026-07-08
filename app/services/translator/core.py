import logging
import os
import re
import time

import requests

from app.services.core.logging import apply_third_party_log_levels

from .utils import ProgressTracker, build_prompt, clean_output


logger = logging.getLogger(__name__)


class HYMTTranslator:
    _TARGET_LANGUAGE_MAP = {
        "chinese": "zh",
        "中文": "zh",
        "zh": "zh",
        "zh-cn": "zh",
        "english": "en",
        "英文": "en",
        "en": "en",
        "french": "fr",
        "法文": "fr",
        "法语": "fr",
        "fr": "fr",
        "german": "de",
        "德文": "de",
        "德语": "de",
        "de": "de",
        "spanish": "es",
        "西文": "es",
        "西班牙语": "es",
        "es": "es",
        "japanese": "ja",
        "日文": "ja",
        "日语": "ja",
        "ja": "ja",
        "korean": "ko",
        "韩文": "ko",
        "韩语": "ko",
        "ko": "ko",
        "russian": "ru",
        "俄文": "ru",
        "俄语": "ru",
        "ru": "ru",
        "italian": "it",
        "意文": "it",
        "意大利语": "it",
        "it": "it",
    }
    _PIVOT_LANGUAGE_CODE = "en"
    _ARGOS_LANGUAGE_PACKAGES = [
        ("zh", "en", "中文→英文"),
        ("en", "zh", "英文→中文"),
        ("ja", "en", "日文→英文"),
        ("ru", "en", "俄文→英文"),
        ("ko", "en", "韩文→英文"),
        ("fr", "en", "法文→英文"),
        ("de", "en", "德文→英文"),
        ("it", "en", "意文→英文"),
    ]
    _LATIN_LANGUAGE_MARKERS = {
        "fr": {
            "le", "la", "les", "des", "du", "de", "un", "une", "et", "est", "dans",
            "pour", "avec", "sur", "que", "qui", "pas", "au", "aux", "ce", "cette",
            "par", "plus", "peut", "porte", "avions", "mauvais", "temps", "il",
            "aujourd'hui", "aujourd", "hui", "fait", "beau", "nous", "testons",
            "traduction",
        },
        "de": {
            "der", "die", "das", "den", "dem", "des", "und", "ist", "nicht", "mit",
            "zu", "ein", "eine", "im", "auf", "für", "von", "bei", "kann",
            "flugzeugträger", "flugzeuge", "wetter", "schlechtem", "starten",
        },
        "it": {
            "il", "lo", "la", "gli", "le", "un", "una", "e", "è", "che", "di",
            "del", "della", "dei", "degli", "delle", "per", "con", "non", "in",
            "su", "da", "al", "alla", "può", "portaerei", "lanciare", "aerei",
            "maltempo",
        },
    }
    _LATIN_DIACRITIC_MARKERS = {
        "fr": "àâçéèêëîïôûùüÿœæ",
        "de": "äöüß",
        "it": "àèéìòù",
    }

    def __init__(self, model_name=None, device_map="auto", check_ollama: bool = True):
        """
        初始化翻译器。
        :param model_name: 默认为 None 使用 ollama 本地模型
        :param device_map: 设备映射策略 (此处主要适配 Ollama API)
        :param check_ollama: 是否初始化时探测 Ollama；机器翻译默认路径不需要探测
        """
        # 默认使用 qwen3.5:4b 模型（更高效，幻觉更小）
        if model_name is None:
            self.model_name = "Qwen3-4B-Instruct-2507-Q4_K_M"
        else:
            self.model_name = model_name

        # 【修改】从环境变量获取 Ollama 地址，默认回退到 localhost
        ollama_host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
        self.ollama_api_url = f"{ollama_host}/api/generate"
        self.ollama_tags_url = f"{ollama_host}/api/tags"

        if check_ollama:
            logger.info(f"Using Ollama model: {self.model_name} at {ollama_host}")

            # 测试连接
            try:
                test_response = requests.post(
                    self.ollama_tags_url,
                    timeout=5
                )
                if test_response.status_code == 200:
                    logger.info("Ollama service connected successfully.")
                else:
                    logger.warning(f"Warning: Ollama service returned status code {test_response.status_code}")
            except Exception as e:
                logger.warning(f"Warning: Could not connect to Ollama service: {e}")
                logger.info("Please ensure Ollama is running and the model is available.")

        self.progress_tracker = ProgressTracker()

        # Argostranslate 缓存（避免重复加载）
        self._argo_translators = {}
        self._auto_install_argos_packages()

    def translate_text(self, text: str, target_lang: str = "Chinese", progress_callback=None,
                       max_retries: int = 2, fast_translate: bool = True, model_name: str = None) -> str:
        """
        翻译单段文本，作为选择大模型翻译或快速翻译的路由入口。
        :param text: 待翻译文本
        :param target_lang: 目标语言
        :param progress_callback: 进度回调函数
        :param max_retries: 最大重试次数
        :param fast_translate: 是否启用快速翻译（使用 argostranslate 而非大模型）
        :param model_name: 覆盖默认的模型名称，如果传 None 则使用初始化时的 self.model_name
        :return: 翻译后的文本
        """
        if not text.strip():
            return ""

        # 启用快速翻译模式
        if fast_translate:
            return self._translate_with_argos(text, target_lang)

        # 大模型翻译模式
        return self._translate_with_llm(
            text=text,
            target_lang=target_lang,
            progress_callback=progress_callback,
            max_retries=max_retries,
            model_name=model_name
        )

    def _translate_with_llm(self, text: str, target_lang: str = "Chinese", progress_callback=None,
                            max_retries: int = 2, model_name: str = None) -> str:
        """
        使用大模型进行单段文本翻译，增加重试机制以应对模型幻觉或不稳定。
        """
        original_text = text
        attempt = 0

        actual_model_name = model_name if model_name is not None else self.model_name

        while attempt <= max_retries:
            try:
                # 统一使用强大的增强型 Prompt 和清洗逻辑
                prompt = build_prompt(text, target_lang)

                # 构建 ollama 请求
                # 针对小模型，适当降低 temperature 以减少随机性，减少稳定性
                payload = {
                    "model": actual_model_name,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,  # 降低温度，减少幻觉
                        "top_p": 0.5,  # 降低采样范围
                        "top_k": 10,  # 限制候选词数量
                        "repeat_penalty": 1.2  # 增加重复惩罚，防止复读机
                    }
                }

                response = requests.post(
                    self.ollama_api_url,
                    json=payload,
                    timeout=None  # 增加超时时间
                )

                # 【新增】检查 HTTP 状态码
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Ollama API 返回错误状态码：{response.status_code}, 响应内容：{response.text[:200]}")

                response.raise_for_status()
                result = response.json()
                translated = result.get("response", "")

                # 【关键修复】检测是否返回了 token IDs 而不是文本
                if not translated and "context" in result:
                    # 尝试从 token IDs 重建文本（如果可能）
                    logger.warning(f"  [警告] 检测到模型返回 token IDs 而非文本，尝试使用 'done_reason' 字段")
                    # qwen3.5 有时会返回原始 token 数据，此时应标记为失败并重试
                    raise RuntimeError("Ollama 返回 token IDs 而非解码文本，可能是模型加载问题")

                # 【新增】检查 response 是否为空或包含异常数据
                if not translated:
                    # 检查是否有其他字段包含有效响应
                    if "done_reason" in result:
                        logger.warning(f"  [警告] 模型提前终止 (原因：{result['done_reason']})")
                    raise RuntimeError(f"Ollama API 返回空响应，完整响应：{result}")

                # 清理输出 (包含去重、去幻觉逻辑)
                translated = clean_output(translated, prompt)

                # 【关键检查】如果清理后结果为空或包含明显的失败标记，且还有重试机会，则重试
                if not translated or "[内容过滤" in translated or "[警告" in translated:
                    if attempt < max_retries:
                        attempt += 1
                        logger.info(f"  [重试] 第 {attempt} 次重试...")
                        time.sleep(1)  # 短暂等待后重试
                        continue
                    else:
                        # 重试耗尽，返回原文或标记
                        logger.info(f"  [失败] 多次重试后仍无法生成有效翻译")
                        return f"[翻译失败：模型多次生成无效内容] {original_text}"

                # 如果翻译成功，跳出循环
                break

            except Exception as e:
                logger.error(f"  [异常] 第 {attempt + 1} 次尝试失败：{e}")
                if attempt < max_retries:
                    attempt += 1
                    logger.info(f"  [重试] 开始第 {attempt + 1} 次重试...")
                    time.sleep(2)  # 错误后等待更久
                    continue
                else:
                    logger.info(f"  [失败] 所有重试均失败，返回原文")
                    raise RuntimeError(f"Translation failed via Ollama after {max_retries} retries: {e}")

        if progress_callback:
            progress_callback()

        return translated

    def _translate_with_argos(self, text: str, target_lang: str) -> str:
        """
        使用 argostranslate 进行快速翻译
        :param text: 待翻译文本
        :param target_lang: 目标语言
        :return: 翻译后的文本
        """
        try:
            from argostranslate import translate
            import argostranslate.sbd

            # Argos Translate 会在延迟导入 utils 模块时把自身 logger 重置为 INFO。必须在
            # 导入完成后重新应用应用级策略，确保待翻译原文和分句过程不会进入生产日志。
            apply_third_party_log_levels()

            # --- Monkey patch StanzaSentencizer to fallback to simple split on error ---
            if not getattr(argostranslate.sbd.StanzaSentencizer, '_is_patched', False):
                original_split_sentences = argostranslate.sbd.StanzaSentencizer.split_sentences
                def safe_split_sentences(self, text: str):
                    try:
                        return original_split_sentences(self, text)
                    except Exception as e:
                        logger.warning(f"  [警告] StanzaSentencizer 失败，回退到基础断句: {e}")
                        import re
                        sentences = re.split(r'(?<=[.!?。！？\n])\s+', text)
                        return [s for s in sentences if s.strip()]
                
                argostranslate.sbd.StanzaSentencizer.split_sentences = safe_split_sentences
                argostranslate.sbd.StanzaSentencizer._is_patched = True
            # ---------------------------------------------------------------------------

            from_lang_code = self._detect_argos_source_language(text)
            to_lang_code = self._target_argos_language_code(target_lang)

            # 如果源语言和目标语言相同，直接返回
            if from_lang_code == to_lang_code:
                return text

            installed_languages = translate.get_installed_languages()
            return self._translate_argos_path(
                text=text,
                installed_languages=installed_languages,
                from_lang_code=from_lang_code,
                to_lang_code=to_lang_code,
            )

        except ImportError as ie:
            logger.error(f"  [错误] argostranslate 未安装：{ie}")
            raise RuntimeError(f"argostranslate 未安装：{ie}") from ie
        except AttributeError as ae:
            logger.error(f"  [错误] ArgoTranslate API 调用失败：{ae}")
            raise RuntimeError(f"ArgoTranslate API 调用失败：{ae}") from ae
        except Exception as e:
            logger.error(f"  [错误] ArgoTranslate 翻译失败：{e}")
            raise RuntimeError(f"ArgoTranslate 翻译失败：{e}") from e


    def get_progress_tracker(self) -> ProgressTracker:
        """获取进度追踪器"""
        return self.progress_tracker

    @classmethod
    def _target_argos_language_code(cls, target_lang: str) -> str:
        return cls._TARGET_LANGUAGE_MAP.get((target_lang or "").strip().lower(), "en")

    @classmethod
    def _detect_argos_source_language(cls, text: str) -> str:
        """
        检测 Argos 源语言。优先用字符集识别日/韩/俄/中，再对拉丁语系做轻量词表判断。
        未能识别的拉丁文本保持历史行为，默认当作英文处理。
        """
        if re.search(r"[\u3040-\u30ff]", text):
            return "ja"
        if re.search(r"[\uac00-\ud7af]", text):
            return "ko"
        if re.search(r"[\u0400-\u04ff]", text):
            return "ru"
        if re.search(r"[\u4e00-\u9fff]", text):
            return "zh"

        latin_language = cls._detect_latin_source_language(text)
        return latin_language or "en"

    @classmethod
    def _detect_latin_source_language(cls, text: str) -> str | None:
        normalized = text.lower()
        tokens = re.findall(r"[a-zA-ZÀ-ÖØ-öø-ÿ]+(?:['’][a-zA-ZÀ-ÖØ-öø-ÿ]+)?", normalized)
        if not tokens:
            return None

        scores = {code: 0 for code in cls._LATIN_LANGUAGE_MARKERS}
        for code, chars in cls._LATIN_DIACRITIC_MARKERS.items():
            scores[code] += sum(2 for char in normalized if char in chars)

        for token in tokens:
            for code, markers in cls._LATIN_LANGUAGE_MARKERS.items():
                if token in markers:
                    scores[code] += 1

        best_code, best_score = max(scores.items(), key=lambda item: item[1])
        if best_score <= 0:
            return None

        # 只在分值有明确优势时切到非英文，避免普通英文被短词误判。
        other_scores = [score for code, score in scores.items() if code != best_code]
        if best_score >= 2 and best_score >= max(other_scores, default=0) + 1:
            return best_code
        return None

    def _translate_argos_path(
            self,
            text: str,
            installed_languages,
            from_lang_code: str,
            to_lang_code: str,
    ) -> str:
        direct_translation = self._get_argos_translation(installed_languages, from_lang_code, to_lang_code)
        if direct_translation:
            return self._run_argos_translation(direct_translation, text, f"{from_lang_code} -> {to_lang_code}")

        if (
                to_lang_code == "zh"
                and from_lang_code != self._PIVOT_LANGUAGE_CODE
                and (
                        pivot_translation := self._get_argos_translation(
                            installed_languages,
                            from_lang_code,
                            self._PIVOT_LANGUAGE_CODE,
                        )
                )
                and (
                        final_translation := self._get_argos_translation(
                            installed_languages,
                            self._PIVOT_LANGUAGE_CODE,
                            to_lang_code,
                        )
                )
        ):
            logger.info(
                "  [提示] 未找到 %s -> %s 直达翻译包，使用 %s -> %s -> %s 中转翻译",
                from_lang_code,
                to_lang_code,
                from_lang_code,
                self._PIVOT_LANGUAGE_CODE,
                to_lang_code,
            )
            pivot_text = self._run_argos_translation(
                pivot_translation,
                text,
                f"{from_lang_code} -> {self._PIVOT_LANGUAGE_CODE}",
            )
            return self._run_argos_translation(
                final_translation,
                pivot_text,
                f"{self._PIVOT_LANGUAGE_CODE} -> {to_lang_code}",
            )

        self._ensure_argos_language_available(installed_languages, from_lang_code, "源语言")
        self._ensure_argos_language_available(installed_languages, to_lang_code, "目标语言")
        logger.warning(f"  [警告] 无法创建 {from_lang_code} -> {to_lang_code} 的翻译器")
        raise RuntimeError(f"无法创建 {from_lang_code} -> {to_lang_code} 的翻译器")

    def _get_argos_translation(self, installed_languages, from_lang_code: str, to_lang_code: str):
        from_lang_obj = self._find_argos_language(installed_languages, from_lang_code)
        to_lang_obj = self._find_argos_language(installed_languages, to_lang_code)
        if not from_lang_obj or not to_lang_obj:
            return None
        return from_lang_obj.get_translation(to_lang_obj)

    @staticmethod
    def _find_argos_language(installed_languages, lang_code: str):
        return next((lang for lang in installed_languages if lang.code == lang_code), None)

    def _ensure_argos_language_available(self, installed_languages, lang_code: str, role: str) -> None:
        if not self._find_argos_language(installed_languages, lang_code):
            logger.warning(f"  [警告] 未找到{role} {lang_code} 的翻译包")
            raise RuntimeError(f"未找到{role} {lang_code} 的翻译包")

    @staticmethod
    def _run_argos_translation(translation, text: str, path_desc: str) -> str:
        translated = translation.translate(text)
        if not translated:
            logger.warning(f"  [警告] ArgoTranslate {path_desc} 返回空结果")
            raise RuntimeError(f"ArgoTranslate {path_desc} 返回空结果")
        return translated

    def _auto_install_argos_packages(self) -> None:
        """
        自动下载并安装常用的 argostranslate 翻译包
        """
        try:
            from argostranslate import package, translate
            import argostranslate.settings

            # 自动安装路径同样会触发 Argos 模块初始化，因此也必须在任何包查询或下载
            # 日志产生前重新应用统一级别，避免构造翻译器时泄漏 INFO 级详细信息。
            apply_third_party_log_levels()

            logger.info("[ArgoTranslate] 检查并安装翻译包...")

            # 【关键修复】先设置为离线模式
            argostranslate.settings.use_online = False

            # 【新增】检查已安装的语言，避免重复下载
            installed_languages = translate.get_installed_languages()
            logger.info(f"[ArgoTranslate] 当前已安装的语言：{[str(lang) for lang in installed_languages]}")

            for from_code, to_code, desc in self._ARGOS_LANGUAGE_PACKAGES:
                # 检查是否已安装
                from_lang_obj = next((lang for lang in installed_languages if lang.code == from_code), None)
                to_lang_obj = next((lang for lang in installed_languages if lang.code == to_code), None)

                if from_lang_obj and to_lang_obj:
                    # 检查是否已有翻译路径
                    translation = from_lang_obj.get_translation(to_lang_obj)
                    if translation:
                        logger.info(f"  ✓ {desc} 翻译包已存在，跳过下载")
                        continue
                    else:
                        logger.info(f"   {desc} 翻译包未完全安装")
                else:
                    logger.info(f"   {desc} 翻译包未安装")

                # 只有在未安装时才尝试下载
                try:
                    # 尝试从本地缓存加载（如果之前下载过）
                    available_packages = package.get_available_packages()
                    package_to_install = next(
                        filter(lambda x: x.from_code == from_code and x.to_code == to_code, available_packages),
                        None
                    )

                    if package_to_install:
                        package_path = package_to_install.download()
                        package.install_from_path(package_path)
                        logger.info(f"  ✓ {desc} 翻译包安装完成")
                    else:
                        logger.info(f"  ! {desc} 翻译包不可用")
                except Exception as e:
                    logger.info(f"  ✗ {desc} 翻译包安装失败：{e}")

            # 验证已安装的语言
            installed_languages = translate.get_installed_languages()
            logger.info(f"[ArgoTranslate] 已安装的语言：{[str(lang) for lang in installed_languages]}")
            logger.info("[ArgoTranslate] 翻译包检查完成\n")

        except ImportError:
            logger.info("[ArgoTranslate] argostranslate 未安装，跳过自动安装")
        except Exception as e:
            logger.info(f"[ArgoTranslate] 自动安装翻译包失败：{e}，无法使用 ArgoTranslate 快速翻译")
