# 阶段 1H-2：Legacy Office 通用 Artifact 纳管执行记录

## 0. 执行结论

阶段 1H-2 已完成并通过离线门禁：

- 将唯一 LibreOffice 转换内核和可信 OOXML 校验器迁入
  `app/modules/document_processing/adapters/libreoffice/`；
- 原 `libreoffice.py`、`ooxml_validator.py` 保留为无转换实现的兼容 Facade；
- 新增冻结实际 LibreOffice 版本、策略指纹和源/目标格式的 Processing Profile；
- 新增 `LibreOfficeDocumentProcessorAdapter`，通过 Artifact 流读取源内容，只在 Adapter 内部物化路径；
- 由 `PrepareDocument` 发布 `normalized/ooxml` Artifact，并原子提交 source→child Lineage；
- 同一步骤重复执行只复用已校验 Artifact，不再次运行转换；
- 保持 Container 既有 preflight-before-dispatcher、稳定版 26.2.x、默认关闭和显式开启语义；
- 完成 143 项新旧定向扩大回归，失败 0、错误 0、跳过 3；
- 未运行 `run.py`，未执行真实 LibreOffice Smoke，未修改接口文档或部署参数。

---

## 1. 唯一转换实现与兼容 Facade

迁移后的结构：

```text
app/modules/document_processing/
├── libreoffice.py                         # 旧公开导入 Facade
├── ooxml_validator.py                     # 旧脚本/导入 Facade
└── adapters/libreoffice/
    ├── engine.py                          # 唯一 LibreOffice 安全内核
    ├── ooxml_validator.py                 # 唯一可信 ZIP/OOXML 校验实现
    ├── profile.py                         # 冻结 profile 构造
    └── processor.py                       # 通用 DocumentProcessor Adapter
```

AST 门禁确认：

- `LibreOfficeLegacyOfficePreparer` 只在 `engine.py` 定义一次；
- `validate_ooxml_archive` 只在 Adapter 包定义一次；
- 两个根 Facade 不定义类或函数，不导入 subprocess/shutil/tempfile；
- Analysis、Report、Container 仍可使用旧包根导入，不形成第二套转换路径。

原 Legacy Office 测试只调整内部实现 patch/import 位置，所有安全、进程、版本、格式、超时、大小、
ZIP、清理和并发断言保持。

---

## 2. Profile 与 Processor

`create_legacy_office_profile` 冻结：

- `sourceSuffix`：`.doc/.ppt/.xls`；
- `targetSuffix`：`.docx/.pptx/.xlsx`；
- `libreofficeVersion`：preflight 已确认的实际版本；
- `policyFingerprint`：调用方冻结的部署/格式策略；
- `processor_fingerprint`：上述事实的稳定 SHA-256。

Processor 执行时再次比较实际 preflight 版本；发生漂移时以
`snapshot_version_mismatch` 在物化和转换前失败。Adapter 继续保留原
`LegacyOfficeConversionError.code`，迁移不会把精确错误退化为通用异常。

Processor 的路径边界：

1. 通过 `ArtifactStorePort.open_reader` 获取源流；
2. 在带所有权标记的随机任务目录物化原后缀文件；
3. 调用唯一 `LegacyOfficePreparer`；
4. 校验 converted/source suffix/target suffix 与冻结 profile 一致；
5. 返回带幂等清理租约的 `ProcessorOutput`，不向 Application 返回路径；
6. Application 发布并提交记录后关闭候选租约；
7. 清理中断不撤销已提交成功，启动巡检只删除受控根下带标记的直接 `job-*` 子目录。

转换失败没有 raw fallback，不能把原 `.doc/.ppt/.xls` 冒充派生 OOXML。

---

## 3. Artifact 与 Lineage 所有权

新链的成功事实是：

```text
source Artifact
  -- libreoffice-legacy-office / frozen profile -->
normalized OOXML Artifact
```

`PrepareDocument` 负责：

- 将待发布候选流式复制到确定性 Artifact；
- 复核 SHA-256、size、media type；
- 在一个短 SQLite 事务中提交 Artifact 元数据、Lineage 与步骤成功状态；
- 记录提交失败时保留已发布 Artifact 并返回 `outcome_unknown`；
- 仅在完成上述处理后释放 LibreOffice job 与物化 scratch。

旧 `LegacyOfficePreparer.prepare(path)` 仍为 Analysis/Report 兼容 Facade，其生命周期将在 1H-6
调用方切换后退出生产编排；底层算法与新 Processor 共用同一个 `engine.py`。

---

## 4. 验证结果

所有测试使用 `venv\Scripts\python.exe -B`，未启动主进程。

### 4.1 新增门禁

覆盖：

- `.doc → normalized/ooxml` Artifact 与 source→child Lineage；
- 同步骤幂等复用，不进行第二次转换；
- LibreOffice 版本漂移在物化前拒绝；
- 转换失败无 raw fallback；
- 物化清理中断不反转成功，后续所有权巡检可清理；
- 50 个任务的源、物化目录、派生 Artifact 和 Processing Record 隔离。

### 4.2 新旧扩大回归

覆盖：

- Legacy Office Config/Conversion/Delivery；
- Dependency Container；
- Analysis Production Adapter；
- Report I/O/Runtime Adapter；
- 1H Domain/Application/Artifact/Record/Architecture/Baseline。

```text
Ran 143 tests
OK (skipped=3)
```

跳过项为既有平台条件及 Windows 符号链接权限。故障注入产生的 ERROR/WARNING 日志不是测试失败。

### 4.3 静态检查

- `compileall` 通过；
- `git diff --check` 通过，仅输出预期 CRLF 提示；
- 代码搜索证明 LibreOffice 类和 OOXML 校验入口各只有一个实现；
- 1H-0 接口文档只读 Hash 门禁通过。

---

## 5. 阶段边界与下一步

1H-2 不代表：

- Analysis/Report 已切换新 Artifact 链；
- MHTML、MinerU/OCR 或 Translation 已解耦；
- 真实 Windows/macOS LibreOffice Smoke 已在本轮重跑；
- 多实例、可靠队列、MySQL/MinIO 或跨实例 fencing 已完成。

阶段复核未发现需要修改公开接口、部署参数或 Legacy Office 失败策略的事项，可以进入 1H-3，
迁移 MHTML 纯规则与浏览器转换边界。
