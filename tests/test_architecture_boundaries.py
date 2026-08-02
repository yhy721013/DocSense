"""阶段 1A-2：模块骨架与依赖方向架构测试。

这些测试只静态读取源码，不导入 ``app`` 中的生产模块，因此不会构造容器、初始化
SQLite、连接 AnythingLLM 或启动 ``run.py``。除扫描当前仓库外，自测用例还会在
临时目录注入违规导入，证明每条门禁确实可以失败，而不是因骨架暂时为空自然通过。
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.architecture.import_rules import (
    APPLICATION_RULE,
    DOMAIN_RULE,
    FRAMEWORK_FREE_CONTAINER_RULE,
    PORTS_RULE,
    PRESENTER_RULE,
    TASKS_MODULE_RULE,
    WEB_ROUTE_RULE,
    ArchitectureRule,
    ImportViolation,
    collect_violations,
    collect_forbidden_web_route_operations,
    describe_violations,
)


ROOT = Path(__file__).resolve().parents[1]
MODULES_ROOT = ROOT / "app" / "modules"
TASKS_ROOT = MODULES_ROOT / "tasks"
REPORT_ROOT = MODULES_ROOT / "report"
WEAPONRY_ROOT = MODULES_ROOT / "weaponry"
REASSIGN_ROOT = MODULES_ROOT / "reassign"
ANALYSIS_ROOT = MODULES_ROOT / "analysis"
CHAT_ROOT = MODULES_ROOT / "chat"
ANALYSIS_APPLICATION_FACADE_PATH = ANALYSIS_ROOT / "application" / "run_analysis.py"
ANALYSIS_APPLICATION_COLLABORATOR_PATHS = {
    "models": ANALYSIS_ROOT / "application" / "workflow_models.py",
    "model_workflow": ANALYSIS_ROOT / "application" / "model_workflow.py",
    "audit_lifecycle": ANALYSIS_ROOT / "application" / "audit_lifecycle.py",
    "knowledge_handoff": ANALYSIS_ROOT / "application" / "knowledge_handoff.py",
    "failure_convergence": ANALYSIS_ROOT / "application" / "failure_convergence.py",
}
PRESENTERS_ROOT = ROOT / "app" / "presenters"
LLM_ROUTE_PATH = ROOT / "app" / "blueprints" / "llm.py"
DEBUG_ROUTE_PATH = ROOT / "app" / "blueprints" / "debug.py"
CONTAINER_PATH = ROOT / "app" / "container.py"
REASSIGN_COMPOSITION_PATH = REASSIGN_ROOT / "composition.py"
REASSIGN_RECOVERY_COMPATIBILITY_PATH = (
    REASSIGN_ROOT / "application" / "recover_reassignment.py"
)
REASSIGN_RECOVERY_FACADE_PATH = REASSIGN_ROOT / "application" / "recovery_facade.py"
REASSIGN_RECOVERY_COLLABORATOR_PATHS = {
    "observer": REASSIGN_ROOT / "application" / "recovery_observer.py",
    "checkpoints": REASSIGN_ROOT / "application" / "recovery_checkpoints.py",
    "compensator": REASSIGN_ROOT / "application" / "recovery_compensator.py",
    "finalizer": REASSIGN_ROOT / "application" / "recovery_finalizer.py",
}
# 1E-7 拆分后的基线：Facade 只负责编排分支，具体 Port 算法必须留在四个协作器中。数值
# 来自首次拆分完成的源码 AST；后续功能若让该文件反向膨胀，应先重新评审职责归属。
# 1E 全面审查修复后，Facade 新增“从恢复命令入口锚定远端预算”的纯编排代码；四个协作器
# 的最小 Port 依赖和 callback-wrapper 禁令仍由下方独立门禁锁定，因此只更新精确规模基线。
REASSIGN_RECOVERY_FACADE_MAX_LINES = 746
REASSIGN_RECOVERY_FACADE_MAX_COMPLEXITY = 24
# 1F-3S 等价拆分后的硬上限：外观只保留公开入口和跨协作器的顶层顺序，具体 Prompt、
# 领域分类、结果映射及条件写算法不得重新回迁。该数值保留合理的未来编排余量，不以
# 当前行数作为紧绷阈值，避免无关注释或诊断日志造成误报。
ANALYSIS_APPLICATION_FACADE_MAX_LINES = 700
CONTAINER_PATH = ROOT / "app" / "container.py"


def _method_cyclomatic_complexity(node: ast.FunctionDef) -> int:
    """计算足以识别恢复 Facade 分支反弹的保守圈复杂度近似值。"""

    decision_nodes = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ExceptHandler,
        ast.IfExp,
    )
    decisions = sum(
        1 for child in ast.walk(node) if isinstance(child, decision_nodes)
    )
    decisions += sum(
        max(len(child.values) - 1, 0)
        for child in ast.walk(node)
        if isinstance(child, ast.BoolOp)
    )
    return decisions + 1


def _is_single_private_forwarder(node: ast.FunctionDef) -> bool:
    """识别 ``return self._callback(...)`` 形式的协作器空转发方法。"""

    statements = [
        statement
        for statement in node.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Return):
        return False
    returned = statements[0].value
    if not isinstance(returned, ast.Call) or not isinstance(returned.func, ast.Attribute):
        return False
    receiver = returned.func.value
    return isinstance(receiver, ast.Name) and receiver.id == "self" and returned.func.attr.startswith("_")


def _module_layer_dirs(layer_name: str) -> tuple[Path, ...]:
    """返回所有业务模块中指定分层目录，避免规则只绑定 tasks 一个模块。"""

    return tuple(
        sorted(
            (
                module_dir / layer_name
                for module_dir in MODULES_ROOT.iterdir()
                if module_dir.is_dir() and (module_dir / layer_name).is_dir()
            ),
            key=lambda path: path.as_posix(),
        )
    )


class CurrentArchitectureBoundaryTests(unittest.TestCase):
    """对当前工作树执行长期生效的架构门禁。"""

    def assert_rule_clean(
        self,
        paths: tuple[Path, ...],
        rule: ArchitectureRule,
    ) -> None:
        violations = collect_violations(paths, project_root=ROOT, rule=rule)
        self.assertFalse(
            violations,
            "发现架构边界违规:\n"
            + describe_violations(violations, project_root=ROOT),
        )

    def test_module_package_skeletons_are_complete(self) -> None:
        """已建立的模块分层必须具备包标识和职责文档，不能留下无说明空目录。"""

        required_directories = (
            MODULES_ROOT,
            TASKS_ROOT,
            TASKS_ROOT / "domain",
            TASKS_ROOT / "application",
            TASKS_ROOT / "ports",
            TASKS_ROOT / "adapters",
            REPORT_ROOT,
            REPORT_ROOT / "domain",
            REPORT_ROOT / "application",
            REPORT_ROOT / "ports",
            WEAPONRY_ROOT,
            WEAPONRY_ROOT / "domain",
            WEAPONRY_ROOT / "application",
            WEAPONRY_ROOT / "ports",
            WEAPONRY_ROOT / "adapters",
            REASSIGN_ROOT,
            REASSIGN_ROOT / "domain",
            REASSIGN_ROOT / "application",
            REASSIGN_ROOT / "ports",
            REASSIGN_ROOT / "adapters",
            ANALYSIS_ROOT,
            ANALYSIS_ROOT / "domain",
            ANALYSIS_ROOT / "application",
            ANALYSIS_ROOT / "ports",
            ANALYSIS_ROOT / "adapters",
            CHAT_ROOT,
            CHAT_ROOT / "domain",
            CHAT_ROOT / "application",
            CHAT_ROOT / "ports",
            CHAT_ROOT / "adapters",
            ROOT / "app" / "adapters",
            ROOT / "app" / "adapters" / "web",
            ROOT / "app" / "adapters" / "web" / "flask",
        )
        for directory in required_directories:
            with self.subTest(directory=directory.relative_to(ROOT)):
                self.assertTrue((directory / "__init__.py").is_file())
                self.assertTrue((directory / "README.md").is_file())

    def test_all_module_domain_layers_are_framework_and_infrastructure_free(self) -> None:
        self.assert_rule_clean(_module_layer_dirs("domain"), DOMAIN_RULE)

    def test_all_module_ports_remain_abstract(self) -> None:
        self.assert_rule_clean(_module_layer_dirs("ports"), PORTS_RULE)

    def test_all_module_application_layers_depend_inward(self) -> None:
        self.assert_rule_clean(_module_layer_dirs("application"), APPLICATION_RULE)

    def test_tasks_module_does_not_reach_chat_persistence_or_foreign_modules(self) -> None:
        self.assert_rule_clean((TASKS_ROOT,), TASKS_MODULE_RULE)

    def test_presenters_do_not_read_database_or_anythingllm_client(self) -> None:
        self.assert_rule_clean((PRESENTERS_ROOT,), PRESENTER_RULE)

    def test_web_routes_do_not_import_infrastructure_implementations(self) -> None:
        """1G-0 永久冻结正式路由和 Debug 路由的基础设施导入边界。"""

        self.assert_rule_clean(
            (LLM_ROUTE_PATH, DEBUG_ROUTE_PATH),
            WEB_ROUTE_RULE,
        )

    def test_application_container_is_independent_from_web_frameworks(self) -> None:
        """1G-2 永久禁止框架依赖重新进入生产组合根。"""

        self.assert_rule_clean((CONTAINER_PATH,), FRAMEWORK_FREE_CONTAINER_RULE)

    def test_formal_routes_do_not_construct_infrastructure_or_open_files(self) -> None:
        """1G-3 锁定 AST 导入规则无法识别的直接调用与 daemon 调度。"""

        for route_path in (LLM_ROUTE_PATH, DEBUG_ROUTE_PATH):
            with self.subTest(route=route_path.name):
                violations = collect_forbidden_web_route_operations(
                    route_path.read_text(encoding="utf-8")
                )
                self.assertEqual((), violations)

    def test_reassign_route_and_composition_root_keep_saga_boundaries(self) -> None:
        """长期锁定 1E-6 的薄路由和唯一 Application 组合根边界。

        该测试只解析源码，不导入生产模块，因此不会构造 Container、SQLite 或 AnythingLLM
        Client。它防止后续为“方便”把 SQL、供应商客户端或终态收口重新塞回公开路由/Container。
        """

        route_source = LLM_ROUTE_PATH.read_text(encoding="utf-8")
        route_tree = ast.parse(route_source, filename=str(LLM_ROUTE_PATH))
        route = next(
            node
            for node in route_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "llm_reassign"
        )
        route_text = ast.get_source_segment(route_source, route) or ""
        for forbidden in (
            "AnythingLLMClient",
            "SQLiteReassignmentRepository",
            "update_document_architecture",
            "update_embeddings",
            "update_embeddings_batch",
            "threading.Thread",
            "sqlite3",
        ):
            with self.subTest(route_forbidden=forbidden):
                self.assertNotIn(forbidden, route_text)
        for required in (
            "parse_reassign_request",
            "document_reassignment.execute",
            "presenter.present_bad_request",
            "presenter.present_result",
        ):
            with self.subTest(route_required=required):
                self.assertIn(required, route_text)

        composition_source = REASSIGN_COMPOSITION_PATH.read_text(encoding="utf-8")
        composition_tree = ast.parse(
            composition_source,
            filename=str(REASSIGN_COMPOSITION_PATH),
        )
        composition_function = next(
            node
            for node in composition_tree.body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "compose_reassign_application_services"
            )
        )
        composition_text = (
            ast.get_source_segment(composition_source, composition_function) or ""
        )
        for required in (
            "DocumentReassignmentService(",
            "RecoverReassignmentOperation(",
            "ReassignApplicationServices(",
        ):
            with self.subTest(composition_required=required):
                self.assertIn(required, composition_text)

        services_class = next(
            node
            for node in composition_tree.body
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ReassignApplicationServices"
            )
        )
        public_fields = {
            node.target.id
            for node in services_class.body
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
            )
        }
        self.assertEqual(
            {"document_reassignment", "recovery"},
            public_fields,
            "Application 外观不得公开 Repository、Port Bundle 或基础设施配置",
        )

        container_source = CONTAINER_PATH.read_text(encoding="utf-8")
        container_tree = ast.parse(container_source, filename=str(CONTAINER_PATH))
        container_function = next(
            node
            for node in container_tree.body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "create_application_services"
            )
        )
        container_text = (
            ast.get_source_segment(container_source, container_function) or ""
        )
        self.assertIn("compose_reassign_application_services(", container_text)
        for forbidden in (
            "DocumentReassignmentService(",
            "RecoverReassignmentOperation(",
            "transition_operation(",
            "finalize_recovery_operation(",
        ):
            with self.subTest(container_forbidden=forbidden):
                self.assertNotIn(forbidden, container_text)

    def test_reassign_recovery_facade_delegates_to_four_internal_collaborators(self) -> None:
        """Facade 只能装配协作器并选择流程，兼容模块只保留既有导入路径。"""

        source = REASSIGN_RECOVERY_FACADE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(REASSIGN_RECOVERY_FACADE_PATH))
        recovery_class = next(
            node
            for node in tree.body
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "RecoverReassignmentOperation"
            )
        )
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in recovery_class.body
            if isinstance(node, ast.FunctionDef)
        }
        initializer = methods["__init__"]
        for collaborator in (
            "ReassignmentRecoveryObserver(",
            "ReassignmentRecoveryCheckpointReconciler(",
            "ReassignmentRecoveryCompensator(",
            "ReassignmentRecoveryFinalizer(",
        ):
            with self.subTest(collaborator=collaborator):
                self.assertIn(collaborator, initializer)

        expected_usage = {
            "recover": ("self._observer.", "self._finalizer."),
            "_recover_local_only": (
                "self._checkpoints.",
                "self._observer.",
                "self._finalizer.",
            ),
            "_recover_remote": (
                "self._observer.",
                "self._checkpoints.",
                "self._compensator.",
                "self._finalizer.",
            ),
        }
        for method_name, collaborators in expected_usage.items():
            method_text = methods[method_name]
            for collaborator in collaborators:
                with self.subTest(method=method_name, collaborator=collaborator):
                    self.assertIn(collaborator, method_text)

        compatibility_source = REASSIGN_RECOVERY_COMPATIBILITY_PATH.read_text(
            encoding="utf-8"
        )
        self.assertIn("from .recovery_facade import", compatibility_source)
        self.assertNotIn("class RecoverReassignmentOperation", compatibility_source)

    def test_reassign_recovery_collaborators_own_real_algorithms(self) -> None:
        """四个协作器必须直接调用其最小 Port，禁止退化为 Facade callback wrapper。"""

        expected_port_calls = {
            "observer": (
                "probe_local_commit_state(",
                "renew_lease(",
                "probe_workspace_reference(",
                "record_recovery_observation(",
            ),
            "checkpoints": (
                "complete_step(",
                "record_workspace_preparation_fact(",
                "decide_compensation(",
            ),
            "compensator": (
                "begin_step_mutation(",
                "transition_operation(",
                "detach_document(",
                "attach_document(",
            ),
            "finalizer": (
                "finalize_recovery_operation(",
                "transition_operation(",
            ),
        }
        forbidden_dependencies = {
            "observer": ("ReassignmentKnowledgePortFactory",),
            "checkpoints": (
                "ReassignmentKnowledgePort",
                "ReassignmentKnowledgePortFactory",
                "ReassignmentExecutionSettings",
            ),
            "compensator": (
                "ReassignmentKnowledgePortFactory",
                "ReassignmentExecutionSettings",
            ),
            "finalizer": (
                "ReassignmentKnowledgePort",
                "ReassignmentKnowledgePortFactory",
                "ReassignmentExecutionSettings",
            ),
        }
        for role, path in REASSIGN_RECOVERY_COLLABORATOR_PATHS.items():
            with self.subTest(collaborator=role):
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=str(path))
                class_node = next(
                    node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef)
                    and node.name.startswith("ReassignmentRecovery")
                )
                public_methods = [
                    node
                    for node in class_node.body
                    if isinstance(node, ast.FunctionDef)
                    and not node.name.startswith("_")
                ]
                self.assertTrue(public_methods, "协作器必须暴露可直接测试的职责方法")
                self.assertNotIn("Callable", source)
                self.assertNotIn("_callback", source)
                for method in public_methods:
                    self.assertFalse(
                        _is_single_private_forwarder(method),
                        f"{role}.{method.name} 不能只转发私有 callback",
                    )
                for expected in expected_port_calls[role]:
                    self.assertIn(expected, source)
                for forbidden in forbidden_dependencies[role]:
                    self.assertNotIn(forbidden, source)

    def test_reassign_recovery_facade_size_and_complexity_do_not_regress(self) -> None:
        """锁定 1E-7 Facade 文件规模和最高圈复杂度，防止算法重新回迁。"""

        source = REASSIGN_RECOVERY_FACADE_PATH.read_text(encoding="utf-8")
        source_lines = source.splitlines()
        self.assertLessEqual(
            len(source_lines),
            REASSIGN_RECOVERY_FACADE_MAX_LINES,
            "恢复 Facade 超出 1E-7 基线；请把具体 Port 算法下沉到对应协作器",
        )
        tree = ast.parse(source, filename=str(REASSIGN_RECOVERY_FACADE_PATH))
        facade = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "RecoverReassignmentOperation"
        )
        for method in (
            node for node in facade.body if isinstance(node, ast.FunctionDef)
        ):
            with self.subTest(method=method.name):
                self.assertLessEqual(
                    _method_cyclomatic_complexity(method),
                    REASSIGN_RECOVERY_FACADE_MAX_COMPLEXITY,
                    "恢复 Facade 分支复杂度反弹；请先评估是否应归属到独立协作器",
                )

    def test_analysis_application_facade_keeps_internal_collaborator_boundary(self) -> None:
        """锁定 1F-3S 的薄外观与五个内部协作器，防止算法回迁或形成反向依赖。"""

        source = ANALYSIS_APPLICATION_FACADE_PATH.read_text(encoding="utf-8")
        self.assertLessEqual(
            len(source.splitlines()),
            ANALYSIS_APPLICATION_FACADE_MAX_LINES,
            "文件分析 Application 外观超过 1F-3S 上限；请把具体算法下沉到内部协作器",
        )
        tree = ast.parse(source, filename=str(ANALYSIS_APPLICATION_FACADE_PATH))
        facade = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "RunAnalysisTask"
        )
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in facade.body
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(
            {"__init__", "execute", "_execute_in_rag_factory", "_execute_with_rag", "_knowledge_idempotency_key"},
            set(methods),
            "RunAnalysisTask 只能保留公开入口、Factory 作用域和顶层编排",
        )
        initializer = methods["__init__"]
        for collaborator in (
            "_AnalysisModelWorkflow()",
            "_AnalysisAuditLifecycle(audit)",
            "_AnalysisKnowledgeHandoff(knowledge, translation)",
            "_AnalysisFailureConvergence(",
        ):
            with self.subTest(collaborator=collaborator):
                self.assertIn(collaborator, initializer)

        workflow_text = methods["_execute_with_rag"]
        for collaborator in (
            "self._model_workflow.",
            "self._audit_lifecycle.",
            "self._knowledge_handoff.",
            "self._failure_convergence.",
        ):
            with self.subTest(workflow_collaborator=collaborator):
                self.assertIn(collaborator, workflow_text)

        # Prompt、分类规则和结果映射都属于模型工作流。Facade 只能调用协作器，不能为了
        # “方便”重新导入这些算法；否则会绕过 1F-3S 的职责隔离。
        for forbidden_import in (
            "app.modules.analysis.domain.prompts",
            "app.modules.analysis.domain.classification_rules",
            "app.modules.analysis.domain.result_mapping",
        ):
            with self.subTest(forbidden_import=forbidden_import):
                self.assertNotIn(forbidden_import, source)

        for role, path in ANALYSIS_APPLICATION_COLLABORATOR_PATHS.items():
            with self.subTest(collaborator=role):
                collaborator_source = path.read_text(encoding="utf-8")
                collaborator_tree = ast.parse(collaborator_source, filename=str(path))
                reverse_imports = [
                    node.module or ""
                    for node in ast.walk(collaborator_tree)
                    if isinstance(node, ast.ImportFrom)
                ]
                self.assertFalse(
                    any(module.endswith("run_analysis") for module in reverse_imports),
                    "内部协作器不得反向导入 Facade，以免形成循环依赖",
                )

    def test_analysis_application_collaborators_keep_real_port_algorithms(self) -> None:
        """协作器必须直接拥有职责算法，不能退化成 Facade 的空 callback 转发。"""

        expected_port_calls = {
            "model_workflow": ("rag.execute(", "map_analysis_result("),
            "audit_lifecycle": (
                "self._audit.reserve_recall(",
                "self._audit.persist_interaction(",
                "rag.close_session(",
            ),
            "knowledge_handoff": (
                "self._knowledge.persist(",
                "self._translation.translate(",
            ),
            "failure_convergence": (
                "self._task_commands.update_progress_if_current(",
                "self._task_commands.finish_if_current(",
                "self._progress_publisher.publish_guarded(",
            ),
        }
        for role, expected_calls in expected_port_calls.items():
            path = ANALYSIS_APPLICATION_COLLABORATOR_PATHS[role]
            source = path.read_text(encoding="utf-8")
            with self.subTest(collaborator=role):
                self.assertNotIn("from .run_analysis import", source)
                for expected_call in expected_calls:
                    with self.subTest(port_call=expected_call):
                        self.assertIn(expected_call, source)

    def test_route_tests_do_not_construct_uninjected_production_applications(self) -> None:
        """只有显式生命周期用例可以调用无参 create_app，其他测试必须注入离线容器。"""

        # 仅生产容器所有权测试可以刻意构造无参应用工厂：一项验证正常启动和退出钩子，
        # 另一项验证启动失败后的补偿关闭。白名单使用精确文件名和方法名，防止范围扩散。
        allowed = {
            (
                "test_dependency_container.py",
                "test_production_owned_container_starts_once_and_registers_close",
            ),
            (
                "test_dependency_container.py",
                "test_production_start_failure_closes_owned_container_without_atexit",
            ),
        }
        violations: list[str] = []
        for source_path in sorted((ROOT / "tests").glob("test_*.py")):
            # 仓库少量历史测试仍带 UTF-8 BOM；AST 门禁应兼容读取，而不是要求本轮
            # 顺带机械改写无关文件编码。
            source = source_path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    if not isinstance(child.func, ast.Name):
                        continue
                    if child.func.id != "create_app":
                        continue
                    has_services = any(
                        keyword.arg == "services" for keyword in child.keywords
                    )
                    if has_services:
                        continue
                    identity = (source_path.name, node.name)
                    if identity not in allowed:
                        violations.append(
                            f"{source_path.name}:{child.lineno} {node.name}"
                        )
        self.assertEqual(
            [],
            violations,
            "路由测试禁止无参 create_app()，请注入完全离线 ApplicationServices",
        )


class ArchitectureRuleSelfTests(unittest.TestCase):
    """用临时源码验证规则本身能识别真实违规。"""

    def _scan_source(
        self,
        relative_path: str,
        source_text: str,
        rule: ArchitectureRule,
    ) -> tuple[ImportViolation, ...]:
        with tempfile.TemporaryDirectory(prefix="docsense-architecture-") as temp_dir:
            project_root = Path(temp_dir)
            source_path = project_root / relative_path
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(
                textwrap.dedent(source_text).lstrip(),
                encoding="utf-8",
            )
            return collect_violations(
                (source_path,),
                project_root=project_root,
                rule=rule,
            )

    @staticmethod
    def _targets(violations: tuple[ImportViolation, ...]) -> set[str]:
        return {violation.target for violation in violations}

    def test_domain_rule_rejects_web_database_and_http_libraries(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/domain/models.py",
            """
            from flask import request
            import sqlite3
            import requests
            """,
            DOMAIN_RULE,
        )
        self.assertEqual({"flask", "sqlite3", "requests"}, self._targets(violations))

    def test_ports_rule_rejects_reverse_application_dependency(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/ports/task_read.py",
            """
            from app.modules.tasks.application import CheckTaskStatusService
            import sqlalchemy
            """,
            PORTS_RULE,
        )
        self.assertEqual(
            {"app.modules.tasks.application", "sqlalchemy"},
            self._targets(violations),
        )

    def test_application_rule_rejects_flask_legacy_service_and_relative_adapter(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/application/check_status.py",
            """
            from flask import Blueprint, current_app, request
            from app.services.llm_service.task_service import LLMTaskService
            from ..adapters import legacy_task_service
            """,
            APPLICATION_RULE,
        )
        self.assertEqual(
            {
                "flask",
                "app.services.llm_service.task_service",
                "app.modules.tasks.adapters",
            },
            self._targets(violations),
        )

    def test_report_layers_can_only_reuse_tasks_public_control_plane(self) -> None:
        """Report 可使用 TaskId/Port，但不能穿透到通用任务具体 Adapter。"""

        allowed_ports = self._scan_source(
            "app/modules/report/ports/dispatcher.py",
            "from app.modules.tasks.domain import TaskId\n",
            PORTS_RULE,
        )
        allowed_application = self._scan_source(
            "app/modules/report/application/run_report.py",
            "from app.modules.tasks.ports import TaskCommandPort\n",
            APPLICATION_RULE,
        )
        rejected = self._scan_source(
            "app/modules/report/application/run_report.py",
            "from app.modules.tasks.adapters import LegacyTaskReadAdapter\n",
            APPLICATION_RULE,
        )

        self.assertEqual((), allowed_ports)
        self.assertEqual((), allowed_application)
        self.assertIn("app.modules.tasks.adapters", self._targets(rejected))

    def test_tasks_rule_rejects_chat_persistence_and_any_foreign_business_layer(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/adapters/legacy_task_service.py",
            """
            from app.modules.chat.adapters.sqlite import ChatStore
            from app.modules.report.adapters.mysql import ReportRepository
            from app.modules.weaponry.domain import WeaponryTask
            """,
            TASKS_MODULE_RULE,
        )
        self.assertEqual(
            {
                "app.modules.chat.adapters.sqlite",
                "app.modules.report.adapters.mysql",
                "app.modules.weaponry.domain",
            },
            self._targets(violations),
        )

    def test_positive_allowlists_reject_unlisted_client_libraries(self) -> None:
        cases = (
            (
                "app/modules/tasks/domain/models.py",
                "import httpx\nimport redis\n",
                DOMAIN_RULE,
                {"httpx", "redis"},
            ),
            (
                "app/modules/tasks/ports/task_read.py",
                "import pika\nimport minio\n",
                PORTS_RULE,
                {"pika", "minio"},
            ),
            (
                "app/modules/tasks/application/check_status.py",
                "import boto3\nimport os\n",
                APPLICATION_RULE,
                {"boto3", "os"},
            ),
        )
        for relative_path, source_text, rule, expected in cases:
            with self.subTest(path=relative_path, rule=rule.name):
                self.assertEqual(
                    expected,
                    self._targets(self._scan_source(relative_path, source_text, rule)),
                )

    def test_dynamic_imports_cannot_bypass_protected_layers(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/application/check_status.py",
            "client = __import__('httpx')\n",
            APPLICATION_RULE,
        )

        self.assertEqual(
            {"<dynamic-import>:httpx"},
            self._targets(violations),
        )

    def test_importlib_alias_is_detected_as_dynamic_import(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/domain/models.py",
            """
            import importlib as loader
            client = loader.import_module("redis")
            """,
            DOMAIN_RULE,
        )

        self.assertEqual(
            {"importlib", "<dynamic-import>:redis"},
            self._targets(violations),
        )

    def test_presenter_rule_rejects_database_and_anythingllm_dependencies(self) -> None:
        violations = self._scan_source(
            "app/presenters/task_status.py",
            """
            from app.services.core.database import DatabaseService
            from app.integrations.anythingllm.transport import AnythingLLMTransport
            """,
            PRESENTER_RULE,
        )
        self.assertEqual(
            {
                "app.services.core.database",
                "app.integrations.anythingllm.transport",
            },
            self._targets(violations),
        )

    def test_web_route_rule_rejects_database_thread_and_business_adapters(self) -> None:
        violations = self._scan_source(
            "app/blueprints/example.py",
            """
            import sqlite3
            import threading
            from app.services.core.database import DatabaseService
            from app.integrations.anythingllm.transport import AnythingLLMTransport
            from app.modules.report.adapters import SQLiteReportCallbackAdapter
            from app.modules.debug.composition import compose_debug_services
            """,
            WEB_ROUTE_RULE,
        )

        self.assertEqual(
            {
                "sqlite3",
                "threading",
                "app.services.core.database",
                "app.integrations.anythingllm.transport",
                "app.modules.report.adapters",
                "app.modules.debug.composition",
            },
            self._targets(violations),
        )

    def test_framework_free_container_rule_rejects_flask_and_fastapi(self) -> None:
        violations = self._scan_source(
            "app/container.py",
            """
            from flask import current_app
            from fastapi import Request
            """,
            FRAMEWORK_FREE_CONTAINER_RULE,
        )
        self.assertEqual({"flask", "fastapi"}, self._targets(violations))

    def test_formal_route_operation_rule_rejects_file_thread_and_repository(self) -> None:
        source = """
        worker = Thread(target=run, daemon=True)
        worker.daemon = True
        payload = open("payload.json")
        path = Path("payload.json")
        repository = TaskRepository()
        client = AnythingLLMClient()
        services = compose_debug_services()
        """
        violations = collect_forbidden_web_route_operations(
            textwrap.dedent(source)
        )
        self.assertEqual(
            {
                "AnythingLLMClient",
                "Path",
                "TaskRepository",
                "Thread",
                "compose_debug_services",
                "daemon",
                "open",
            },
            {item.symbol for item in violations},
        )

    def test_rules_allow_expected_inward_dependencies(self) -> None:
        allowed_cases = (
            (
                "app/modules/tasks/domain/models.py",
                "from dataclasses import dataclass\n",
                DOMAIN_RULE,
            ),
            (
                "app/modules/reassign/domain/models.py",
                "from datetime import datetime, timezone\n",
                DOMAIN_RULE,
            ),
            (
                "app/modules/tasks/ports/task_read.py",
                "from typing import Protocol\nfrom app.modules.tasks.domain import TaskSnapshot\n",
                PORTS_RULE,
            ),
            (
                "app/modules/tasks/application/check_status.py",
                "from app.modules.tasks.domain import TaskSnapshot\n"
                "from app.modules.tasks.ports import TaskReadPort\n",
                APPLICATION_RULE,
            ),
            (
                "app/modules/weaponry/application/field_execution.py",
                "import hashlib\nimport json\n",
                APPLICATION_RULE,
            ),
            (
                "app/modules/tasks/adapters/legacy_task_service.py",
                "from app.services.llm_service.task_service import LLMTaskService\n"
                "from app.modules.tasks.ports import TaskReadPort\n",
                TASKS_MODULE_RULE,
            ),
            (
                "app/presenters/task_status.py",
                "from app.modules.chat.domain.events import ChatStreamEvent\n",
                PRESENTER_RULE,
            ),
        )
        for relative_path, source_text, rule in allowed_cases:
            with self.subTest(path=relative_path, rule=rule.name):
                self.assertEqual(
                    (),
                    self._scan_source(relative_path, source_text, rule),
                )


if __name__ == "__main__":
    unittest.main()
