# Flask 入站适配器

阶段 1B-2 已在本目录加入 Progress 的 Flask 请求解析器和连接级 Registry：解析器完整
校验无 action 消息，Registry 持有该连接唯一的有界投递缓冲和待释放令牌，但二者均不
持有或写入 WebSocket。正式路由在 FastAPI 迁移前仍注册于 `app/blueprints/llm.py`，
并由路由线程作为当前连接唯一写入者。

报告类型 Progress 请求通过上级 Web Adapter 的共享 `reportId` 规范化器解析整数或
整数字符串；格式错误只返回 error 并保持连接。HTTP 报告生成与旧 check-task 路由也
复用同一规则，避免前导零等不同表示形成不同任务键。

check-task 当前只完成框架无关 typed request、可靠命令服务和 Presenter；其生产解析器
随阶段 6 可靠链路一次性切换，本目录不提供同步回调恢复 Adapter。

这里不得访问 SQLite、调用 Callback、操作进度 Hub 或创建后台线程；解析后的输入交给框架无关应用服务，输出通过 Presenter 映射回已确认契约。
