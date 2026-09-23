$ErrorActionPreference = "Stop"
$projectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectPath "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Sanal ortam bulunamadı: $pythonPath"
}

Set-Location -LiteralPath $projectPath
& $pythonPath -m uvicorn api:app --host 0.0.0.0 --port 8000

