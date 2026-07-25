from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional
from uuid import uuid4

from flask import Blueprint, Response, jsonify, request
from flask_sock import Sock

from app.adapters.web import (
    ArchitectureIdValidationError,
    ReportIdValidationError,
    normalize_architecture_id,
    normalize_report_id,
)
from app.adapters.web.flask import (
    ReportRequestValidationError,
    ProgressConnectionRegistry,
    ProgressRequestValidationError,
    ReassignRequestValidationError,
    WeaponryRequestValidationError,
    parse_report_request,
    parse_progress_subscription,
    parse_reassign_request,
    parse_weaponry_request,
)
from app.container import ApplicationServices, get_application_services
from app.modules.report.domain import ReportId, ReportTaskConflictError
from app.modules.tasks.application import ProgressSubscriptionRollbackError
from app.modules.weaponry.application import WeaponryTaskConflictError
from app.modules.weaponry.ports import (
    WeaponryDocumentScopeAmbiguityError,
    WeaponryDocumentScopeError,
    WeaponryDocumentScopeNotFoundError,
)
from app.presenters.chat_stream import (
    finalize_chat_run_stream,
)
from app.presenters.task_progress import ProgressWebSocketPresenter
from app.presenters.report_submission import (
    ReportSubmissionHttpPresentation,
    ReportSubmissionResponsePresenter,
)
from app.presenters.reassign_result import (
    ReassignHttpPresentation,
    ReassignResponsePresenter,
)
from app.presenters.weaponry_submission import (
    WeaponrySubmissionHttpPresentation,
    WeaponrySubmissionResponsePresenter,
)
from app.services.chat import (
    ChatDeleteBusyError,
    ChatDeleteCleanupError,
    ChatDeleteNotFoundError,
    ChatDocumentNotFoundError,
    ChatRunBusyError,
    ChatSessionUnavailableError,
    ChatTitleEmptyHistoryError,
    ChatTitleGenerationError,
    ChatTitleUnavailableError,
)
from app.services.chat.domain.chat_id import (
    chat_id_storage_key,
    parse_query_chat_id,
    require_public_chat_id,
)
from app.services.core.architecture_tree import (
    ArchitectureTreeValidationError,
)
from app.services.core.settings import (
    CHAT_MAX_FILES_PER_REQUEST,
    CHAT_MAX_MESSAGE_CHARS,
)
from app.services.llm_service.analysis_service import (
    MAX_ANALYSIS_PARAMS_PER_REQUEST,
    MAX_ANALYSIS_REQUEST_BYTES,
    run_file_analysis_batch_task,
    run_file_analysis_task,
    validate_analysis_architecture_ranges,
)
from app.services.llm_service.task_service import (
    TaskAdmissionBusyError,
    TaskAlreadyProcessingError,
    file_task_admission_block_reason,
)


llm_bp = Blueprint("llm", __name__)
sock = Sock()

logger = logging.getLogger(__name__)

_PROGRESS_RECEIVE_POLL_SECONDS = 0.1
_PROGRESS_RELEASE_ATTEMPTS = 3


def _services() -> ApplicationServices:
    """读取当前 Flask 应用的依赖容器，禁止路由使用模块级可变服务单例。"""
    return get_application_services()


def _report_http_response(
    presentation: ReportSubmissionHttpPresentation,
) -> Response:
    """把框架无关报告展示值机械转换为 Flask Response。"""

    if not isinstance(presentation, ReportSubmissionHttpPresentation):
        raise TypeError("presentation 必须是 ReportSubmissionHttpPresentation")
    response = Response(
        response=presentation.body,
        status=presentation.status_code,
    )
    if presentation.content_type is None:
        # 202 成功体严格为零字节，也不暗示存在可解析的 JSON/文本实体。
        response.headers.pop("Content-Type", None)
    else:
        response.headers["Content-Type"] = presentation.content_type
    return response


def _weaponry_http_response(
    presentation: WeaponrySubmissionHttpPresentation,
) -> Response:
    """把框架无关武器谱展示值机械转换为 Flask Response。"""

    if not isinstance(presentation, WeaponrySubmissionHttpPresentation):
        raise TypeError("presentation 必须是 WeaponrySubmissionHttpPresentation")
    response = Response(
        response=presentation.body,
        status=presentation.status_code,
    )
    if presentation.content_type is None:
        # 已批准的成功响应必须严格为零字节，且不能暗示 JSON 实体。
        response.headers.pop("Content-Type", None)
    else:
        response.headers["Content-Type"] = presentation.content_type
    return response


def _reassign_http_response(
    presentation: ReassignHttpPresentation,
) -> Response:
    """把框架无关分类节点变更展示值机械转换为 Flask Response。"""

    if not isinstance(presentation, ReassignHttpPresentation):
        raise TypeError("presentation 必须是 ReassignHttpPresentation")
    response = Response(
        response=presentation.body,
        status=presentation.status_code,
    )
    response.headers["Content-Type"] = presentation.content_type
    return response


def _empty_http_response(status_code: int) -> Response:
    """构造不携带公开业务数据的严格空 HTTP 响应。

    文件解析、报告、武器谱提交以及 ``check-task`` 的成功响应均不能泄露内部
    ``execution_id``、回调状态或调度细节。调用方应使用既有业务键通过 Progress、
    ``check-task`` 的副作用和最终 Callback 跟踪结果，而不是依赖受理响应体。
    """

    if isinstance(status_code, bool) or not isinstance(status_code, int):
        raise TypeError("status_code 必须是 HTTP 状态整数")
    if not 100 <= status_code <= 599:
        raise ValueError("status_code 必须在 100 到 599 之间")

    response = Response(response=b"", status=status_code)
    # Flask 默认会附加 text/html Content-Type。严格空成功体不应暗示存在可解析实体。
    response.headers.pop("Content-Type", None)
    return response


def _read_json_chat_id(
    params: Dict[str, Any],
    *,
    operation: str,
) -> tuple[int, str]:
    """校验 JSON chatId，并返回公开整数与持久化层文本键。

    HTTP 边界必须严格要求 JSON number 整数，不能因内部 SQLite 当前采用文本键
    而接受字符串。内部服务仍使用规范文本键，以避免这次接口契约升级牵动表结构、
    外键和资源租约标识；公开响应由应用服务统一转换回整数。
    """

    raw_chat_id = params.get("chatId")
    try:
        public_chat_id = require_public_chat_id(raw_chat_id)
    except ValueError:
        logger.warning(
            "%s被拒绝: chatId必须为正整数 chat_id_type=%s",
            operation,
            type(raw_chat_id).__name__,
        )
        raise
    return public_chat_id, chat_id_storage_key(public_chat_id)


def _read_query_chat_id(
    raw_chat_id: Any,
    *,
    operation: str,
) -> tuple[int, str]:
    """校验 Query chatId 的规范十进制表示，并返回两种边界值。

    URL Query 在 Flask 中只能读取为字符串，因此这里要求其写成无前导零的十进制
    正整数；服务端随后转换为与 JSON 路由一致的整数和内部文本键。
    """

    try:
        public_chat_id = parse_query_chat_id(raw_chat_id)
    except ValueError:
        logger.warning(
            "%s被拒绝: chatId必须为规范正整数 query_value_type=%s",
            operation,
            type(raw_chat_id).__name__,
        )
        raise
    return public_chat_id, chat_id_storage_key(public_chat_id)


def _get_params(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    """校验 check-task 的完整参数数组，禁止静默过滤非法元素。

    已批准契约要求 ``params`` 中任一元素不是对象时拒绝整次请求。此前的过滤式
    解析会让调用方误以为无效项已被检查，且与 Progress/Report 的原子校验不一致。
    """

    params = payload.get("params")
    if not isinstance(params, list) or not params:
        return []

    validated_params: list[Dict[str, Any]] = []
    for item in params:
        if not isinstance(item, dict):
            return []
        validated_params.append(item)
    return validated_params


def _get_first_param(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    params = _get_params(payload)
    return params[0] if params else None


@llm_bp.post("/llm/analysis")
def llm_analysis():
    services = _services()
    task_service = services.task_service
    kb_service = services.kb_service
    progress_hub = services.progress_hub
    llm_config = services.llm_config
    content_length = request.content_length
    if (
        content_length is not None
        and content_length > MAX_ANALYSIS_REQUEST_BYTES
    ):
        logger.warning(
            "文件分析请求被拒绝: 请求体过大 content_length=%s limit=%s",
            content_length,
            MAX_ANALYSIS_REQUEST_BYTES,
        )
        return jsonify({"error": "请求体过大"}), 413
    try:
        if content_length is None:
            # 对 chunked/未知长度请求只读取上限加一个字节，避免为了判断超限先把
            # 无界请求体完整载入内存。标准 WSGI 未声明流已终止时，Werkzeug 会安全
            # 返回空数据，而不会等待无限输入。
            raw_body = request.stream.read(
                MAX_ANALYSIS_REQUEST_BYTES + 1
            )
            if len(raw_body) > MAX_ANALYSIS_REQUEST_BYTES:
                logger.warning(
                    "文件分析请求被拒绝: 无Content-Length请求体过大 "
                    "body_bytes>%s",
                    MAX_ANALYSIS_REQUEST_BYTES,
                )
                return jsonify({"error": "请求体过大"}), 413
            payload = json.loads(raw_body) if raw_body else {}
        else:
            payload = request.get_json(silent=True)
            if payload is None:
                payload = {}
    except json.JSONDecodeError:
        payload = {}
    except (RecursionError, UnicodeError, ValueError):
        logger.warning(
            "文件分析请求被拒绝: JSON结构无法安全解析"
        )
        return jsonify({"error": "请求JSON格式无效"}), 400
    if not isinstance(payload, dict):
        logger.warning(
            "文件分析请求被拒绝: JSON顶层不是对象 type=%s",
            type(payload).__name__,
        )
        return jsonify({"error": "请求JSON必须是对象"}), 400
    try:
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (
        TypeError,
        ValueError,
        RecursionError,
        UnicodeEncodeError,
    ):
        logger.warning(
            "文件分析请求被拒绝: JSON含非有限数值或非法Unicode"
        )
        return jsonify(
            {"error": "请求JSON包含非法数值或Unicode字符"}
        ), 400
    logger.info("收到文件分析请求: payload_keys=%s", list(payload.keys()))
    if payload.get("businessType") != "file":
        logger.warning(
            "文件分析请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为file"}), 400

    raw_params = payload.get("params")
    if not isinstance(raw_params, list) or not raw_params:
        logger.warning("文件分析请求被拒绝: params为空或格式无效")
        return jsonify({"error": "params不能为空"}), 400
    if len(raw_params) > MAX_ANALYSIS_PARAMS_PER_REQUEST:
        logger.warning(
            "文件分析请求被拒绝: params数量过多 count=%s limit=%s",
            len(raw_params),
            MAX_ANALYSIS_PARAMS_PER_REQUEST,
        )
        return jsonify(
            {
                "error": (
                    "params数量不能超过"
                    f"{MAX_ANALYSIS_PARAMS_PER_REQUEST}"
                )
            }
        ), 400
    for index, item in enumerate(raw_params):
        if not isinstance(item, dict):
            logger.warning(
                "文件分析请求被拒绝: params项不是对象 index=%s",
                index,
            )
            return jsonify(
                {"error": f"params[{index}]必须是对象"}
            ), 400
    params_list = list(raw_params)

    seen_file_names = set()
    for index, params in enumerate(params_list):
        file_name = params.get("fileName")
        if not isinstance(file_name, str) or not file_name.strip():
            logger.warning("文件分析请求被拒绝: fileName为空 index=%s", index)
            return jsonify({"error": "fileName不能为空"}), 400
        normalized_name = file_name.strip()
        if normalized_name in seen_file_names:
            logger.warning(
                "文件分析请求被拒绝: fileName重复 fileName=%s index=%s",
                normalized_name,
                index,
            )
            return jsonify({"error": "fileName不能重复"}), 400
        seen_file_names.add(normalized_name)

        file_path = params.get("filePath")
        if not isinstance(file_path, str) or not file_path.strip():
            logger.warning(
                "文件分析请求被拒绝: filePath为空 fileName=%s index=%s",
                normalized_name,
                index,
            )
            return jsonify({"error": "filePath不能为空"}), 400

        try:
            validate_analysis_architecture_ranges(params)
        except ArchitectureTreeValidationError as exc:
            logger.warning(
                "文件分析请求被拒绝: 领域树无效 index=%s error=%s",
                index,
                exc,
            )
            return jsonify(
                {"error": f"params[{index}]: {exc}"}
            ), 400

    for params in params_list:
        normalized_name = params["fileName"].strip()
        existing_task = task_service.get_task("file", normalized_name)
        block_reason = file_task_admission_block_reason(existing_task)
        if block_reason:
            error_message = (
                "上一次任务回调尚未结束"
                if block_reason == "callback_pending"
                else "任务正在处理中"
            )
            logger.warning(
                "文件分析请求被拒绝: %s fileName=%s status=%s "
                "callback_status=%s",
                error_message,
                normalized_name,
                existing_task["status"],
                existing_task["callback_status"],
            )
            return jsonify({"error": error_message}), 409

    submissions = [
        (
            params["fileName"].strip(),
            {"businessType": "file", "params": [params]},
            "1" if index == 0 else "0",
        )
        for index, params in enumerate(params_list)
    ]
    try:
        tasks = task_service.create_file_tasks_if_available(submissions)
    except TaskAlreadyProcessingError as exc:
        error_message = (
            "上一次任务回调尚未结束"
            if exc.reason == "callback_pending"
            else "任务正在处理中"
        )
        logger.warning(
            "文件分析请求在原子受理阶段冲突: %s fileName=%s "
            "status=%s callback_status=%s",
            error_message,
            exc.business_key,
            exc.status,
            exc.callback_status,
        )
        return jsonify({"error": error_message}), 409
    except TaskAdmissionBusyError:
        logger.warning("文件分析任务库持续繁忙，暂时无法受理", exc_info=True)
        return jsonify({"error": "任务服务繁忙，请稍后重试"}), 503

    for task in tasks:
        file_name = task["business_key"]
        progress_hub.publish(
            "file",
            file_name.strip(),
            {"businessType": "file", "data": {"fileName": file_name.strip(), "progress": 0.0}},
            task_id=task["execution_id"],
        )

    _task_fn = run_file_analysis_task if len(tasks) == 1 else run_file_analysis_batch_task
    _task_kwargs = {
        "task_service": task_service,
        "progress_hub": progress_hub,
        "request_payload": payload if len(tasks) > 1 else {"businessType": "file", "params": [params_list[0]]},
        "download_root": llm_config.download_dir,
        "callback_url": llm_config.callback_url or "",
        "callback_timeout": llm_config.callback_timeout,
        # Factory 本身只保存不可变配置和线程安全协调依赖。真正持有网络 Session 的
        # Gateway 在后台任务线程内部按文件创建，批量任务也不会跨文件共享有状态对象。
        "document_rag_factory": services.document_rag_factory,
        "knowledge_index_factory": services.knowledge_index_factory,
        "analysis_classification_mode": (
            services.analysis_classification_config.mode
        ),
        "analysis_filename_constraint_mode": (
            services.analysis_classification_config.filename_constraint_mode
        ),
        "analysis_data_standard_mode": (
            services.analysis_classification_config.data_standard_mode
        ),
        "analysis_identity_reselect_mode": (
            services.analysis_classification_config.identity_reselect_mode
        ),
    }
    if len(tasks) == 1:
        _task_kwargs["execution_id"] = tasks[0]["execution_id"]
    else:
        _task_kwargs["execution_ids"] = {
            task["business_key"]: task["execution_id"] for task in tasks
        }
    worker = threading.Thread(
        target=services.upload_task_limiter.run,
        args=(_task_fn,),
        kwargs=_task_kwargs,
        daemon=True,
    )
    worker.start()
    logger.info("已启动后台文件分析线程: task_count=%d", len(tasks))
    logger.info(
        "文件分析任务已受理并返回严格空响应: task_count=%d status_code=202",
        len(tasks),
    )
    # 不再将 task/execution_id 暴露给调用方；既有业务 fileName 是唯一公开关联键。
    return _empty_http_response(202)


@llm_bp.post("/llm/generate-report")
def llm_generate_report():
    services = _services()
    presenter = ReportSubmissionResponsePresenter()
    raw_payload = request.get_json(silent=True)
    logger.info(
        "收到报告生成请求: payload_type=%s",
        type(raw_payload).__name__,
    )
    try:
        parsed_request = parse_report_request(raw_payload)
    except ReportRequestValidationError as exc:
        # 公开错误体只包含已确认的 error 字段；日志记录类型和原因即可定位问题，不能输出
        # 文件 URL、模板内容或完整请求，避免调试日志泄漏业务数据。
        logger.warning(
            "报告生成请求被拒绝: payload_type=%s validation_error=%s",
            type(raw_payload).__name__,
            str(exc),
        )
        return _report_http_response(presenter.present_bad_request(str(exc)))

    normalized_report_id = parsed_request.report_id
    trace_id = uuid4().hex
    logger.info(
        "报告生成请求参数校验通过: report_id=%s file_count=%s trace_id=%s",
        normalized_report_id.business_key,
        len(parsed_request.params["filePathList"]),
        trace_id,
    )
    submission = parsed_request.to_submission(trace_id=trace_id)
    try:
        result = services.report_submit.execute(submission)
    except ReportTaskConflictError:
        logger.info(
            "报告生成请求因活动任务或回调Guard被拒绝: report_id=%s trace_id=%s",
            normalized_report_id.business_key,
            trace_id,
        )
        return _report_http_response(presenter.present_conflict())
    except Exception:
        # 受理事务失败不能伪装成 202。异常交给 Flask 统一 500 边界，同时日志只记录
        # reportId/trace，不输出 URL、模板文本或完整请求。
        logger.exception(
            "报告生成受理失败: report_id=%s trace_id=%s",
            normalized_report_id.business_key,
            trace_id,
        )
        raise

    logger.info(
        "报告生成请求已可靠受理: report_id=%s task_id=%s trace_id=%s",
        normalized_report_id.business_key,
        result.task_id,
        trace_id,
    )
    return _report_http_response(presenter.present_success(result))


@llm_bp.post("/llm/weaponry")
def llm_weaponry():
    services = _services()
    weaponry_services = services.weaponry_services
    if weaponry_services is None:
        # 生产组合根在 1D-6 后必须提供该能力。测试夹具若遗漏依赖也应明确失败，禁止
        # 静默回退到已经删除的路由线程与遗留 Service。
        raise RuntimeError("应用容器未装配武器谱运行链")
    presenter = WeaponrySubmissionResponsePresenter()
    raw_payload = request.get_json(silent=True)
    logger.info(
        "收到武器装备提取请求: payload_type=%s",
        type(raw_payload).__name__,
    )
    try:
        parsed_request = parse_weaponry_request(raw_payload)
    except WeaponryRequestValidationError as exc:
        logger.warning(
            "武器装备提取请求被拒绝: validation_error=%s payload_type=%s",
            str(exc),
            type(raw_payload).__name__,
        )
        return _weaponry_http_response(presenter.present_bad_request(str(exc)))

    trace_id = uuid4().hex
    try:
        document_scope = weaponry_services.document_scope.resolve(
            architecture_id=parsed_request.architecture_id,
            requested_file_names=parsed_request.selected_file_names,
        )
    except WeaponryDocumentScopeNotFoundError as exc:
        logger.warning(
            "武器装备提取请求引用未解析文档: architecture_id=%s file_count=%d",
            parsed_request.architecture_id,
            len(parsed_request.selected_file_names),
        )
        return _weaponry_http_response(presenter.present_not_found(str(exc)))
    except (WeaponryDocumentScopeAmbiguityError, WeaponryDocumentScopeError) as exc:
        logger.warning(
            "武器装备提取请求文档范围不确定: architecture_id=%s file_count=%d "
            "error_type=%s",
            parsed_request.architecture_id,
            len(parsed_request.selected_file_names),
            type(exc).__name__,
        )
        return _weaponry_http_response(presenter.present_bad_request(str(exc)))

    policies = weaponry_services.policies
    submission = parsed_request.to_submission(
        document_scope=document_scope,
        evidence_selection_policy=policies.evidence_selection,
        execution_policy=policies.execution,
        auxiliary_guidance_policy=policies.auxiliary_guidance,
        trace_id=trace_id,
    )
    try:
        result = weaponry_services.submit.execute(submission)
    except WeaponryTaskConflictError:
        logger.info(
            "武器装备提取请求因活动任务或回调 Guard 被拒绝: "
            "architecture_id=%s trace_id=%s",
            parsed_request.architecture_id,
            trace_id,
        )
        return _weaponry_http_response(presenter.present_conflict())
    except Exception:
        logger.exception(
            "武器装备提取受理失败: architecture_id=%s trace_id=%s",
            parsed_request.architecture_id,
            trace_id,
        )
        raise

    logger.info(
        "武器装备提取请求已可靠受理: architecture_id=%s task_id=%s "
        "document_count=%d field_count=%d trace_id=%s",
        parsed_request.architecture_id,
        result.task_id.value,
        len(document_scope.documents),
        len(parsed_request.fields),
        trace_id,
    )
    return _weaponry_http_response(presenter.present_success())


@llm_bp.post("/llm/check-task")
def llm_check_task():
    services = _services()
    task_service = services.task_service
    llm_config = services.llm_config
    payload = request.get_json(silent=True) or {}
    business_type = payload.get("businessType")
    if business_type not in {"file", "report", "weaponry"}:
        logger.warning(
            "任务查询请求被拒绝: businessType无效 businessType=%s",
            business_type,
        )
        return jsonify({"error": "businessType无效"}), 400

    params_list = _get_params(payload)
    if not params_list:
        logger.warning(
            "任务查询请求被拒绝: params为空或格式无效 businessType=%s",
            business_type,
        )
        return jsonify({"error": "params不能为空"}), 400

    missing_count = 0
    callback_replayed_count = 0
    for index, params in enumerate(params_list):
        if business_type == "file":
            business_key = params.get("fileName")
            if not isinstance(business_key, str) or not business_key.strip():
                logger.warning(
                    "任务查询请求被拒绝: fileName为空 index=%s",
                    index,
                )
                return jsonify({"error": "fileName不能为空"}), 400
            normalized_key = business_key.strip()
        elif business_type == "weaponry":
            architecture_id = params.get("architectureId")
            if architecture_id is None:
                logger.warning(
                    "任务查询请求被拒绝: architectureId为空 index=%s",
                    index,
                )
                return jsonify({"error": "architectureId不能为空"}), 400
            try:
                normalized_architecture_id = normalize_architecture_id(
                    architecture_id
                )
            except ArchitectureIdValidationError as exc:
                logger.warning(
                    "任务查询请求被拒绝: architectureId格式无效 index=%s "
                    "architecture_id_type=%s",
                    index,
                    type(architecture_id).__name__,
                )
                return jsonify({"error": str(exc)}), 400
            normalized_key = normalized_architecture_id.business_key
        else:
            report_id = params.get("reportId")
            if report_id is None:
                logger.warning(
                    "任务查询请求被拒绝: reportId为空 index=%s",
                    index,
                )
                return jsonify({"error": "reportId不能为空"}), 400
            try:
                normalized_report_id = normalize_report_id(report_id)
            except ReportIdValidationError as exc:
                logger.warning(
                    "任务查询请求被拒绝: reportId格式无效 index=%s "
                    "report_id_type=%s",
                    index,
                    type(report_id).__name__,
                )
                return jsonify({"error": str(exc)}), 400
            normalized_key = normalized_report_id.business_key

        task = task_service.get_task(business_type, normalized_key)
        if not task:
            if len(params_list) == 1:
                logger.warning(
                    "任务查询请求未找到任务: businessType=%s businessKey=%s",
                    business_type,
                    normalized_key,
                )
                return jsonify({"error": "任务不存在"}), 404
            logger.warning(
                "批量任务查询项未找到任务: businessType=%s businessKey=%s index=%s",
                business_type,
                normalized_key,
                index,
            )
            # 批量缺失项不终止其余项的回调恢复；成功体不再公开占位任务快照。
            missing_count += 1
            continue

        if business_type == "report":
            # 甲方规定 check-task 必须在本次请求内触发报告回调恢复。报告链路不能再走
            # 遗留直发方法，而是与正常 Worker 共用 execution 级 latest-wins、Guard 和
            # fencing，避免并发请求重复发送或新任务受理后继续发送旧回调。
            replayed = services.report_callback_recovery.execute(
                ReportId.from_public_value(normalized_report_id.value)
            )
        elif business_type == "weaponry":
            weaponry_services = services.weaponry_services
            if weaponry_services is None:
                raise RuntimeError("应用容器未装配武器谱运行链")
            replayed = weaponry_services.callback_recovery.execute(
                normalized_architecture_id.value
            )
        else:
            replayed = task_service.replay_callback_if_needed(
                business_type,
                normalized_key,
                callback_url=llm_config.callback_url or "",
                timeout=llm_config.callback_timeout,
            )
        # 保持原有“恢复后重读”的一致性门禁，但不再把内部状态投影到 HTTP 响应。
        refreshed_task = task_service.get_task(business_type, normalized_key)
        if refreshed_task is None:
            logger.error(
                "任务回调恢复后重新读取失败: businessType=%s businessKey=%s index=%s",
                business_type,
                normalized_key,
                index,
            )
            raise RuntimeError("任务回调恢复后不存在")
        if replayed:
            callback_replayed_count += 1

    logger.info(
        "任务检查与必要回调恢复已完成: businessType=%s requested_count=%d "
        "missing_count=%d callback_replayed_count=%d status_code=200",
        business_type,
        len(params_list),
        missing_count,
        callback_replayed_count,
    )
    # 公开接口只承诺检查已完成；任务状态、进度、Callback 细节继续留在内部存储。
    return _empty_http_response(200)


@llm_bp.post("/llm/reassign")
def llm_reassign():
    """执行冻结同步契约下的 Parser → Application → Presenter 薄路由。"""

    services = _services()
    presenter = ReassignResponsePresenter()
    raw_payload = request.get_json(silent=True)
    logger.info(
        "收到文档分类变更请求: payload_type=%s",
        type(raw_payload).__name__,
    )
    try:
        parsed_request = parse_reassign_request(raw_payload)
    except ReassignRequestValidationError as exc:
        logger.warning(
            "文档分类变更请求被拒绝: validation_error=%s payload_type=%s",
            str(exc),
            type(raw_payload).__name__,
        )
        return _reassign_http_response(presenter.present_bad_request(str(exc)))

    reassign_services = services.reassign_services
    if reassign_services is None:
        # 参数校验属于公开契约，必须优先返回既有 400；只有合法请求才要求运行链已完成装配。
        # 绝不以旧的蓝图直连编排作为兜底，避免静默绕过持久化意图、fencing 与补偿状态机。
        raise RuntimeError("应用容器未装配分类节点变更运行链")

    result = reassign_services.document_reassignment.execute(
        parsed_request.command
    )
    logger.info(
        "文档分类变更同步 Saga 已结束: result_category=%s",
        result.category.value,
    )
    return _reassign_http_response(
        presenter.present_result(
            file_name=parsed_request.file_name,
            old_architecture_id=parsed_request.old_architecture_id,
            new_architecture_id=parsed_request.new_architecture_id,
            result=result,
        )
    )


@sock.route("/llm/progress")
def llm_progress(ws):
    services = _services()
    subscription_service = services.progress_subscription_service
    presenter = ProgressWebSocketPresenter()
    registry = ProgressConnectionRegistry(
        connection_id=f"progress-{uuid4().hex}",
    )
    logger.info(
        "Progress WebSocket 连接已建立: connection_id=%s",
        registry.connection_id,
    )

    def _send(message: dict[str, Any]) -> None:
        """本路由线程是当前连接唯一 WebSocket 写入者。"""

        ws.send(presenter.serialize(message))

    def _flush_notifications() -> None:
        """从连接有界缓冲取出通知；业务发布线程绝不执行网络 I/O。"""

        for snapshot in registry.delivery.drain():
            _send(presenter.present_snapshot(snapshot))

    try:
        while True:
            # ``simple-websocket`` 的超时返回 None；连接仍处于 connected 时继续轮询，
            # 让同一线程能够在没有新客户端帧时发送后台任务产生的进度通知。
            _flush_notifications()
            raw_message = ws.receive(timeout=_PROGRESS_RECEIVE_POLL_SECONDS)
            if raw_message is None:
                if bool(getattr(ws, "connected", False)):
                    continue
                break

            try:
                payload = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                logger.warning(
                    "Progress 订阅消息被拒绝: connection_id=%s reason=invalid_json",
                    registry.connection_id,
                )
                _send(presenter.present_error("订阅消息不是合法JSON"))
                continue

            try:
                request_model = parse_progress_subscription(payload)
            except ProgressRequestValidationError as exc:
                logger.warning(
                    "Progress 订阅消息被拒绝: connection_id=%s error_type=%s "
                    "payload_keys=%s",
                    registry.connection_id,
                    type(exc).__name__,
                    list(payload.keys()) if isinstance(payload, dict) else "n/a",
                )
                _send(presenter.present_error(str(exc)))
                continue

            try:
                result = subscription_service.subscribe(
                    request_model,
                    delivery=registry.delivery,
                    existing_subscriptions=registry.subscriptions,
                    connection_id=registry.connection_id,
                )
            except ProgressSubscriptionRollbackError as exc:
                # 应用服务已经尽力补偿；未释放令牌必须进入连接 Registry，finally
                # 才能再次重试，而不是仅记录异常后遗失。
                registry.retain(exc.failed_subscriptions)
                raise

            try:
                # 必须在首条网络写入前登记令牌；若 send 失败，finally 仍能释放。
                registry.register_result(result)
                for item in result.current_items:
                    _send(presenter.present_current(item))
                result.complete_initial_delivery()
            except Exception:
                if registry.delivery.buffering_initial_batch:
                    try:
                        result.abort_initial_delivery()
                    except Exception:
                        logger.exception(
                            "Progress 初始快照屏障撤销失败: connection_id=%s",
                            registry.connection_id,
                        )
                raise

            logger.info(
                "Progress 订阅消息处理完成: connection_id=%s business_type=%s "
                "key_count=%s active_subscription_count=%s",
                registry.connection_id,
                request_model.business_type,
                len(request_model.ordered_keys),
                registry.active_count,
            )
            _flush_notifications()
    finally:
        remaining = registry.close_and_release(
            subscription_service,
            max_attempts=_PROGRESS_RELEASE_ATTEMPTS,
        )
        if remaining:
            logger.error(
                "Progress WebSocket 关闭后仍有订阅释放失败: connection_id=%s "
                "remaining_count=%s subscription_ids=%s",
                registry.connection_id,
                len(remaining),
                ",".join(item.subscription_id for item in remaining),
            )
        logger.info(
            "Progress WebSocket 连接已关闭: connection_id=%s remaining_count=%s",
            registry.connection_id,
            len(remaining),
        )


# ══════════════════════════════════════════════════════════════
#  文件对话接口
# ══════════════════════════════════════════════════════════════


@llm_bp.post("/llm/chat")
def llm_chat():
    services = _services()
    payload = request.get_json(silent=True) or {}
    logger.info("收到文件对话请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "文件对话请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "文件对话请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    try:
        _, chat_id = _read_json_chat_id(params, operation="文件对话请求")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    file_names = params.get("fileNames")
    if not isinstance(file_names, list):
        logger.warning(
            "文件对话请求被拒绝: fileNames类型无效 chatId=%s file_names_type=%s",
            chat_id,
            type(file_names).__name__,
        )
        return jsonify({"error": "fileNames必须为数组"}), 400

    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        logger.warning("文件对话请求被拒绝: message为空 chatId=%s", chat_id)
        return jsonify({"error": "message不能为空"}), 400
    message = message.strip()
    if len(message) > CHAT_MAX_MESSAGE_CHARS:
        logger.warning(
            "文件对话请求被拒绝：消息长度超过上限: chatId=%s message_length=%d limit=%d",
            chat_id,
            len(message),
            CHAT_MAX_MESSAGE_CHARS,
        )
        return jsonify({"error": "message超过文件对话长度上限"}), 400
    logger.info(
        "文件对话请求参数校验通过: chatId=%s raw_file_count=%d message_length=%d",
        chat_id,
        len(file_names),
        len(message),
    )

    normalized_file_names: list[str] = []
    seen_file_names: set[str] = set()

    # 对话入参中的 fileNames 是业务侧哈希文件名。这里必须在进入 AnythingLLM 前
    # 完成“存在性校验 + 首次出现顺序去重”，否则本地历史快照与远端检索上下文会不一致。
    for index, fn in enumerate(file_names):
        if not isinstance(fn, str) or not fn.strip():
            logger.warning(
                "文件对话请求被拒绝: fileNames中包含无效文件名 chatId=%s index=%s",
                chat_id,
                index,
            )
            return jsonify({"error": "fileNames中包含无效文件名"}), 400
        normalized_file_name = fn.strip()
        if normalized_file_name in seen_file_names:
            logger.debug(
                "文件对话请求去重重复文件名: chatId=%s fileName=%s index=%s",
                chat_id,
                normalized_file_name,
                index,
            )
            continue
        seen_file_names.add(normalized_file_name)
        normalized_file_names.append(normalized_file_name)

    logger.info(
        "文件对话引用文件校验完成: chatId=%s normalized_file_count=%d",
        chat_id,
        len(normalized_file_names),
    )
    if len(normalized_file_names) > CHAT_MAX_FILES_PER_REQUEST:
        logger.warning(
            "文件对话请求被拒绝：文件数量超过上限: chatId=%s file_count=%d limit=%d",
            chat_id,
            len(normalized_file_names),
            CHAT_MAX_FILES_PER_REQUEST,
        )
        return jsonify({"error": "fileNames超过文件对话数量上限"}), 400

    chat_run_executor = services.chat_run_executor
    if not chat_run_executor.try_acquire_stream_slot():
        logger.warning(
            "文件对话请求被拒绝：进程内流并发容量已满: chatId=%s max_concurrent_streams=%d",
            chat_id,
            chat_run_executor.max_concurrent_streams,
        )
        return jsonify({"error": "文件对话并发流已达上限，请稍后重试"}), 429
    stream_slot_acquired = True
    logger.info(
        "已获得文件对话进程内流容量许可: chatId=%s max_concurrent_streams=%d",
        chat_id,
        chat_run_executor.max_concurrent_streams,
    )

    def _release_stream_slot() -> None:
        nonlocal stream_slot_acquired
        if stream_slot_acquired:
            chat_run_executor.release_stream_slot()
            stream_slot_acquired = False
            logger.debug("已释放文件对话进程内流容量许可: chatId=%s", chat_id)

    try:
        prepared_run = chat_run_executor.prepare_chat_run(
            chat_id=chat_id,
            message=message,
            file_names=tuple(normalized_file_names),
        )
    except ChatDocumentNotFoundError as exc:
        _release_stream_slot()
        logger.warning(
            "文件对话请求被拒绝：存在尚未解析的文件: chatId=%s",
            chat_id,
        )
        return jsonify({"error": str(exc)}), 404
    except (ChatRunBusyError, ChatSessionUnavailableError):
        _release_stream_slot()
        logger.warning(
            "文件对话请求被拒绝: chatId已有进行中的流式响应 chatId=%s",
            chat_id,
        )
        return jsonify({"error": "当前对话已有进行中的流式响应"}), 409
    except ValueError as exc:
        _release_stream_slot()
        logger.warning(
            "文件对话请求被拒绝：输入或受理校验失败: chatId=%s error_type=%s",
            chat_id,
            exc.__class__.__name__,
        )
        return jsonify({"error": str(exc)}), 400
    except Exception:
        # 此时尚未创建 Response 对象，Flask 不会调用常规关闭钩子。向外抛出意外
        # 受理异常前，必须释放进程内并发容量许可。
        _release_stream_slot()
        logger.exception("文件对话请求受理发生未预期异常: chatId=%s", chat_id)
        raise

    logger.info(
        "文件对话运行已分配，准备创建流式响应: chatId=%s runId=%s file_count=%d",
        chat_id,
        prepared_run.run_id,
        len(normalized_file_names),
    )

    try:
        stream = services.chat_dispatcher.dispatch(run_id=prepared_run.run_id)
        stream_started = False

        def generate_sse_response():
            """在消费内部迭代器前标记执行已开始。"""
            nonlocal stream_started
            stream_started = True
            logger.info(
                "文件对话 SSE 事件流开始被客户端消费: chatId=%s runId=%s",
                chat_id,
                prepared_run.run_id,
            )
            yield from finalize_chat_run_stream(
                stream=stream,
                run_id=prepared_run.run_id,
                on_close=_release_stream_slot,
            )

        def close_response() -> None:
            """释放并发容量，并收敛一条已受理但未开始的运行。"""
            if not stream_started:
                logger.warning(
                    "文件对话 SSE 响应在执行开始前关闭，准备收敛未启动运行: chatId=%s runId=%s",
                    chat_id,
                    prepared_run.run_id,
                )
                try:
                    services.chat_commands.discard_unstarted_chat_run(
                        run_id=prepared_run.run_id,
                        error_message="SSE response closed before execution started",
                    )
                except Exception:
                    logger.exception(
                        "未启动文件对话运行关闭时收敛失败: chatId=%s runId=%s",
                        chat_id,
                        prepared_run.run_id,
                    )
            else:
                logger.debug(
                    "文件对话 SSE 响应关闭，运行已开始执行: chatId=%s runId=%s",
                    chat_id,
                    prepared_run.run_id,
                )
            _release_stream_slot()

        logger.info(
            "文件对话 SSE 响应已创建: chatId=%s runId=%s",
            chat_id,
            prepared_run.run_id,
        )
        response = Response(
            generate_sse_response(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        response.call_on_close(close_response)
        return response
    except Exception as exc:
        logger.exception(
            "文件对话请求在SSE响应创建前失败: chatId=%s runId=%s",
            chat_id,
            prepared_run.run_id,
        )
        try:
            services.chat_commands.discard_unstarted_chat_run(
                run_id=prepared_run.run_id,
                error_message=str(exc) or exc.__class__.__name__,
            )
        finally:
            _release_stream_slot()
        raise


@llm_bp.post("/llm/chat/title")
def llm_chat_title():
    services = _services()
    payload = request.get_json(silent=True) or {}
    logger.info("收到生成对话标题请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "生成对话标题请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "生成对话标题请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    try:
        _, chat_id = _read_json_chat_id(params, operation="生成对话标题请求")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        result = services.chat_title.generate_title(chat_id=chat_id)
    except ChatTitleEmptyHistoryError as exc:
        return jsonify({"error": str(exc)}), 400
    except ChatTitleUnavailableError as exc:
        return jsonify({"error": str(exc)}), 409
    except ChatTitleGenerationError as exc:
        logger.exception("生成文件对话标题失败: chatId=%s", chat_id)
        return jsonify({"error": str(exc)}), 500

    logger.info(
        "返回文件对话标题: chatId=%s title_chars=%d",
        result.chat_id,
        len(result.title),
    )
    return jsonify(result.to_response())


@llm_bp.get("/llm/chat/history")
def llm_chat_history():
    services = _services()
    try:
        _, chat_id = _read_query_chat_id(
            request.args.get("chatId"),
            operation="对话历史请求",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    history = services.chat_history.list_history(chat_id)
    logger.info("返回文件对话历史: chatId=%s message_count=%d", chat_id, len(history))
    return jsonify(history)


@llm_bp.post("/llm/chat/abort")
def llm_chat_abort():
    services = _services()
    payload = request.get_json(silent=True) or {}
    logger.info("收到中断对话请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "中断对话请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "中断对话请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    try:
        _, chat_id = _read_json_chat_id(params, operation="中断对话请求")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    result = services.chat_abort.abort_chat(chat_id=chat_id)
    logger.info(
        "返回文件对话中断结果: chatId=%s aborted=%s",
        result.chat_id,
        result.aborted,
    )
    return jsonify(result.to_response())


@llm_bp.post("/llm/chat/delete")
def llm_chat_delete():
    services = _services()
    payload = request.get_json(silent=True) or {}
    logger.info("收到删除对话请求: payload_keys=%s", list(payload.keys()))

    if payload.get("businessType") != "chat":
        logger.warning(
            "删除对话请求被拒绝: businessType无效 businessType=%s",
            payload.get("businessType"),
        )
        return jsonify({"error": "businessType必须为chat"}), 400

    params = payload.get("params")
    if not isinstance(params, dict):
        logger.warning(
            "删除对话请求被拒绝: params无效 params_type=%s",
            type(params).__name__,
        )
        return jsonify({"error": "params不能为空"}), 400

    try:
        public_chat_id, chat_id = _read_json_chat_id(
            params,
            operation="删除对话请求",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        result = services.chat_delete.delete_chat(chat_id=chat_id)
        return jsonify(result.to_response())
    except ChatDeleteNotFoundError:
        logger.warning("删除对话请求未找到对话: chatId=%s", chat_id)
        return jsonify({"error": "对话不存在"}), 404
    except ChatDeleteBusyError as exc:
        return jsonify(
            {
                "chatId": public_chat_id,
                "deleted": False,
                "error": exc.reason,
            }
        ), 409
    except ChatDeleteCleanupError as exc:
        logger.warning(
            "删除对话远端资源失败: chatId=%s failed_count=%d",
            exc.chat_id,
            len(exc.failed_leases),
        )
        return jsonify(
            {
                "chatId": public_chat_id,
                "deleted": False,
                "error": "对话资源清理失败",
            }
        ), 500
