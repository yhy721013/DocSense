"""既有 HYMTTranslator 的窄 TranslationEngine Adapter。"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Protocol

from app.modules.translation.domain import TranslationError, TranslationMode


logger = logging.getLogger(__name__)


class _HYMTLike(Protocol):
    def translate_text(
        self,
        text: str,
        target_lang: str = "Chinese",
        progress_callback=None,
        max_retries: int = 2,
        fast_translate: bool = True,
        model_name: str | None = None,
    ) -> str:
        ...


class HYMTTranslationEngineAdapter:
    """只在可能线程不安全的引擎调用周围串行化。

    Artifact 读取、分段、Renderer 和文档准备均发生在锁外。锁属于具体引擎实例，
    不再形成跨所有文档处理任务的全局执行锁。
    """

    def __init__(
        self,
        engine: _HYMTLike,
        *,
        engine_fingerprint: str,
    ) -> None:
        if not callable(getattr(engine, "translate_text", None)):
            raise TypeError("engine 必须提供 translate_text")
        fingerprint = str(engine_fingerprint).strip()
        if not fingerprint:
            raise ValueError("engine_fingerprint 不能为空")
        self._engine = engine
        self._fingerprint = fingerprint
        self._lock = threading.RLock()

    @property
    def engine_id(self) -> str:
        return "hymt"

    @property
    def engine_fingerprint(self) -> str:
        return self._fingerprint

    def translate(
        self,
        text: str,
        *,
        target_language: str,
        mode: TranslationMode,
    ) -> str:
        if not isinstance(mode, TranslationMode):
            raise TypeError("mode 必须是 TranslationMode")
        wait_started = time.monotonic()
        try:
            with self._lock:
                wait_ms = int((time.monotonic() - wait_started) * 1000)
                logger.debug(
                    "进入 HYMT 引擎实例临界区: mode=%s source_chars=%d "
                    "wait_ms=%d",
                    mode.value,
                    len(text),
                    wait_ms,
                )
                return self._engine.translate_text(
                    text,
                    target_language,
                    fast_translate=mode is TranslationMode.MACHINE,
                )
        except Exception as exc:
            logger.warning(
                "HYMT 翻译引擎失败: mode=%s source_chars=%d error_type=%s",
                mode.value,
                len(text),
                type(exc).__name__,
            )
            raise TranslationError(
                "hymt_engine_failed",
                "HYMT 翻译引擎执行失败",
            ) from exc


class LazyHYMTTranslationEngineAdapter:
    """惰性创建 HYMT 运行时，并复用同一个实例级引擎锁。

    Container 构造不应安装 Argos 包或探测模型服务。第一次真实翻译才调用工厂；初始化
    结果随后固定，全文和摘要共享同一个 Adapter 身份与锁，不再使用覆盖文档准备阶段的
    全局执行锁。
    """

    def __init__(
        self,
        factory: Callable[[], _HYMTLike],
        *,
        engine_fingerprint: str,
    ) -> None:
        if not callable(factory):
            raise TypeError("factory 必须可调用")
        fingerprint = str(engine_fingerprint).strip()
        if not fingerprint:
            raise ValueError("engine_fingerprint 不能为空")
        self._factory = factory
        self._fingerprint = fingerprint
        self._delegate: HYMTTranslationEngineAdapter | None = None
        self._init_lock = threading.Lock()

    @property
    def engine_id(self) -> str:
        return "hymt"

    @property
    def engine_fingerprint(self) -> str:
        return self._fingerprint

    def translate(
        self,
        text: str,
        *,
        target_language: str,
        mode: TranslationMode,
    ) -> str:
        return self._get_delegate().translate(
            text,
            target_language=target_language,
            mode=mode,
        )

    def _get_delegate(self) -> HYMTTranslationEngineAdapter:
        delegate = self._delegate
        if delegate is not None:
            return delegate
        with self._init_lock:
            delegate = self._delegate
            if delegate is None:
                runtime = self._factory()
                delegate = HYMTTranslationEngineAdapter(
                    runtime,
                    engine_fingerprint=self._fingerprint,
                )
                self._delegate = delegate
        return delegate


__all__ = [
    "HYMTTranslationEngineAdapter",
    "LazyHYMTTranslationEngineAdapter",
]
