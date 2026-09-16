# Legacy Office 主线功能文件处置矩阵

## 0. 资产信息

| 项目 | 内容 |
| --- | --- |
| 生成日期 | 2026-07-28 |
| 来源提交 | `2eee53c0d3a3c86de612a8fedea088118d6aa10a` |
| 来源主题 | `feat: add legacy Office local conversion support` |
| 文件总数 | 49 |
| 目标分支 | `main` 与 `refactor/file-analysis` 的独立集成分支 |
| 上位计划 | `docs/重构记录/260728-main与file-analysis分支Legacy-Office集成实施计划.md` |

本矩阵冻结 `main` 功能提交的全部文件范围。任何一项只有在对应阶段完成代码审查、定向测试和
最终处置复核后才能标记关闭。`继承` 不代表无须审查；`语义移植` 表示保留功能但不得照搬旧
Analysis 执行链；`人工合并` 表示 Git 自动结果也必须逐段复核。

## 1. 逐文件处置

| # | 来源文件 | 处置 | 阶段 | 验收重点 |
| ---: | --- | --- | --- | --- |
| 1 | `.env.example` | 人工合并 | M6 | 部署显式默认开启，代码缺省安全关闭；保留当前全部任务配置 |
| 2 | `.gitignore` | 人工合并 | M1/M2 | 保留双方忽略项，只忽略可再生离线包和运行产物 |
| 3 | `README.md` | 人工合并 | M8 | 只描述最终真实生产链和已验证平台边界 |
| 4 | `app/blueprints/llm.py` | 当前架构优先、拒绝旧接线 | M1/M5 | `/llm/analysis` 只调用 Stage 1F Submit，不创建路由线程 |
| 5 | `app/container.py` | 人工组合 | M6 | 单一 Preparer、Preflight 先于 Dispatcher、失败释放生命周期 |
| 6 | `app/integrations/anythingllm/documents.py` | 人工合并 | M3 | 单 Sheet 成功，多 Sheet 整 Folder 拒绝/清理 |
| 7 | `app/integrations/anythingllm/errors.py` | 人工合并 | M3 | 清理不确定错误携带 opaque Token，不泄漏路径 |
| 8 | `app/integrations/anythingllm/knowledge_gateway.py` | 人工合并 | M3 | 新单 Sheet 替换只解绑旧 Sheet，不全局删除旧 Folder |
| 9 | `app/integrations/anythingllm/models.py` | 人工合并 | M3 | Sheet location 严格身份、路径规范化和重复拒绝 |
| 10 | `app/integrations/anythingllm/rag_gateway.py` | 人工合并 | M3/M5 | Cleanup Token 生命周期、单文档来源隔离保持成立 |
| 11 | `app/modules/document_processing/__init__.py` | 继承并审查 | M2 | 只导出稳定 Domain/Port/Adapter，不反向依赖业务模块 |
| 12 | `app/modules/document_processing/domain.py` | 继承并审查 | M2 | 不可变配置/结果、稳定错误和幂等清理 |
| 13 | `app/modules/document_processing/libreoffice.py` | 继承并审查 | M2 | OLE2、进程、Profile、大小、超时、并发和清理边界 |
| 14 | `app/modules/document_processing/ooxml_validator.py` | 继承并审查 | M2 | ZIP/ZIP64、成员、解压量、路径和超时安全门禁 |
| 15 | `app/modules/document_processing/ports.py` | 继承并审查 | M2 | 供应商无关准备 Port，不携带业务 Callback |
| 16 | `app/modules/report/adapters/anythingllm_rag.py` | 人工合并 | M4 | 单 Sheet、来源身份和清理事件进入 Report 资源事实 |
| 17 | `app/modules/report/adapters/legacy_files.py` | 人工合并 | M4 | 转换产物发布到任务目录，失败不回退 raw 文件 |
| 18 | `app/modules/report/application/run_report.py` | 人工合并 | M4 | 内部文件名清洗、任一坏源使报告整体失败 |
| 19 | `app/modules/report/domain/__init__.py` | 人工合并 | M4 | 只导出纯规则，不导出供应商实现 |
| 20 | `app/modules/report/domain/rules.py` | 人工合并 | M4 | Unicode 安全文件名、严格 percent decode、碰撞 fail-closed |
| 21 | `app/services/core/config.py` | 人工合并 | M6 | 严格配置、代码缺省 false、版本/限额固定语义 |
| 22 | `app/services/llm_service/analysis_service.py` | 不接生产链，仅兼容审查 | M1/M5 | 不持有新 Converter，不恢复旧 Analysis Worker |
| 23 | `docs/接口文档/文件处理和报告生成.md` | 已确认语义后人工更新 | M8 | 不增删参数；写明部署开关、单 Sheet、失败和清理语义 |
| 24 | `scripts/legacy_office/BUNDLE_README.md` | 继承并审查 | M2 | 离线包边界、无自动安装和验证步骤 |
| 25 | `scripts/legacy_office/Install-Windows.ps1` | 继承并审查 | M2/M10 | Windows x64 安装安全、显式目标和失败退出 |
| 26 | `scripts/legacy_office/Preflight-Windows.ps1` | 继承并审查 | M2/M10 | 版本、三格式、进程残留和稳定退出码 |
| 27 | `scripts/legacy_office/README.md` | 继承并审查 | M2/M8 | 只声明 Windows x64/macOS Apple Silicon 当前资产和真实认证状态 |
| 28 | `scripts/legacy_office/THIRD_PARTY_NOTICES.md` | 继承并审查 | M2 | 来源、许可证和版本一致 |
| 29 | `scripts/legacy_office/artifacts.lock.json` | 继承并审查 | M2 | 现有两平台 URL、版本、架构和 SHA-256 固定 |
| 30 | `scripts/legacy_office/bundle_manifest.template.json` | 继承并审查 | M2 | Manifest 字段与打包脚本严格一致 |
| 31 | `scripts/legacy_office/fetch_apache_poi_sample.py` | 继承并审查 | M2 | 固定提交和 Hash，不信任远端文件名 |
| 32 | `scripts/legacy_office/fetch_assets.py` | 继承并审查 | M2 | 原子发布、Hash 不匹配不覆盖、平台白名单 |
| 33 | `scripts/legacy_office/install_macos.sh` | 继承并审查 | M2/M10 | 当前仅 macOS Apple Silicon 资产，不扩大认证声明 |
| 34 | `scripts/legacy_office/package_offline.py` | 继承并审查 | M2 | 离线包可重复校验，默认不覆盖 |
| 35 | `scripts/legacy_office/preflight_macos.sh` | 继承并审查 | M2/M10 | 版本、权限、进程和三格式 Smoke |
| 36 | `scripts/legacy_office/smoke_test_macos.py` | 继承并审查 | M2/M10 | 隔离样例、OOXML 结构和无残留进程 |
| 37 | `tests/test_analysis_service.py` | 测试语义移植 | M5 | Legacy 场景迁入新 Analysis Adapter/Application，旧服务不成生产入口 |
| 38 | `tests/test_anythingllm_documents.py` | 人工合并 | M3 | 单 Sheet、多个 Sheet、畸形响应和 Folder 清理三态 |
| 39 | `tests/test_anythingllm_knowledge_gateway.py` | 人工合并 | M3 | 单 Sheet 替换、旧 Folder 保留和恢复 |
| 40 | `tests/test_anythingllm_rag_gateway.py` | 人工合并 | M3 | 上传/绑定/来源/清理事件与 outcome_unknown |
| 41 | `tests/test_dependency_container.py` | 人工合并 | M6 | 默认开关、Fake 注入、共享 Preparer 和失败释放 |
| 42 | `tests/test_legacy_office_config.py` | 继承并调整默认断言 | M2/M6 | 部署显式 true、代码缺省 false、严格非法配置 |
| 43 | `tests/test_legacy_office_conversion.py` | 继承并审查 | M2 | 转换安全、并发、超时、清理和脱敏 |
| 44 | `tests/test_legacy_office_delivery.py` | 继承并审查 | M2 | 两平台锁文件、脚本、包和许可证 |
| 45 | `tests/test_report_application.py` | 人工合并 | M4 | 多来源顺序、失败终态和公开内容清洗 |
| 46 | `tests/test_report_io_adapters.py` | 人工合并 | M4 | Legacy 转换、单 Sheet、任务目录和 raw fallback 禁止 |
| 47 | `tests/test_report_rag_adapter.py` | 人工合并 | M4 | 单 Sheet RAG、来源身份和清理资源事实 |
| 48 | `tests/test_task_service.py` | 人工合并 | M3/M7 | Folder Cleanup Token 生命周期、旧记录兼容和只读诊断 |
| 49 | `tests/test_weaponry_production_adapters.py` | 人工合并 | M3/M7 | Markdown/普通文档不被误判为 XLSX |

## 2. 数量自检

| 处置类别 | 数量 |
| --- | ---: |
| 继承并审查（含默认断言调整） | 21 |
| 人工合并/组合 | 24 |
| 当前架构优先、语义移植或兼容审查 | 3 |
| 已确认后更新接口文档 | 1 |
| 合计 | 49 |

> 数量分类只用于证明没有漏文件，不代表工作量。最终关闭时必须以逐行状态和对应测试证据为准。
