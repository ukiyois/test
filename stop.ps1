$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $projectRoot "data\server.pid"
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output "没有保存的服务进程。"
    exit 0
}
$serverId = 0
try { $serverId = [int](Get-Content -LiteralPath $pidFile -Raw) } catch {}
if ($serverId -gt 0) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $serverId" -ErrorAction SilentlyContinue
    if ($process -and $process.CommandLine -match "uvicorn llmplatform\.app:app") {
        try { Invoke-RestMethod -Uri "http://127.0.0.1:7860/api/runtime/gguf/stop" -Method Post -TimeoutSec 30 | Out-Null } catch {}
        Stop-Process -Id $serverId -Force -ErrorAction SilentlyContinue
        Write-Output "平台服务已关闭。"
    } else {
        Write-Output "平台服务进程已经退出。"
    }
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
