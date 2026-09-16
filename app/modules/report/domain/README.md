# 报告领域层

本目录只保存报告生成的稳定业务概念与纯规则，不读取 Web 请求、数据库、文件、队列或
AnythingLLM。所有对象均不可变，便于后续通过 task ID 在 Worker 中恢复，而不会继续引用
Flask 请求期间的可变 `dict/list`。

## 当前内容

- `models.py`：不受 32/64 位限制且最多 128 位十进制数字的 `ReportId`、提交命令、输入快照、报告结果和回调载荷；
- `rules.py`：HTML 规范化、成功/失败回调、Context/Conversation 名称；
- `errors.py`：冲突、stale、输入、模板、RAG 和 Artifact 等稳定错误分类。

`None`、空字符串和纯空白 RAG 结果按照已确认契约生成空 HTML 并保持成功。领域层不写
日志；`ReportResult.empty_rag_result` 供应用层记录结构化日志和指标。

## 禁止事项

- 不导入 Flask、FastAPI、Celery、SQLAlchemy、sqlite3、requests 或供应商 Client；
- 不把 HTTP 状态码、SQLite Row、SQLAlchemy Entity 或 workspace/thread/docpath 放入 DTO；
- 不执行下载、RAG、回调、持久化、线程创建或时间/UUID 生成；
- 不因内部 DTO 校验更严格而擅自改变公开接口对遗留异常输入的处理方式。
