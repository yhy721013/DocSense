# DocSense LibreOffice 26.2.5 离线依赖包

本包只负责离线安装与预检 LibreOffice，不包含 DocSense，也不会自动修改
DocSense 的 `.env`、启动服务或启用 Legacy Office 转换功能。

安装前先在包根目录核验 `SHA256SUMS` 和 `MANIFEST.json`。安装并通过 preflight 后，
再由运维人员设置：

```dotenv
DOCSENSE_LEGACY_OFFICE_ENABLED=true
DOCSENSE_LIBREOFFICE_ALLOWED_VERSION_SERIES=26.2
```

如果使用非标准安装位置，还需把 `DOCSENSE_LIBREOFFICE_EXECUTABLE` 设置为 `soffice`
可执行文件的绝对路径；Windows 必须指向控制台入口 `soffice.com`，不要指向
`soffice.exe`。标准安装留空即可按受控顺序发现。DocSense 启动时会再次执行版本门禁；
缺失、`LibreOfficeDev` 或非稳定 26.2.x 均拒绝启动。

## macOS Apple Silicon

```bash
shasum -a 256 -c SHA256SUMS
./install.sh
./preflight.sh
```

安装脚本只接受 Apple Silicon，并把官方 `LibreOffice.app` 安装到 `/Applications`。
目标已存在且不是同一 26.2.5 版本时默认停止；只有显式传入 `--replace` 才会先把旧
应用移动到带时间戳的备份路径。preflight 会对包内 `.doc/.ppt/.xls` 样本执行真实转换。

## Windows x64

以管理员 PowerShell 运行：

```powershell
Get-Content .\SHA256SUMS | ForEach-Object {
    if ($_ -notmatch '^([0-9a-fA-F]{64})  (.+)$') {
        throw "SHA256SUMS 格式无效：$_"
    }
    $Expected = $Matches[1].ToLowerInvariant()
    $RelativePath = $Matches[2]
    $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $RelativePath).Hash.ToLowerInvariant()
    if ($Actual -ne $Expected) {
        throw "SHA-256 校验失败：$RelativePath"
    }
}
.\Install.ps1
.\Preflight.ps1
```

安装脚本使用官方 MSI 的静默安装模式。preflight 会验证版本并对三份样本执行转换与
OOXML 结构检查。本轮 Windows 脚本和包结构仅完成静态/mock 验证，**未进行 Windows
x64 实机认证**；部署前必须在目标 Windows x64 主机补做安装、转换、超时清理和残留
进程验收。

## 支持与限制

- 转换范围：`.doc → .docx`、`.ppt → .pptx`、`.xls → .xlsx`。
- DocSense 仅在 `/llm/analysis` 和 `/llm/generate-report.filePathList` 使用转换层；
  `.doc` 不支持作为 `templateOutline`。
- 宏、ActiveX、嵌入对象、动画、媒体和外部链接不会执行，也不承诺完整保留。
- 密码保护、损坏、伪造后缀或非 OLE2 文件会失败，不会原样上传。
- XLS/XLSX 仅支持恰好一个可解析 Sheet；零个或多个可解析 Sheet 均 fail-closed，
  不会只取第一张表继续处理。
- Docker、macOS Intel 和 Windows ARM64 不在本轮交付范围。
