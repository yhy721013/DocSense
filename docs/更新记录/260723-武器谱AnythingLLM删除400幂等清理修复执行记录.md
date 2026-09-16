# 武器谱 AnythingLLM 删除 400 幂等清理修复执行记录

## 1. 结论

| 项目 | 结论 |
| --- | --- |
| 日期 | 2026-07-23 |
| 分支 | `refactor/concurrency` |
| 修复范围 | `/llm/weaponry` 内部临时 workspace 关闭与后台资源恢复 |
| 根因 | AnythingLLM 在删除一个已经不存在的 workspace 时可能返回 HTTP 400；旧实现只把 404 视为幂等成功，导致本地资源长期停留在 `cleanup_pending` |
| 修复原则 | 不把所有 400 放宽为成功；仅在 400 后通过完整 workspace 清单确认精确 slug 不存在时提交成功 |
| 公开接口 | 未增删、改名或调整任何 HTTP、WebSocket、Progress、Callback 请求/响应参数；未修改 `docs/接口文档/` |
| 数据迁移 | 无数据库结构和数据迁移 |
| 运行验证 | 只使用项目 `venv` 执行离线测试；未启动 `run.py`，未主动修改生产 SQLite 或调用真实 AnythingLLM DELETE |

## 2. 故障链

武器谱执行结束时，会先持久化远端资源的清理意图，再尝试关闭任务级检索 workspace。后台资源恢复器
随后还会根据已持久化的资源事实执行幂等删除。因此，同一个 workspace 可能在请求内关闭后再次收到
DELETE。

AnythingLLM 当前 workspace 删除路由在目标 slug 不存在时返回 HTTP 400 和 `Bad Request`。旧清理
Adapter 只接受 404，因而会把这类“资源实际已经消失”的重复删除持续记录为
`weaponry_resource_cleanup_rejected`，并按失败冷却重试，无法把本地资源事实推进到
`succeeded`。

现场任务 `0063984d55bb4bab8415169a86853940` 的本地资源为 `cleanup_pending`，而 AnythingLLM
完整 workspace 清单中已经不存在对应精确 slug，与上述故障链一致。

## 3. 修复设计

### 3.1 严格的 400 三态判定

workspace DELETE 返回 400 后执行一次只读完整清单查询，并使用统一的 slug 判定规则：去除首尾空白、
忽略大小写差异，但不使用前缀、后缀或模糊匹配。

| 查回结果 | 本地处理 |
| --- | --- |
| 清单中不存在精确 slug | 证明目标已不存在，按幂等成功处理 |
| 清单中仍存在精确 slug | 保留原 HTTP 400 的明确失败语义 |
| 清单请求失败、响应对象非法或 Transport 关闭失败 | 无法证明目标不存在，保守保持失败并进入既有重试链 |

404 仍直接按幂等成功处理；409、5xx 和其他非成功状态没有被放宽。SOURCE_CONVERSATION 等非
workspace 资源也不会套用 workspace 清单判定。

### 3.2 两个清理入口使用同一规则

1. `AnythingLLMTargetEvidenceRetrievalAdapter.close_scope()` 在任务持有的 Transport 中直接执行
   只读查回。失败时不关闭 scope 或租约，使既有资源恢复链仍可接管。
2. `AnythingLLMWeaponryResourceCleanupAdapter.cleanup()` 的 DELETE 异常会退出原 Transport 租约，
   因而使用新的短租约执行只读查回。查回失败时不覆盖原始 HTTP 400 分类。

共享辅助函数只接收 AnythingLLM 集成层模型，不把供应商 DTO 传入 Application 或 Domain；没有
新增跨任务 Session，也没有削弱资源 lease、版本 CAS 或 fencing 边界。

## 4. 修改文件

| 文件 | 修改内容 |
| --- | --- |
| `app/modules/weaponry/adapters/anythingllm_clients.py` | 新增严格、可复用的 workspace 精确 slug 缺失判定 |
| `app/modules/weaponry/adapters/anythingllm_retrieval.py` | 修复任务内 `close_scope()` 的 DELETE 400 幂等语义 |
| `app/modules/weaponry/adapters/anythingllm_resource_cleanup.py` | 修复后台恢复 Adapter 的 DELETE 400 幂等语义与日志 |
| `tests/test_weaponry_production_adapters.py` | 覆盖任务内关闭的“不存在、仍存在、查回失败”分支及可重试性 |
| `tests/test_weaponry_stage1d6.py` | 覆盖后台资源恢复的 400 三态判定，并确认其他 HTTP 状态不会触发查回 |

## 5. 验证

所有命令均使用 `venv\Scripts\python.exe`，未运行主进程：

- 清理 Adapter 与检索 Adapter 定向测试：18 项通过；
- `test_weaponry*.py`：258 项通过；
- `tests.test_architecture_boundaries`：17 项通过；
- Python 编译检查和 `git diff --check` 通过。

## 6. 发布、历史数据与回滚

本次没有 Schema 变化。更新后的进程启动并执行既有资源恢复扫描后，若远端清单确认 workspace 已
不存在，历史 `cleanup_pending` 记录会沿现有状态机自然收敛；本次离线修复过程没有直接修改现场
SQLite，也没有手工伪造资源成功状态。

回滚时只需回退本次代码和测试变更；数据库无需回滚。但回退后，AnythingLLM 对缺失 workspace
返回的 400 会再次进入永久冷却重试，因此不建议在仍有相关 `cleanup_pending` 记录时回退。

## 7. 剩余边界

1. 完整 workspace 清单是当前 AnythingLLM API 可用的只读权威查回手段。若未来供应商提供按 slug
   查询且能明确区分 404，可替换为更低成本的精确查询，但仍必须保持“查回失败即保守失败”。
2. 本修复只解决单项远端资源的幂等收敛，不等价于多实例 Task Attempt、heartbeat、checkpoint、
   Reaper 或数据库级执行权。后续多实例部署仍需统一 execution lease 与 fencing。
3. 历史资源何时完成收敛取决于更新后进程的维护扫描周期和 AnythingLLM 清单查询可用性。
