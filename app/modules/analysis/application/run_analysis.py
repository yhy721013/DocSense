"""只按内部 ``TaskId`` 执行一次文件分析的框架无关 Application 外观。

本模块不导入 Flask、SQLite、旧 ``analysis_service``、HTTP 客户端或文件系统实现。它只
保留公开结果类型、依赖装配和一次调用的顶层编排；具体模型规则、审计、知识库与失败
收敛分别位于内部协作器，生产组合根和旧路由仍未切换到此用例。
"""

from __future__ import annotations

import logging
import time

from app.modules.tasks.domain import TaskExecutionSnapshot, TaskId
from app.modules.tasks.ports import (
    GuardedProgressPublisherPort,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskCommandPort,
)

from app.modules.analysis.domain.task_inputs import (
    AnalysisDocumentProcessingPolicySnapshot,
    AnalysisTaskInputV1,
    AnalysisTaskInputV2,
    AnalysisTaskInputV3,
)
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
    AnalysisAuditPort,
    AnalysisCallbackPort,
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisKnowledgePort,
    AnalysisRagPort,
    AnalysisRagPortFactory,
    AnalysisResourceActivityPort,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenRequest,
    AnalysisRagUploadDescriptor,
    AnalysisResourcePort,
    AnalysisTaskWorkspacePort,
    AnalysisTranslationPort,
    FilePreparationPort,
    PreparedAnalysisDocument,
)

from .audit_lifecycle import _AnalysisAuditLifecycle
from .failure_convergence import _AnalysisFailureConvergence
from .knowledge_handoff import _AnalysisKnowledgeHandoff
from .model_workflow import _AnalysisModelWorkflow
from .recover_resources import AnalysisResourceLifecycle
from .workflow_models import (
    AnalysisApplicationContractError,
    AnalysisTaskCompletion,
    AnalysisTaskPersistenceError,
    RunAnalysisOutcome,
    RunAnalysisResult,
    _AnalysisWorkflowPlan,
    _RagWorkflowState,
    _build_rag_upload_descriptor,
)


# 保持拆分前的日志分类，避免日志采集和既有检索规则因模块路径变化而失效。
logger = logging.getLogger(__name__)


_PROGRESS_DOWNLOADING = (0.15, "正在下载文件")
_PROGRESS_PARSING = (0.35, "正在执行文档解析")
_PROGRESS_TRANSLATING = (0.65, "正在翻译文档")
_PROGRESS_CALLBACK_READY = (0.95, "翻译完成，准备回调")


class RunAnalysisTask:
    """文件分析 Worker 编排用例。

    用例只接收 ``TaskId``；所有状态推进都使用 expected TaskId，通知通过 Guarded Progress
    复核 latest owner。Resource/Callback 均是可选的内部注入点：1F-6 可以离线验证完整
    闭环，但 1F-5B 前不会由 Flask 路由或生产容器接线。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[object, AnalysisTaskInputV1, AnalysisTaskCompletion],
        progress_publisher: GuardedProgressPublisherPort,
        workspaces: AnalysisTaskWorkspacePort,
        files: FilePreparationPort,
        rag_factory: AnalysisRagPortFactory,
        knowledge: AnalysisKnowledgePort,
        audit: AnalysisAuditPort,
        translation: AnalysisTranslationPort,
        resources: AnalysisResourcePort | None = None,
        callbacks: AnalysisCallbackPort | None = None,
        callback_url: str = "",
        resource_close_running_grace_seconds: float = 300.0,
        resource_activity: AnalysisResourceActivityPort | None = None,
    ) -> None:
        if not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(progress_publisher, GuardedProgressPublisherPort):
            raise TypeError("progress_publisher 必须实现 GuardedProgressPublisherPort")
        if not isinstance(workspaces, AnalysisTaskWorkspacePort):
            raise TypeError("workspaces 必须实现 AnalysisTaskWorkspacePort")
        if not isinstance(files, FilePreparationPort):
            raise TypeError("files 必须实现 FilePreparationPort")
        if not isinstance(rag_factory, AnalysisRagPortFactory):
            raise TypeError("rag_factory 必须实现 AnalysisRagPortFactory")
        if not isinstance(knowledge, AnalysisKnowledgePort):
            raise TypeError("knowledge 必须实现 AnalysisKnowledgePort")
        if not isinstance(audit, AnalysisAuditPort):
            raise TypeError("audit 必须实现 AnalysisAuditPort")
        if not isinstance(translation, AnalysisTranslationPort):
            raise TypeError("translation 必须实现 AnalysisTranslationPort")
        if resources is not None and not isinstance(resources, AnalysisResourcePort):
            raise TypeError("resources 必须实现 AnalysisResourcePort 或为 None")
        if callbacks is not None and not isinstance(callbacks, AnalysisCallbackPort):
            raise TypeError("callbacks 必须实现 AnalysisCallbackPort 或为 None")
        if not isinstance(callback_url, str):
            raise TypeError("callback_url 必须是 str")
        if callbacks is None and callback_url.strip():
            raise ValueError("未注入 callbacks 时不得配置 callback_url")
        if resource_activity is not None and not isinstance(
            resource_activity,
            AnalysisResourceActivityPort,
        ):
            raise TypeError(
                "resource_activity 必须实现 "
                "AnalysisResourceActivityPort 或为 None"
            )
        if (
            isinstance(resource_close_running_grace_seconds, bool)
            or not isinstance(
                resource_close_running_grace_seconds,
                (int, float),
            )
        ):
            raise TypeError(
                "resource_close_running_grace_seconds 必须是正有限数字"
            )
        normalized_close_grace_seconds = float(
            resource_close_running_grace_seconds
        )
        if (
            normalized_close_grace_seconds != normalized_close_grace_seconds
            or normalized_close_grace_seconds
            in (float("inf"), float("-inf"))
            or normalized_close_grace_seconds <= 0.0
            or normalized_close_grace_seconds > 7 * 24 * 60 * 60
        ):
            raise ValueError(
                "resource_close_running_grace_seconds 必须是 0~604800 的正有限数字"
            )

        # 每个 Application 实例只保存其注入 Port 和无状态协作器；单次执行状态始终在
        # ``execute`` 内新建，不能被另一个 TaskId、线程或未来 Worker 复用。
        self._task_commands = task_commands
        self._workspaces = workspaces
        self._files = files
        self._rag_factory = rag_factory
        self._resources = resources
        self._resource_close_running_grace_seconds = (
            normalized_close_grace_seconds
        )
        self._resource_activity = resource_activity
        self._model_workflow = _AnalysisModelWorkflow()
        self._audit_lifecycle = _AnalysisAuditLifecycle(audit)
        self._knowledge_handoff = _AnalysisKnowledgeHandoff(knowledge, translation)
        self._failure_convergence = _AnalysisFailureConvergence(
            task_commands=task_commands,
            progress_publisher=progress_publisher,
            audit_lifecycle=self._audit_lifecycle,
            callbacks=callbacks,
            callback_url=callback_url,
        )

    def execute(self, task_id: TaskId) -> RunAnalysisResult:
        """读取、领取并执行一个文件分析任务；不从 Flask request 或运行时配置取输入。"""

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        loaded = self._task_commands.get_execution(task_id)
        if loaded is None:
            logger.warning("文件分析执行不存在，跳过派发: task_id=%s", task_id)
            return RunAnalysisResult(task_id, RunAnalysisOutcome.MISSING)
        self._failure_convergence.validate_execution(loaded, task_id)

        claim = self._task_commands.claim(task_id)
        if not isinstance(claim, TaskClaimResult):
            raise AnalysisApplicationContractError("TaskCommandPort.claim 返回类型错误")
        if claim.outcome is TaskClaimOutcome.MISSING:
            return RunAnalysisResult(task_id, RunAnalysisOutcome.MISSING)
        if claim.outcome is not TaskClaimOutcome.CLAIMED:
            logger.info(
                "文件分析任务未取得执行权，幂等跳过: task_id=%s outcome=%s",
                task_id,
                claim.outcome.value,
            )
            return RunAnalysisResult(task_id, RunAnalysisOutcome.NOT_CLAIMED)
        if not isinstance(claim.execution, TaskExecutionSnapshot):
            raise AnalysisApplicationContractError("claimed 结果缺少执行快照")
        snapshot = self._failure_convergence.validate_execution(claim.execution, task_id)
        if claim.execution.execution_state != "running":
            raise AnalysisApplicationContractError("claimed 执行快照必须处于 running")
        execution = AnalysisExecutionRef(
            task_id=task_id,
            file_name=snapshot.file_name,
            batch_id=snapshot.batch_id,
            batch_sequence=snapshot.batch_sequence,
        )
        state = _RagWorkflowState()
        resource_lifecycle = (
            AnalysisResourceLifecycle(
                store=self._resources,
                execution=execution,
                close_running_grace_seconds=(
                    self._resource_close_running_grace_seconds
                ),
                resource_activity=self._resource_activity,
            )
            if self._resources is not None
            else None
        )
        if resource_lifecycle is not None:
            # 资源协作器只绑定当前 execution 的局部状态。Application 实例未来可被多个
            # Worker 调用，但绝不能在实例字段缓存某个任务的 Session 或资源版本。
            state.resource_checkpoint = resource_lifecycle.checkpoint_rag_state
        started_at = time.perf_counter()
        resource_handed_off_to_factory = False

        try:
            # 第一次 expected TaskId 条件写既是进度事实，也是创建任何本地任务目录前的
            # owner 门禁。这样重复派发或已经 stale 的 execution 不会遗留空工作目录。
            if not self._failure_convergence.update_progress(
                claim.execution,
                *_PROGRESS_DOWNLOADING,
            ):
                return RunAnalysisResult(task_id, RunAnalysisOutcome.STALE)
            workspace = self._workspaces.create(execution)
            if workspace.execution != execution:
                raise AnalysisApplicationContractError("任务目录不属于当前 execution")
            prepared = self._files.prepare(
                AnalysisFilePreparationRequest(
                    execution=execution,
                    source_url=snapshot.file_path,
                    task_root=workspace.root_path,
                    document_processing_policy=(
                        snapshot.document_processing_policy
                        if isinstance(snapshot, AnalysisTaskInputV2)
                        else AnalysisDocumentProcessingPolicySnapshot.for_source(
                            snapshot.file_path,
                            business_file_name=snapshot.file_name,
                        )
                    ),
                )
            )
            self._failure_convergence.require_prepared_document(prepared, execution)
            upload_descriptor = _build_rag_upload_descriptor(
                snapshot=snapshot,
                prepared=prepared,
            )
            state.upload_descriptor = upload_descriptor
            if not self._failure_convergence.update_progress(
                claim.execution,
                *_PROGRESS_PARSING,
            ):
                return RunAnalysisResult(task_id, RunAnalysisOutcome.STALE)
            plan = self._model_workflow.build_plan(
                snapshot,
                prepared.original_text,
            )
            state.recall_receipt = self._audit_lifecycle.reserve_recall(execution, plan)
            if state.recall_receipt.finalized:
                raise AnalysisApplicationContractError("当前 execution 的召回审计已终结")
            if not self._failure_convergence.is_latest(claim.execution):
                logger.info(
                    "创建 RAG 会话前发现旧 execution，停止外部调用: task_id=%s",
                    task_id,
                )
                return RunAnalysisResult(task_id, RunAnalysisOutcome.STALE)
            if resource_lifecycle is not None:
                # 召回审计是本地硬前置；资源事实必须在 RAG Factory 之前创建，确保首次
                # Context/Conversation 引用出现时可以立即通过 state checkpoint 落库。
                resource_lifecycle.register(
                    task_root=workspace.root_path,
                    source_path=prepared.source_path,
                    processing_path=prepared.processing_path,
                    upload_path=prepared.upload_path,
                    state=state,
                    upload_descriptor=upload_descriptor,
                )
                if upload_descriptor is not None:
                    state.document_upload_intent_checkpoint = (
                        resource_lifecycle.prepare_document_upload
                    )
                resource_lifecycle.record_recall_state(state)
                # 从此处开始由 Factory 作用域的 finally 负责释放活跃权；在此之前的
                # 任意 return/Exception/BaseException 均由本层 finally 兜底。
                resource_handed_off_to_factory = True
        except AnalysisTaskPersistenceError:
            # 条件写提交结果不确定时，绝不把可能成功的任务改写为失败，也不触发外部补偿。
            logger.critical(
                "文件分析任务事实写入结果不确定，停止二次终态写并保留现场: task_id=%s",
                task_id,
                exc_info=True,
            )
            raise
        except Exception as error:
            if resource_lifecycle is not None and resource_lifecycle.record is not None:
                try:
                    # 已创建资源记录后又在进入 Factory 前失败时，没有远端调用可安全补偿的
                    # 证明；先隔离该记录，避免终态 tracking 被后续恢复器误判为可清理。
                    resource_lifecycle.quarantine(
                        stage="pre_rag",
                        reason=type(error).__name__,
                    )
                except Exception:
                    logger.critical(
                        "文件分析 RAG 前失败后的资源隔离未确认，禁止自动补偿: task_id=%s",
                        execution.task_id,
                        exc_info=True,
                    )
            return self._failure_convergence.finish_pre_rag_failure(
                execution=execution,
                task_execution=claim.execution,
                snapshot=snapshot,
                state=state,
                error=error,
                started_at=started_at,
            )
        finally:
            if (
                resource_lifecycle is not None
                and not resource_handed_off_to_factory
            ):
                resource_lifecycle.finish_worker()

        return self._execute_in_rag_factory(
            task_execution=claim.execution,
            snapshot=snapshot,
            execution=execution,
            prepared=prepared,
            plan=plan,
            state=state,
            started_at=started_at,
            resources=resource_lifecycle,
            upload_descriptor=upload_descriptor,
        )

    def _execute_in_rag_factory(
        self,
        *,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        execution: AnalysisExecutionRef,
        prepared: PreparedAnalysisDocument,
        plan: _AnalysisWorkflowPlan,
        state: _RagWorkflowState,
        started_at: float,
        resources: AnalysisResourceLifecycle | None,
        upload_descriptor: AnalysisRagUploadDescriptor | None,
    ) -> RunAnalysisResult:
        """在任务级 Factory 中执行 RAG，并隔离 Transport 释放异常。

        Factory 进入失败时尚未得到可审计的 SessionRef，因此按 RAG 前失败收敛一次任务终态。
        但当内部工作流已经返回（包括成功、失败或 stale）后，``__exit__`` 的异常只能说明
        本地 Transport 释放出现问题，绝不能反向覆盖已提交的条件终态或触发第二次外部补偿。
        """

        result: RunAnalysisResult | None = None
        try:
            # 把完整 RAG 生命周期置于同一 Factory 作用域，确保业务 close 在 Transport 释放前
            # 发生；Factory 本身不持有跨任务的 Session 或客户端缓存。
            with self._rag_factory.create(execution) as rag:
                if not isinstance(rag, AnalysisRagPort):
                    raise AnalysisApplicationContractError(
                        "AnalysisRagPortFactory.create 返回类型错误"
                    )
                result = self._execute_with_rag(
                    task_execution=task_execution,
                    snapshot=snapshot,
                    execution=execution,
                    prepared=prepared,
                    plan=plan,
                    state=state,
                    rag=rag,
                    started_at=started_at,
                    resources=resources,
                    upload_descriptor=upload_descriptor,
                )
        except AnalysisTaskPersistenceError:
            # 条件写确认丢失时没有资格猜测最终状态；与普通 Factory 异常分开处理，禁止
            # ``finish_pre_rag_failure`` 产生相反方向的第二次终态写。
            logger.critical(
                "文件分析任务事实写入结果不确定，Factory 作用域内停止二次终态写: task_id=%s",
                execution.task_id,
                exc_info=True,
            )
            raise
        except Exception as error:
            if result is not None:
                # ``with`` 的 __exit__ 在工作流 return 之后运行。此时结果已经经过 expected
                # TaskId 条件写（或确认 stale）；仅记录可诊断事实，不调用审计、知识库、终态
                # 或 close 的补偿路径。
                logger.critical(
                    "文件分析 RAG Factory 退出失败，已保留既有任务结果: "
                    "task_id=%s outcome=%s error_type=%s",
                    execution.task_id,
                    result.outcome.value,
                    type(error).__name__,
                    exc_info=True,
                )
                return result
            logger.exception(
                "文件分析 RAG Factory 创建失败，按未打开会话收敛任务: "
                "task_id=%s error_type=%s",
                execution.task_id,
                type(error).__name__,
            )
            if resources is not None:
                try:
                    # Factory 创建异常缺少可审计 SessionRef，不能断言远端必然未产生副作用。
                    # 已登记的资源记录因此 fail closed 隔离，不标记 cleaned、更不自动删除。
                    resources.quarantine(
                        stage="rag_factory",
                        reason=type(error).__name__,
                    )
                except Exception:
                    logger.critical(
                        "RAG Factory 失败后的资源隔离未确认，禁止自动补偿: task_id=%s",
                        execution.task_id,
                        exc_info=True,
                    )
            return self._failure_convergence.finish_pre_rag_failure(
                execution=execution,
                task_execution=task_execution,
                snapshot=snapshot,
                state=state,
                error=error,
                started_at=started_at,
            )
        finally:
            # register 在记录可见前取得活跃权；无论成功、失败、stale、Callback 超时或
            # Factory 退出异常，都必须在本次 Worker 完全停止推进后释放。
            if resources is not None:
                resources.finish_worker()
        if result is None:
            raise AnalysisApplicationContractError("RAG Factory 未返回执行结果")
        return result

    def _execute_with_rag(
        self,
        *,
        task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
        snapshot: AnalysisTaskInputV1,
        execution: AnalysisExecutionRef,
        prepared: PreparedAnalysisDocument,
        plan: _AnalysisWorkflowPlan,
        state: _RagWorkflowState,
        rag: AnalysisRagPort,
        started_at: float,
        resources: AnalysisResourceLifecycle | None,
        upload_descriptor: AnalysisRagUploadDescriptor | None,
    ) -> RunAnalysisResult:
        """在已创建的任务级 Factory 内完成模型、审计、知识库和翻译阶段。"""

        try:
            opened = rag.open_session(
                AnalysisRagSessionOpenRequest(
                    execution=execution,
                    upload_path=prepared.upload_path,
                    upload_descriptor=upload_descriptor,
                )
            )
            if opened.session.execution != execution:
                raise AnalysisApplicationContractError("RAG 打开结果 execution 不一致")
            state.session = opened.session
            state.opened = True
            state.lifecycle_events.extend(opened.lifecycle_events)
            # 打开阶段已可能创建 Context/Conversation；不能等待模型完成后才登记。
            state.checkpoint_resource_facts()

            architecture_id, parsed_result = self._model_workflow.run_model_workflow(
                execution=execution,
                snapshot=snapshot,
                plan=plan,
                state=state,
                rag=rag,
            )
            mapped_result = self._model_workflow.map_result(
                parsed_result,
                plan,
                original_text=prepared.original_text,
                architecture_id=architecture_id,
                internal_prepared_basename=prepared.internal_prepared_basename,
            )
            returned_rank = self._model_workflow.returned_rank(plan, architecture_id)
            self._audit_lifecycle.finalize_recall_success(
                state,
                architecture_id,
                returned_rank,
                started_at,
            )
            if resources is not None:
                try:
                    resources.record_recall_state(state)
                except Exception:
                    state.preserve_scene = True
                    raise
            state.interaction_receipt = self._audit_lifecycle.persist_interaction(
                execution=execution,
                snapshot=snapshot,
                state=state,
                outcome=AnalysisAuditOutcome.SUCCEEDED,
                error_code="",
            )
            if resources is not None:
                try:
                    resources.record_interaction_receipt(state.interaction_receipt)
                except Exception:
                    state.preserve_scene = True
                    raise

            # 永久知识库写入是不可逆外部副作用，必须在完整模型审计提交后再次确认 owner。
            if not self._failure_convergence.is_latest(task_execution):
                self._failure_convergence.close_audited_session(
                    execution=execution,
                    state=state,
                    rag=rag,
                    retain_document=False,
                    resources=resources,
                )
                return RunAnalysisResult(execution.task_id, RunAnalysisOutcome.STALE)
            self._knowledge_handoff.persist_knowledge(
                execution=execution,
                snapshot=snapshot,
                plan=plan,
                state=state,
                mapped_result=mapped_result,
                on_result=(resources.record_knowledge_result if resources is not None else None),
            )
            if not self._failure_convergence.update_progress(
                task_execution,
                *_PROGRESS_TRANSLATING,
            ):
                self._failure_convergence.close_audited_session(
                    execution=execution,
                    state=state,
                    rag=rag,
                    retain_document=True,
                    resources=resources,
                )
                return RunAnalysisResult(execution.task_id, RunAnalysisOutcome.STALE)
            self._knowledge_handoff.enrich_translations(
                execution=execution,
                snapshot=snapshot,
                prepared=prepared,
                mapped_result=mapped_result,
            )
            mapped_result = self._model_workflow.sanitize_public_result(
                mapped_result,
                internal_prepared_basename=prepared.internal_prepared_basename,
                business_file_name=(
                    snapshot.original_file_name
                    if snapshot.original_file_name.strip()
                    else snapshot.file_name
                ),
            )
            if not self._failure_convergence.update_progress(
                task_execution,
                *_PROGRESS_CALLBACK_READY,
            ):
                self._failure_convergence.close_audited_session(
                    execution=execution,
                    state=state,
                    rag=rag,
                    retain_document=True,
                    resources=resources,
                )
                return RunAnalysisResult(execution.task_id, RunAnalysisOutcome.STALE)

            completed = self._failure_convergence.finish_success(
                task_execution,
                snapshot,
                mapped_result,
            )
            # 成功终态已提交后，close 失败只追加恢复证据或日志，不能改写 status=2。
            self._failure_convergence.close_audited_session(
                execution=execution,
                state=state,
                rag=rag,
                retain_document=True,
                resources=resources,
            )
            if not completed:
                return RunAnalysisResult(execution.task_id, RunAnalysisOutcome.STALE)
            logger.info(
                "文件分析任务 Application 执行完成: "
                "task_id=%s batch_id=%s batch_sequence=%d",
                execution.task_id,
                execution.batch_id,
                execution.batch_sequence,
            )
            return RunAnalysisResult(execution.task_id, RunAnalysisOutcome.SUCCEEDED)
        except AnalysisTaskPersistenceError:
            logger.critical(
                "文件分析任务事实写入结果不确定，停止二次终态写并保留现场: task_id=%s",
                execution.task_id,
                exc_info=True,
            )
            raise
        except AnalysisRagSessionOpenError as error:
            state.session = error.partial_session
            state.lifecycle_events.extend(error.lifecycle_events)
            state.preserve_scene = error.outcome_unknown
            try:
                state.checkpoint_resource_facts()
            except Exception:
                logger.critical(
                    "文件分析 RAG 打开失败后的资源事实未能保存，保留现场: task_id=%s",
                    execution.task_id,
                    exc_info=True,
                )
            return self._failure_convergence.finish_rag_failure(
                task_execution=task_execution,
                snapshot=snapshot,
                execution=execution,
                state=state,
                rag=rag,
                error=error,
                stage=f"rag_open_{error.stage.value}",
                started_at=started_at,
                resources=resources,
            )
        except Exception as error:
            return self._failure_convergence.finish_rag_failure(
                task_execution=task_execution,
                snapshot=snapshot,
                execution=execution,
                state=state,
                rag=rag,
                error=error,
                stage=self._failure_convergence.failure_stage(error),
                started_at=started_at,
                resources=resources,
            )

    @staticmethod
    def _knowledge_idempotency_key(
        *,
        file_name: str,
        architecture_id: int,
        content_sha256: str,
    ) -> str:
        """兼容既有私有测试入口，算法唯一归属到知识库协作器。"""

        return _AnalysisKnowledgeHandoff.knowledge_idempotency_key(
            file_name=file_name,
            architecture_id=architecture_id,
            content_sha256=content_sha256,
        )


__all__ = (
    "AnalysisApplicationContractError",
    "AnalysisTaskCompletion",
    "AnalysisTaskPersistenceError",
    "RunAnalysisOutcome",
    "RunAnalysisResult",
    "RunAnalysisTask",
)
