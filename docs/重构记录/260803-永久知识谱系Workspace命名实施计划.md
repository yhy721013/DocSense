# 永久知识谱系 Workspace 命名实施计划

## 1. 文档状态与批准范围

- 制订日期：2026-08-03。
- 目标分支：`feat/weaponry-chat`。
- 状态：已完成；阶段已按 0～7 顺序执行，每阶段门禁全通过且无待商讨事项后才进入下一阶段。
- 负责人已批准同步修改 `docs/接口文档/分类节点变更.md` 中的内部 Workspace 命名说明。
- 本改造不增加、删除或改名任何前后端接口参数，不改变请求、响应、Callback、HTTP 状态码、
  Header、SSE 或 WebSocket 语义。
- 当前处于开发阶段，不兼容、不迁移旧 `architectureId-*` Workspace；开发数据可在明确维护窗口
  和资源范围内整体清理，但禁止按名称前缀盲删所有权不明的远端资源。

## 2. 目标规则

所有新建的永久知识谱系 Workspace 必须使用：

```text
archId-{规范化后的 architectureId}
```

示例：`architectureId-10605` 改为 `archId-10605`。

规则说明：

1. `archId-` 的大小写和连字符固定；
2. Analysis 使用已经校验的正整数 `architecture_id`；
3. Reassign 先复用既有 `architecture_id_storage_value()` 得到数据库权威整数，再生成名称，保证
   `12`、`"0012"` 和 `" 12 "` 等既有兼容表示不会产生多套 Workspace 名称；
4. AnythingLLM `slug` 和内部 ID 均是不透明引用，不从名称推导，也不要求等于名称的小写形式；
5. `/llm/weaponry` 的 `docsense-weaponry-retrieval-{taskId}` 任务级临时 Workspace、Provided-
   Evidence Workspace、术语 Workspace 与 Chat Workspace 不在本次范围内。

## 3. 当前实现基线

- 永久知识谱系 Workspace 的首次确保/创建由 `/llm/analysis` 入库链中的
  `LegacyAnalysisKnowledgeAdapter` 构造 `CollectionSpec`，当前名称为
  `architectureId-{architecture_id}`；
- `AnythingLLMKnowledgeGateway` 只接收业务层给出的 `CollectionSpec.name`，按 architecture ID 和
  持久创建预留协调 Workspace，不应吸收业务命名规则；
- `/llm/reassign` 在目标分类尚无本地映射时创建永久 Workspace，前向服务和恢复观察器当前各有
  一处旧前缀拼接；
- `/llm/weaponry` 只冻结本地文档范围并创建任务拥有的临时检索 Workspace，不创建上述永久
  Workspace。

## 4. 架构决策

新增无 I/O 的共享领域命名规则 `app/domain/knowledge_workspace.py`：

- 唯一定义 `PERMANENT_ARCHITECTURE_WORKSPACE_PREFIX = "archId-"`；
- 提供 `permanent_architecture_workspace_name(architecture_id: int) -> str`；
- 拒绝布尔值、非整数和超出有符号 64 位范围的输入；
- 不读取环境、数据库、时钟或网络，不导入 Flask、SQLite 或 AnythingLLM，也不产生日志；
- Analysis Adapter 和 Reassign Application 共用该函数，禁止复制两套 f-string；
- 通过专用 AST 导入门禁保护共享领域目录的纯度。

AnythingLLM 通用集成继续只拥有供应商 Transport、DTO、协议错误与原子 Workspace Client；业务
命名、目标分类规范化和恢复语义继续由业务层拥有。

## 5. 分阶段实施与门禁

### 阶段 0：合同与范围冻结

1. 落盘本计划并登记重构索引；
2. 记录分支、HEAD、工作树和旧前缀引用基线；
3. 冻结公开协议零变化、旧数据零迁移和 `run.py` 零执行边界；
4. 确认接口文档修改已获得负责人批准。

门禁：工作树来源清晰；计划与接口文档无冲突；`git diff --check` 通过；无待商讨事项。

### 阶段 1：共享纯命名规则

1. 新增共享领域函数、导出和通俗中文注释；
2. 新增边界整数、兼容规范化、布尔/非整数/越界拒绝测试；
3. 新增共享领域 AST 纯度门禁。

门禁：命名单测和架构边界测试全部通过；共享领域层无 I/O、供应商或业务 Adapter 依赖。

### 阶段 2：Analysis 永久知识入库链接入

1. 使用共享函数构造 `CollectionSpec.name`；
2. 在业务操作边界记录 task ID、architecture ID、Workspace 名称和结果，禁止记录正文或凭据；
3. 更新永久知识 Gateway Fake/样例，精确断言创建参数和持久集合名称。

门禁：首次创建、映射复用、策略更新、幂等提交和 unknown/补偿回归全部通过。

### 阶段 3：Reassign 前向与恢复链接入

1. 前向和恢复均先投影数据库权威 architecture 整数，再调用共享命名函数；
2. 已有 mapping 继续只按持久 slug 查回；无 mapping 时精确使用新名称创建/查回；
3. 补充名称、操作 ID、匹配数量和结果分类的脱敏日志；
4. 更新 Application、Recovery、AnythingLLM Adapter 和严格 Fake 测试。

门禁：创建、复用、超时查回、冲突、unknown、补偿、claim、lease/fencing 和恢复测试全部通过。

### 阶段 4：旧前缀退场与开发数据策略

1. 生产代码禁止旧前缀和 fallback；
2. 不实现双读、双写、远端重命名、自动认领或数据迁移；
3. 形成开发环境整体清理清单，真实删除必须另行核对维护窗口、活动 Worker、资源所有权和范围。

门禁：静态扫描只允许历史文档和公开字段规范化资产保留 `architectureId-`；无未授权删除。

### 阶段 5：定向离线测试

使用 `venv\\Scripts\\python.exe -B` 执行共享命名、Analysis、Knowledge Gateway、Reassign、Weaponry
合同、架构和并发测试，不运行 `run.py`。

门禁：全部定向测试通过；50 个不同分类名称唯一；同一分类的共享 SQLite 创建协调不重复；公开
接口字节/字段契约无变化；证据明确限定为离线 Fake/SQLite 范围。

### 阶段 6：文档与全面离线关闭

1. 经批准更新 `docs/接口文档/分类节点变更.md` 的内部命名说明；
2. 更新根 README、相关模块 README、测试索引和文档索引；
3. 新增更新记录，写明代码事实、测试统计、旧数据策略和证据边界；
4. 执行安全全仓套件、`compileall`、架构边界、遗留引用审计和 `git diff --check`。

门禁：报告发现/排除/执行/失败/错误/跳过数量；失败与错误均为 0；无未归属文件或待商讨事项。

### 阶段 7：隔离真实 AnythingLLM 验收

在非生产、可清理的虚拟 architecture ID 上，不启动 `run.py`：

1. 记录 Workspace 基线并确认目标名称零碰撞；
2. 通过任务级 Analysis 组合完成最小入库，核对 `name=archId-{id}`；
3. 通过 Weaponry 任务级组合验证已入库文档可用且临时资源清理；
4. 通过 Reassign 任务级组合验证新目标分类使用相同命名规则；
5. 删除测试数据并确认 Workspace 基线恢复。

真实服务不可用、目标名称碰撞、资源范围不明或缺少删除授权时必须停止，不能把离线 Fake 结果
冒充真实验收。真实证据不外推为浏览器、多实例、可靠队列、共享数据库或生产容量证明。

## 6. 文件范围

生产代码：

- `app/domain/__init__.py`；
- `app/domain/knowledge_workspace.py`；
- `app/modules/analysis/adapters/legacy_knowledge.py`；
- `app/modules/reassign/application/service.py`；
- `app/modules/reassign/application/recovery_observer.py`；
- `app/modules/reassign/adapters/anythingllm_knowledge.py`（仅必要日志）。

测试与门禁：

- `tests/test_knowledge_workspace_naming.py`；
- `tests/test_analysis_production_adapters.py`；
- `tests/test_anythingllm_knowledge_gateway.py`；
- `tests/test_reassign_application.py`；
- `tests/test_reassign_recovery.py`；
- `tests/test_reassign_recovery_collaborators.py`；
- `tests/test_reassign_anythingllm_adapter.py`；
- `tests/test_reassign_strict_fakes.py`；
- `tests/architecture/import_rules.py`；
- `tests/test_architecture_boundaries.py`。

明确不修改：

- `/llm/weaponry` 的公开 Parser、Presenter、Callback 和路由参数；
- Weaponry 临时 Workspace、术语 Workspace 和 Chat Workspace 命名；
- SQLite Schema、迁移版本和历史更新记录；
- `run.py`。

## 7. 完成定义

1. 新永久知识谱系 Workspace 精确使用 `archId-{规范化architectureId}`；
2. Analysis、Reassign 前向和 Reassign 恢复共用唯一命名函数；
3. 新代码不再创建或查找 `architectureId-*`；
4. 旧数据不兼容、不迁移、不自动删除；
5. API 参数、响应、Callback、状态码和流式协议零变化；
6. 并发创建协调、幂等、补偿及恢复语义不退化；
7. 日志可定位分类、目标名称和结果，但不泄露凭据、正文或完整供应商响应；
8. 离线门禁与真实供应商验收分开报告，不夸大证据范围。

## 8. 阶段执行状态

截至 2026-08-03：

- 阶段 0～7 已按顺序完成，且每阶段门禁通过后才进入下一阶段；
- 生产代码旧前缀扫描为零，数据库 Schema、迁移与 `run.py` 均未修改；
- 安全全仓发现 2,193 项，精确排除既有 13 项后执行 2,180 项，失败 0、错误 0、跳过 3；
- 已按负责人授权更新 `docs/接口文档/分类节点变更.md` 的内部命名说明，公开参数及协议零变化；
- 阶段 7 已在本机回环 AnythingLLM 的隔离虚拟 ID 上完成永久 Knowledge Gateway、Weaponry
  任务级 Client、Reassign Adapter 的精确核名与清理，Workspace 全量基线从 4 恢复为 4；
- 两次验收脚本因 Windows 进程内 SQLite 句柄延迟释放而未通过本机临时目录删除门禁，远端只读
  复核均确认零残留；最终改为子进程退出后校验绝对路径再清理并完整重跑，退出码为 0；
- 各阶段关闭检查均未留下需要商讨的未决事项。
