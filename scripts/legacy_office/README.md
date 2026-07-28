# Legacy Office 离线交付工具

本目录只保存可审计的构建材料；LibreOffice 安装包、三份 OLE2 smoke 样本、展开目录和
最终 zip 均写到 Git 已忽略的 `dist/legacy-office/`，不得加入 Git/LFS。

锁定版本为 LibreOffice 26.2.5：

| 交付平台 | 官方安装包 | 认证状态 |
|---|---|---|
| Windows x64 | `LibreOffice_26.2.5_Win_x86-64.msi` | 静态/mock 验证；未实机认证 |
| macOS Apple Silicon | `LibreOffice_26.2.5_MacOS_aarch64.dmg` | 必须完成真实 smoke 后交付 |

官方 URL、官方 SHA-256 URL、锁定 SHA-256、版本和架构位于
`artifacts.lock.json`。`fetch_assets.py` 不信任下载退出状态或文件名，只有内容哈希
匹配才会原子发布到下载目录；已存在但哈希不匹配的文件不会被覆盖。

## 1. 下载和校验官方资产

在仓库根目录执行：

```bash
.venv/bin/python scripts/legacy_office/fetch_assets.py --platform macos-arm64
.venv/bin/python scripts/legacy_office/fetch_assets.py --platform windows-x64
```

已完全离线时可只校验：

```bash
.venv/bin/python scripts/legacy_office/fetch_assets.py \
  --platform all \
  --verify-only
```

## 2. 准备 Git 外 smoke 样本

样本目录必须包含：

- `word-sample.doc`
- `excel-sample.xls`
- `powerpoint-2002-apache-poi.ppt`

`word-sample.doc` 和 `excel-sample.xls` 使用锁定的项目方本地最小样本。Apache POI 的
PPT 必须使用固定提交原件，可安全下载到 Git 外目录：

```bash
.venv/bin/python scripts/legacy_office/fetch_apache_poi_sample.py \
  --samples-dir dist/legacy-office/samples
```

构建器会同时校验 OLE2 签名与 `artifacts.lock.json` 中的 SHA-256。此前外部目录中
存在一份被应用重写过、哈希为
`549bac2fedcc883f00df81e70c5684e242603f2a00c710da680a32f49173a558`
的同名 PPT；该文件不是固定 Apache POI 提交的原始字节，构建器会 fail-closed 拒绝
打包。固定原件 SHA-256 是
`7d485f5d3fbfc18191854b3a6c370e7195df736442e29799d38786c62259173f`。

## 3. 构建平台离线包

```bash
.venv/bin/python scripts/legacy_office/package_offline.py \
  --platform macos-arm64 \
  --samples-dir dist/legacy-office/samples

.venv/bin/python scripts/legacy_office/package_offline.py \
  --platform windows-x64 \
  --samples-dir dist/legacy-office/samples
```

输出分别为：

- `dist/legacy-office/docsense-libreoffice-26.2.5-macos-arm64-offline.zip`
- `dist/legacy-office/docsense-libreoffice-26.2.5-windows-x64-offline.zip`

同名 zip 已存在时默认拒绝覆盖；人工确认替换后显式传 `--overwrite`。每个包包含官方
安装包、`MANIFEST.json`、`SHA256SUMS`、许可证/来源说明、平台安装/preflight 脚本
和三份 smoke 样本。DocSense 启动时不会从离线包自动安装软件。

## 4. 发布门禁

- macOS Apple Silicon：在目标机运行 `install.sh`、`preflight.sh`，确认三种真实
  转换、OOXML 结构、连续转换和无残留 profile 进程均通过。
- Windows x64：当前只可作为候选交付包；必须在目标 Windows x64 主机补做
  `Install.ps1`、`Preflight.ps1`、超时进程树和残留进程验证后，才能改写“未实机认证”
  结论。
- 任何平台均应先核对最终 zip SHA-256，再解包核对 `SHA256SUMS`。
- 离线包只安装依赖，不修改 `.env`。preflight 通过后才由运维人员启用
  `DOCSENSE_LEGACY_OFFICE_ENABLED=true`；回滚时关闭该开关并重启。
- 本轮 XLS/XLSX 仅支持恰好一个可解析 Sheet；零个或多个可解析 Sheet 均
  fail-closed，不会只取第一张表继续处理。
