# `main` 与 `refactor/file-analysis` Legacy Office 集成 M0 执行记录

## 1. 阶段结论

M0“基线冻结”已完成，可以进入 M1“Git 图合并”。本阶段没有执行 `run.py`，没有连接真实
AnythingLLM、模型、Callback 或生产数据库，也没有修改 `docs/接口文档/` 下的任何文件。

## 2. Git 基线

| 项目 | 冻结值 |
| --- | --- |
| 远端与本地 `main` | `fb758cda24ca0550c9ea8cfc76b5a523eb75a16e` |
| M0 开始时 `refactor/file-analysis` | `9776f711a9f49540b20f8c710d934b9c9a67c2a9` |
| 共同基点 | `2c886a94cdeb83189494bcdfbc6486c5b3aa9541` |
| 主线 Legacy Office 功能提交 | `2eee53c0d3a3c86de612a8fedea088118d6aa10a` |
| 分叉计数（`main...refactor/file-analysis`） | `main` 独有 2 个提交；功能分支独有 7 个提交 |

已通过远端只读查询确认 `origin/main` 与本地 `main` 指向相同提交。主线功能提交涉及的 49 个
文件均已登记在
`docs/重构记录/阶段0资产/260728-Legacy-Office主线功能文件处置矩阵.md`，不存在未归类资产。

## 3. 已冻结的集成决策

1. XLS/XLSX 继续只允许一个可解析 Sheet，多 Sheet 必须拒绝并清理本次新建 Folder；
2. 部署样例默认开启 Legacy Office，代码在环境变量缺失时保持安全关闭；
3. 平台范围维持主线现状，不扩大认证声明；
4. 历史 XLSX Folder 只做可观测治理，没有所有权证明时不得全局删除；
5. `/llm/analysis` 保持阶段 1F 新架构，不恢复路由线程或旧 Analysis Worker；
6. 不增加、删除或重命名任何公开接口参数。

## 4. 契约摘要门禁修复

M0 首次运行契约测试时，发现接口权威文档摘要仍是一个无法对应当前正文或历史版本的旧值，且
测试直接对工作树字节求摘要，会受 Windows `core.autocrlf` 影响。经负责人授权后完成永久修复：

- `tests/test_analysis_contract_assets.py` 对 UTF-8 BOM 与 LF/CRLF/CR 进行唯一化后再计算摘要；
- 只规范化存储编码差异，不裁剪空白或改写正文，因此真实接口文字变化仍会触发门禁；
- `tests/contracts/stage1f0_analysis_contracts.json` 更新为当前权威正文的规范 LF 摘要
  `C6656AFB77C7CA6C560F6791C51D5BD3F3A2B7D5F451069BA1BBB94F6DD1A538`；
- 新增 LF、CRLF、UTF-8 BOM + CRLF 三种存储形式摘要一致性测试；
- `lastApprovedAt` 与 `approvedChange` 保持不变，接口文档正文没有变化。

## 5. 离线验收

全部测试使用 `venv\\Scripts\\python.exe -B -m unittest -v`，并按模块分组运行：

| 测试组 | 数量 | 结果 |
| --- | ---: | --- |
| Analysis 契约、预检、批量、Dispatcher、生产适配器和资源恢复 | 59 | `OK` |
| AnythingLLM、Weaponry 术语目录和生产适配器 | 173 | `OK` |
| Report、容器、路由、Callback 与资源恢复 | 171 | `OK` |
| Analysis 应用、隔离、任务适配器、Web 适配器和 Task Service | 90 | `OK` |
| 合计 | 493 | 全部通过 |

测试日志中的部分 `ERROR`/`CRITICAL` 为故障注入用例的预期日志，测试结果没有 Failure、Error
或 Skip。另已通过 `git diff --check`，并确认 `git diff --name-only -- docs/接口文档` 输出为空。

## 6. 阶段后商讨项检查

M0 唯一新增疑问是契约摘要的跨平台稳定性，已经取得授权并完成修复与回归。当前没有新的接口
语义、部署平台或数据清理决策需要确认，可以按总计划进入 M1。
