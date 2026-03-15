param(
    [string]$BaseUrl = "",
    [string]$PayloadPath = "tests/fixtures/llm/check_task_file_request.json"
)

if ([string]::IsNullOrEmpty($BaseUrl)) {
    $envPath = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path $envPath)) { $envPath = Join-Path $PSScriptRoot "..\.env.example" }
    if (Test-Path $envPath) {
        Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match '^\s*([^#\s][^=]*)=(.*)' } | ForEach-Object {
            [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
    $hostAddr = if ($env:WEB_UI_HOST) { $env:WEB_UI_HOST } else { "127.0.0.1" }
    $port = if ($env:WEB_UI_PORT) { $env:WEB_UI_PORT } else { "5001" }
    $BaseUrl = "http://${hostAddr}:${port}"
}

$body = Get-Content -Path $PayloadPath -Raw -Encoding utf8
Invoke-WebRequest -Uri "$BaseUrl/llm/check-task" -Method Post -ContentType "application/json; charset=utf-8" -Body $body
