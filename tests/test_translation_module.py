"""阶段 1H-5 独立 Translation Domain/Application/Adapter 门禁。"""

from __future__ import annotations

import ast
import hashlib
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from pathlib import Path
from unittest.mock import Mock

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    LocalArtifactStoreAdapter,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentRepresentation,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from app.modules.translation.adapters import (
    HYMTTranslationEngineAdapter,
    LazyHYMTTranslationEngineAdapter,
    SafeHTMLTranslationRendererAdapter,
)
from app.modules.translation.application import (
    TranslatePreparedDocument,
    build_translation_profile,
)
from app.modules.translation.domain import (
    RenderedTranslation,
    TranslationError,
    TranslationFailurePolicy,
    TranslationMode,
    TranslationRequest,
    TranslationUnit,
)
from tests import workspace_tempdir
from tests.fakes.translation import (
    StrictPreparedArtifactReaderFake,
    StrictTranslationEngineFake,
    StrictTranslationRendererFake,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MODULE_ROOT = _REPOSITORY_ROOT / "app" / "modules" / "translation"
_BASELINE_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "document_processing"
    / "stage1h_baseline.json"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact(
    store,
    *,
    index: int,
    payload: bytes,
    representation: DocumentRepresentation = DocumentRepresentation.TEXT,
    media_type: str = "text/plain",
):
    task_id = TaskId(f"stage1h-translation-{index:02d}")
    artifact = store.publish(
        ArtifactPublication(
            task_id=task_id,
            step_key=_digest(f"translation-source-{index}"),
            kind=ArtifactKind.PREPARED,
            representation=representation,
            media_type=media_type,
        ),
        BytesArtifactContent(payload),
    )
    return task_id, artifact


def _request(task_id, artifact, engine, renderer, **overrides):
    return TranslationRequest(
        task_id=task_id,
        prepared_artifact=artifact,
        target_language=overrides.get("target_language", "Chinese"),
        item_limit=overrides.get("item_limit", 0),
        profile=build_translation_profile(
            engine=engine,
            renderer=renderer,
            mode=overrides.get("mode", TranslationMode.MACHINE),
            failure_policy=overrides.get(
                "failure_policy",
                TranslationFailurePolicy.PLACEHOLDER,
            ),
        ),
        trace_id="trace-translation",
    )


class TranslationModuleTests(unittest.TestCase):
    def test_hymt_adapter_maps_explicit_mode_to_legacy_engine_flag(self) -> None:
        """机器/LLM 选择由接受时冻结的 Mode 决定，不再从进程环境隐式读取。"""

        runtime = Mock()
        runtime.translate_text.side_effect = ("machine", "llm")
        engine = HYMTTranslationEngineAdapter(
            runtime,
            engine_fingerprint="mode-mapping-v1",
        )

        self.assertEqual(
            "machine",
            engine.translate(
                "hello",
                target_language="Chinese",
                mode=TranslationMode.MACHINE,
            ),
        )
        self.assertEqual(
            "llm",
            engine.translate(
                "hello",
                target_language="Chinese",
                mode=TranslationMode.LLM,
            ),
        )
        self.assertEqual(
            [True, False],
            [call.kwargs["fast_translate"] for call in runtime.translate_text.call_args_list],
        )

    def test_lazy_hymt_adapter_initializes_runtime_once(self) -> None:
        """半初始化兼容逻辑由线程安全 Lazy Adapter 替代，运行时工厂只允许成功一次。"""

        runtime = Mock()
        runtime.translate_text.return_value = "translated"
        factory = Mock(return_value=runtime)
        engine = LazyHYMTTranslationEngineAdapter(
            factory,
            engine_fingerprint="lazy-runtime-v1",
        )

        for text in ("first", "second"):
            self.assertEqual(
                "translated",
                engine.translate(
                    text,
                    target_language="Chinese",
                    mode=TranslationMode.MACHINE,
                ),
            )

        factory.assert_called_once_with()
        self.assertEqual(2, runtime.translate_text.call_count)

    def test_request_contract_has_no_document_conversion_switch(self) -> None:
        field_names = {field.name for field in fields(TranslationRequest)}
        self.assertEqual(
            {
                "task_id",
                "prepared_artifact",
                "target_language",
                "item_limit",
                "profile",
                "trace_id",
            },
            field_names,
        )
        self.assertTrue(
            {
                "use_minerU",
                "use_ocr",
                "mhtml",
                "file_path",
                "source_suffix",
            }.isdisjoint(field_names)
        )

    def test_translation_profile_roundtrip_is_strict_and_stable(self) -> None:
        engine = StrictTranslationEngineFake()
        renderer = StrictTranslationRendererFake()
        profile = build_translation_profile(
            engine=engine,
            renderer=renderer,
            mode=TranslationMode.LLM,
            failure_policy=TranslationFailurePolicy.FAIL_DOCUMENT,
        )
        self.assertEqual(
            profile,
            type(profile).from_dict(profile.to_dict()),
        )
        drifted = profile.to_dict()
        drifted["unknown"] = True
        with self.assertRaises(ValueError):
            type(profile).from_dict(drifted)

    def test_application_translates_range_and_skips_mostly_chinese(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            payload = "hello\n\n这是中文\n\noutside range".encode("utf-8")
            task_id, artifact = _artifact(store, index=1, payload=payload)
            reader = StrictPreparedArtifactReaderFake()
            engine = StrictTranslationEngineFake()
            renderer = StrictTranslationRendererFake()
            request = _request(
                task_id,
                artifact,
                engine,
                renderer,
                item_limit=2,
            )
            expected_units = (
                TranslationUnit(1, "hello", "你好", True),
                TranslationUnit(2, "这是中文", "这是中文", False),
                TranslationUnit(
                    3,
                    "outside range",
                    "outside range",
                    False,
                ),
            )
            rendered = RenderedTranslation("<html>b</html>", "<html>m</html>")
            reader.expect(artifact, payload=payload)
            engine.expect(
                "hello",
                target_language="Chinese",
                mode=TranslationMode.MACHINE,
                result="你好",
            )
            renderer.expect(request, expected_units, result=rendered)

            result = TranslatePreparedDocument(
                reader=reader,
                engine=engine,
                renderer=renderer,
            ).execute(request)

            self.assertEqual(expected_units, result.units)
            self.assertEqual(1, result.translated_count)
            self.assertEqual(0, result.failed_count)
            reader.assert_complete()
            engine.assert_complete()
            renderer.assert_complete()

    def test_empty_input_and_strict_engine_failure_are_explicit(self) -> None:
        cases = (
            (b" \r\n ", "translation_input_empty"),
            (b"hello", "translation_engine_failed"),
        )
        for index, (payload, expected_code) in enumerate(cases, start=2):
            with self.subTest(expected_code=expected_code):
                with workspace_tempdir() as temporary:
                    store = LocalArtifactStoreAdapter(
                        Path(temporary) / "artifacts"
                    )
                    task_id, artifact = _artifact(
                        store,
                        index=index,
                        payload=payload,
                    )
                    reader = StrictPreparedArtifactReaderFake()
                    engine = StrictTranslationEngineFake()
                    renderer = StrictTranslationRendererFake()
                    request = _request(
                        task_id,
                        artifact,
                        engine,
                        renderer,
                        failure_policy=(
                            TranslationFailurePolicy.FAIL_DOCUMENT
                        ),
                    )
                    reader.expect(artifact, payload=payload)
                    if payload.strip():
                        engine.expect(
                            "hello",
                            target_language="Chinese",
                            mode=TranslationMode.MACHINE,
                            error=RuntimeError("injected"),
                        )
                    with self.assertRaises(TranslationError) as raised:
                        TranslatePreparedDocument(
                            reader=reader,
                            engine=engine,
                            renderer=renderer,
                        ).execute(request)
                    self.assertEqual(expected_code, raised.exception.code)

    def test_placeholder_and_renderer_failure_are_separate(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            payload = b"hello"
            task_id, artifact = _artifact(store, index=4, payload=payload)
            reader = StrictPreparedArtifactReaderFake()
            engine = StrictTranslationEngineFake()
            renderer = StrictTranslationRendererFake()
            request = _request(task_id, artifact, engine, renderer)
            failed_unit = TranslationUnit(
                1,
                "hello",
                "hello",
                False,
                failed=True,
            )
            reader.expect(artifact, payload=payload)
            engine.expect(
                "hello",
                target_language="Chinese",
                mode=TranslationMode.MACHINE,
                error=RuntimeError("secret"),
            )
            renderer.expect(
                request,
                (failed_unit,),
                error=RuntimeError("render failed"),
            )
            with self.assertRaises(TranslationError) as raised:
                TranslatePreparedDocument(
                    reader=reader,
                    engine=engine,
                    renderer=renderer,
                ).execute(request)
            self.assertEqual("translation_renderer_failed", raised.exception.code)

    def test_safe_renderer_preserves_bilingual_and_escapes_html(self) -> None:
        renderer = SafeHTMLTranslationRendererAdapter()
        engine = StrictTranslationEngineFake()
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            task_id, artifact = _artifact(
                store,
                index=5,
                payload=b"<script>alert(1)</script>",
            )
            request = _request(task_id, artifact, engine, renderer)
            rendered = renderer.render(
                request=request,
                source_text="<script>alert(1)</script>",
                units=(
                    TranslationUnit(
                        1,
                        "<script>alert(1)</script>",
                        "<b>safe</b>",
                        True,
                    ),
                ),
            )
        self.assertNotIn("<script>", rendered.bilingual_html)
        self.assertNotIn("<b>safe</b>", rendered.monolingual_html)
        self.assertIn("&lt;script&gt;", rendered.bilingual_html)
        self.assertIn("&lt;b&gt;safe&lt;/b&gt;", rendered.monolingual_html)
        self.assertIn("original-text", rendered.bilingual_html)
        self.assertNotIn(
            '<div class="original-text">',
            rendered.monolingual_html,
        )

    def test_safe_renderer_preserves_markdown_structure(self) -> None:
        renderer = SafeHTMLTranslationRendererAdapter()
        engine = StrictTranslationEngineFake()
        markdown_source = (
            "# Heading\n\n"
            "- first\n"
            "- second\n\n"
            "| Name | Value |\n"
            "| --- | --- |\n"
            "| A | B |\n\n"
            "![diagram](data:image/png;base64,QUJD)"
        )
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            task_id, artifact = _artifact(
                store,
                index=50,
                payload=markdown_source.encode("utf-8"),
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown",
            )
            request = _request(task_id, artifact, engine, renderer)
            source_units = tuple(
                renderer.extract_units(
                    request=request,
                    source_text=markdown_source,
                )
            )
            rendered = renderer.render(
                request=request,
                source_text=markdown_source,
                units=tuple(
                    TranslationUnit(
                        ordinal=index,
                        source_text=text,
                        translated_text=f"译:{text}",
                        translated=True,
                    )
                    for index, text in enumerate(source_units, start=1)
                ),
            )

        for marker in ("<h1>", "<ul>", "<table>", "<img"):
            self.assertIn(marker, rendered.monolingual_html)
        self.assertIn("data:image/png;base64,QUJD", rendered.monolingual_html)
        self.assertNotIn("# Heading", rendered.monolingual_html)

    def test_preprocessing_occurs_outside_engine_instance_lock(self) -> None:
        class BlockingLegacyEngine:
            def __init__(self) -> None:
                self.first_entered = threading.Event()
                self.release = threading.Event()
                self.calls = 0
                self.lock = threading.Lock()

            def translate_text(self, text, target_lang, *, fast_translate):
                del target_lang, fast_translate
                with self.lock:
                    self.calls += 1
                    call = self.calls
                if call == 1:
                    self.first_entered.set()
                    self.release.wait(timeout=10)
                return f"translated:{text}"

        class ObservedStore(LocalArtifactStoreAdapter):
            def __init__(self, root):
                super().__init__(root)
                self.second_read = threading.Event()
                self.second_artifact = None

            def open_reader(self, artifact):
                if artifact == self.second_artifact:
                    self.second_read.set()
                return super().open_reader(artifact)

        with workspace_tempdir() as temporary:
            store = ObservedStore(Path(temporary) / "artifacts")
            task1, artifact1 = _artifact(store, index=6, payload=b"first")
            task2, artifact2 = _artifact(store, index=7, payload=b"second")
            store.second_artifact = artifact2
            legacy = BlockingLegacyEngine()
            engine = HYMTTranslationEngineAdapter(
                legacy,
                engine_fingerprint="blocking-engine-v1",
            )
            renderer = SafeHTMLTranslationRendererAdapter()
            application = TranslatePreparedDocument(
                reader=store,
                engine=engine,
                renderer=renderer,
            )
            request1 = _request(task1, artifact1, engine, renderer)
            request2 = _request(task2, artifact2, engine, renderer)
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(application.execute, request1)
                self.assertTrue(legacy.first_entered.wait(timeout=10))
                second = executor.submit(application.execute, request2)
                # 第二任务可在引擎锁被第一任务占用时完成 Artifact 读取/分段。
                self.assertTrue(store.second_read.wait(timeout=10))
                legacy.release.set()
                results = (first.result(timeout=10), second.result(timeout=10))
        self.assertNotEqual(
            results[0].rendered.monolingual_html,
            results[1].rendered.monolingual_html,
        )

    def test_translation_module_has_no_format_converter_imports(self) -> None:
        forbidden = (
            "mineru",
            "mhtml",
            "libreoffice",
            "builtin_ocr",
            "document_processing.adapters",
        )
        violations: list[str] = []
        for source in _MODULE_ROOT.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            for imported in imports:
                if any(item in imported for item in forbidden):
                    violations.append(
                        f"{source.relative_to(_REPOSITORY_ROOT)} -> {imported}"
                    )
        self.assertEqual([], violations)

    def test_translation_module_has_no_services_or_framework_imports(
        self,
    ) -> None:
        violations: list[str] = []
        for source in _MODULE_ROOT.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                imported = ""
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(("app.services", "flask")):
                            violations.append(
                                f"{source.relative_to(_REPOSITORY_ROOT)}"
                                f" -> {alias.name}"
                            )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported = node.module
                if imported.startswith(("app.services", "flask")):
                    violations.append(
                        f"{source.relative_to(_REPOSITORY_ROOT)} -> {imported}"
                    )
        self.assertEqual([], violations)

    def test_translation_consumers_match_stage_inventory(self) -> None:
        baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        expected = set(baseline["currentTranslationConsumers"])
        actual: set[str] = set()
        for source in (_REPOSITORY_ROOT / "app").rglob("*.py"):
            if _MODULE_ROOT in source.parents:
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports = [
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            ]
            if any(
                imported == "app.modules.translation"
                or imported.startswith("app.modules.translation.")
                for imported in imports
            ):
                actual.add(source.relative_to(_REPOSITORY_ROOT).as_posix())
        self.assertEqual(expected, actual)

    def test_old_engine_support_files_are_thin_facades(self) -> None:
        """1G-5B 后旧翻译引擎支持文件必须保持物理退出。"""

        facades = (
            _REPOSITORY_ROOT / "app/services/translator/core.py",
            _REPOSITORY_ROOT / "app/services/translator/utils.py",
            _REPOSITORY_ROOT / "app/services/translator/chunk_processor.py",
        )
        for facade in facades:
            self.assertFalse(
                facade.exists(),
                facade.name,
            )


if __name__ == "__main__":
    unittest.main()
