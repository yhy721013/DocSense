# `main` 与 `refactor/file-analysis` Legacy Office 集成 M1 执行记录

## 1. 阶段结论

M1“独立集成工作树与 Git 合并”已完成，可以进入 M2。集成分支从已核验的最新 `main` 建立，
再以 `refactor/file-analysis@6f5c64ffc70215b2bdf276239698052938668567` 为第二父分支执行
非快进合并；两个来源分支均未改写历史。

| 项目 | 值 |
| --- | --- |
| 集成分支 | `codex/refactor-file-analysis-main-integration` |
| 独立工作树 | `.runtime/worktrees/file-analysis-integration` |
| 第一父提交 | `main@fb758cda24ca0550c9ea8cfc76b5a523eb75a16e` |
| 第二父提交 | `refactor/file-analysis@6f5c64ffc70215b2bdf276239698052938668567` |
| 实际文本冲突 | 7 个，与 M0 冻结集合完全一致 |

## 2. 冲突处置

| 文件 | 处置结果 |
| --- | --- |
| `.env.example` | 同时保留 Legacy Office 与 Analysis Dispatcher 配置；部署样例按已确认决策设为开启，代码缺省语义不在此改变 |
| `.gitignore` | 同时保留 Legacy Office 离线包与现有运行依赖忽略项 |
| `app/blueprints/llm.py` | 完整保留 Stage 1F 薄路由，拒绝主线旧线程接线 |
| `app/container.py` | 同时保留 Analysis 组合根和共享 Legacy Office Preparer 依赖，生命周期细审留在 M6 |
| `app/services/core/config.py` | 保留 Legacy Office 配置类型；Analysis 常量继续从 Domain 导入，避免重新复制业务规则 |
| `app/services/llm_service/analysis_service.py` | 保留当前兼容实现，不让旧 Service 持有 Legacy Office 生产能力 |
| `tests/test_dependency_container.py` | 保留新受理链策略快照断言；丢弃依赖已删除路由 `task_kwargs` 的旧断言 |

`docs/接口文档/文件处理和报告生成.md` 虽然 Git 可以自动合并，但已主动恢复为功能分支的当前
批准版本。Legacy Office 公开语义将在 M8 完成全部实现与离线验收后再同步，避免提前发布尚未
验收的说明。

## 3. 补充质量修复

合并补丁检查发现两个 Analysis 包标识文件和一份既有执行记录末尾存在多余空行。该问题仅在
这些文件作为相对 `main` 的新增文件时会被 `git diff --check` 暴露；现已删除多余 EOF 空行，
没有改变 Python 或文档语义。

## 4. 门禁证据

全部命令使用项目根目录 `venv` 的绝对 Python 路径，没有执行 `run.py`：

- `git diff --cached --check`：通过；
- `python -B -m compileall -q app`：通过；
- Analysis 切换预检、契约资产、组合根、依赖容器、路由与架构边界：101 项全部通过；
- `/llm/analysis` 路由中 `threading.Thread`、`run_file_analysis_task` 引用数：0；
- 旧 `analysis_service.py` 中 Legacy Office 生产依赖引用数：0；
- 接口权威文档与 `refactor/file-analysis` 当前批准版本逐字一致；
- 测试结果无 Failure、Error 或 Skip。

测试中的供应商能力门禁告警是缺少生产证明文件时的预期 fail-closed 行为，不影响本阶段离线
验收，也不构成多实例或生产可用证明。

## 5. 阶段后商讨项检查

实际冲突没有超出 M0 冻结范围，处置方式均属于已确认计划。当前没有新增接口字段、平台范围、
清理所有权或任务一致性决策需要确认，可以进入 M2。
