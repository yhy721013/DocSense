# Flask 入站适配器

本目录将在波次 1B 保存 check-task/Progress 的 Flask 请求解析与错误映射辅助代码。正式路由在 FastAPI 迁移前仍注册于 `app/blueprints/llm.py`。

这里不得访问 SQLite、调用 Callback、操作进度 Hub 或创建后台线程；解析后的输入交给框架无关应用服务，输出通过 Presenter 映射回已确认契约。
