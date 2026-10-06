param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCommand = Get-Command python -ErrorAction Stop
$pythonExe = $pythonCommand.Source
$pidFile = Join-Path $projectRoot "data\server.pid"
$logFile = Join-Path $projectRoot "data\server.log"
$errorFile = Join-Path $projectRoot "data\server-error.log"
$port = 7860
$expectedBuild = (& $pythonExe -c "from llmplatform.buildinfo import get_build_id; print(get_build_id())").Trim()
if (-not $expectedBuild) { throw "无法计算当前平台构建标识。" }

New-Item -ItemType Directory -Path (Join-Path $projectRoot "data") -Force | Out-Null

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
    if ($health.status -eq "ok" -and $health.build_id -eq $expectedBuild) {
        if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port" }
        exit 0
    }
} catch {}

$listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $runningServer = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $listener.OwningProcess) -ErrorAction SilentlyContinue
    if (-not $runningServer -or $runningServer.Name -ne "python.exe" -or $runningServer.CommandLine -notmatch "-m uvicorn llmplatform\.app:app") {
        throw "端口 $port 已被其他程序占用；为避免关闭无关程序，平台没有自动停止它。"
    }
    Stop-Process -Id $listener.OwningProcess -Force
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 250
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)) { break }
    }
}

$arguments = @(
    "-m", "uvicorn", "llmplatform.app:app",
    "--host", "127.0.0.1",
    "--port", "$port"
)
$server = Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $logFile -RedirectStandardError $errorFile -PassThru

Set-Content -LiteralPath $pidFile -Value $server.Id -Encoding ascii
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 2
        if ($health.status -eq "ok" -and $health.build_id -eq $expectedBuild) {
            $ready = $true
            break
        }
    } catch {}
    if ($server.HasExited) { break }
}

if (-not $ready) {
    Write-Error "平台没有启动成功。查看 data\server.log 获取启动错误。"
}
if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$port" }
