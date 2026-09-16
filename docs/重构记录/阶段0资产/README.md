# 阶段 0 资产说明

本目录索引阶段 0 的可复现资产。可执行脚本和测试仍放在仓库既有 `scripts/`、`tests/` 目录，避免把源代码复制到文档目录形成双份真相。

| 资产 | 路径 | 用途 |
| --- | --- | --- |
| 目标契约黄金样例 | `tests/contracts/stage0_contracts.json` | 冻结已批准目标 HTTP/WS 差异与继续保持的 Callback/SSE 样例 |
| 契约校验 | `tests/test_stage0_contract_assets.py` | 完全离线验证黄金样例与当前 Callback/SSE Presenter |
| 离线路由容器 | `tests/offline_application.py` | 用临时 SQLite 和 Fake 供应商构建路由测试依赖 |
| 容量采集器 | `scripts/stage0_load_baseline.py` | 连接已有服务采集 HTTP/SSE/WS 成功率、成功/失败延迟、首帧/保持耗时和吞吐；使用有界 Future 窗口与 Ping/Pong/持续 receive 探测，不启动服务 |
| 容量场景 | `scripts/stage0_workloads.example.json` | 定义 50 短请求、50 WS、禁用的 50 SSE 与在途任务场景 |
| SQLite 盘点器 | `scripts/stage0_sqlite_inventory.py` | 使用 `mode=ro`、`query_only` 和显式只读事务输出同一快照内的 Schema/聚合，识别 WAL/SHM；不迁移数据 |
| 执行结论 | `docs/重构记录/260715-阶段0执行记录.md` | 汇总已完成项、实测缺口、基础设施和安全门禁 |

## 安全执行原则

- 容量采集器默认只接受回环地址；非回环地址必须显式授权。
- 重型场景默认禁用，同时需要修改场景并传入 `--allow-heavy-services`。
- 示例中的在途任务正文只是口径占位，不得直接用于真实提交；执行前必须生成唯一业务键和专用测试数据。
- SQLite 工具只读，但仍应对明确列出的测试文件运行；不要把大范围路径扫描结果当成迁移清单。
- SQLite 的 `sha256` 是盘点结束后主文件与 WAL 的物理指纹，不是事务逻辑版本；应同时检查 `dataVersionUnchangedDuringInspection`、物理文件变化和 hash 稳定标记。
- 任何输出都不得提交密钥、Authorization、文件正文、Prompt 或回调正文。
