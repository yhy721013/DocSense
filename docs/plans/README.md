# 历史计划目录说明

本目录保存较早期的方案与设计档案。它们可用于理解项目演进背景，但不自动代表当前实现、当前数据模型或当前接口契约。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `260309-llm-integration.md`、`260309-llm-integration-design.md` | 早期 LLM 集成计划与设计。 |
| `260311-llm-analysis-range.md`、`260311-llm-analysis-range-design.md` | 分析范围能力的计划与设计。 |
| `260311-llm-architecture-classification.md` | LLM 架构分类相关方案。 |
| `260316-llm-multi-task.md`、`260316-llm-multi-task-design.md` | 多任务能力的计划与设计。 |
| `260319-mhtml-analysis-report.md`、`260319-mhtml-analysis-report-design.md` | MHTML 分析报告相关方案。 |

## 与文件对话改造的关系

阶段 1～12 的主文件对话计划已迁入 `docs/重构记录/260707-文件对话功能改造计划.md`，与当前有效的跨业务重构路线统一维护。本目录不再新增该改造的活动计划。

## 使用规则

- 阅读历史计划时，必须与当前接口文档、重构记录、更新记录和代码实现交叉验证。
- `260316` 多任务文档中的 check-task JSON 返回以及 Progress 显式 subscribe/query/unsubscribe/ack 只记录历史实现；2026-07-15 的目标契约已批准删除这些公开输出，以当前接口文档和 `docs/重构记录/` 为准。
- 不要依据历史计划恢复已删除的文件对话结构，例如旧的 `llm_service/chat_service.py`。
- 若需新建计划，应放入符合当前项目约定的位置，并明确它是提案、实施中还是已完成记录。
