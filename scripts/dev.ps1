# Windows equivalent of dev.sh: FastAPI with --reload on 127.0.0.1:PORT + Vite on 127.0.0.1:VITE_DEV_PORT
# (ports from .env, defaults 8620 / 5620). Ctrl+C stops both.
# Stop the background service first (.\scripts\service.ps1 stop) - it uses the same API port.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) { throw "backend venv missing: cd backend; uv venv --python 3.12; uv pip install -e .[dev]" }
if (-not (Test-Path "$Root\frontend\node_modules")) { Push-Location "$Root\frontend"; npm install; Pop-Location }

$cfg = @{}
if (Test-Path "$Root\.env") {
    Get-Content "$Root\.env" | Where-Object { $_ -match '^\s*([A-Z_]+)\s*=\s*(.*)$' } | ForEach-Object { $cfg[$Matches[1]] = $Matches[2].Trim() }
}
$port = if ($cfg.PORT) { $cfg.PORT } else { '8620' }
$vite = if ($cfg.VITE_DEV_PORT) { $cfg.VITE_DEV_PORT } else { '5620' }
$env:PYTHONDONTWRITEBYTECODE = $null

$api = Start-Process -FilePath $Py -WorkingDirectory "$Root\backend" -NoNewWindow -PassThru `
    -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', $port, '--reload', '--reload-dir', 'app'
try {
    Write-Host "API -> http://127.0.0.1:$port/api/health   UI -> http://127.0.0.1:$vite"
    Push-Location "$Root\frontend"
    npm run dev
} finally {
    Pop-Location
    if (-not $api.HasExited) { & taskkill.exe /PID $api.Id /T /F | Out-Null }
}
