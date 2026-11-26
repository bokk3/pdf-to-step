# Windows equivalent of start_tailscale.sh: start the API on 127.0.0.1:PORT and publish it with
# `tailscale serve` on TAILSCALE_HTTPS_PORT (both from .env). Delegates to service.ps1 (single implementation),
# which also repairs a missing/wrong tailnet mapping and runs an end-to-end check.
#   -RebuildUi  force `npm run build` first
#   -Force      replace a tailnet mapping on that port that belongs to something else
param([switch]$RebuildUi, [switch]$Force)
$Root = Split-Path -Parent $PSScriptRoot
if ($RebuildUi) {
    Push-Location "$Root\frontend"
    try {
        if (-not (Test-Path node_modules)) { npm ci }
        npm run build
    } finally { Pop-Location }
}
& (Join-Path $PSScriptRoot 'service.ps1') ensure -Force:$Force
exit $LASTEXITCODE
