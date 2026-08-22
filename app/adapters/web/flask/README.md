# Flask 入站适配器

阶段 1B-2 已在本目录加入 Progress 的 Flask 请求解析器和连接级 Registry：解析器完整
校验无 action 消息，Registry 持有该连接唯一的有界投递缓冲和待释放令牌，但二者均不
持有或写入 WebSocket。正式路由在 FastAPI 迁移前仍注册于 `app/blueprints/llm.py`，
并由路由线程作为当前连接唯一写入者。

报告类型 Progress 请求通过上级 Web Adapter 的共享 `reportId` 规范化器解析整数或
整数字符串；数字部分最多为 128 位，正负号不计入、前导零计入上限。格式错误只返回
error 并保持连接。HTTP 报告生成与旧 check-task 路由也复用同一规则，避免前导零等不同
表示形成不同任务键。

阶段 1C-0 完成后，本目录还包含报告生成 HTTP 请求解析器。它落实已确认的顶层 JSON
对象、严格 `params` 元素和非空字符串 `filePathList` 契约，并保留隔离的完整兼容快照；
`templateDesc`/`requirement` 在副本内按旧语义兼容字符串化。阶段 1C-6 已将解析结果直接
映射为不可变 `ReportSubmission` 并交给 Report Application；兼容字典只供遗留行为测试，
不再进入当前报告执行链。解析器不负责任务受理、线程创建、持久化或报告执行。

check-task 当前只完成框架无关 typed request、可靠命令服务和 Presenter；其生产解析器
随阶段 6 可靠链路一次性切换，本目录不提供同步回调恢复 Adapter。

阶段 1E-6 新增 `reassign_requests.py`。它逐项复刻 `/llm/reassign` 已冻结的入站兼容边界：
原始新旧 ID 比较、旧 ID 的 `int(...)` 转换时点、非严格的新 ID 以及既有 400 文案。解析器不访问
Repository、AnythingLLM、Flask 全局对象或线程；非空非对象 JSON 和旧 ID 转换异常仍由路由保留原有
500 边界，不能被适配器擅自收紧为新 400。

阶段 1F-2 新增的 `analysis_requests.py` 和 `analysis_submission.py` 已在 1F-5B 接入当前
`app/blueprints/llm.py`。前者复刻 `/llm/analysis` 的有界 JSON 读取、400/413 校验顺序和不可变请求
投影；后者映射既有 202 严格空体、409 与 503 结果。路由现在只执行
`Parser → SubmitAnalysisBatch → Presenter`，不再直接受理任务、发布 Progress 或创建后台线程。

该接线不改变参数、字段、Header、状态码、Progress 或 Callback；file `check-task` 的同步恢复也只使用
新 Analysis Callback Guard 链，不能回退到遗留恢复器。真实部署必须打开已经严格验证的 v2 控制库：
保留旧库的迁移现场先通过只读切换预检；旧文件集已明确弃用的全新现场先运行一次性 fresh bootstrap。
两种路径都禁止新旧链路双跑。

这里不得访问 SQLite、调用 Callback、操作进度 Hub 或创建后台线程；解析后的输入交给框架无关应用服务，输出通过 Presenter 映射回已确认契约。
