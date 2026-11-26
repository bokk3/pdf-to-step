<#
.SYNOPSIS
    PDF-to-STEP as its own self-healing background service on Windows (separate from other autostarts).

.DESCRIPTION
    ensure     (default) Start the API if it is not healthy, verify the `tailscale serve` mapping and
               re-create it when it is missing or wrong, then test https://<machine>.<tailnet>:<port>/api/health
               end-to-end and re-apply the mapping once if that fails. Idempotent: safe to run any time.
    status     Show API, ports, tailnet mapping, end-to-end check and the scheduled task.
    stop       Stop the API (and its CAD worker processes). The tailnet mapping is kept.
    restart    stop + ensure.
    install    Register the scheduled task "PDF-to-STEP service" (current user, no admin needed):
               at sign-in (+45 s) and every 5 minutes as a watchdog, both running `ensure`. Then runs ensure.
    uninstall  Remove the task, stop the API and remove this service's tailnet mapping.

    Fixed ports come from .env: PORT (API on 127.0.0.1), TAILSCALE_HTTPS_PORT (tailnet HTTPS).
    The script never overwrites a tailnet port that serves something else unless -Force is given.
    Logs: logs\service.log (actions), logs\api.out.log / logs\api.err.log (server).

.EXAMPLE
    .\scripts\service.ps1                 # ensure
    .\scripts\service.ps1 status
    .\scripts\service.ps1 install
#>
param(
    [ValidateSet('ensure', 'status', 'stop', 'restart', 'install', 'uninstall')]
    [string]$Action = 'ensure',
    [switch]$Force,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'  # native tools (tailscale) write warnings to stderr; errors are checked explicitly
$Root = Split-Path -Parent $PSScriptRoot
$TaskName = 'PDF-to-STEP service'
$LogDir = Join-Path $Root 'logs'
$LogFile = Join-Path $LogDir 'service.log'
$PidFile = Join-Path $LogDir 'api.pid'
$StateFile = Join-Path $LogDir 'service.state.json'
$Py = Join-Path $Root 'backend\.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force $LogDir | Out-Null

# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------

function Write-Log([string]$Msg, [string]$Level = 'INFO') {
    $line = "{0}  {1,-5} {2}" -f (Get-Date -Format s), $Level, $Msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    if (-not $Quiet) {
        $color = @{ INFO = 'Gray'; OK = 'Green'; WARN = 'Yellow'; ERROR = 'Red' }[$Level]
        Write-Host $line -ForegroundColor $color
    }
}

function Read-DotEnv {
    $cfg = @{}
    $f = Join-Path $Root '.env'
    if (Test-Path $f) {
        foreach ($l in Get-Content $f -Encoding UTF8) {
            if ($l -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $cfg[$Matches[1].ToUpper()] = $Matches[2].Trim().Trim('"') }
        }
    }
    return $cfg
}

$Cfg = Read-DotEnv
$Port = if ($Cfg.PORT) { [int]$Cfg.PORT } else { 8620 }
$TsPort = if ($Cfg.TAILSCALE_HTTPS_PORT) { [int]$Cfg.TAILSCALE_HTTPS_PORT } else { 8445 }
$Target = "http://127.0.0.1:$Port"

function Get-Tailscale {
    $c = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $p) { return $p }
    return $null
}

function Get-State {
    if (Test-Path $StateFile) { try { return Get-Content $StateFile -Raw | ConvertFrom-Json } catch {} }
    return $null
}

function Save-State([hashtable]$s) { $s | ConvertTo-Json | Set-Content -Path $StateFile -Encoding utf8 }

function Test-Api {
    try {
        $h = Invoke-RestMethod -Uri "$Target/api/health" -TimeoutSec 5
        return ($h.status -eq 'ok' -and $h.bind -eq "127.0.0.1:$Port")
    } catch { return $false }
}

function Get-PortOwner([int]$p) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return $null }
    $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    return [pscustomobject]@{ Pid = $c.OwningProcess; Name = $proc.ProcessName }
}

function Get-ApiProcesses {
    # our uvicorn process(es) for this port, found by command line (robust against a stale pid file)
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn\s+app\.main:app' -and $_.CommandLine -match "--port\s+$Port(\s|$)" }
}

function Invoke-Curl([string]$Url) {
    # HTTP status code via curl.exe (handles TLS on PowerShell 5.1); 000 = no connection
    $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 15 --ssl-no-revoke $Url 2>$null
    if (-not $code) { return '000' }
    return "$code"
}

function Limit-Log([string]$f) {
    if ((Test-Path $f) -and (Get-Item $f).Length -gt 10MB) { Move-Item $f "$f.1" -Force }
}

# ------------------------------------------------------------------------------------------------
# API
# ------------------------------------------------------------------------------------------------

function Start-Api {
    if (Test-Api) { Write-Log "API healthy on $Target" 'OK'; return $true }

    $owner = Get-PortOwner $Port
    $ours = @(Get-ApiProcesses)
    if ($owner -and -not ($ours | Where-Object { $_.ProcessId -eq $owner.Pid })) {
        Write-Log "port $Port is used by $($owner.Name) (pid $($owner.Pid)), not by this service; change PORT in .env" 'ERROR'
        return $false
    }
    if ($ours) {
        # running but not answering: give a starting instance time, otherwise restart it
        $age = (Get-Date) - ($ours | ForEach-Object { $_.CreationDate } | Sort-Object | Select-Object -First 1)
        if ($age.TotalSeconds -lt 180) {
            Write-Log "API process is starting (age $([int]$age.TotalSeconds)s), waiting"
            if (Wait-Api 150) { Write-Log "API healthy on $Target" 'OK'; return $true }
        }
        Write-Log 'API process not healthy, restarting it' 'WARN'
        Stop-Api | Out-Null
    }

    if (-not (Test-Path $Py)) { Write-Log "backend venv missing ($Py)" 'ERROR'; return $false }
    if (-not (Test-Path (Join-Path $Root 'frontend\dist\index.html'))) {
        $npm = Get-Command npm -ErrorAction SilentlyContinue
        if ($npm) {
            Write-Log 'frontend\dist missing, building UI'
            Push-Location (Join-Path $Root 'frontend')
            try {
                if (-not (Test-Path node_modules)) { & npm ci 2>&1 | Out-Null }
                & npm run build 2>&1 | Out-Null
            } finally { Pop-Location }
        } else { Write-Log 'frontend\dist missing and npm not found: API only' 'WARN' }
    }

    Limit-Log (Join-Path $LogDir 'api.out.log'); Limit-Log (Join-Path $LogDir 'api.err.log')
    $env:PYTHONDONTWRITEBYTECODE = $null  # PyMuPDF/OCCT imports are slow without cached bytecode
    $p = Start-Process -FilePath $Py -WorkingDirectory (Join-Path $Root 'backend') -WindowStyle Hidden -PassThru `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port" `
        -RedirectStandardOutput (Join-Path $LogDir 'api.out.log') -RedirectStandardError (Join-Path $LogDir 'api.err.log')
    Set-Content -Path $PidFile -Value $p.Id
    Write-Log "API started (pid $($p.Id)) on $Target, waiting for health"
    if (Wait-Api 150) { Write-Log "API healthy on $Target" 'OK'; return $true }
    Write-Log "API did not become healthy; see logs\api.err.log" 'ERROR'
    return $false
}

function Wait-Api([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Api) { return $true }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Stop-Api {
    $pids = @(Get-ApiProcesses | ForEach-Object { $_.ProcessId })
    if (Test-Path $PidFile) { $pids += [int](Get-Content $PidFile) }
    $stopped = 0
    foreach ($id in ($pids | Sort-Object -Unique)) {
        if (Get-Process -Id $id -ErrorAction SilentlyContinue) {
            & taskkill.exe /PID $id /T /F 2>&1 | Out-Null  # /T: include the CAD worker processes
            $stopped++
        }
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
    if ($stopped) { Write-Log "API stopped ($stopped process tree(s))" } else { Write-Log 'API was not running' }
    return $stopped
}

# ------------------------------------------------------------------------------------------------
# tailscale serve
# ------------------------------------------------------------------------------------------------

function Get-TailnetInfo {
    $ts = Get-Tailscale
    if (-not $ts) { return $null }
    try { $st = (& $ts status --json 2>$null) -join "`n" | ConvertFrom-Json } catch { return $null }
    $dns = "$($st.Self.DNSName)".TrimEnd('.')
    return [pscustomobject]@{ Exe = $ts; Running = ($st.BackendState -eq 'Running'); Dns = $dns }
}

function Get-ServeProxy($ts) {
    try { $cfg = (& $ts.Exe serve status --json 2>$null) -join "`n" | ConvertFrom-Json } catch { return $null }
    if (-not $cfg -or -not $cfg.Web) { return $null }
    $site = $cfg.Web.PSObject.Properties | Where-Object { $_.Name -eq "$($ts.Dns):$TsPort" } | Select-Object -First 1
    if (-not $site) { return $null }
    $root = $site.Value.Handlers.PSObject.Properties | Where-Object { $_.Name -eq '/' } | Select-Object -First 1
    if (-not $root) { return '(non-root handlers)' }
    if ($root.Value.Proxy) { return "$($root.Value.Proxy)".TrimEnd('/') }
    return '(not a proxy)'
}

function Set-Serve($ts) {
    if ($TsPort -eq 443) { $out = & $ts.Exe serve --bg $Target 2>&1 | Out-String }
    else { $out = & $ts.Exe serve --bg "--https=$TsPort" $Target 2>&1 | Out-String }
    if ($LASTEXITCODE -ne 0) { Write-Log "tailscale serve failed: $($out.Trim())" 'ERROR'; return $false }
    Save-State @{ tailnet_port = $TsPort; target = $Target; set_at = (Get-Date -Format s) }
    Write-Log "tailscale serve: https://$($ts.Dns):$TsPort -> $Target" 'OK'
    return $true
}

function Reset-Serve($ts) {
    & $ts.Exe serve "--https=$TsPort" off 2>&1 | Out-Null
    return Set-Serve $ts
}

function Confirm-Serve {
    $ts = Get-TailnetInfo
    if (-not $ts) { Write-Log 'tailscale CLI not found or not responding' 'ERROR'; return $false }
    if (-not $ts.Running) { Write-Log 'tailscale is not connected (BackendState != Running); cannot publish' 'ERROR'; return $false }

    $current = Get-ServeProxy $ts
    $state = Get-State
    if ($current -eq $Target) {
        Write-Log "tailnet mapping OK: https://$($ts.Dns):$TsPort -> $Target" 'OK'
    } elseif (-not $current) {
        Write-Log "tailnet port $TsPort not published, configuring" 'WARN'
        if (-not (Set-Serve $ts)) { return $false }
    } elseif ($Force -or ($state -and $state.tailnet_port -eq $TsPort -and $state.target -eq $current)) {
        $why = if ($Force) { 'replacing it (-Force)' } else { 'stale config of this service, re-configuring' }
        Write-Log "tailnet port $TsPort points to $current; $why" 'WARN'
        if (-not (Reset-Serve $ts)) { return $false }
    } else {
        Write-Log "tailnet port $TsPort already serves $current (not this service). Set another TAILSCALE_HTTPS_PORT in .env or use -Force." 'ERROR'
        return $false
    }

    # end-to-end: through the tailnet HTTPS endpoint back into the API
    $url = "https://$($ts.Dns):$TsPort/api/health"
    $code = Invoke-Curl $url
    if ($code -eq '200') { Write-Log "end-to-end OK: $url" 'OK'; return $true }
    Write-Log "end-to-end check returned HTTP $code for $url; re-applying the mapping" 'WARN'
    if (-not (Reset-Serve $ts)) { return $false }
    Start-Sleep -Seconds 3
    $code = Invoke-Curl $url
    if ($code -eq '200') { Write-Log "end-to-end OK after reset: $url" 'OK'; return $true }
    Write-Log "end-to-end still failing (HTTP $code) for $url" 'ERROR'
    return $false
}

# ------------------------------------------------------------------------------------------------
# scheduled task
# ------------------------------------------------------------------------------------------------

function Install-Task {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" ensure -Quiet"
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg -WorkingDirectory $Root
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $atLogon.Delay = 'PT45S'
    $watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $watchdog.Repetition.Duration = ''  # repeat indefinitely
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $watchdog) -Settings $settings `
            -Principal $principal -Description "PDF-to-STEP: API on $Target + tailscale serve :$TsPort, watchdog every 5 min ($PSCommandPath)" `
            -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Log "could not register scheduled task: $($_.Exception.Message)" 'ERROR'
        return $false
    }
    Write-Log "scheduled task '$TaskName' installed (sign-in + every 5 min)" 'OK'
    return $true
}

function Show-Status {
    $api = Test-Api
    $owner = Get-PortOwner $Port
    $ts = Get-TailnetInfo
    $proxy = if ($ts) { Get-ServeProxy $ts } else { $null }
    $e2e = if ($ts -and $ts.Running) { Invoke-Curl "https://$($ts.Dns):$TsPort/api/health" } else { '-' }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $info = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
    [pscustomobject][ordered]@{
        'API'             = "$Target  healthy=$api  listener=$(if ($owner) { "$($owner.Name)/$($owner.Pid)" } else { 'none' })"
        'Tailnet URL'     = if ($ts) { "https://$($ts.Dns):$TsPort" } else { 'tailscale not available' }
        'Tailscale'       = if ($ts) { "connected=$($ts.Running)" } else { '-' }
        'Serve mapping'   = if ($proxy) { "$proxy$(if ($proxy -eq $Target) { '  (OK)' } else { '  (MISMATCH)' })" } else { 'not published' }
        'End-to-end'      = "HTTP $e2e"
        'Scheduled task'  = if ($task) { "$($task.State), last run $($info.LastRunTime) result $($info.LastTaskResult), next $($info.NextRunTime)" } else { 'not installed' }
        'Logs'            = $LogDir
    } | Format-List | Out-String | Write-Host
}

# ------------------------------------------------------------------------------------------------

switch ($Action) {
    'ensure' {
        Write-Log "ensure: API $Target, tailnet :$TsPort"
        $okApi = Start-Api
        $okServe = Confirm-Serve
        if (-not ($okApi -and $okServe)) { exit 1 }
    }
    'status' { Show-Status }
    'stop' { Stop-Api | Out-Null }
    'restart' {
        Stop-Api | Out-Null
        $okApi = Start-Api
        $okServe = Confirm-Serve
        if (-not ($okApi -and $okServe)) { exit 1 }
    }
    'install' {
        if (-not (Install-Task)) { exit 1 }
        $okApi = Start-Api
        $okServe = Confirm-Serve
        if (-not ($okApi -and $okServe)) { exit 1 }
    }
    'uninstall' {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Log "scheduled task '$TaskName' removed"
        Stop-Api | Out-Null
        $ts = Get-TailnetInfo
        if ($ts -and (Get-ServeProxy $ts) -eq $Target) {
            & $ts.Exe serve "--https=$TsPort" off 2>&1 | Out-Null
            Write-Log "tailnet mapping :$TsPort removed"
        }
    }
}
