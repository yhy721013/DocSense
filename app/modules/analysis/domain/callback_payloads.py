"""文件分析回调载荷的纯投影规则。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


def build_file_callback_payload(
    file_name: str,
    mapped_result: Dict[str, Any],
    status: str,
) -> Dict[str, Any]:
    """构造历史回调格式的独立字典，不执行实际 HTTP 回调。"""

    # 回调可能在任务终态之后延迟发送或进入恢复队列。这里必须冻结嵌套结果，避免
    # mapped_result 后续被翻译、清理或兼容代码修改时，已构造的回调事实随之漂移。
    data = {"fileName": file_name, "status": status}
    data.update(deepcopy(mapped_result))
    return {
        "businessType": "file",
        "data": data,
        "msg": "解析成功" if status == "2" else "解析失败",
    }


__all__ = ("build_file_callback_payload",)
