# ============================================================================
# Agentic Edge — single control surface for the whole live-trading system.
#
# Usage:
#   pwsh -ExecutionPolicy Bypass -File "C:\Projects\Agentic Edge\start-all.ps1" [verb]
#
# Verbs:
#   start        (default) free ports + launch BACKEND (:8000) and DASHBOARD (:3001).
#                Backend lifespan brings up scheduler jobs, BOTH trading loops,
#                the 15-min health monitor, and the IBKR connection (client 20).
#   restart      same as start (frees ports first) — use after code changes.
#   stop         stop backend + dashboard (frees ports 8000 / 3001).
#   status       show kill-switch, circuit-breaker, and scheduler state.
#   rearm        clear the entry circuit-breaker latch so NEW entries resume.
#   arm          turn the auto-trading kill switch ON  (enable new entries).
#   disarm       turn the auto-trading kill switch OFF (emergency: pause entries).
#   run-themes   fire all theme scorecard runs now (manual signal generation).
#   reconcile    force a position<->intent reconcile sweep now.
#   positions    print current open positions from the broker.
#   snapshot     compute a quant feature snapshot now (graph + market + flow).
#   research     print the feature IC / alpha-decay research report (offline).
#   help         show this list.
#
# Trading control verbs call the admin API (needs the backend running) and
# authenticate with ADMIN_API_TOKEN read from .env — no secrets to paste.
# ============================================================================

param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'restart', 'stop', 'status', 'rearm', 'arm', 'disarm',
                 'run-themes', 'reconcile', 'positions', 'snapshot', 'research',
                 'watchdog', 'install-autostart', 'uninstall-autostart', 'help')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$root = if ($PSScriptRoot) { $PSScriptRoot } else { 'C:\Projects\Agentic Edge' }
$BackendUrl = 'http://127.0.0.1:8000'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Get-AdminToken {
    $envFile = Join-Path $root '.env'
    if (-not (Test-Path $envFile)) { throw ".env not found at $envFile" }
    $line = Get-Content $envFile | Select-String '^ADMIN_API_TOKEN='
    if (-not $line) { throw 'ADMIN_API_TOKEN is not set in .env' }
    return (($line.Line -split '=', 2)[1]).Trim()
}

function Invoke-Admin {
    param([string]$Method, [string]$Path, [hashtable]$BodyObj)
    $params = @{
        Method      = $Method
        Uri         = "$BackendUrl$Path"
        Headers     = @{ 'X-Admin-Token' = (Get-AdminToken) }
        ContentType = 'application/json'
        TimeoutSec  = 15
    }
    if ($BodyObj) { $params.Body = ($BodyObj | ConvertTo-Json -Compress) }
    try {
        return Invoke-RestMethod @params
    }
    catch {
        Write-Host "  ERROR calling $Path : $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  (is the backend up? run:  start-all.ps1 start )" -ForegroundColor DarkGray
        return $null
    }
}

function Stop-Stack {
    foreach ($port in 8000, 3001) {
        $pids = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) {
            Write-Host "  stopping PID $procId on port $port" -ForegroundColor DarkYellow
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
}

function Start-Stack {
    Write-Host '=== Agentic Edge: starting backend + dashboard ===' -ForegroundColor Cyan
    Stop-Stack                       # clean (re)start — free the ports first
    Start-Sleep -Seconds 1

    # BACKEND — uvicorn lifespan = scheduler + both trading loops + health monitor + IBKR.
    Start-Process pwsh -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location '$root'; `$Host.UI.RawUI.WindowTitle = 'Agentic Edge - BACKEND (loops + scheduler + monitor)'; " +
        "`$env:PYTHONUTF8 = '1'; python -m uvicorn api.app.main:app --port 8000"
    )
    # DASHBOARD — Next.js on :3001.
    Start-Process pwsh -ArgumentList @(
        '-NoExit', '-Command',
        "Set-Location '$root\web'; `$Host.UI.RawUI.WindowTitle = 'Agentic Edge - DASHBOARD'; npm run dev -- -p 3001"
    )

    Write-Host ''
    Write-Host '  Backend   : http://127.0.0.1:8000   (trading loops, scheduler, 15-min health monitor)' -ForegroundColor Green
    Write-Host '  Dashboard : http://localhost:3001' -ForegroundColor Green
    Write-Host ''
    Write-Host '  Control verbs: status | rearm | arm | disarm | run-themes | reconcile | positions | stop' -ForegroundColor Gray
    Write-Host '  e.g.  pwsh -ExecutionPolicy Bypass -File "start-all.ps1" status' -ForegroundColor DarkGray
}

function Show-Help {
    Write-Host 'Agentic Edge control:' -ForegroundColor Cyan
    'start  restart  stop  status  rearm  arm  disarm  run-themes  reconcile  positions  snapshot  research' |
        ForEach-Object { Write-Host "  $_" }
    '  watchdog            (supervise: relaunch backend if it crashes; checks /api/health every 5 min)' | ForEach-Object { Write-Host $_ }
    '  install-autostart   (launch + supervise automatically at logon, via watchdog)' | ForEach-Object { Write-Host $_ }
    '  uninstall-autostart (remove the logon task)' | ForEach-Object { Write-Host $_ }
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
switch ($Action) {
    'start'      { Start-Stack }
    'restart'    { Start-Stack }
    'stop'       { Write-Host 'Stopping Agentic Edge...' -ForegroundColor Cyan; Stop-Stack; Write-Host '  stopped.' -ForegroundColor Green }

    'status' {
        Write-Host '=== Auto-trading / breaker ===' -ForegroundColor Cyan
        $s = Invoke-Admin -Method Get -Path '/api/admin/autotrade/status'
        if ($s) {
            Write-Host ("  kill switch (effective) : {0}" -f $s.effective_enabled)
            Write-Host ("  entry breaker tripped   : {0}  {1}" -f $s.entry_breaker_tripped, $s.entry_breaker_reason)
        }
        Write-Host '=== Scheduler ===' -ForegroundColor Cyan
        $sch = Invoke-Admin -Method Get -Path '/api/admin/scheduler/status'
        if ($sch) { $sch | Format-List }
    }

    'rearm' {
        Write-Host 'Re-arming entry circuit breaker...' -ForegroundColor Cyan
        $r = Invoke-Admin -Method Post -Path '/api/admin/autotrade/rearm-breaker' -BodyObj @{ actor = 'smaan'; reason = 'via launcher' }
        if ($r) { $r | Format-List }
    }
    'arm' {
        Write-Host 'Arming auto-trading kill switch (entries ENABLED)...' -ForegroundColor Cyan
        $r = Invoke-Admin -Method Post -Path '/api/admin/autotrade/enable' -BodyObj @{ actor = 'smaan'; reason = 'via launcher' }
        if ($r) { $r | Format-List }
    }
    'disarm' {
        Write-Host 'DISARMING auto-trading kill switch (entries PAUSED)...' -ForegroundColor Yellow
        $r = Invoke-Admin -Method Post -Path '/api/admin/autotrade/disable' -BodyObj @{ actor = 'smaan'; reason = 'via launcher' }
        if ($r) { $r | Format-List }
    }

    'run-themes' {
        Write-Host 'Firing all theme scorecard runs now...' -ForegroundColor Cyan
        $r = Invoke-Admin -Method Post -Path '/api/admin/scheduler/run-now'
        if ($r) { $r | Format-List }
    }
    'reconcile' {
        Write-Host 'Triggering position<->intent reconcile...' -ForegroundColor Cyan
        $r = Invoke-Admin -Method Post -Path '/api/admin/reconcile'
        if ($r) { $r | Format-List }
    }
    'positions' {
        try {
            $p = Invoke-RestMethod -Method Get -Uri "$BackendUrl/api/positions" -TimeoutSec 15
            $p | Where-Object { [double]$_.qty -ne 0 } |
                Select-Object symbol, qty, avg_price, last_price, unrealized_pnl | Format-Table -AutoSize
        }
        catch { Write-Host "  ERROR: $($_.Exception.Message)  (is the backend up?)" -ForegroundColor Red }
    }

    'snapshot' {
        Write-Host 'Computing quant feature snapshot (graph + market + flow)...' -ForegroundColor Cyan
        $r = Invoke-Admin -Method Post -Path '/api/admin/features/snapshot-now'
        if ($r) { $r | Format-List }
        Write-Host 'Backfilling any elapsed forward-return labels...' -ForegroundColor Cyan
        $l = Invoke-Admin -Method Post -Path '/api/admin/features/label-now'
        if ($l) { $l | Format-List }
    }
    'research' {
        # Offline IC / alpha-decay report — reads the sqlite DB directly, no
        # backend required. Run from the api/ dir so `app.config` is importable.
        Write-Host 'Feature research — IC + alpha-decay profiler...' -ForegroundColor Cyan
        Push-Location (Join-Path $root 'api')
        try { $env:PYTHONUTF8 = '1'; python -m app.research.feature_research }
        finally { Pop-Location }
    }

    'install-autostart' {
        $taskName = 'AgenticEdge-AutoStart'
        Write-Host "Setting up logon auto-start..." -ForegroundColor Cyan
        $registered = $false
        # Preferred: a Scheduled Task (survives, battery-aware). Needs rights;
        # NOTE: don't name locals $action — collides with the $Action param.
        try {
            $taskAction  = New-ScheduledTaskAction -Execute 'pwsh.exe' `
                             -Argument "-ExecutionPolicy Bypass -File `"$root\start-all.ps1`" watchdog"
            $taskTrigger = New-ScheduledTaskTrigger -AtLogOn
            $taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                             -DontStopIfGoingOnBatteries -StartWhenAvailable
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger `
                -Settings $taskSettings -Force `
                -Description 'Auto-start Agentic Edge (backend + dashboard) at logon' | Out-Null
            $registered = $true
            Write-Host "  Registered Scheduled Task '$taskName' (runs at logon)." -ForegroundColor Green
        }
        catch {
            Write-Host "  Scheduled Task needs elevation ($($_.Exception.Message))." -ForegroundColor DarkYellow
            Write-Host "  Falling back to a Startup-folder launcher (no admin needed)..." -ForegroundColor DarkYellow
        }
        # Fallback: drop a .cmd in the per-user Startup folder — runs at logon,
        # no privileges required.
        if (-not $registered) {
            $startup = [Environment]::GetFolderPath('Startup')
            $cmdFile = Join-Path $startup 'AgenticEdge-AutoStart.cmd'
            "@echo off`r`nstart `"AgenticEdge`" pwsh -ExecutionPolicy Bypass -File `"$root\start-all.ps1`" watchdog" |
                Set-Content -Path $cmdFile -Encoding ASCII
            Write-Host "  Created: $cmdFile" -ForegroundColor Green
            Write-Host "  Agentic Edge will launch at logon." -ForegroundColor Green
        }
        Write-Host "  Remove later with:  start-all.ps1 uninstall-autostart" -ForegroundColor DarkGray
    }
    'uninstall-autostart' {
        Write-Host 'Removing auto-start...' -ForegroundColor Cyan
        $removed = $false
        try { Unregister-ScheduledTask -TaskName 'AgenticEdge-AutoStart' -Confirm:$false; $removed = $true; Write-Host '  Removed Scheduled Task.' -ForegroundColor Green } catch {}
        $cmdFile = Join-Path ([Environment]::GetFolderPath('Startup')) 'AgenticEdge-AutoStart.cmd'
        if (Test-Path $cmdFile) { Remove-Item $cmdFile -Force; $removed = $true; Write-Host '  Removed Startup-folder launcher.' -ForegroundColor Green }
        if (-not $removed) { Write-Host '  (nothing was registered)' -ForegroundColor DarkGray }
    }

    'watchdog' {
        # Crash-restart supervisor: poll the lightweight /api/health every 5 min;
        # if the backend is down (crash, killed, post-reboot), relaunch the stack.
        # This is the LOCAL equivalent of an external cron uptime check — an
        # external cron website can't reach 127.0.0.1, so we supervise locally.
        # On first run it also performs the initial launch (health fails -> start).
        Write-Host 'Agentic Edge WATCHDOG — checking /api/health every 5 min; relaunches on failure. Ctrl+C to stop.' -ForegroundColor Cyan
        while ($true) {
            $up = $false
            try { Invoke-RestMethod -Uri "$BackendUrl/api/health" -TimeoutSec 8 | Out-Null; $up = $true } catch {}
            if ($up) {
                Write-Host ("{0}  backend OK" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor DarkGray
            }
            else {
                Write-Host ("{0}  backend DOWN -> relaunching stack" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')) -ForegroundColor Yellow
                Start-Stack
            }
            Start-Sleep -Seconds 300
        }
    }

    'help'       { Show-Help }
    default      { Show-Help }
}
