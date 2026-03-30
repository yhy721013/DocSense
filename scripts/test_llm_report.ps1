param(
    [string]$BaseUrl = "",
    [string]$PayloadPath = "tests/fixtures/llm/report_request.json"
)

if ([string]::IsNullOrEmpty($BaseUrl)) {
    $envPath = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path $envPath)) { $envPath = Join-Path $PSScriptRoot "..\.env.example" }
    if (Test-Path $envPath) {
        Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match '^\s*([^#\s][^=]*)=(.*)' } | ForEach-Object {
            [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
    $hostAddr = $env:APP_HOST
    $port = $env:APP_PORT
    $BaseUrl = "http://${hostAddr}:${port}"
}

$body = Get-Content -Path $PayloadPath -Raw -Encoding utf8
Invoke-WebRequest -Uri "$BaseUrl/llm/generate-report" -Method Post -ContentType "application/json; charset=utf-8" -Body $body
