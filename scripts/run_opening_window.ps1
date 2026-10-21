param(
    [Parameter(Mandatory = $true)]
    [string]$Profile,
    [string]$TradingDate = (Get-Date).ToString('yyyy-MM-dd'),
    [string]$OpeningPack,
    [string]$Strategy = 'momentum_12_1_20',
    [string]$OutputRoot = 'D:\QuantificationData\trading\research\opening_session_runs',
    [string]$CaptureOutputRoot = 'D:\QuantificationData\trading\ths\opening_readonly',
    [switch]$StructuredSession,
    [string]$CashflowsCsv,
    [switch]$RepeatCapture
)

$ErrorActionPreference = 'Stop'
$python = 'D:\Quantification\env\python.exe'
$committee = 'D:\Quantification\run_committee.py'
$readOnlyCapture = 'D:\Quantification\scripts\capture_ths_opening_readonly.ps1'
$openingRunner = 'D:\Quantification\scripts\run_next_session_opening.ps1'
$dayRoot = Join-Path $OutputRoot $TradingDate
$statusPath = Join-Path $dayRoot 'opening_window_coordinator.json'
$now = Get-Date

if (-not (Test-Path -LiteralPath $Profile -PathType Leaf)) {
    throw "Missing THS profile: $Profile"
}
if ($TradingDate -ne $now.ToString('yyyy-MM-dd')) {
    throw "Coordinator is restricted to today's trading date: $TradingDate"
}
New-Item -ItemType Directory -Force -Path $dayRoot | Out-Null

function Write-Status {
    param([System.Collections.IDictionary]$Status)
    $Status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    $Status | ConvertTo-Json -Depth 8
}

$openClock = [datetime]::ParseExact('09:25', 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
if ($now.TimeOfDay -lt $openClock.TimeOfDay) {
    Write-Status ([ordered]@{
        schema = 'opening_window_coordinator_v1'
        state = 'waiting_for_open_window'
        trading_date = $TradingDate
        observed_at = $now.ToString('o')
        earliest_action_time = "$TradingDate`T09:25:00+08:00"
        profile = (Resolve-Path -LiteralPath $Profile).Path
        execution_admitted = $false
        client_interacted = $false
        network_probe_attempted = $false
        ledger_mutated = $false
        submission_attempted = $false
        next_action = 'rerun after 09:25 local time; do not use historical quotes'
    })
    exit 0
}

if ((-not $RepeatCapture) -and (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
    $existingStatus = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
    if ($existingStatus.state -in @('opening_actions_completed', 'opening_actions_failed')) {
        $existingStatus | ConvertTo-Json -Depth 8
        exit 0
    }
}

# THS recreates its top-level windows after a restart. Refresh only the
# volatile HWND/PID values, and only after the helper has read back the mapped
# account summary. The original profile remains unchanged.
$refreshedProfile = Join-Path $dayRoot 'ths_session_profile_refreshed.json'
$refreshOutput = @(& $python $committee 'ths' 'refresh-profile' '--profile' $Profile '--out' $refreshedProfile 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "THS profile refresh failed with exit code $LASTEXITCODE`: $($refreshOutput -join ' ')"
}
$Profile = $refreshedProfile

function Invoke-ChildScript {
    param(
        [string]$Path,
        [string[]]$Arguments
    )
    $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "$Path failed with exit code $LASTEXITCODE`: $($output -join ' ')"
    }
    return $output
}

$captureArguments = @(
    '-Profile', $Profile,
    '-TradingDate', $TradingDate,
    '-OutputRoot', $CaptureOutputRoot,
    '-OCRCandidates'
)
if ($RepeatCapture) {
    $captureArguments += '-RepeatCapture'
}
if ($StructuredSession) {
    # Keep the structured THS read path explicitly opt-in.  The coordinator
    # remains image-only by default and never changes submission semantics.
    $captureArguments += '-StructuredSession'
}
if ($CashflowsCsv) {
    if (-not $StructuredSession) {
        throw '-CashflowsCsv requires -StructuredSession'
    }
    $captureArguments += @('-CashflowsCsv', $CashflowsCsv)
}

$captureManifest = $null
$capturePayload = $null
$runnerStatus = Join-Path $dayRoot 'session_runner_status.json'
try {
    Invoke-ChildScript -Path $readOnlyCapture -Arguments $captureArguments | Out-Null
    $captureDayRoot = Join-Path $CaptureOutputRoot $TradingDate
    $captureManifest = Get-ChildItem -LiteralPath $captureDayRoot -Filter 'manifest.json' -File -Recurse |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $captureManifest) {
        throw 'Read-only capture completed without a manifest'
    }
    $capturePayload = Get-Content -Raw -LiteralPath $captureManifest.FullName | ConvertFrom-Json

    $runnerArguments = @('-Strategy', $Strategy, '-OutputRoot', $OutputRoot)
    if ($OpeningPack) {
        $runnerArguments += @('-OpeningPack', $OpeningPack)
    }
    Invoke-ChildScript -Path $openingRunner -Arguments $runnerArguments | Out-Null
    $runnerPayload = $null
    if (Test-Path -LiteralPath $runnerStatus -PathType Leaf) {
        $runnerPayload = Get-Content -Raw -LiteralPath $runnerStatus | ConvertFrom-Json
    }

    $executionAdmitted = if ($runnerPayload) { [bool]$runnerPayload.execution_admitted } else { $false }
    $clientInteracted = if ($runnerPayload) { [bool]$runnerPayload.client_interacted } else { $false }
    $networkProbeAttempted = if ($runnerPayload) { [bool]$runnerPayload.network_probe_attempted } else { $false }
    $ledgerMutated = if ($runnerPayload) { [bool]$runnerPayload.ledger_mutated } else { $false }
    $submissionAttempted = if ($runnerPayload) { [bool]$runnerPayload.submission_attempted } else { $false }

    Write-Status ([ordered]@{
        schema = 'opening_window_coordinator_v1'
        state = 'opening_actions_completed'
        trading_date = $TradingDate
        observed_at = (Get-Date).ToString('o')
        profile = (Resolve-Path -LiteralPath $Profile).Path
        read_only_manifest = $captureManifest.FullName
        structured_session_receipt = if ($capturePayload) { [string]$capturePayload.structured_session_receipt } else { $null }
        structured_session_bundle = if ($capturePayload) { [string]$capturePayload.structured_session_bundle } else { $null }
        structured_cashflows_csv = if ($capturePayload) { [string]$capturePayload.structured_cashflows_csv } else { $null }
        runner_status = if (Test-Path -LiteralPath $runnerStatus -PathType Leaf) { $runnerStatus } else { $null }
        execution_admitted = $executionAdmitted
        client_interacted = $clientInteracted
        network_probe_attempted = $networkProbeAttempted
        ledger_mutated = $ledgerMutated
        submission_attempted = $submissionAttempted
        next_action = 'review the live evidence and keep submission manual'
    })
} catch {
    $errorMessage = $_.Exception.Message
    Write-Status ([ordered]@{
        schema = 'opening_window_coordinator_v1'
        state = 'opening_actions_failed'
        trading_date = $TradingDate
        observed_at = (Get-Date).ToString('o')
        profile = (Resolve-Path -LiteralPath $Profile).Path
        read_only_manifest = if ($captureManifest) { $captureManifest.FullName } else { $null }
        structured_session_receipt = if ($capturePayload) { [string]$capturePayload.structured_session_receipt } else { $null }
        structured_session_bundle = if ($capturePayload) { [string]$capturePayload.structured_session_bundle } else { $null }
        structured_cashflows_csv = if ($capturePayload) { [string]$capturePayload.structured_cashflows_csv } else { $null }
        runner_status = if (Test-Path -LiteralPath $runnerStatus -PathType Leaf) { $runnerStatus } else { $null }
        execution_admitted = $false
        client_interacted = $false
        network_probe_attempted = $false
        ledger_mutated = $false
        submission_attempted = $false
        error = $errorMessage
        next_action = 'inspect this failure; retry only explicitly with -RepeatCapture'
    }) | Out-Null
    throw
}
