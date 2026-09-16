"""阶段 1F-1 领域树兼容模块别名。

新实现位于 app.modules.analysis.domain.architecture_tree。把历史模块名直接别名到同一
模块对象，可让既有测试替身、缓存身份和私有辅助函数观察保持一致；本模块不得新增算法。
"""

import sys

from app.modules.analysis.domain import architecture_tree as _implementation


sys.modules[__name__] = _implementation
