# 分类节点变更端口层

阶段 1E-2 已在本目录定义供应商无关协议，1E-2R 补齐恢复扫描、严格事实和接管审计，
1E-4R 增加目标准备现场、按引用查回、claim 续租和 Pin 审计；1E-5 增加恢复观测、终态专用
收口和接管 claim 的原子释放：

- `repository.py`：文档快照、Operation、Step、Event、短生命周期 UoW、同文档活动保护、
  lease/fencing、只读稳定游标恢复扫描、workspace 映射、未映射远端准备事实、Pin 审计、
  过期接管、恢复观测、终态事实收口和本地条件 CAS；
- `knowledge.py`：workspace 定位/创建、精确成员探测、加入、删除和 Pin 的 Knowledge 能力；
  目标准备同时提供按确定性名称查回和按既有 slug 引用查回；每个外部结果均明确为已执行、
  已满足、明确失败或结果未知，
  禁止用 `None`/空字典表示成功；
- `ReassignmentPortBundle`：只验证未来 Application 接收到 Repository 与请求级
  `ReassignmentKnowledgePortFactory`；每次 Operation 必须由 Factory 创建独立 Knowledge Port，
  不得共享 deadline、Transport 或其他请求级可变状态。Bundle 不读取配置、不创建
  SQLite/HTTP Client，也不接入 Flask 容器。

端口 DTO 只能使用本模块领域对象和不透明外部引用；不得泄露 SQLite connection、SQLAlchemy
Session、requests Response、AnythingLLM DTO、Flask/FastAPI 对象或真实文件路径。Repository
不得在数据库事务中执行网络 I/O，也不得发送日志外的业务副作用。

## 事务与结果约束

1. Application 每次外部写前后都必须分别打开并关闭 UoW；不得把 Knowledge Port 调用放入 UoW。
2. 所有条件写都携带不可变 `ReassignmentLease`，Repository 必须同时核验 owner、token、到期时间
   和 fencing token；失权只能返回确定的内部结果，不能继续写入检查点。
3. `ReassignmentStepCompletion` 只接受强类型终态，并校验状态与探测结论一致；已知失败重试和
   `recovery_required` 出边都必须先取得更大的 fencing。
4. 通用 `ReassignmentOperationTransition` 只允许非终态转换；正常成功由本地 CAS 专用入口验证步骤
   事实后原子收敛，恢复终态只能通过携带最新恢复观测的专用入口提交。两类成功都必须满足完整
   前向 workspace/Step 事实，失败和补偿终态也不能绕过证据。
5. 所有 DTO 仅服务内部编排与审计，禁止由 Presenter 直接序列化为 `/llm/reassign` 响应。
6. workspace 创建归属使用 `created_by_operation / preexisting / unknown` 三态。`unknown`
   可以在唯一资源已查回时继续使用，但绝不能作为自动删除整个 workspace 的所有权证据。
7. Operation 续租必须在同一事务中延长其尚未过期的活动 preparation claim；已经过期的 claim
   只能由更大 fencing 接管，不能通过普通续租复活。
8. 恢复观测只记录枚举结论、操作者摘要和原因码；终态提交必须引用当前 fencing 下最新观测，并在
   同一短事务精确释放已接管的 preparation claim，旧 owner、旧 token 或旧观测均不能释放保护。
