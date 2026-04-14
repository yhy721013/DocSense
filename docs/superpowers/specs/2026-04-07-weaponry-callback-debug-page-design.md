# 武器装备回调调试页扩展设计

## 1. 背景

DocSense 当前已经提供本地只读调试页，用于展示最近一次写入 `.runtime/call_back.json` 的回调结果。现状上：

1. `businessType=file` 已有结构化展示。
2. `businessType=report` 已有结构化展示。
3. `businessType=weaponry` 后端接口、任务流转、回调写盘均已实现，但调试页仍走“未支持类型”的兜底分支。

本次目标是在不引入新前端工程、不改动 `weaponry` 主业务流程的前提下，把知识谱系解析结果纳入本地调试页展示，并补齐本地联调入口。

## 2. 目标

本期需要完成以下目标：

1. 在现有调试页中为 `businessType=weaponry` 提供结构化展示。
2. 展示逻辑基于 `weaponryTemplateFieldList` 的通用模板结构实现，而不是写死某一套业务字段。
3. 对 `INPUT` 和 `TABLE` 两种字段类型都提供稳定展示。
4. 每个字段的 `analyseDataSource` 以折叠方式查看，默认不展开。
5. 保留现有原始 JSON 区域作为调试兜底。
6. 补齐 `weaponry` 的本地联调样例和双平台脚本。
7. 在 README 中把 `weaponry` 纳入本地联调说明。
8. 为页面与脚本新增回归测试，后续验证优先在本机 macOS 环境执行。

## 3. 非目标

以下内容明确不在本期范围内：

1. 不引入 React、Vue、Vite、Next.js 等前端工程。
2. 不修改 `app/services/llm_service/weaponry_service.py` 的字段提取逻辑。
3. 不改动 `.runtime/call_back.json` 的写盘结构。
4. 不按“舰级名称”“建造厂”等字段名做专门分组映射。
5. 不增加历史回调列表、搜索、下载、自动轮询或实时推送。
6. 不增加浏览器级 E2E 自动化。
7. 不在本期处理 `reassign`、`chat` 等其他业务类型的本地调试展示。

## 4. 约束与前提

1. 当前调试页仍采用 Flask 模板 + 原生 JavaScript，无独立前端工程。
2. 调试页数据源仍是仓库根目录 `.runtime/call_back.json`。
3. 回调文件由 `app/services/utils/callback_client.py` 在发送回调前写入；因此本地查看页面时，仍建议配置 `CALLBACK_URL` 并启动模拟回调服务。
4. `weaponry` 回调成功时的主要结果位于 `payload.data.weaponryTemplateFieldList`。
5. `weaponry` 字段类型至少支持 `INPUT` 与 `TABLE`，其中 `TABLE.tableFieldList` 为二维数组结构。
6. 当前仓库已有 Windows 版 `scripts/test_llm_weaponry.ps1`，缺少 macOS 对应的 `zsh` 脚本。
7. 当前仓库中的 `tests/fixtures/llm/weaponry_request.json` 使用的是人事示例，本期需要替换为舰艇字段示例。
8. 本期验证顺序以本机 macOS 环境优先，Windows 脚本保持结构一致但不作为首轮验证环境。

## 5. 方案选择

### 5.1 备选方案

#### 方案 A：按模板结构通用渲染 `weaponry`

调试页新增 `weaponry` 专用渲染分支，直接消费 `weaponryTemplateFieldList`，递归处理 `INPUT` 与 `TABLE`，字段证据链折叠展示。

#### 方案 B：常见舰艇字段做固定分组，未知字段走通用回退

对“舰级名称、建造厂、航速”等已知字段做人工分组，其余字段按列表展示。

#### 方案 C：只增加浅层摘要和原始 JSON

页面识别 `weaponry` 后，仅展示 `architectureId`、状态和原始 JSON，不做字段级结构化增强。

### 5.2 结论

本期采用方案 A。

原因如下：

1. 与后端协议天然一致，最符合 `weaponryTemplateFieldList` 的模板驱动设计。
2. 不会把前端绑定在当前这批舰艇字段上，未来新增字段可直接显示。
3. 能同时覆盖 `INPUT` 和 `TABLE`，避免后续再补二次设计。
4. 溯源折叠展示既保留调试价值，也避免页面信息密度失控。

## 6. 架构与改动边界

本期只增强本地调试能力，不触碰 `weaponry` 解析主链路。

### 6.1 保持不变的部分

1. `app/blueprints/debug.py` 的路由职责不变，继续只负责返回页面和 JSON 数据接口。
2. `app/services/utils/callback_preview.py` 不做协议裁剪，继续返回完整 payload。
3. `app/services/llm_service/weaponry_service.py` 的任务执行、回调构建和回调发送逻辑不修改。
4. `.runtime/call_back.json` 的来源、路径和写盘方式保持不变。

### 6.2 新增和调整的部分

1. 在 `app/templates/debug/callback.html` 中新增 `weaponry` 渲染分支。
2. 为 `weaponry` 增加字段统计与结构化展示函数。
3. 新增 macOS 脚本 `scripts/test_llm_weaponry.sh`，风格与现有 `test_llm_analysis.sh`、`test_llm_report.sh` 保持一致。
4. 更新 `tests/fixtures/llm/weaponry_request.json` 和 `tests/fixtures/llm/check_task_weaponry_request.json`。
5. 扩展 README 的本地联调章节与调试页说明。
6. 扩展测试覆盖，确保页面模板和本地脚本都纳入回归。

## 7. 页面信息架构

`weaponry` 继续沿用现有调试页的四个区域，只调整每个区域的内容组织方式。

### 7.1 顶部状态区

展示以下信息：

1. `businessType`
2. `msg`
3. `architectureId`
4. `status`
5. 字段统计信息

状态映射规则：

1. `status="2"` 显示“解析成功”
2. `status="3"` 显示“解析失败”
3. 其他状态显示“未知状态（原始值）”

字段统计信息通过遍历 `weaponryTemplateFieldList` 计算，至少包含：

1. 字段总数
2. 已提取字段数
3. 表格字段数

### 7.2 结构化结果区

#### 7.2.1 INPUT 字段

每个 `INPUT` 字段展示：

1. `fieldName`
2. `fieldDescription`
3. `analyseData`
4. 折叠式溯源区域

展示规则：

1. 空值统一显示“暂无内容”。
2. `fieldDescription` 缺失时显示为空说明，不影响字段主值渲染。
3. 不对字段名做硬编码分组或映射。

#### 7.2.2 TABLE 字段

每个 `TABLE` 字段展示：

1. 表格标题，即 `fieldName`
2. 字段说明，即 `fieldDescription`
3. `tableFieldList` 的二维结构内容

表格单元格仍保留字段语义，而不是只输出裸文本：

1. 单元格标题为子字段 `fieldName`
2. 主值为 `analyseData`
3. 单元格内保留折叠式溯源入口

这样既能看清表格结构，又不丢失单元格的调试信息。

### 7.3 溯源展示区

`analyseDataSource` 默认折叠，避免页面被大量来源片段淹没。

每条溯源记录展示：

1. `content`
2. `source`
3. `time`
4. `translate`

展示规则：

1. 折叠标题显示来源数量，例如“查看溯源（3）”。
2. 即使来源为空，也展示统一空状态，避免用户误判为页面漏渲染。
3. 溯源只在字段上下文中展开，不单独做全局预览区。

### 7.4 失败态展示

若 `weaponry` 回调为失败态：

1. 顶部状态区正常展示任务摘要。
2. 若 payload 中不存在字段结果，则结构化结果区显示失败说明，不渲染空字段卡片。
3. 页面底部仍保留原始 JSON 作为诊断入口。

### 7.5 原始 JSON 区

页面底部继续保留格式化后的完整 payload，不做裁剪。该区是任何结构化展示遗漏时的最终兜底。

## 8. 联调样例与脚本设计

### 8.1 请求样例

`tests/fixtures/llm/weaponry_request.json` 改为舰艇字段样例，请求中的 `weaponryTemplateFieldList` 至少包含以下 20 个 `INPUT` 字段：

1. 舰级名称
2. 单舰名称
3. 舷号
4. 建造厂
5. 开工时间
6. 下水时间
7. 服役时间
8. 状态
9. 标准排水量
10. 满载排水量
11. 舰长
12. 舰宽
13. 吃水
14. 甲板长度
15. 甲板宽度
16. 航速
17. 编制
18. 动力系统
19. 武器系统
20. 传感器系统

约束如下：

1. 所有字段均为 `fieldType="INPUT"`。
2. 请求阶段不传非空 `analyseData` 和 `analyseDataSource`。
3. 每个字段提供明确的 `fieldDescription`，用于驱动模型抽取。
4. `architectureId` 取值与 `check_task_weaponry_request.json` 保持一致。

### 8.2 脚本设计

Windows：

1. 保持 `scripts/test_llm_weaponry.ps1` 存在。
2. 默认 payload 路径继续指向 `tests/fixtures/llm/weaponry_request.json`。

macOS：

1. 新增 `scripts/test_llm_weaponry.sh`。
2. 与现有 shell 脚本保持统一结构：
   - 使用 `#!/bin/zsh`
   - `set -euo pipefail`
   - `source scripts/_script_common.sh`
   - 自动读取 `.env/.env.example`
   - 默认推导 `BASE_URL`
   - 使用 `post_json` 调用 `${BASE_URL}/llm/weaponry`

### 8.3 README 更新

README 的本地联调与测试章节需要补充以下内容：

1. PowerShell 命令列表中增加 `test_llm_weaponry.ps1`
2. macOS 命令列表中增加 `test_llm_weaponry.sh`
3. “脚本默认行为”中增加 `POST /llm/weaponry`
4. “可选参数示例”中增加 `weaponry` 调用示例
5. “本地调试页联调建议”中把触发接口扩展为 `analysis`、`generate-report` 或 `weaponry`
6. 明确调试页依赖回调落盘，建议联调时配置 `CALLBACK_URL` 并启动模拟回调服务

## 9. 测试与验证策略

### 9.1 自动化测试范围

扩展以下测试：

1. `tests/test_callback_debug_routes.py`
   - 增加 `weaponry` payload 的 API 读取测试
   - 增加页面模板中 `weaponry` 渲染钩子的断言
   - 增加 `businessType === "weaponry"` 分支断言

2. `tests/test_local_scripts.py`
   - 增加 `weaponry` 脚本测试
   - 校验脚本请求路径为 `/llm/weaponry`
   - 校验脚本提交内容与 `weaponry_request.json` 一致

3. `tests/test_test_assets.py`
   - 增加或更新 `weaponry_request.json` 的结构断言
   - 明确校验舰艇字段清单存在且顺序正确

### 9.2 首轮验证环境

本期后续验证先在本机 macOS 环境执行，原因如下：

1. 当前新增的脚本实现首先补的是 `zsh` 版本。
2. 调试页联调场景以本地开发机为主，优先验证实际使用路径。
3. 先在单一环境收敛页面和脚本行为，再补充跨平台确认更稳妥。

首轮建议验证链路：

1. 启动服务：`python run.py`
2. 启动模拟回调服务：`python scripts/mock_callback_server.py`
3. 使用 macOS 脚本触发 `weaponry` 请求
4. 打开 `http://127.0.0.1:5001/debug/callback`
5. 核对 `weaponry` 的结构化展示、折叠溯源和原始 JSON

### 9.3 验收标准

满足以下条件即可认为本期目标达成：

1. `weaponry` 请求可通过 macOS 脚本正常发起。
2. 调试页识别 `businessType=weaponry` 后不再走“未支持类型”兜底。
3. `weaponryTemplateFieldList` 中的 `INPUT` 字段可正确展示主值。
4. `weaponryTemplateFieldList` 中的 `TABLE` 字段可按二维结构展示。
5. 每个字段的 `analyseDataSource` 可折叠查看。
6. README 中存在 `weaponry` 的双平台联调说明。
7. 原始 JSON 区仍保留完整 payload。

## 10. 风险与处理

1. 风险：`weaponryTemplateFieldList` 层级不规则
   - 处理：渲染器按类型和结构逐层判断，遇到异常结构时回落到空状态或兜底展示，不让整页报错。

2. 风险：字段很多时页面过长
   - 处理：证据链折叠，表格字段独立成块，避免默认全展开。

3. 风险：未来模板字段变化
   - 处理：采用通用模板渲染，不引入字段名硬编码分组。

4. 风险：未配置回调地址时 `.runtime/call_back.json` 不更新
   - 处理：README 明确联调前提，并在验证步骤中保留模拟回调服务。

## 11. 影响文件

预计实施阶段会涉及以下文件：

1. `app/templates/debug/callback.html`
2. `scripts/test_llm_weaponry.sh`
3. `scripts/test_llm_weaponry.ps1`
4. `tests/fixtures/llm/weaponry_request.json`
5. `tests/fixtures/llm/check_task_weaponry_request.json`
6. `tests/test_callback_debug_routes.py`
7. `tests/test_local_scripts.py`
8. `tests/test_test_assets.py`
9. `README.md`

## 12. 与既有设计的关系

`docs/superpowers/specs/2026-04-01-callback-debug-page-design.md` 定义了调试页的初版范围，其中将 `weaponry` 明确排除在外。本设计是在不推翻初版总体架构的前提下，对该页面进行增量扩展。
