# 任务领域层

本目录只表达任务模块的稳定业务语言。领域对象不得接收 Flask request、SQLite Row、任意未校验字典或供应商响应对象。

允许依赖 Python 标准库和本模块其他纯领域类型；禁止依赖 Flask/FastAPI、Celery、SQLAlchemy、`sqlite3`、`requests`、具体 Adapter 或遗留 Service。

阶段 1A-3 已加入 `models.py`，当前包含：

- `TaskId` 与 `TaskBusinessRef`：分离一次执行身份和公开业务定位键；
- `TaskSnapshot` 与 `TaskLookupItem`：保存任务事实并保持请求顺序和公开键值类型；
- `ProgressKey`、`ProgressSnapshot`、`ProgressSubscriptionRequest`：表达内部进度身份、事件序号和有序订阅；
- callback 状态常量：只覆盖当前 `pending/success/failed/skipped`，不自行扩张公开状态。

所有对象均为不可变 dataclass；内部 task ID 和 sequence 不得进入现有公开协议。
