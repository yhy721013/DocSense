import os
import requests
import time
from typing import Optional
from .utils import build_prompt, clean_output, ProgressTracker


class HYMTTranslator:
    def __init__(self, model_name=None, device_map="auto"):
        """
        初始化翻译器。
        :param model_name: 默认为 None 使用 ollama 本地模型
        :param device_map: 设备映射策略 (此处主要适配 Ollama API)
        """
        # 默认使用 ollama 模型
        if model_name is None:
            self.model_name = "tencent-hy-mt:1.8b"
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

    def translate_text(self, text: str, target_lang: str = "Chinese", progress_callback=None,
                       max_retries: int = 2) -> str:
        """
        翻译单段文本，增加重试机制以应对模型幻觉或不稳定。
        :param text: 待翻译文本
        :param target_lang: 目标语言
        :param progress_callback: 进度回调函数
        :param max_retries: 最大重试次数
        :return: 翻译后的文本
        """
        if not text.strip():
            return ""

        original_text = text
        attempt = 0

        while attempt <= max_retries:
            try:
                prompt = build_prompt(text, target_lang)

                # 构建 ollama 请求
                # 针对小模型，适当降低 temperature 以减少随机性，提高稳定性
                payload = {
                    "model": self.model_name,
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
                    timeout=120  # 增加超时时间
                )
                response.raise_for_status()
                result = response.json()
                translated = result.get("response", "")

                # 清理输出 (包含去重、去幻觉逻辑)
                translated = clean_output(translated, prompt)

                # 【关键检查】如果清理后结果为空或包含明显的失败标记，且还有重试机会，则重试
                if not translated or "[内容过滤" in translated or "[警告" in translated:
                    if attempt < max_retries:
                        attempt += 1
                        time.sleep(1)  # 短暂等待后重试
                        continue
                    else:
                        # 重试耗尽，返回原文或标记
                        return f"[翻译失败: 模型多次生成无效内容] {original_text}"

                # 如果翻译成功，跳出循环
                break

            except Exception as e:
                if attempt < max_retries:
                    attempt += 1
                    time.sleep(2)  # 错误后等待更久
                    continue
                else:
                    raise RuntimeError(f"Translation failed via Ollama after {max_retries} retries: {e}")

        if progress_callback:
            progress_callback()

        return translated

    def get_progress_tracker(self) -> ProgressTracker:
        """获取进度追踪器"""
        return self.progress_tracker