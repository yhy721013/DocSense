# 阶段 2-0 Runtime Config 归属矩阵

> 冻结日期：2026-08-12  
> 公开契约：无变化；下列均为后端内部启动、容量、租约、恢复或执行 Profile 配置  
> 真实 `.env`：禁止由应用、迁移脚本或本次改造读取后回写

每个键的默认值、严格校验、目标所有者、日志口径和 `.env.example` 完整行由机器资产
`tests/contracts/stage2_runtime_config_ownership.json` 逐键保存。本文件说明分组、迁移边界和交叉约束。

## 1. 所有权总表

| 配置组 | 当前所有者 | 目标所有者 | 键数量 | `.env.example` 状态 | 切换阶段 |
| --- | --- | --- | ---: | --- | --- |
| Task Executor/lease/heartbeat/Reaper/SQLite | 2-0 设计资产，当前代码尚未读取 | `app/modules/tasks/adapters/runtime_config.py::TaskRuntimeConfig` | 13 | 尚未加入；必须与 2-1/2-3 实现同一提交加入 | 2-1 定义，2-3 接线 |
| Report runtime/source limits | `app/services/core/config.py::ReportInfrastructureConfig` | `app/modules/report/adapters/runtime_config.py::ReportRuntimeConfig` | 11 | 11/11 已存在 | 2-4 等价迁移，键名/默认值/错误不变 |
| Analysis runtime + execution profile | Core Config 两个配置对象 | `app/modules/analysis/adapters/runtime_config.py` | 16 | 16/16 已存在 | 2-6 等价迁移 |
| Weaponry runtime/profile/terms | `app/modules/weaponry/adapters/infrastructure_config.py` | 同目录 `runtime_config.py::WeaponryRuntimeConfig` | 32 | 均已有配置行或明确兼容注释 | 2-5 纯改名后再改语义 |
| Analysis 前缀的扫描 PDF 引擎 | `OCRConfig`，实际属于 DocumentProcessing 兼容输入 | 后续 DocumentProcessing Runtime Config | 1 | 已存在 | 阶段 2 不顺带迁移 |

`DOCSENSE_ANALYSIS_SCANNED_PDF_ENGINE` 虽以 Analysis 命名，但当前决定权属于文档处理/OCR 装配；
本阶段只登记，避免为了前缀整齐把未触达职责错误搬入 Analysis Executor 配置。

## 2. Task 新键与交叉校验

固定 13 个键：`DOCSENSE_TASK_CONTROL_DB_PATH`、三类 `*_WORKER_COUNT`、
`DOCSENSE_TASK_HEAVY_CONCURRENCY`、heartbeat、Task/Recovery lease、SQLite busy budget、最大时钟
抖动、Executor/Reaper scan 和 stop grace。默认值与 D2-17 保持一致。

除逐键类型/范围外，装配前必须同时验证：

```text
task_lease >= 3 * heartbeat + 2 * sqlite_busy_budget + max_clock_jitter
recovery_lease >= 3 * heartbeat + 2 * sqlite_busy_budget + max_clock_jitter
reaper_scan <= task_lease
stop_grace >= heartbeat + sqlite_busy_budget
```

初始 `heavy_concurrency=1`。三个业务 Worker 数表示可持久化在途/等待槽，不表示三个重任务可并发
调用供应商；提高任何值必须先用真实锁等待、超时和容量证据重新评审。

2-0 不把尚未生效的 Task 键写进 `.env.example`，避免用户误以为当前代码已经读取它们。首次实现
`TaskRuntimeConfig` 时必须把机器资产中的 `plannedEnvExampleLine` 原样或等价写入 `.env.example`，
并把资产状态改为 `present`；实现与样例缺一不可。

## 3. 既有业务键的迁移规则

- Report 11 个键在 2-4 只做路径和类型化对象等价迁移；Callback/cleanup lease 与 HTTP timeout 的
  既有不等式保持不变。
- Analysis 16 个键在 2-6 等价迁移；批次、重试、资源、Callback 与分类 Profile 不允许由 Task
  通用默认值静默覆盖。
- Weaponry 32 个键在 2-5 先把 `infrastructure_config.py` 等价改名为 `runtime_config.py`；策略常量、
  required fingerprint、Production Gate 和 terms 的“关闭时不读取专属键”门禁保持不变。
- `WEAPONRY_ANALYSE_MODE` 只是迁移期拒绝旧配置的 Guard，不是可继续使用的模式开关；2-5 引用/部署
  盘点完成后删除兼容读取，不能带入新 Runtime Config 公共表面。

## 4. 日志与秘密

这些键本身不包含 API Key，但路径、workspace 名和部署能力标识可能暴露环境信息。允许日志记录：
校验后的模式、数值、布尔 Gate，以及 Canonical Config 的 SHA-256；路径只记录摘要。禁止记录真实
`.env` 原文、完整路径、endpoint、Token/API Key、lease token 或业务正文。配置非法时记录键名和
规则，不回显非法原值。

## 5. 当前一致性结论

现有 Report/Analysis/Weaponry/DocumentProcessing 相关键均能在 `.env.example` 找到对应配置行或
明确兼容注释；阶段 2 新 Task 键尚无运行代码，因此登记为“随实现同步加入”。所有键只有一个目标
所有者，未发现需要改变环境键名称或公开接口的事项。
