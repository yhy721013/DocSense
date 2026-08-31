#!/usr/bin/env python3
"""阶段 1G 遗留引用只读检查器。

本工具只读取仓库源码和说明文件，不导入生产模块、不读取本机 .env、不创建数据库，
也不连接 AnythingLLM 或其他后台服务。它把真实执行引用、测试执行引用、禁止项字符串、
配置/脚本引用、当前说明和历史审计引用分别报告，避免把简单文本命中误当成删除证据。
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Iterable, Sequence


_SCHEMA_VERSION = 1
_EXIT_INVENTORY_INCOMPLETE = 3


@dataclass(frozen=True)
class CandidateSpec:
    """一个需要由 1G 持续跟踪的遗留对象。"""

    candidate_id: str
    modules: tuple[str, ...]
    paths: tuple[str, ...]
    symbols: tuple[str, ...]
    deferred_stage: str


@dataclass(frozen=True)
class ReferenceFinding:
    """一条已经分类、可稳定排序的引用事实。"""

    candidate_id: str
    category: str
    path: str
    line: int
    reference_kind: str
    target: str

    def to_dict(self) -> dict[str, object]:
        return {
            "candidateId": self.candidate_id,
            "category": self.category,
            "path": self.path,
            "line": self.line,
            "referenceKind": self.reference_kind,
            "target": self.target,
        }


CANDIDATES: tuple[CandidateSpec, ...] = (
    CandidateSpec(
        candidate_id="analysis_legacy_executor",
        modules=("app.services.llm_service.analysis_service",),
        paths=("app/services/llm_service/analysis_service.py",),
        symbols=("run_file_analysis_task", "run_file_analysis_batch_task"),
        deferred_stage="1G-5A",
    ),
    CandidateSpec(
        candidate_id="analysis_legacy_recall",
        modules=("app.services.llm_service.architecture_recall_service",),
        paths=("app/services/llm_service/architecture_recall_service.py",),
        symbols=(),
        deferred_stage="1G-5A",
    ),
    CandidateSpec(
        candidate_id="report_legacy_executor",
        modules=("app.services.llm_service.report_service",),
        paths=("app/services/llm_service/report_service.py",),
        symbols=("run_report_task",),
        deferred_stage="1G-5A",
    ),
    CandidateSpec(
        candidate_id="weaponry_legacy_executor",
        modules=("app.services.llm_service.weaponry_service",),
        paths=("app/services/llm_service/weaponry_service.py",),
        symbols=("run_weaponry_task",),
        deferred_stage="1G-5A",
    ),
    CandidateSpec(
        candidate_id="translation_legacy_service",
        modules=("app.services.llm_service.translation_service",),
        paths=("app/services/llm_service/translation_service.py",),
        symbols=("LLMTranslationService", "get_translation_service"),
        deferred_stage="1G-5A",
    ),
    CandidateSpec(
        candidate_id="translator_legacy_package",
        modules=("app.services.translator",),
        paths=("app/services/translator",),
        symbols=("DocumentTranslator", "MarkdownHandler", "MHTMLHandler"),
        deferred_stage="1G-5B",
    ),
    CandidateSpec(
        candidate_id="document_processing_legacy_facades",
        modules=(
            "app.services.utils.mhtml_normalizer",
            "app.services.utils.ocr_preprocessor",
            "app.services.translator.mhtml2pdf",
            "app.services.translator.MinerUConverter",
        ),
        paths=(
            "app/services/utils/mhtml_normalizer.py",
            "app/services/utils/ocr_preprocessor.py",
            "app/services/translator/mhtml2pdf.py",
            "app/services/translator/MinerUConverter.py",
        ),
        symbols=(),
        deferred_stage="1G-5B",
    ),
    CandidateSpec(
        candidate_id="debug_preview_legacy_facades",
        modules=(
            "app.services.utils.callback_preview",
            "app.services.utils.chat_debug_preview",
        ),
        paths=(
            "app/services/utils/callback_preview.py",
            "app/services/utils/chat_debug_preview.py",
        ),
        symbols=("load_callback_preview", "load_chat_debug_bootstrap"),
        deferred_stage="1G-5C",
    ),
    CandidateSpec(
        candidate_id="anythingllm_legacy_wrapper",
        modules=(
            "app.services.utils.anythingllm_client",
            "app.services.utils.rag_pipeline",
        ),
        paths=(
            "app/services/utils/anythingllm_client.py",
            "app/services/utils/rag_pipeline.py",
        ),
        symbols=("AnythingLLMClient", "run_anythingllm_rag"),
        deferred_stage="1G-5D",
    ),
    CandidateSpec(
        candidate_id="task_service_compatibility_methods",
        # 这是“方法级”候选，而不是整个现役 task_service 模块的删除候选。
        # 如果登记模块名或现役文件路径，所有正常导入 LLMTaskService 的代码，或仅仅
        # 保留 task_service.py 本体，都会被误报为遗留实现仍存在。
        modules=(),
        paths=(),
        symbols=(
            "create_file_tasks_if_available",
            "create_file_task",
            "replay_callback_if_needed",
        ),
        deferred_stage="1G-5E",
    ),
    CandidateSpec(
        candidate_id="database_reassign_compatibility_method",
        # database.py 仍是现役基础设施；这里只盘点旧兼容方法本身的定义与调用点，
        # 不能因保留该文件就把整个 DatabaseService 判定为兼容源码。
        modules=(),
        paths=(),
        symbols=("update_document_architecture",),
        deferred_stage="1G-5E",
    ),
)


def _is_module_match(target: str, module: str) -> bool:
    return target == module or target.startswith(f"{module}.")


def _candidate_for_text(value: str) -> tuple[tuple[CandidateSpec, str], ...]:
    """返回文本中出现的候选模块或唯一符号，不输出文本正文。"""

    matches: list[tuple[CandidateSpec, str]] = []
    for candidate in CANDIDATES:
        for module in candidate.modules:
            if module in value:
                matches.append((candidate, module))
        for symbol in candidate.symbols:
            # 唯一符号必须按完整 Python 标识符匹配。例如旧类
            # ``AnythingLLMClient`` 不能把现行 ``AnythingLLMClients``、
            # ``AnythingLLMClientFactory`` 误报成遗留聚合 Client 引用。
            if re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])",
                value,
            ):
                matches.append((candidate, symbol))
    return tuple(matches)


def _candidate_for_module(target: str) -> tuple[CandidateSpec, ...]:
    return tuple(
        candidate
        for candidate in CANDIDATES
        if any(_is_module_match(target, module) for module in candidate.modules)
    )


def _candidate_for_symbol(symbol: str) -> tuple[CandidateSpec, ...]:
    return tuple(
        candidate for candidate in CANDIDATES if symbol in candidate.symbols
    )


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _source_module(path: Path, root: Path) -> str:
    """把仓库内 Python 路径转换为绝对模块名。"""

    relative = path.resolve().relative_to(root.resolve())
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from_module(node: ast.ImportFrom, *, path: Path, root: Path) -> str:
    """解析 ``from`` 导入，避免相对导入绕过遗留引用盘点。

    ``from .x`` 留在当前包，``from ..x`` 向上一级。无法越过仓库包根时返回
    能够确定的最短模块名；候选匹配仍采用精确模块前缀，不会把任意同名文件误报。
    """

    if node.level == 0:
        return node.module or ""
    source_module = _source_module(path, root)
    package_parts = source_module.split(".") if source_module else []
    if path.name != "__init__.py" and package_parts:
        package_parts.pop()
    ascend_count = node.level - 1
    if ascend_count > len(package_parts):
        base_parts: list[str] = []
    elif ascend_count:
        base_parts = package_parts[:-ascend_count]
    else:
        base_parts = package_parts
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(part for part in base_parts if part)


def _is_candidate_source(relative_path: str) -> bool:
    for candidate in CANDIDATES:
        for candidate_path in candidate.paths:
            normalized = candidate_path.rstrip("/")
            if relative_path == normalized or relative_path.startswith(
                f"{normalized}/"
            ):
                return True
    return False


def _category_for_python(
    *,
    relative_path: str,
    reference_kind: str,
) -> str:
    if (
        relative_path == "scripts/inspect_stage1g_references.py"
        and reference_kind == "historical_string"
    ):
        # Manifest 必须包含被检查模块和符号的字面量；它属于检查器配置，不是运行时
        # 动态导入或调用。若检查器真的 import/patch 候选，仍会落入 script_execution。
        return "manifest_definition"
    if relative_path.startswith("tests/"):
        if reference_kind in {"guard_string", "historical_string"}:
            return "test_guard_string"
        return "test_execution"
    if relative_path.startswith("scripts/"):
        return "script_execution"
    if relative_path.startswith("app/") and _is_candidate_source(relative_path):
        return "compatibility_source"
    if relative_path.startswith("app/") or relative_path in {
        "run.py",
        "clean.py",
    }:
        return "production_runtime"
    return "script_execution"


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_guard_string(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    """识别仅用于 assertNotIn/禁止项集合的字符串，避免误报为执行引用。"""

    current: ast.AST | None = node
    for _ in range(5):
        if current is None:
            return False
        parent = parents.get(current)
        if isinstance(parent, ast.Call):
            call_name = _call_name(parent)
            if call_name in {
                "assertNotIn",
                "assertNotEqual",
                "assertFalse",
                "assertRaises",
            }:
                return True
        current = parent
    return False


def _finding(
    *,
    candidate: CandidateSpec,
    relative_path: str,
    line: int,
    reference_kind: str,
    target: str,
) -> ReferenceFinding:
    return ReferenceFinding(
        candidate_id=candidate.candidate_id,
        category=_category_for_python(
            relative_path=relative_path,
            reference_kind=reference_kind,
        ),
        path=relative_path,
        line=max(int(line), 1),
        reference_kind=reference_kind,
        target=target,
    )


def _scan_python_file(
    path: Path,
    *,
    root: Path,
) -> tuple[tuple[ReferenceFinding, ...], tuple[dict[str, object], ...]]:
    """解析一个 Python 文件并返回引用和无法静态解析的动态导入。"""

    relative_path = _relative(path, root)
    # 仓库中仍有少量历史 Python 文件带 UTF-8 BOM。使用 utf-8-sig 只在读取时
    # 去掉 BOM，不修改源文件；否则 ast.parse 会把 U+FEFF 视为非法字符并误报盘点失败。
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=relative_path)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    findings: set[ReferenceFinding] = set()
    unknown_dynamic: list[dict[str, object]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for candidate in _candidate_for_module(alias.name):
                    findings.add(
                        _finding(
                            candidate=candidate,
                            relative_path=relative_path,
                            line=node.lineno,
                            reference_kind="import",
                            target=alias.name,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_from_module(node, path=path, root=root)
            targets = [module]
            targets.extend(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
            for target in targets:
                for candidate in _candidate_for_module(target):
                    findings.add(
                        _finding(
                            candidate=candidate,
                            relative_path=relative_path,
                            line=node.lineno,
                            reference_kind="import",
                            target=target,
                        )
                    )
        elif isinstance(node, ast.Call):
            call_name = _call_name(node)
            if call_name in {"import_module", "__import__"}:
                imported = _literal_string(node.args[0] if node.args else None)
                if imported is None:
                    unknown_dynamic.append(
                        {
                            "path": relative_path,
                            "line": int(getattr(node, "lineno", 1)),
                            "call": call_name,
                        }
                    )
                else:
                    for candidate in _candidate_for_module(imported):
                        findings.add(
                            _finding(
                                candidate=candidate,
                                relative_path=relative_path,
                                line=node.lineno,
                                reference_kind="dynamic_import",
                                target=imported,
                            )
                        )
            if call_name == "patch" and node.args:
                patched = _literal_string(node.args[0])
                if patched is not None:
                    for candidate, matched in _candidate_for_text(patched):
                        findings.add(
                            _finding(
                                candidate=candidate,
                                relative_path=relative_path,
                                line=node.lineno,
                                reference_kind="patch",
                                target=matched,
                            )
                        )
            if call_name == "object" and len(node.args) >= 2:
                attribute = _literal_string(node.args[1])
                if attribute is not None:
                    for candidate in _candidate_for_symbol(attribute):
                        findings.add(
                            _finding(
                                candidate=candidate,
                                relative_path=relative_path,
                                line=node.lineno,
                                reference_kind="patch_object",
                                target=attribute,
                            )
                        )
        elif isinstance(node, ast.Attribute):
            for candidate in _candidate_for_symbol(node.attr):
                findings.add(
                    _finding(
                        candidate=candidate,
                        relative_path=relative_path,
                        line=node.lineno,
                        reference_kind="attribute",
                        target=node.attr,
                    )
                )
        elif isinstance(node, ast.Name):
            for candidate in _candidate_for_symbol(node.id):
                findings.add(
                    _finding(
                        candidate=candidate,
                        relative_path=relative_path,
                        line=node.lineno,
                        reference_kind="name",
                        target=node.id,
                    )
                )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            matches = _candidate_for_text(node.value)
            if not matches:
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and _call_name(parent) in {
                "patch",
                "import_module",
                "__import__",
            }:
                continue
            kind = "guard_string" if _is_guard_string(node, parents) else "historical_string"
            for candidate, matched in matches:
                findings.add(
                    _finding(
                        candidate=candidate,
                        relative_path=relative_path,
                        line=node.lineno,
                        reference_kind=kind,
                        target=matched,
                    )
                )

    return (
        tuple(
            sorted(
                findings,
                key=lambda item: (
                    item.candidate_id,
                    item.category,
                    item.path,
                    item.line,
                    item.reference_kind,
                    item.target,
                ),
            )
        ),
        tuple(unknown_dynamic),
    )


def _iter_python_files(root: Path) -> tuple[Path, ...]:
    discovered: set[Path] = set()
    for relative in ("app", "tests", "scripts"):
        base = root / relative
        if base.is_dir():
            discovered.update(path for path in base.rglob("*.py") if path.is_file())
    discovered.update(path for path in root.glob("*.py") if path.is_file())
    return tuple(sorted(discovered, key=lambda item: item.as_posix()))


def _iter_text_files(root: Path) -> tuple[Path, ...]:
    """发现说明和配置文件，显式排除本机 .env 与运行目录。"""

    allowed_suffixes = {
        ".md",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".ps1",
        ".sh",
        ".bat",
        ".example",
    }
    discovered: set[Path] = set()
    extensionless_configuration_names = {
        "Containerfile",
        "Dockerfile",
        "Makefile",
        "Procfile",
    }
    for relative in (
        "app",
        "tests",
        "scripts",
        "docs",
        "docker",
        "config",
        "configs",
        "deploy",
        "deployment",
        "k8s",
        ".github",
    ):
        base = root / relative
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix == ".py":
                continue
            if path.name == ".env":
                continue
            if (
                path.suffix.lower() in allowed_suffixes
                or path.name.startswith(".env.")
                or path.name in extensionless_configuration_names
            ):
                discovered.add(path)
    # 根目录同样可能承载 CI、容器和部署选择配置。只读取已知文本后缀与明确的
    # extensionless 配置名，避免把数据库、图片或运行产物误当文本解析。
    for path in root.iterdir():
        if not path.is_file() or path.name == ".env" or path.suffix == ".py":
            continue
        if (
            path.suffix.lower() in allowed_suffixes
            or path.name.startswith(".env.")
            or path.name in extensionless_configuration_names
        ):
            discovered.add(path)
    return tuple(sorted(discovered, key=lambda item: item.as_posix()))


def _text_category(relative_path: str) -> str:
    if relative_path.startswith(("tests/contracts/", "tests/assets/")):
        # 冻结契约和阶段资产保存的是历史/禁止项字面量，不是当前调用说明。
        # 将其单列为 Manifest，既保留审计事实，也避免物理删除后被旧快照反向阻塞。
        return "manifest_definition"
    if relative_path.startswith("docs/"):
        if (
            relative_path.startswith("docs/接口文档/")
            or relative_path == "docs/重构记录/README.md"
            or relative_path.startswith("docs/重构记录/260801-阶段1G")
        ):
            return "current_documentation"
        return "historical_documentation"
    if relative_path == "README.md" or (
        relative_path.endswith("/README.md")
        or relative_path.endswith("/readme.md")
    ):
        return "current_documentation"
    if relative_path.startswith("scripts/") or relative_path.startswith("docker/"):
        return "script_or_configuration"
    return "current_documentation"


def _scan_text_file(path: Path, *, root: Path) -> tuple[ReferenceFinding, ...]:
    relative_path = _relative(path, root)
    # 进入扫描集合的文件都应是仓库文本资产；解码失败必须让盘点失败，不能静默
    # 返回零引用并继续声称 inventoryComplete=true。
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    findings: set[ReferenceFinding] = set()
    for line_number, line in enumerate(lines, start=1):
        for candidate, matched in _candidate_for_text(line):
            findings.add(
                ReferenceFinding(
                    candidate_id=candidate.candidate_id,
                    category=_text_category(relative_path),
                    path=relative_path,
                    line=line_number,
                    reference_kind="text",
                    target=matched,
                )
            )
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                item.candidate_id,
                item.category,
                item.path,
                item.line,
                item.target,
            ),
        )
    )


def _summary(
    findings: Sequence[ReferenceFinding],
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {
        candidate.candidate_id: {} for candidate in CANDIDATES
    }
    for finding in findings:
        candidate_counts = result[finding.candidate_id]
        candidate_counts[finding.category] = (
            candidate_counts.get(finding.category, 0) + 1
        )
    return result


def _candidate_definition_findings(root: Path) -> tuple[ReferenceFinding, ...]:
    """把候选实现自身的存在记录为兼容源码事实。

    如果只统计 Import/Call，一个仅定义但无人调用的旧文件会错误显示为零引用。定义事实
    保证文件实际删除前 deletionReady 始终为 false，同时不把定义误写成生产调用。
    """

    findings: list[ReferenceFinding] = []
    for candidate in CANDIDATES:
        for candidate_path in candidate.paths:
            path = root / candidate_path
            if not path.exists():
                continue
            if path.is_dir() and not any(
                child.is_file() and "__pycache__" not in child.parts
                for child in path.rglob("*")
            ):
                # compileall 或历史解释器运行可能留下被忽略的 __pycache__；它不代表
                # 候选源码仍存在，不能使已经删除的目录永远无法达到 deletionReady。
                continue
            findings.append(
                ReferenceFinding(
                    candidate_id=candidate.candidate_id,
                    category="compatibility_source",
                    path=candidate_path,
                    line=1,
                    reference_kind="candidate_definition",
                    target=candidate_path,
                )
            )
    return tuple(findings)


def inspect_stage1g_references(root: Path) -> dict[str, object]:
    """扫描仓库并返回稳定、无绝对路径的引用报告。"""

    resolved_root = root.resolve()
    if not (resolved_root / "app").is_dir() or not (
        resolved_root / "tests"
    ).is_dir():
        raise ValueError("检查根目录必须包含 app/ 和 tests/")

    findings: list[ReferenceFinding] = list(
        _candidate_definition_findings(resolved_root)
    )
    unknown_dynamic: list[dict[str, object]] = []
    for path in _iter_python_files(resolved_root):
        file_findings, file_unknown = _scan_python_file(path, root=resolved_root)
        findings.extend(file_findings)
        unknown_dynamic.extend(file_unknown)
    for path in _iter_text_files(resolved_root):
        findings.extend(_scan_text_file(path, root=resolved_root))

    unique_findings = tuple(
        sorted(
            set(findings),
            key=lambda item: (
                item.candidate_id,
                item.category,
                item.path,
                item.line,
                item.reference_kind,
                item.target,
            ),
        )
    )
    counts = _summary(unique_findings)
    blocking_categories = {
        "production_runtime",
        "compatibility_source",
        "test_execution",
        "script_execution",
        "script_or_configuration",
        "current_documentation",
    }
    candidates = []
    for candidate in CANDIDATES:
        candidate_counts = counts[candidate.candidate_id]
        blocking_count = sum(
            count
            for category, count in candidate_counts.items()
            if category in blocking_categories
        )
        candidates.append(
            {
                "candidateId": candidate.candidate_id,
                "modules": list(candidate.modules),
                "paths": list(candidate.paths),
                "symbols": list(candidate.symbols),
                "deferredStage": candidate.deferred_stage,
                "counts": dict(sorted(candidate_counts.items())),
                "blockingReferenceCount": blocking_count,
                "deletionReady": blocking_count == 0
                and not unknown_dynamic,
            }
        )

    return {
        "schemaVersion": _SCHEMA_VERSION,
        "inventoryComplete": not unknown_dynamic,
        "pythonFileCount": len(_iter_python_files(resolved_root)),
        "textFileCount": len(_iter_text_files(resolved_root)),
        "candidateCount": len(CANDIDATES),
        "findingCount": len(unique_findings),
        "unknownDynamicImports": sorted(
            unknown_dynamic,
            key=lambda item: (
                str(item["path"]),
                int(item["line"]),
                str(item["call"]),
            ),
        ),
        "candidates": candidates,
        "findings": [finding.to_dict() for finding in unique_findings],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读盘点阶段 1G 遗留实现的运行、测试、脚本和文档引用"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="仓库根目录，默认使用脚本所在仓库",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="输出格式，默认 text；JSON 始终只写 stdout",
    )
    return parser


def _write_text(report: dict[str, object]) -> None:
    lines = [
        "阶段1G引用盘点: "
        f"complete={report['inventoryComplete']} "
        f"candidates={report['candidateCount']} "
        f"findings={report['findingCount']} "
        f"python_files={report['pythonFileCount']} "
        f"text_files={report['textFileCount']}"
    ]
    for candidate in report["candidates"]:
        assert isinstance(candidate, dict)
        counts = candidate["counts"]
        assert isinstance(counts, dict)
        compact_counts = ",".join(
            f"{name}={count}" for name, count in sorted(counts.items())
        )
        lines.append(
            f"{candidate['candidateId']}: "
            f"deletion_ready={candidate['deletionReady']} "
            f"blocking={candidate['blockingReferenceCount']} "
            f"counts={compact_counts or 'none'}"
        )
    sys.stdout.write("\n".join(lines) + "\n")
    for item in report["unknownDynamicImports"]:
        assert isinstance(item, dict)
        sys.stderr.write(
            "未解析动态导入: "
            f"path={item['path']} line={item['line']} call={item['call']}\n"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = inspect_stage1g_references(args.root)
    except (OSError, SyntaxError, UnicodeError, ValueError) as exc:
        sys.stderr.write(
            f"阶段1G引用盘点失败: error_type={type(exc).__name__}\n"
        )
        return 1

    if args.format == "json":
        json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True)
        sys.stdout.write("\n")
    else:
        _write_text(report)
    return 0 if report["inventoryComplete"] else _EXIT_INVENTORY_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
