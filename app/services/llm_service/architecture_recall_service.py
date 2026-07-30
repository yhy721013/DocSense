"""阶段 1F-1 领域候选召回兼容模块别名。

新实现位于 app.modules.analysis.domain.architecture_recall。把历史模块名直接别名到同一
模块对象，可保持既有私有辅助函数替身、缓存与导入身份一致；本模块不得新增召回算法。
"""

import sys

from app.modules.analysis.domain import architecture_recall as _implementation


sys.modules[__name__] = _implementation
