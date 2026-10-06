$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$engineDir = Join-Path $projectRoot "data\engines\llama.cpp"
$modelDir = Join-Path $projectRoot "data\models\gguf\qwen3.8-27b-q3"
$serverPath = Join-Path $engineDir "llama-server.exe"
$modelPath = Join-Path $modelDir "Qwen3.8-27B-Q3_K_M.gguf"
$llamaArchive = Join-Path $engineDir "llama-cuda.zip"
$cudaArchive = Join-Path $engineDir "cudart.zip"
$releaseTag = "b11277"
$releaseBase = "https://github.com/ggml-org/llama.cpp/releases/download/$releaseTag"

New-Item -ItemType Directory -Path $engineDir,$modelDir -Force | Out-Null

if (-not (Test-Path -LiteralPath $serverPath)) {
    if (-not (Test-Path -LiteralPath $llamaArchive)) {
        Invoke-WebRequest -Uri "$releaseBase/llama-$releaseTag-bin-win-cuda-12.4-x64.zip" -OutFile $llamaArchive -Headers @{"User-Agent"="LLMmodol"}
    }
    if (-not (Test-Path -LiteralPath $cudaArchive)) {
        Invoke-WebRequest -Uri "$releaseBase/cudart-llama-bin-win-cuda-12.4-x64.zip" -OutFile $cudaArchive -Headers @{"User-Agent"="LLMmodol"}
    }
    Expand-Archive -LiteralPath $llamaArchive -DestinationPath $engineDir -Force
    Expand-Archive -LiteralPath $cudaArchive -DestinationPath $engineDir -Force
    if (-not (Test-Path -LiteralPath $serverPath)) {
        $found = Get-ChildItem -LiteralPath $engineDir -Filter "llama-server.exe" -Recurse | Select-Object -First 1
        if (-not $found) { throw "llama-server.exe was not found after extracting the official CUDA package." }
        $serverDir = $found.Directory.FullName
        if ($serverDir -ne $engineDir) {
            Get-ChildItem -LiteralPath $serverDir -File | Copy-Item -Destination $engineDir -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $modelPath) -or (Get-Item -LiteralPath $modelPath).Length -lt 13404051168) {
    $hf = Get-Command hf -ErrorAction SilentlyContinue
    if ($hf) {
        & $hf.Source download bartowski/Qwen3.8-27B-GGUF Qwen3.8-27B-Q3_K_M.gguf --local-dir $modelDir --max-workers 1
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) { throw "Python is required to locate the Hugging Face CLI." }
        $pythonRoot = Split-Path -Parent $pythonCommand.Source
        $hfPath = Join-Path (Join-Path $pythonRoot "Scripts") "hf.exe"
        if (-not (Test-Path -LiteralPath $hfPath)) { throw "Hugging Face CLI (hf) is not installed." }
        & $hfPath download bartowski/Qwen3.8-27B-GGUF Qwen3.8-27B-Q3_K_M.gguf --local-dir $modelDir --max-workers 1
    }
}

if (-not (Test-Path -LiteralPath $serverPath) -or (Get-Item -LiteralPath $modelPath).Length -lt 13100000000) {
    throw "Engine or model files are incomplete. Check the download output and rerun this script to resume."
}

Write-Output "Ready: llama.cpp CUDA $releaseTag and Qwen3.8-27B Q3_K_M. Open the platform's Models & API page to start hybrid inference."
