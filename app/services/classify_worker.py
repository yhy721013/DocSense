# app/services/classify_worker.py
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database_service import document_db
from database_service.mysql_data_converter import MySQLDataConverter
from rag_with_ocr import process_file_with_rag

from app.services.file_ops import move_file_to_category_folder, normalize_category_path
from app.services.task_store import InMemoryTaskStore

# 创建专门的logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
# 确保有处理器
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# “100% 确信”阈值：用于兼容模型把 100/100%/1/1.0 等不同格式写入 category_confidence 的情况
AUTO_CLASSIFY_THRESHOLD = 0.999


def _parse_result(result: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if result is None:
        return None, "未收到 AnythingLLM 的响应"
    if isinstance(result, dict):
        return result, None
    if isinstance(result, str):
        text = result.strip()
        # 兼容模型偶尔输出 ```json ... ``` 的围栏
        if text.startswith("```"):
            text = text.strip("`").strip()
            # 再次尝试去掉可能的 'json' 前缀
            if text.lower().startswith("json"):
                text = text[4:].strip()
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"结果不是合法 JSON: {exc}"
    return None, "未知的结果类型"


def _parse_confidence(value: Any) -> Optional[float]:
    """把多种置信度表达统一为 [0,1] 浮点数。"""
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            v = float(value)
        elif isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            if s.endswith("%"):
                s = s[:-1].strip()
                v = float(s) / 100.0
            else:
                v = float(s)
        else:
            return None
    except (TypeError, ValueError):
        return None

    # 兼容 100/99.5 这类“百分数但没写 %”的情况
    if v > 1.0:
        v = v / 100.0 if v <= 100.0 else 1.0
    if v < 0.0:
        v = 0.0
    if v > 1.0:
        v = 1.0
    return v


def _handle_category_move(
    file_path: Path,
    parsed_result: Dict[str, Any],
) -> Tuple[bool, str, bool, List[Dict[str, Any]]]:
    """解析分类并（如确定）移动文件。

    业务规则：
    1) 当模型“确信”（category_confidence≈1.0）时：直接采用 category/sub_category 并移动文件；
       即便模型同时给出了 category_candidates，也应忽略候选并不触发人工确认。
    2) 当模型不确信时：不移动文件，返回候选列表供人工确认。

    Returns:
        (success, move_message, manual_selection_required, category_candidates)
    """
    raw_candidates = parsed_result.get("category_candidates")
    category_candidates: List[Dict[str, Any]] = raw_candidates if isinstance(raw_candidates, list) else []

    category = parsed_result.get("category")
    sub_category = parsed_result.get("sub_category")
    confidence = _parse_confidence(parsed_result.get("category_confidence"))

    has_category = isinstance(category, str) and category.strip() != ""
    confident = has_category and (confidence is None or confidence >= AUTO_CLASSIFY_THRESHOLD)

    # Case A: 确信分类 -> 忽略候选，避免前端误判为“需要人工确认”
    if confident:
        parsed_result["category"] = category.strip()  # type: ignore[union-attr]
        if isinstance(sub_category, str):
            parsed_result["sub_category"] = sub_category.strip()
        # 将置信度规范化为 1.0（前端会显示 100%）
        parsed_result["category_confidence"] = 1.0
        parsed_result.pop("category_candidates", None)

    # Case B: 不确信且有候选 -> 进入人工确认模式（并避免同时输出 category/sub_category）
    elif category_candidates:
        parsed_result.pop("category", None)
        parsed_result.pop("sub_category", None)
        return True, "等待人工选择分类", True, category_candidates

    # Case C: 没有候选（或候选为空）-> 退化为直接采用 category（保持旧逻辑，不阻塞流程）
    category_value = parsed_result.get("category")
    sub_category_value = parsed_result.get("sub_category")
    full_category, normalize_error = normalize_category_path(category_value, sub_category_value)
    if not full_category:
        return True, normalize_error or "未获取到有效的分类信息，文件保留在原位置", False, []

    moved, move_message = move_file_to_category_folder(file_path, full_category)
    if not moved:
        return True, move_message, False, []
    return True, move_message, False, []


def process_single_file_task(
    store: InMemoryTaskStore,
    task_id: str,
    file_path: str,
    workspace_name: str,
    thread_name: str,
    user_id: int = 1,
) -> None:
    """后台线程：处理单文件分类/抽取任务。"""
    path = Path(file_path)
    try:
        result = process_file_with_rag(
            file_path=str(path),
            workspace_name=workspace_name,
            thread_name=thread_name,
            user_id=user_id,
        )

        parsed, parse_error = _parse_result(result)
        if parse_error:
            store.update(
                task_id,
                status="error",
                message=f"处理失败：{parse_error}",
                error=f"处理失败：{parse_error}",
            )
            return

        assert parsed is not None
        _, move_message, manual_required, candidates = _handle_category_move(path, parsed)

        store.update(
            task_id,
            status="completed",
            progress=100,
            message="处理完成" + (f" - {move_message}" if move_message else ""),
            manual_selection_required=manual_required,
            category_candidates=candidates if manual_required else [],
            # 注意：存“规范化后的 parsed”，避免前端仅凭 category_candidates 字段误判需要人工确认
            result=parsed,
            raw_result=result,
        )

    except Exception as exc:  # pylint: disable=broad-except
        store.update(
            task_id,
            status="error",
            message=f"处理失败：{exc}",
            error=f"处理失败：{exc}",
        )


def process_folder_task(
    store: InMemoryTaskStore,
    task_id: str,
    saved_files: List[Dict[str, Any]],
    workspace_name: str,
    thread_name: str,
    user_id: int = 1,
) -> None:
    """后台线程：批量处理文件夹。"""
    results: List[Dict[str, Any]] = []
    processed = 0
    total_files = len(saved_files)

    for idx, item in enumerate(saved_files):
        file_path_obj = Path(item["path"])
        display_name = item["display_name"]
        specific_thread_name = f"{thread_name}-{idx + 1}-{file_path_obj.stem}"

        try:
            raw_result = process_file_with_rag(
                file_path=str(file_path_obj),
                workspace_name=workspace_name,
                thread_name=specific_thread_name,
                user_id=user_id,
            )

            parsed, parse_error = _parse_result(raw_result)
            category_candidates: List[Dict[str, Any]] = []

            manual_selection_required = False
            move_message = ""
            category_error = ""
            error_message = ""

            result_for_ui: Any = raw_result
            if parse_error:
                error_message = parse_error
                category_error = parse_error
            else:
                assert parsed is not None
                result_for_ui = parsed
                _, move_message, manual_selection_required, category_candidates = _handle_category_move(
                    file_path_obj,
                    parsed,
                )
                # _handle_category_move 在“未取得有效分类/移动失败”时也会返回说明
                if move_message and ("失败" in move_message or "无效" in move_message):
                    category_error = move_message

            results.append(
                {
                    "file": display_name,
                    "file_path": str(file_path_obj),
                    "result": result_for_ui,  # 统一给前端“规范化结果”
                    "raw_result": raw_result,  # 保留原始返回用于排障
                    "success": raw_result is not None and parse_error is None,
                    "error": error_message,
                    "thread_name": specific_thread_name,
                    "category_candidates": category_candidates if manual_selection_required else [],
                    "manual_selection_required": manual_selection_required,
                    "move_message": move_message,
                    "category_error": category_error,
                }
            )

        except Exception as exc:  # pylint: disable=broad-except
            results.append(
                {
                    "file": display_name,
                    "file_path": str(file_path_obj),
                    "result": None,
                    "raw_result": None,
                    "success": False,
                    "error": str(exc),
                    "thread_name": specific_thread_name,
                    "category_candidates": [],
                    "manual_selection_required": False,
                    "move_message": "",
                    "category_error": "处理失败",
                }
            )

        processed += 1
        store.update(
            task_id,
            progress=int(processed / total_files * 100),
            processed=processed,
            message=f"正在处理第 {processed}/{total_files} 个文件 ({display_name})...",
        )

        # 轻微节流，避免 AnythingLLM/embedding 更新峰值
        time.sleep(0.1)

    store.update(
        task_id,
        status="completed",
        progress=100,
        message=f"文件夹处理完成，成功 {sum(1 for r in results if r['success'])}/{len(results)}",
        result={
            "batch_summary": {
                "total": len(results),
                "successful": sum(1 for r in results if r["success"]),
                "failed": sum(1 for r in results if not r["success"]),
            },
            "files": results,
        },
    )


def process_single_upload_task(
        store: InMemoryTaskStore,
        task_id: str,
        file_path: str,
        workspace_name: str,
        thread_name: str,
        user_id: int,
        original_filename: str,
        upload_timestamp: int
) -> None:
    """处理单文件上传任务"""
    try:
        # 准备附加元数据
        additional_metadata = {
            "upload_timestamp": upload_timestamp,
            "source": "web_ui_single_upload",
            "original_filename": original_filename,
        }

        result = process_file_with_rag(
            file_path=file_path,
            workspace_name=workspace_name,
            thread_name=thread_name,
            user_id=user_id,
            store_in_db=False,  # 不立即存储到数据库
            store_original_file=True,
            additional_metadata=additional_metadata,
        )

        if result:
            try:
                parsed_result = json.loads(result) if isinstance(result, str) else result
                logging.info(f"[DEBUG] 原始解析结果: {parsed_result}")
                success, move_message, manual_required, candidates = _handle_category_move(
                    Path(file_path), parsed_result
                )
                logging.info(f"[DEBUG] 分类处理结果 - 成功: {success}, 需要人工: {manual_required}")
                # 根据是否需要人工选择决定存储策略
                if manual_required:
                    # 需要人工选择时，先存储初始记录但标记为待处理
                    initial_db_id = document_db.save_result(
                        original_file_path=file_path,
                        final_file_path=file_path,  # 初始路径
                        result=result,
                        metadata=additional_metadata,
                        store_original_file=True
                    )

                    store.update(
                        task_id,
                        status="requires_manual_selection",
                        message="等待人工选择分类",
                        manual_selection_required=True,
                        category_candidates=candidates,
                        result=parsed_result,
                        db_result_id=initial_db_id  # 保存数据库ID供后续更新使用
                    )
                else:
                    # 自动分类完成，直接存储最终结果
                    final_path = file_path
                    if success and move_message.startswith("📁"):
                        final_path_parts = move_message.split(": ")
                        if len(final_path_parts) > 1:
                            final_path = final_path_parts[1]

                    db_result_id = document_db.save_result(
                        original_file_path=file_path,
                        final_file_path=final_path,
                        result=result,
                        metadata=additional_metadata
                    )

                    # 调用MySQL数据转换器插入数据
                    logging.debug(f"[DEBUG] 准备插入MySQL数据: {parsed_result}")
                    converter = MySQLDataConverter()
                    # 确保parsed_result包含必要字段
                    if 'category' not in parsed_result:
                        logging.warning("parsed_result缺少category字段，尝试从移动消息中提取")
                        # 这里可以根据需要添加更多逻辑来恢复分类信息
                    converter.convert_and_insert(parsed_result, final_path)

                    store.update(
                        task_id,
                        status="completed",
                        progress=100,
                        message="处理完成" + (f" - {move_message}" if move_message else ""),
                        result=parsed_result,
                        manual_selection_required=False,
                    )

            except (json.JSONDecodeError, Exception) as e:
                logging.error(f"⚠️ 解析分类信息失败: {e}，文件保留在原位置")
                store.update(
                    task_id,
                    status="error",
                    message=f"解析分类信息失败: {e}",
                    error=f"解析分类信息失败: {e}"
                )
    except Exception as exc:
        logging.error(f"❌ 处理单文件上传任务失败: {exc}")
        store.update(
            task_id,
            status="error",
            message=f"处理失败：{exc}",
            error=f"处理失败：{exc}",
        )


def process_folder_upload_task(
        store: InMemoryTaskStore,
        task_id: str,
        saved_files: List[Path],
        workspace_name: str,
        thread_name: str,
        user_id: int
) -> None:
    """处理文件夹上传任务"""
    results = []
    processed = 0
    total_files = len(saved_files)

    for idx, file_path in enumerate(saved_files):
        try:
            # 为每个文件创建特定的线程名称
            specific_thread_name = f"{thread_name}-{idx + 1}-{file_path.stem}"

            # 准备附加元数据
            additional_metadata = {
                "upload_timestamp": int(time.time() * 1000),  # 使用当前时间戳
                "source": "web_ui_folder_upload",
                "original_filename": file_path.name,
                "file_index": idx,
                "total_files_in_batch": len(saved_files),
            }

            # 不立即存储到数据库
            result = process_file_with_rag(
                file_path=str(file_path),
                workspace_name=workspace_name,
                thread_name=specific_thread_name,
                user_id=user_id,
                store_in_db=False,  # 不立即存储到数据库
                store_original_file=True,
                additional_metadata=additional_metadata,
            )

            final_path = str(file_path)  # 默认为原始路径
            manual_selection_required = False
            move_message = ""
            category_candidates = []

            if result:
                # 解析分类结果并移动文件
                try:
                    parsed_result = json.loads(result) if isinstance(result, str) else result

                    # ✅ 使用 _handle_category_move 函数来处理分类和移动逻辑
                    success, move_message, manual_required, candidates = _handle_category_move(
                        file_path, parsed_result
                    )

                    # 更新标志位
                    manual_selection_required = manual_required
                    category_candidates = candidates if manual_required else []

                    # 统一处理文件移动逻辑 - 无论是否需要人工选择
                    if success and move_message.startswith("📁"):
                        # 从消息中提取新路径
                        final_path_parts = move_message.split(": ")
                        if len(final_path_parts) > 1:
                            final_path = final_path_parts[1]

                    if manual_required:
                        # 需要人工选择时，存储记录但标记为待处理
                        initial_db_id = document_db.save_result(
                            original_file_path=str(file_path),
                            final_file_path=final_path,  # 使用更新后的路径
                            result=result,
                            metadata=additional_metadata,
                            store_original_file=True
                        )
                    else:
                        # 自动分类完成，直接存储最终结果
                        db_result_id = document_db.save_result(
                            original_file_path=str(file_path),
                            final_file_path=final_path,
                            result=result,
                            metadata=additional_metadata
                        )

                        if db_result_id:
                            print(f"✅ 文档结果已保存到MongoDB，ID: {db_result_id}")
                        else:
                            print(f"⚠️  文档结果保存到MongoDB失败")

                        # ✅ 只调用一次MySQL数据转换器插入数据
                        converter = MySQLDataConverter()
                        logging.debug(f"[DEBUG] parsed_result: {parsed_result}")
                        converter.convert_and_insert(parsed_result, final_path)

                except (json.JSONDecodeError, Exception) as e:
                    print(f"⚠️ 解析分类信息失败: {e}")

            results.append({
                "file": str(file_path.name),
                "file_path": str(file_path),
                "result": result,
                "success": result is not None,
                "thread_name": specific_thread_name,
                # ✅ 正确设置人工选择标志
                "manual_selection_required": manual_selection_required,
                "category_candidates": category_candidates,
                "move_message": move_message,
                "category_error": "" if manual_selection_required or "📁" in move_message else "处理失败"
            })

            processed += 1
            store.update(
                task_id,
                progress=int(processed / total_files * 100),
                processed=processed,
                message=f"正在处理第 {processed}/{total_files} 个文件 ({file_path.name})..."
            )

        except Exception as exc:
            results.append({
                "file": str(file_path.name),
                "file_path": str(file_path),
                "result": None,
                "success": False,
                "error": str(exc),
                "thread_name": f"{thread_name}-{idx + 1}-{file_path.stem}",
                # 设置为不需要人工选择，标记为处理失败
                "manual_selection_required": False,
                "category_candidates": [],
                "move_message": "",
                "category_error": "处理失败"
            })
            processed += 1

    # 更新最终状态
    store.update(
        task_id,
        status="completed",
        progress=100,
        message=f"文件夹处理完成，成功 {sum(1 for r in results if r['success'])}/{len(results)}",
        result={
            "batch_summary": {
                "total": len(results),
                "successful": sum(1 for r in results if r["success"]),
                "failed": sum(1 for r in results if not r["success"]),
            },
            "files": results,
        },
        # 添加一个标识，表示这是批量处理
        is_batch=True,
        # 收集所有需要人工选择的文件
        pending_files=[r["file_path"] for r in results if r.get("manual_selection_required")]
    )


def handle_manual_category_selection(
        store: InMemoryTaskStore,
        task_id: str,
        document_id: str,
        selected_category: str,
        selected_sub_category: str,
        file_path: str,
        parsed_result: Optional[Dict[str, Any]] = None
) -> None:
    """
    处理人工选择的分类
    """
    # 加强parsed_result校验和完整性确保
    if parsed_result is None or not isinstance(parsed_result, dict):
        logging.error(f"❌ parsed_result无效或缺失 (task_id: {task_id}, document_id: {document_id})")
        logging.error(f"❌ received parsed_result type: {type(parsed_result)}, value: {parsed_result}")

        # 从任务状态恢复并确保完整性
        task_status = store.get(task_id)
        if task_status and 'result' in task_status:
            parsed_result = task_status['result']
            logging.info(f"✅ 从任务状态恢复parsed_result: {type(parsed_result)}")
        else:
            # 创建基础的parsed_result结构
            parsed_result = {
                "category": selected_category,
                "sub_category": selected_sub_category
            }
            logging.info("✅ 创建新的parsed_result结构")

    # 确保parsed_result包含必要的分类字段
    if 'category' not in parsed_result or not parsed_result['category']:
        parsed_result['category'] = selected_category
        logging.info(f"✅ 补充category字段: {selected_category}")

    if 'sub_category' not in parsed_result:
        parsed_result['sub_category'] = selected_sub_category
        logging.info(f"✅ 补充sub_category字段: {selected_sub_category}")

    try:
        # 规范化分类路径
        full_category, normalize_error = normalize_category_path(selected_category, selected_sub_category)
        if not full_category:
            store.update(
                task_id,
                status="error",
                message=f"无效的分类路径: {normalize_error}",
                error=f"无效的分类路径: {normalize_error}",
            )
            return

        # 移动文件到新分类目录
        moved, move_message = move_file_to_category_folder(Path(file_path), full_category)
        if not moved:
            store.update(
                task_id,
                status="error",
                message=f"文件移动失败: {move_message}",
                error=f"文件移动失败: {move_message}",
            )
            return

        # 更新MongoDB数据库中的分类信息
        updated = document_db.update_document_category(
            document_id,
            selected_category,
            selected_sub_category,
            new_file_path=str(Path(file_path).parent / move_message.split(": ")[1]) if move_message.startswith(
                "📁") else file_path
        )

        if updated:
            # 调用 MySQL 数据转换器插入数据
            converter = MySQLDataConverter()
            logging.debug(f"[DEBUG] 最终使用的parsed_result: {parsed_result}")

            # 额外验证确保数据完整性
            category = parsed_result.get('category')
            sub_category = parsed_result.get('sub_category', '')

            if not category:
                raise ValueError("分类信息不完整：缺少category字段")

            logging.info(f"✅ 准备插入MySQL数据 - 分类: {category}, 子分类: {sub_category}")
            #converter.convert_and_insert(parsed_result, file_path)
            # 使用移动后的文件路径
            moved_file_path = str(Path(file_path).parent / move_message.split(": ")[1]) if move_message.startswith(
                "📁") else file_path
            converter.convert_and_insert(parsed_result, moved_file_path)

            store.update(
                task_id,
                status="completed",
                progress=100,
                message=f"人工分类确认完成，文件已移动到: {move_message}",
                result={
                    "category": selected_category,
                    "sub_category": selected_sub_category,
                    "new_file_path": move_message
                },
                manual_selection_required=False,
                manual_selected=True
            )
        else:
            store.update(
                task_id,
                status="error",
                message="数据库更新失败",
                error="数据库更新失败"
            )

    except Exception as exc:
        logging.error(f"❌ 处理人工分类选择失败: {exc}")
        store.update(
            task_id,
            status="error",
            message=f"处理人工分类选择失败：{exc}",
            error=f"处理人工分类选择失败：{exc}",
        )
