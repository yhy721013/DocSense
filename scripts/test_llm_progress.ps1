param(
    [string]$WsUrl = "ws://127.0.0.1:5001/llm/progress",
    [string]$PayloadPath = "tests/fixtures/llm/check_task_file_request.json",
    [int]$ReadCount = 5
)

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
