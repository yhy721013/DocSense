# `/llm/analysis` Top-K 两阶段分类三文件 E2E 验证记录

## 1. 结论

2026-07-15 在 `topk_two_stage` 模式下完成真实 DocSense、AnythingLLM、静态文件服务和 mock callback 的单请求三文件顺序串行验证，最终结论为 `PASS`。

- 三个文件均进入 `status=2`，外部 callback 均成功一次。
- GJB 文件落入数据标准六叶之一；Ford 和 Nimitz 分别落入 CVN-78、CVN-68 目标子树。
- 三个返回 ID 均同时属于完整领域树和本次模型可见候选。
- callback JSON 与 `llm_tasks.result_payload` 逐对象完全一致。
- 候选最大 108，Prompt 最大 10,162 字符，模型调用最大 2 次；均低于 128、32,000 和 4 次上限。
- 召回审计失败数为 0，没有完整领域树 Prompt 降级。

本次验证只证明三个指定文件的正确子树与业务闭环，不代表完整人工 gold set 上的生产准确率已经提升。

## 2. 输入完整性

最终请求严格读取验收提供的 `文件解析领域树.json` 中 `.params[0].architectureList`，未使用默认树、合成树、截断树或重建节点。

| 检查项 | 结果 |
|---|---|
| 源 JSON SHA-256 | `6dfc2b89a424f3ed84a5eed23fcd63b2d0ff0141fcdac495b366ff737c059304` |
| `architectureList` 节点数 | 6,822 |
| 树索引 fingerprint | `e227f3ee75ecaefab32491b22b42d4ef2f52e3885280b8accc32fc7904227101` |
| 请求中三个节点数 | `[6822, 6822, 6822]` |
| 三个列表与源列表逐项深度相等 | 是 |
| 原始节点顺序与字段内容保持 | 是 |
| `architectureStandardList` | 三项均保持源请求的空数组 |

同一个 `POST /llm/analysis` 请求依次使用：

1. `测试文件/GJB 9001C-2017.pdf`
2. `测试文件/Gerald R Ford (CVN 78) class (CVNM)-14-Jul-2023.pdf`
3. `测试文件/Nimitz (CVN 68) class (CVNM) 16-Aug-2023.pdf`

请求全文、数据库、callback 历史和验证 JSON 保存在未跟踪的 `.runtime/e2e/20260715-topk-two-stage-final/`，未提交正文、Prompt 或密钥。

## 3. 最终分类结果

| 文件 | execution ID | 模型原始 ID | 最终 ID 与路径 | 候选 rank | 选择依据 |
|---|---|---:|---|---:|---|
| GJB 9001C-2017 | `63ccba7bdb204421aa92ddcddfe753a8` | 654 | `1779259704774970`，`数据标准/建模与仿真` | 65 | 强 GJB 文件身份将普通分支结果约束到按完整树顺序出现的首个可见数据标准六叶 |
| Gerald R Ford / CVN-78 | `8d9b55033a2341deb420660748de8b82` | `1778670713864013` | `1778670713864013`，`装备目标/海基装备/水面装备/航母/“福特”级航母/CVN-78/基础数据` | 1 | 模型结果通过受限候选和完整树合同 |
| Nimitz / CVN-68 | `41f21d1c2e1046448af6e4354ac8c233` | 56 | `56`，`装备目标/海基装备/水面装备/航母/“尼米兹”级航母/CVN-68` | 93 | 模型返回可见、深度合规的最深可靠父节点 |

GJB 请求的 `architectureStandardList` 为空，因此其 `fileDataItem` 未出现 `militaryName`、`num`、`startTime`、`implTime`、`approvalDept`，符合“六叶参与分类、五个扩展字段仍仅由 `architectureStandardList` 控制”的合同。

## 4. 资源、审计与闭环

| 文件 | 候选数 | Prompt 字符 | 模型调用 | callback | callback/SQLite | 永久存储 ID | workspace slug |
|---|---:|---:|---:|---|---|---:|---|
| GJB 9001C-2017 | 86 | 6,283 | 2 | success，1 次 | 一致 | `1779259704774970` | `architectureid-1779259704774970` |
| Gerald R Ford / CVN-78 | 79 | 7,370 | 2 | success，1 次 | 一致 | 67 | `architectureid-67-57160384` |
| Nimitz / CVN-68 | 108 | 10,162 | 2 | success，1 次 | 一致 | 56 | `architectureid-56` |

Ford 的最终叶节点按现有装备明细归并规则存入父节点 67；Nimitz 已返回父节点 56，直接存入 56。Ford workspace slug 带稳定冲突后缀，是因为缺陷修复前的对照运行已创建同名外部 workspace；本地 `architecture_id -> workspace_slug` 映射和存储 ID 均正确。

每个模型调用都返回至少一个属于当前文档的已验证 source。三个召回决策均已 finalization，`failure_stage` 为空，最终 rank 与 `final_candidates_json` 中的 1-based 位置一致。

## 5. E2E 暴露的问题与修复

缺陷修复前的首次真实运行发现：

- GJB 模型返回合法但错误的普通父节点 654，旧逻辑只在空值或非法 ID 时执行 GJB 兜底。
- Nimitz 的 CVN-68 七叶位于候选 rank 1–7、父节点 56 可见，但模型选择了正文中的部件父节点 515。

提交 `7237449 fix: 约束强文件标识的领域分类` 后：

- 强 GJB 文档身份只由文件名或原始文件名触发；普通装备正文仅引用 GJB 不会被覆盖。
- 模型已返回可见数据标准六叶时保持原结果，否则约束到首个可见六叶。
- 文件名唯一命中带数字的装备标识时，只有完整树中唯一、模型可见、父节点合同合规且具备七类明细叶的装备分支可约束越支结果。
- 多型号、重复同名、边界不匹配和父节点不可见时不做确定性覆盖。
- 约束统一位于解析后的共享主链，回滚模式不会突破模型可见候选。

修复后重新创建隔离运行目录，并完整重跑三个文件；最终结果见第 3 节。

## 6. 可复现命令形状

以下只记录脱敏后的命令形状；实际路径统一落在未跟踪的 E2E runtime 目录。

```bash
zsh scripts/start_test_file_server.sh 8000 "测试文件"
DOCSENSE_RUNTIME_DIR=<e2e-runtime> \
  .venv/bin/python scripts/mock_callback_server.py --port 9000
DOCSENSE_RUNTIME_DIR=<e2e-runtime> \
  DOCSENSE_LLM_TASK_DB=<e2e-runtime>/llm_tasks.sqlite3 \
  DOCSENSE_KNOWLEDGE_BASE_DB=<e2e-runtime>/knowledge_base.sqlite3 \
  DOCSENSE_CHAT_DB=<e2e-runtime>/chat_sessions.sqlite3 \
  FILE_DOWNLOAD_DIR=<e2e-runtime>/llm_downloads \
  CALLBACK_URL=http://127.0.0.1:9000/llm/callback \
  DOCSENSE_ANALYSIS_CLASSIFICATION_MODE=topk_two_stage \
  APP_PORT=5001 APP_DEBUG=false \
  .venv/bin/python run.py
.venv/bin/python -u <e2e-runtime>/run_e2e.py
.venv/bin/python <e2e-runtime>/audit_e2e.py
```

## 7. 原子提交与模块验证记录

| commit | 模块与实际文件 | 对应验证 |
|---|---|---|
| `122fcde docs: 添加领域树分类实施计划` | `docs/plans/260715-llm-analysis-topk-two-stage.md` | `git diff --check` 通过 |
| `adb8211 feat: 新增领域树索引与缓存` | `app/services/core/architecture_tree.py`；`tests/test_architecture_tree.py` | `tests.test_architecture_tree`：20 项通过 |
| `0f2ffcd feat: 新增领域候选召回服务` | `app/services/llm_service/architecture_recall_service.py`；`tests/test_architecture_recall_service.py` | `tests.test_architecture_recall_service`：17 项通过 |
| `12fc3eb feat: 拆分文件分类与字段抽取提示词` | `app/services/core/prompts.py`；`tests/test_analysis_prompts.py` | `tests.test_analysis_prompts`：8 项通过 |
| `a0d3cf8 feat: 扩展文档 RAG 分阶段调用合同` | `app/container.py`；`app/integrations/anythingllm/factory.py`；`app/integrations/anythingllm/policies.py`；`app/integrations/anythingllm/rag_gateway.py`；`app/ports/rag.py`；`tests/fakes/rag.py`；`tests/test_anythingllm_policies.py`；`tests/test_anythingllm_rag_gateway.py`；`tests/test_dependency_container.py`；`tests/test_rag_port_contract.py` | policies/port/gateway/container：96 项通过 |
| `b2153d3 feat: 新增领域召回审计` | `app/services/llm_service/task_service.py`；`tests/test_task_service.py` | `-k recall_audit`：9 项通过 |
| `e0f5ebd feat: 接入文件分析两阶段分类流程` | `app/services/llm_service/analysis_service.py`；`tests/test_analysis_service.py`；`tests/test_analysis_two_stage.py` | 提交前主链 157 项通过；缺陷修复后相同范围 166 项通过 |
| `8225de8 feat: 增加领域分类运行模式配置` | `app/blueprints/llm.py`；`app/container.py`；`app/services/core/config.py`；`tests/test_analysis_classification_config.py`；`tests/test_dependency_container.py` | 配置、容器、chat：33 项通过 |
| `e979c3b test: 添加领域召回基准与大树测试` | `scripts/benchmark_architecture_recall.py`；`tests/test_architecture_recall_benchmark.py` | 4 项通过；完整 6,822 节点树 CVN-78/CVN-68 目标叶均 Top-1 |
| `d7c32a0 docs: 更新文件解析分类说明` | `.env.example`；`README.md`；`docs/接口文档/文件处理和报告生成.md` | 文档对应定向范围 157 项通过；`git diff --check` 通过 |
| `97d4517 fix: 遵循召回基准脚本输出规范` | `scripts/benchmark_architecture_recall.py` | benchmark + no-print：5 项通过 |
| `7237449 fix: 约束强文件标识的领域分类` | `app/services/llm_service/analysis_service.py`；`tests/test_analysis_two_stage.py` | 两阶段主链 28 项、扩展主链 166 项通过；完整树三案例回放通过；最终三文件 E2E PASS |

所有提交均只暂存对应模块文件，没有 squash 或 amend。

## 8. 全量测试基线

最终运行：

```bash
.venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

结果：666 项，14 个失败、4 个错误。新增的 9 个强文件标识回归测试全部通过；失败/错误数量与修复前全量基线一致，没有新增回归。

剩余既有问题集中在未修改范围：被 `.gitignore` 排除的本地 fixture 缺失、`/reassign` 历史断言、MHTML 环境状态、runtime settings、既有 task lifecycle audit 冲突断言，以及 AnythingLLM knowledge gateway 的既有 metadata 冲突用例。本次不跨模块修复这些基线问题。
