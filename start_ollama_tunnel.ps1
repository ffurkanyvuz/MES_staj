param([int]$Port = 8765)

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Error "cloudflared bulunamadı. Önce Cloudflare Tunnel aracını kurun."
    exit 1
}

cloudflared tunnel --url "http://127.0.0.1:$Port"


