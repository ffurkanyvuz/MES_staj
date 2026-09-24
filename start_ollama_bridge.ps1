param(
    [string]$Token = $env:TREX_OLLAMA_TOKEN,
    [int]$Port = 8765
)

if (-not $Token) {
    Write-Error "TREX_OLLAMA_TOKEN tanımlı değil. Önce güçlü bir anahtar belirleyin."
    exit 1
}

$env:TREX_OLLAMA_TOKEN = $Token
python -m uvicorn ollama_bridge:app --host 127.0.0.1 --port $Port


