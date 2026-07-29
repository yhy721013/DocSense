"""基于 Python AST 的静态导入边界检查器。

架构测试不能通过真正 import 生产模块来检查依赖，否则模块级装配、数据库初始化或
远端客户端构造可能在测试收集阶段发生。本文件只读取源码并解析 ``import`` 语句，
同时解析相对导入的完整模块路径，使 ``from ..adapters import ...`` 也无法绕过门禁。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class ImportReference:
    """一条源码导入及其可匹配的完整目标。"""

    source: Path
    line: int
    statement: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class RuleMatch:
    """规则命中的具体目标和原因。"""

    target: str
    reason: str


RuleMatcher = Callable[[ImportReference], RuleMatch | None]


@dataclass(frozen=True)
class ArchitectureRule:
    """可复用的架构导入规则。"""

    name: str
    matcher: RuleMatcher


@dataclass(frozen=True)
class ImportViolation:
    """一条确定的架构违规，包含可直接定位的文件和行号。"""

    rule: str
    source: Path
    line: int
    statement: str
    target: str
    reason: str

    def describe(self, project_root: Path) -> str:
        """返回稳定、适合 unittest 失败信息的中文描述。"""

        try:
            display_path = self.source.resolve().relative_to(project_root.resolve())
        except ValueError:
            display_path = self.source
        return (
            f"{display_path}:{self.line} [{self.rule}] 禁止导入 {self.target}: "
            f"{self.reason}; statement={self.statement}"
        )


_DYNAMIC_IMPORT_PREFIX = "<dynamic-import>"

# 采用正向白名单：新增依赖必须明确进入对应集合，而不是等发现某个新客户端库后
# 再补黑名单。这样 httpx/redis/pika/minio/boto3 等替代库不能绕过分层门禁。
_DOMAIN_STDLIB_ROOTS = frozenset(
    {
        "__future__",
        "dataclasses",
        # 领域层只允许对已传入时间做格式与时区规范化，不在此读取系统时钟。
        "datetime",
        "decimal",
        "enum",
        "hashlib",
        "html",
        "json",
        "math",
        "re",
        "typing",
        "unicodedata",
    }
)
# Analysis 的额外标准库能力按文件授予，避免为了树索引缓存而让整个 Domain 都能引入
# 线程、时钟或路径对象。ranges/callback 只用 deepcopy 建立任务级不可变快照；
# result_mapping 的 pathlib 只允许做纯词法文件名解析，I/O 调用由独立 AST 门禁禁止。
_ANALYSIS_DOMAIN_FILE_STDLIB_ROOTS = {
    "architecture_recall.py": frozenset(
        {"collections", "threading", "time"}
    ),
    "architecture_tree.py": frozenset(
        {"collections", "concurrent", "threading", "types"}
    ),
    "callback_payloads.py": frozenset({"copy"}),
    "ranges.py": frozenset({"copy"}),
    "result_mapping.py": frozenset({"pathlib"}),
}
# 1H 的共享文档处理包含两类经评审的纯标准库例外：
# - MHTML 领域规则使用 ``email`` 解析不可变 MIME 字节，不接触文件或网络；
# - Legacy Office 兼容 DTO 暂时保留 Path 和幂等 cleanup lease，直到三类引用清零后
#   随旧 Python 兼容签名一起删除。例外按“模块 + 文件”精确授予，不能扩散到整个 Domain。
_DOCUMENT_PROCESSING_DOMAIN_FILE_STDLIB_ROOTS = {
    "legacy_office.py": frozenset({"logging", "pathlib", "threading"}),
    "mhtml.py": frozenset({"email"}),
}
_TRANSLATION_DOMAIN_FILE_STDLIB_ROOTS = {
    # 该文件是从旧 Translator 搬迁的纯分块规则；日志只记录计数/错误类型，不执行 I/O。
    "chunks.py": frozenset({"logging"}),
}
_PORTS_STDLIB_ROOTS = frozenset(
    {"__future__", "dataclasses", "enum", "typing"}
)
# Port 中的例外同样精确到文件。``legacy_office.py`` 保留迁移期路径签名；
# ``processing.py`` 的锁只保护候选 scratch cleanup callback，不是任务执行锁。
_DOCUMENT_PROCESSING_PORT_FILE_STDLIB_ROOTS = {
    "legacy_office.py": frozenset({"pathlib"}),
    "processing.py": frozenset({"threading"}),
}
_APPLICATION_STDLIB_ROOTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        # Application 只使用稳定摘要和确定性 JSON 记录审计事实；两者均不执行
        # 文件、网络或供应商 I/O，因此不会突破框架无关边界。
        "hashlib",
        "json",
        "logging",
        "threading",
        "time",
        "typing",
        "uuid",
    }
)
_PRESENTER_STDLIB_ROOTS = frozenset(
    {"__future__", "dataclasses", "enum", "json", "logging", "typing"}
)


def _iter_python_files(paths: Iterable[Path]) -> tuple[Path, ...]:
    """展开文件/目录并返回去重后的确定性 Python 文件序列。"""

    discovered: dict[Path, None] = {}
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.exists():
            raise FileNotFoundError(f"架构检查路径不存在: {raw_path}")
        if path.is_file():
            if path.suffix == ".py":
                discovered[path] = None
            continue
        for source in path.rglob("*.py"):
            discovered[source.resolve()] = None
    return tuple(sorted(discovered, key=lambda item: item.as_posix()))


def _source_module(path: Path, project_root: Path) -> str:
    """把源码路径转换为 Python 模块名，供相对导入解析使用。"""

    relative = path.resolve().relative_to(project_root.resolve())
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from_module(
    node: ast.ImportFrom,
    *,
    source_path: Path,
    project_root: Path,
) -> str:
    """解析 ``from`` 导入的绝对模块路径。"""

    if node.level == 0:
        return node.module or ""

    source_module = _source_module(source_path, project_root)
    package_parts = source_module.split(".") if source_module else []
    if source_path.name != "__init__.py" and package_parts:
        package_parts.pop()

    # ``from .x`` 留在当前包，``from ..x`` 向上一级，因此上移层数为 level - 1。
    ascend_count = node.level - 1
    if ascend_count > len(package_parts):
        base_parts: list[str] = []
    elif ascend_count:
        base_parts = package_parts[:-ascend_count]
    else:
        base_parts = package_parts

    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _normalize_statement(source_text: str, node: ast.AST) -> str:
    """压缩多行导入，避免失败信息难以阅读。"""

    segment = ast.get_source_segment(source_text, node) or type(node).__name__
    return " ".join(segment.split())


def parse_imports(path: Path, *, project_root: Path) -> tuple[ImportReference, ...]:
    """解析一个源码文件中的全部绝对和相对导入。"""

    source_text = path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(path))
    references: list[ImportReference] = []
    importlib_aliases = {"importlib"}
    import_module_aliases: set[str] = set()

    # 先解析 importlib 的别名，防止 ``import importlib as x`` 绕过动态导入检查。
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            references.append(
                ImportReference(
                    source=path,
                    line=node.lineno,
                    statement=_normalize_statement(source_text, node),
                    targets=tuple(alias.name for alias in node.names),
                )
            )
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        base_module = _resolve_from_module(
            node,
            source_path=path,
            project_root=project_root,
        )
        targets: list[str] = []
        if base_module:
            targets.append(base_module)
        for alias in node.names:
            if alias.name == "*":
                continue
            target = f"{base_module}.{alias.name}" if base_module else alias.name
            targets.append(target)
        references.append(
            ImportReference(
                source=path,
                line=node.lineno,
                statement=_normalize_statement(source_text, node),
                targets=tuple(dict.fromkeys(targets)),
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dynamic = False
        if isinstance(node.func, ast.Name):
            dynamic = (
                node.func.id == "__import__"
                or node.func.id in import_module_aliases
            )
        elif isinstance(node.func, ast.Attribute):
            dynamic = (
                node.func.attr == "import_module"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_aliases
            )
        if not dynamic:
            continue
        requested = ""
        if node.args and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[0].value, str):
                requested = node.args[0].value.strip()
        target = (
            f"{_DYNAMIC_IMPORT_PREFIX}:{requested}"
            if requested
            else _DYNAMIC_IMPORT_PREFIX
        )
        references.append(
            ImportReference(
                source=path,
                line=node.lineno,
                statement=_normalize_statement(source_text, node),
                targets=(target,),
            )
        )

    return tuple(sorted(references, key=lambda item: (item.line, item.statement)))


def collect_violations(
    paths: Iterable[Path],
    *,
    project_root: Path,
    rule: ArchitectureRule,
) -> tuple[ImportViolation, ...]:
    """对指定源码应用单条规则，并返回确定性违规列表。"""

    violations: list[ImportViolation] = []
    for source in _iter_python_files(paths):
        for reference in parse_imports(source, project_root=project_root):
            match = rule.matcher(reference)
            if match is None:
                continue
            violations.append(
                ImportViolation(
                    rule=rule.name,
                    source=reference.source,
                    line=reference.line,
                    statement=reference.statement,
                    target=match.target,
                    reason=match.reason,
                )
            )
    return tuple(violations)


def describe_violations(
    violations: Iterable[ImportViolation],
    *,
    project_root: Path,
) -> str:
    """把多条违规格式化为一段可扫描的失败信息。"""

    return "\n".join(
        violation.describe(project_root)
        for violation in violations
    )


def _first_prefix_match(
    reference: ImportReference,
    prefixes: tuple[str, ...],
) -> str | None:
    for target in reference.targets:
        for prefix in prefixes:
            if target == prefix or target.startswith(f"{prefix}."):
                return target
    return None


def _module_context(source: Path) -> tuple[str, str] | None:
    """从任意绝对路径中识别 ``app/modules/<module>/<layer>``。"""

    parts = source.parts
    for index in range(len(parts) - 3):
        if parts[index : index + 2] != ("app", "modules"):
            continue
        return parts[index + 2], parts[index + 3]
    return None


def _matches_prefix(target: str, prefix: str) -> bool:
    return target == prefix or target.startswith(f"{prefix}.")


def _first_positive_allowlist_violation(
    reference: ImportReference,
    *,
    allowed_stdlib_roots: frozenset[str],
    allowed_internal_prefixes: tuple[str, ...],
    reason: str,
) -> RuleMatch | None:
    """返回第一项未获明确许可的依赖；动态导入始终拒绝。"""

    for target in reference.targets:
        if target.startswith(_DYNAMIC_IMPORT_PREFIX):
            return RuleMatch(target, "受保护分层禁止动态导入，避免绕过静态依赖门禁")
        root = target.split(".", 1)[0]
        if root in allowed_stdlib_roots:
            continue
        if any(_matches_prefix(target, prefix) for prefix in allowed_internal_prefixes):
            continue
        return RuleMatch(target, reason)
    return None


def _domain_matcher(reference: ImportReference) -> RuleMatch | None:
    context = _module_context(reference.source)
    module_name = context[0] if context is not None else ""
    allowed_stdlib_roots = _DOMAIN_STDLIB_ROOTS
    if module_name == "analysis":
        allowed_stdlib_roots = (
            _DOMAIN_STDLIB_ROOTS
            | _ANALYSIS_DOMAIN_FILE_STDLIB_ROOTS.get(
                reference.source.name,
                frozenset(),
            )
        )
    elif module_name == "document_processing":
        allowed_stdlib_roots = (
            _DOMAIN_STDLIB_ROOTS
            | _DOCUMENT_PROCESSING_DOMAIN_FILE_STDLIB_ROOTS.get(
                reference.source.name,
                frozenset(),
            )
        )
    elif module_name == "translation":
        allowed_stdlib_roots = (
            _DOMAIN_STDLIB_ROOTS
            | _TRANSLATION_DOMAIN_FILE_STDLIB_ROOTS.get(
                reference.source.name,
                frozenset(),
            )
        )
    allowed_internal = [f"app.modules.{module_name}.domain"]
    if module_name in {"document_processing", "translation"}:
        # TaskId 是跨业务共享的稳定控制面值对象；只放行 tasks domain。
        allowed_internal.append("app.modules.tasks.domain")
    if module_name == "translation":
        # Translation 只消费 prepared Artifact 值对象；独立 1H 门禁会永久禁止
        # 其接触 DocumentProcessing Application/Adapter 或格式转换实现。
        allowed_internal.append("app.modules.document_processing.domain")
    return _first_positive_allowlist_violation(
        reference,
        allowed_stdlib_roots=allowed_stdlib_roots,
        allowed_internal_prefixes=tuple(allowed_internal),
        reason="领域层只能依赖批准的标准库和本业务模块领域类型",
    )


def _ports_matcher(reference: ImportReference) -> RuleMatch | None:
    context = _module_context(reference.source)
    module_name = context[0] if context is not None else ""
    allowed_internal = [
        f"app.modules.{module_name}.domain",
        f"app.modules.{module_name}.ports",
    ]
    if module_name != "tasks":
        # TaskId/TaskBusinessRef 是跨业务 Port 可以复用的稳定控制面值对象；这里只放行
        # tasks domain，不放行 tasks application/adapters 或其他业务模块。
        allowed_internal.append("app.modules.tasks.domain")
    if module_name in {"analysis", "translation"}:
        # 两个消费者只共享不透明 ArtifactRef/表示类型，不获得 Processor 或路径能力。
        allowed_internal.append("app.modules.document_processing.domain")
    allowed_stdlib_roots = _PORTS_STDLIB_ROOTS
    if module_name == "document_processing":
        allowed_stdlib_roots = (
            _PORTS_STDLIB_ROOTS
            | _DOCUMENT_PROCESSING_PORT_FILE_STDLIB_ROOTS.get(
                reference.source.name,
                frozenset(),
            )
        )
    return _first_positive_allowlist_violation(
        reference,
        allowed_stdlib_roots=allowed_stdlib_roots,
        allowed_internal_prefixes=tuple(allowed_internal),
        reason="端口只能依赖批准的标准库、本模块领域类型和其他抽象端口",
    )


def _application_matcher(reference: ImportReference) -> RuleMatch | None:
    context = _module_context(reference.source)
    module_name = context[0] if context is not None else ""
    allowed_internal = [
        f"app.modules.{module_name}.domain",
        f"app.modules.{module_name}.ports",
        f"app.modules.{module_name}.application",
    ]
    if module_name != "tasks":
        # 各业务 Application 可以使用通用 TaskId、TaskCommand 和 Progress Port，
        # 但不能反向接触 tasks 的 Application/Adapter 实现。
        allowed_internal.extend(
            ("app.modules.tasks.domain", "app.modules.tasks.ports")
        )
    return _first_positive_allowlist_violation(
        reference,
        allowed_stdlib_roots=_APPLICATION_STDLIB_ROOTS,
        allowed_internal_prefixes=tuple(allowed_internal),
        reason="应用层只能依赖批准的标准库、本模块领域类型、端口和应用组件",
    )


def _tasks_module_matcher(reference: ImportReference) -> RuleMatch | None:
    for target in reference.targets:
        if target.startswith(_DYNAMIC_IMPORT_PREFIX):
            return RuleMatch(target, "tasks 模块禁止动态导入以防止隔离规则被绕过")
    target = _first_prefix_match(reference, ("app.services.chat.persistence",))
    if target:
        return RuleMatch(target, "tasks 不拥有 chat 持久化数据，禁止直接依赖其实现")

    for candidate in reference.targets:
        parts = candidate.split(".")
        if len(parts) < 4 or parts[:2] != ["app", "modules"]:
            continue
        module_name = parts[2]
        if module_name != "tasks":
            return RuleMatch(candidate, "tasks 不能直接导入其他业务模块的任何分层")
    return None


def _presenter_matcher(reference: ImportReference) -> RuleMatch | None:
    allowed_internal: list[str] = ["app.services.chat.domain"]
    for target in reference.targets:
        parts = target.split(".")
        if len(parts) >= 4 and parts[:2] == ["app", "modules"]:
            if parts[3] in {"domain", "application"}:
                allowed_internal.append(".".join(parts[:4]))
    return _first_positive_allowlist_violation(
        reference,
        allowed_stdlib_roots=_PRESENTER_STDLIB_ROOTS,
        allowed_internal_prefixes=tuple(dict.fromkeys(allowed_internal)),
        reason="Presenter 只能依赖批准的标准库、领域类型和应用结果，不得接触具体实现",
    )


DOMAIN_RULE = ArchitectureRule("module-domain-purity", _domain_matcher)
PORTS_RULE = ArchitectureRule("module-ports-abstraction", _ports_matcher)
APPLICATION_RULE = ArchitectureRule("module-application-direction", _application_matcher)
TASKS_MODULE_RULE = ArchitectureRule("tasks-module-isolation", _tasks_module_matcher)
PRESENTER_RULE = ArchitectureRule("presenter-infrastructure-isolation", _presenter_matcher)


__all__ = [
    "APPLICATION_RULE",
    "ArchitectureRule",
    "DOMAIN_RULE",
    "ImportReference",
    "ImportViolation",
    "PORTS_RULE",
    "PRESENTER_RULE",
    "RuleMatch",
    "TASKS_MODULE_RULE",
    "collect_violations",
    "describe_violations",
    "parse_imports",
]
