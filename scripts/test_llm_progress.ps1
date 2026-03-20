param(
    [string]$WsUrl = "",
    [string]$PayloadPath = "tests/fixtures/llm/check_task_file_request.json",
    [int]$ReadCount = 5,
    [switch]$SendQuery
)

if ([string]::IsNullOrEmpty($WsUrl)) {
    $envPath = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path $envPath)) { $envPath = Join-Path $PSScriptRoot "..\.env.example" }
    if (Test-Path $envPath) {
        Get-Content $envPath -Encoding UTF8 | Where-Object { $_ -match '^\s*([^#\s][^=]*)=(.*)' } | ForEach-Object {
            [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
    $hostAddr = if ($env:WEB_UI_HOST) { $env:WEB_UI_HOST } else { "127.0.0.1" }
    $port = if ($env:WEB_UI_PORT) { $env:WEB_UI_PORT } else { "5001" }
    $WsUrl = "ws://${hostAddr}:${port}/llm/progress"
}

$socket = [System.Net.WebSockets.ClientWebSocket]::new()
$cts = [System.Threading.CancellationTokenSource]::new()
$uri = [System.Uri]$WsUrl

$socket.ConnectAsync($uri, $cts.Token).GetAwaiter().GetResult()

$body = Get-Content -Path $PayloadPath -Raw -Encoding utf8
$sendBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$sendSegment = [System.ArraySegment[byte]]::new($sendBytes)
$socket.SendAsync(
    $sendSegment,
    [System.Net.WebSockets.WebSocketMessageType]::Text,
    $true,
    $cts.Token
).GetAwaiter().GetResult()

if ($SendQuery) {
    $queryPayload = (Get-Content -Path $PayloadPath -Raw -Encoding utf8 | ConvertFrom-Json)
    $queryPayload | Add-Member -NotePropertyName action -NotePropertyValue query -Force
    $queryBody = $queryPayload | ConvertTo-Json -Depth 10 -Compress
    $queryBytes = [System.Text.Encoding]::UTF8.GetBytes($queryBody)
    $querySegment = [System.ArraySegment[byte]]::new($queryBytes)
    $socket.SendAsync(
        $querySegment,
        [System.Net.WebSockets.WebSocketMessageType]::Text,
        $true,
        $cts.Token
    ).GetAwaiter().GetResult()
}

for ($i = 0; $i -lt $ReadCount -and $socket.State -eq [System.Net.WebSockets.WebSocketState]::Open; $i++) {
    $buffer = New-Object byte[] 4096
    $segment = [System.ArraySegment[byte]]::new($buffer)
    $result = $socket.ReceiveAsync($segment, $cts.Token).GetAwaiter().GetResult()
    if ($result.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) {
        break
    }
    $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $result.Count)
    Write-Output $text
}

$socket.Dispose()
