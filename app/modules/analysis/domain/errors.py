"""文件分析领域的稳定异常分类。"""

from __future__ import annotations


class AnalysisContractError(ValueError):
    """模型返回或领域输入不满足文件分析合同。"""


class ArchitectureContractError(AnalysisContractError):
    """领域分类结果不满足有限候选、层级或范围合同。"""


class DataStandardParentContractError(ArchitectureContractError):
    """数据标准分类错误地指向父节点或不可见节点。"""


__all__ = (
    "AnalysisContractError",
    "ArchitectureContractError",
    "DataStandardParentContractError",
)
