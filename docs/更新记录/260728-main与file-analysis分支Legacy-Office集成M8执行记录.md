# `main` 与 `refactor/file-analysis` Legacy Office 集成 M8 执行记录

## 1. 阶段结论

M8 已完成接口语义和项目记录同步。此次变更只补充已经实现并通过离线门禁的 Legacy Office、
XLS/XLSX 单 Sheet、失败关闭和只读库存治理语义，不增加、删除、重命名任何 HTTP、Callback、
Progress、SSE 或 WebSocket 参数，也不改变公开状态码、Header 和响应结构。

## 2. 文档与契约变更

1. `docs/接口文档/文件处理和报告生成.md`
   - 明确 `/llm/analysis` 和报告来源文件对 `.doc/.ppt/.xls` 的支持方式；
   - 明确转换失败时不把旧格式原文件直接上传到 AnythingLLM；
   - 明确 XLS/XLSX 只允许一个可解析 Sheet，多 Sheet 请求按既有失败契约处理；
   - 明确内部转换文件名和 Folder 名不替代公开业务标识。
2. `README.md`
   - 同步 Analysis 唯一生产链、部署默认开启与代码缺省安全关闭的区别；
   - 补充 XLSX Folder 只读库存诊断和“无所有权证明不删除”的治理边界。
3. 更新重构记录与更新记录索引。
4. 重新计算 `tests/contracts/stage1f0_analysis_contracts.json` 中的接口文档权威摘要；除摘要外，
   Analysis 公共契约金标内容没有变化。

## 3. 验收证据

使用项目虚拟环境执行：

```powershell
venv\Scripts\python.exe -B -m unittest -q `
  tests.test_analysis_contract_assets `
  tests.test_report_contract `
  tests.test_stage0_contract_assets `
  tests.test_legacy_office_config
```

结果：共执行 35 项测试，0 failure、0 error、0 skip，全部通过。

另执行 `git diff --check`，没有空白错误。契约差异复核确认：

- 接口权威摘要由文档语义更新引起；
- 请求字段、响应字段、状态码、Header 和 Callback 结构均未变化；
- 未执行 `run.py`，未连接真实 AnythingLLM、模型、Callback 或生产数据库。

## 4. 阶段后商讨检查

没有发现需要新增产品或接口决策的事项。部署默认开启、代码缺省安全关闭、XLS/XLSX 单 Sheet、
失败关闭以及历史 Folder 只读治理均与负责人已确认决策一致，可以进入 M9 离线关闭验收。
