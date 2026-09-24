$ErrorActionPreference = "Stop"
$projectDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$ollamaExecutable = "C:\Users\Monster\AppData\Local\Programs\Ollama\ollama.exe"
$cloudflaredExecutable = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$pythonExecutable = Join-Path $projectDirectory "venv\Scripts\python.exe"
$portListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$portListener.Start()
$bridgePort = $portListener.LocalEndpoint.Port
$portListener.Stop()

foreach ($requiredFile in @($ollamaExecutable, $cloudflaredExecutable, $pythonExecutable)) {
    if (-not (Test-Path -LiteralPath $requiredFile)) {
        Write-Error "Gerekli dosya bulunamadı: $requiredFile"
        exit 1
    }
}

$env:OLLAMA_LLM_LIBRARY = "cpu_avx2"
if (-not (Get-Process -Name "ollama" -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $ollamaExecutable -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

$randomBytes = New-Object byte[] 32
$randomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
$randomGenerator.GetBytes($randomBytes)
$randomGenerator.Dispose()
$connectionToken = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$env:TREX_OLLAMA_TOKEN = $connectionToken

$bridgeProcess = Start-Process -FilePath $pythonExecutable `
    -ArgumentList "-m", "uvicorn", "ollama_bridge:app", "--host", "127.0.0.1", "--port", "$bridgePort" `
    -WorkingDirectory $projectDirectory -WindowStyle Hidden -PassThru

$logFile = Join-Path $env:TEMP ("trex-ollama-tunnel-{0}-{1}.log" -f $PID, [guid]::NewGuid().ToString("N"))
$tunnelProcess = Start-Process -FilePath $cloudflaredExecutable `
    -ArgumentList "tunnel", "--url", "http://127.0.0.1:$bridgePort" `
    -RedirectStandardError $logFile -WindowStyle Hidden -PassThru

try {
    $tunnelUrl = ""
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-Path $logFile) {
            $logText = Get-Content -Raw $logFile
            if (-not [string]::IsNullOrWhiteSpace($logText)) {
                $match = [regex]::Match($logText, "https://[a-z0-9-]+\.trycloudflare\.com")
                if ($match.Success) { $tunnelUrl = $match.Value; break }
            }
        }
    }
    if (-not $tunnelUrl) { throw "Cloudflare tünel adresi alınamadı." }

    Clear-Host
    Write-Host "TREX Yerel Yapay Zekâ hazır." -ForegroundColor Green
    Write-Host "Akıllı Analiz > Yerel AI bağlantısı bölümüne aşağıdaki bilgileri girin:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Köprü adresi: $tunnelUrl"
    Write-Host "Geçici bağlantı anahtarı: $connectionToken"
    Write-Host "Model: qwen2.5:7b"
    Write-Host ""
    Write-Host "Bu pencere açık kaldığı sürece Ollama kullanılabilir. Kapatmak için Ctrl+C." -ForegroundColor Yellow
    Wait-Process -Id $tunnelProcess.Id
}
finally {
    Stop-Process -Id $tunnelProcess.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $bridgeProcess.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 250
    Remove-Item -LiteralPath $logFile -Force -ErrorAction SilentlyContinue
}

