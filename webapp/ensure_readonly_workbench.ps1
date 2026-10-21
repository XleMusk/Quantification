[CmdletBinding()]
param(
    [string]$TradingHome = 'D:\QuantificationData\trading',
    [int]$ApiPort = 8765,
    [int]$UiPort = 5175,
    [string]$HealthPath = 'D:\QuantificationData\trading\research\ui_evidence\readonly_runtime_health\latest.json'
)

$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot 'start.ps1'
$HealthDir = Split-Path -Parent $HealthPath
New-Item -ItemType Directory -Force -Path $HealthDir | Out-Null

function Get-ApiHealth {
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/health" -TimeoutSec 3
    } catch {
        return $null
    }
}

function Test-Ui {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$UiPort" -TimeoutSec 3
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

$before = Get-Date
$api = Get-ApiHealth
$ui = Test-Ui
$started = $false
$startPid = $null
$errors = @()

if ($api -and $ui -and $api.status -eq 'ok' -and $api.mode -eq 'research_only' -and $api.live_trading -eq $false) {
    $state = 'healthy'
} else {
    try {
        # If only one side of the read-only workbench is alive, let the project's
        # identity-checked stop script remove only its own stale records/processes.
        $stopScript = Join-Path $PSScriptRoot 'stop.ps1'
        if (Test-Path -LiteralPath $stopScript) {
            try { & $stopScript | Out-Null } catch { $errors += $_.Exception.Message }
        }
        $start = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $StartScript,
            '-NoWorkers', '-TradingHome', $TradingHome
        ) -WorkingDirectory $Project -WindowStyle Hidden -PassThru
        $startPid = $start.Id
        $started = $true
        $state = 'starting'
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            $api = Get-ApiHealth
            $ui = Test-Ui
            if ($api -and $ui -and $api.status -eq 'ok' -and $api.mode -eq 'research_only' -and $api.live_trading -eq $false) {
                $state = 'healthy_after_restart'
                break
            }
        }
        if ($state -eq 'starting') { $state = 'start_failed' }
    } catch {
        $state = 'start_failed'
        $errors += $_.Exception.Message
    }
}

$progressScript = Join-Path $Project 'scripts\hourly_progress_supervisor.py'
$progressPython = Join-Path $Project 'env\python.exe'
$progressPath = 'D:\QuantificationData\trading\research\progress_supervision\latest.json'
$progressResult = $null
if ((Test-Path -LiteralPath $progressScript) -and (Test-Path -LiteralPath $progressPython)) {
    try {
        $progressResult = (& $progressPython -B $progressScript | Select-Object -Last 1 | Out-String).Trim()
    } catch {
        $errors += "progress_supervisor: $($_.Exception.Message)"
    }
}
$record = [ordered]@{
    schema = 'readonly_workbench_runtime_health_v1'
    checked_at = (Get-Date).ToUniversalTime().ToString('o')
    state = $state
    api_port = $ApiPort
    ui_port = $UiPort
    api_reachable = [bool]$api
    ui_reachable = [bool]$ui
    api_status = if ($api) { $api.status } else { $null }
    mode = if ($api) { $api.mode } else { $null }
    live_trading = if ($api) { $api.live_trading } else { $null }
    workers_started = $false
    started_supervisor_pid = $startPid
    started = $started
    errors = @($errors)
    progress_snapshot = $progressPath
    progress_result = $progressResult
}
$record | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $HealthPath -Encoding UTF8
$record | ConvertTo-Json -Depth 6


