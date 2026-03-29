import requests
import time
from .utils import build_prompt, clean_output, ProgressTracker


class HYMTTranslator:
    def __init__(self, model_name=None, device_map="auto"):
        """
        初始化翻译器。
        :param model_name: 默认为 None 使用 ollama 本地模型
        :param device_map: 设备映射策略 (此处主要适配 Ollama API)
        """
        # 默认使用 qwen3.5:4b 模型（更高效，幻觉更小）
        if model_name is None:
            self.model_name = "Qwen3-4B-Instruct-2507-Q4_K_M"
        else:
            self.model_name = model_name

        # ollama API 地址
        self.ollama_api_url = "http://localhost:11434/api/generate"

        print(f"Using Ollama model: {self.model_name}")

        # 测试连接
        try:
            test_response = requests.post(
                "http://localhost:11434/api/tags",
                timeout=5
            )
            if test_response.status_code == 200:
                print("Ollama service connected successfully.")
            else:
                print(f"Warning: Ollama service returned status code {test_response.status_code}")
        except Exception as e:
            print(f"Warning: Could not connect to Ollama service: {e}")
            print("Please ensure Ollama is running and the model is available.")

        self.progress_tracker = ProgressTracker()

        # Argostranslate 缓存（避免重复加载）
        self._argo_translators = {}
        self._auto_install_argos_packages()

    def translate_text(self, text: str, target_lang: str = "Chinese", progress_callback=None,
                       max_retries: int = 2, fast_translate: bool = False, model_name: str = None) -> str:
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
                    print(f"  [警告] 检测到模型返回 token IDs 而非文本，尝试使用 'done_reason' 字段")
                    # qwen3.5 有时会返回原始 token 数据，此时应标记为失败并重试
                    raise RuntimeError("Ollama 返回 token IDs 而非解码文本，可能是模型加载问题")

                # 【新增】检查 response 是否为空或包含异常数据
                if not translated:
                    # 检查是否有其他字段包含有效响应
                    if "done_reason" in result:
                        print(f"  [警告] 模型提前终止 (原因：{result['done_reason']})")
                    raise RuntimeError(f"Ollama API 返回空响应，完整响应：{result}")

                # 清理输出 (包含去重、去幻觉逻辑)
                translated = clean_output(translated, prompt)

                # 【关键检查】如果清理后结果为空或包含明显的失败标记，且还有重试机会，则重试
                if not translated or "[内容过滤" in translated or "[警告" in translated:
                    if attempt < max_retries:
                        attempt += 1
                        print(f"  [重试] 第 {attempt} 次重试...")
                        time.sleep(1)  # 短暂等待后重试
                        continue
                    else:
                        # 重试耗尽，返回原文或标记
                        print(f"  [失败] 多次重试后仍无法生成有效翻译")
                        return f"[翻译失败：模型多次生成无效内容] {original_text}"

                # 如果翻译成功，跳出循环
                break

            except Exception as e:
                print(f"  [异常] 第 {attempt + 1} 次尝试失败：{e}")
                if attempt < max_retries:
                    attempt += 1
                    print(f"  [重试] 开始第 {attempt + 1} 次重试...")
                    time.sleep(2)  # 错误后等待更久
                    continue
                else:
                    print(f"  [失败] 所有重试均失败，返回原文")
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
            from argostranslate import package, translate

            # 检测源语言（简单判断：如果包含中文字符则为中文）
            from_lang_code = "zh" if any('\u4e00' <= c <= '\u9fff' for c in text) else "en"

            # 目标语言映射
            lang_map = {
                "chinese": "zh",
                "english": "en",
                "french": "fr",
                "german": "de",
                "spanish": "es",
                "japanese": "ja",
                "korean": "ko",
            }
            to_lang_code = lang_map.get(target_lang.lower(), "en")

            # 如果源语言和目标语言相同，直接返回
            if from_lang_code == to_lang_code:
                return text

            # 【关键修复】使用正确的 API 调用方式
            # 1. 获取已安装的语言列表
            installed_languages = translate.get_installed_languages()

            # 2. 找到源语言和目标语言对象
            from_lang_obj = next((lang for lang in installed_languages if lang.code == from_lang_code), None)
            to_lang_obj = next((lang for lang in installed_languages if lang.code == to_lang_code), None)

            # 3. 检查是否找到对应的语言
            if not from_lang_obj:
                print(f"  [警告] 未找到源语言 {from_lang_code} 的翻译包")
                return self.translate_text(text, target_lang, fast_translate=False)

            if not to_lang_obj:
                print(f"  [警告] 未找到目标语言 {to_lang_code} 的翻译包")
                return self.translate_text(text, target_lang, fast_translate=False)

            # 4. 获取翻译器对象
            translation = from_lang_obj.get_translation(to_lang_obj)

            if not translation:
                print(f"  [警告] 无法创建 {from_lang_code} -> {to_lang_code} 的翻译器")
                return self.translate_text(text, target_lang, fast_translate=False)

            # 5. 执行翻译
            translated = translation.translate(text)

            # 6. 检查翻译结果
            if not translated:
                print(f"  [警告] ArgoTranslate 返回空结果")
                return self.translate_text(text, target_lang, fast_translate=False)

            return translated

        except ImportError as ie:
            print(f"  [警告] argostranslate 未安装：{ie}，回退到大模型翻译")
            return self.translate_text(text, target_lang, fast_translate=False)
        except AttributeError as ae:
            print(f"  [错误] ArgoTranslate API 调用失败：{ae}")
            return self.translate_text(text, target_lang, fast_translate=False)
        except Exception as e:
            print(f"  [错误] ArgoTranslate 翻译失败：{e}")
            return self.translate_text(text, target_lang, fast_translate=False)


    def get_progress_tracker(self) -> ProgressTracker:
        """获取进度追踪器"""
        return self.progress_tracker

    def _auto_install_argos_packages(self) -> None:
        """
        自动下载并安装常用的 argostranslate 翻译包
        """
        try:
            from argostranslate import package, translate
            import argostranslate.settings

            print("\n[ArgoTranslate] 检查并安装翻译包...")

            # 【关键修复】先设置为离线模式
            argostranslate.settings.use_online = False

            # 【新增】检查已安装的语言，避免重复下载
            installed_languages = translate.get_installed_languages()
            print(f"[ArgoTranslate] 当前已安装的语言：{[str(lang) for lang in installed_languages]}")

            # 需要安装的语言对
            language_pairs = [
                ("zh", "en", "中文→英文"),
                ("en", "zh", "英文→中文"),
            ]

            for from_code, to_code, desc in language_pairs:
                # 检查是否已安装
                from_lang_obj = next((lang for lang in installed_languages if lang.code == from_code), None)
                to_lang_obj = next((lang for lang in installed_languages if lang.code == to_code), None)

                if from_lang_obj and to_lang_obj:
                    # 检查是否已有翻译路径
                    translation = from_lang_obj.get_translation(to_lang_obj)
                    if translation:
                        print(f"  ✓ {desc} 翻译包已存在，跳过下载")
                        continue
                    else:
                        print(f"   {desc} 翻译包未完全安装")
                else:
                    print(f"   {desc} 翻译包未安装")

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
                        print(f"  ✓ {desc} 翻译包安装完成")
                    else:
                        print(f"  ! {desc} 翻译包不可用")
                except Exception as e:
                    print(f"  ✗ {desc} 翻译包安装失败：{e}")

            # 验证已安装的语言
            installed_languages = translate.get_installed_languages()
            print(f"\n[ArgoTranslate] 已安装的语言：{[str(lang) for lang in installed_languages]}")
            print("[ArgoTranslate] 翻译包检查完成\n")

        except ImportError:
            print("[ArgoTranslate] argostranslate 未安装，跳过自动安装")
        except Exception as e:
            print(f"[ArgoTranslate] 自动安装翻译包失败：{e}，无法使用 ArgoTranslate 快速翻译")
