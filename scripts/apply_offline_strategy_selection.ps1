param(
    [Parameter(Mandatory = $true)]
    [string]$OpeningPack,
    [Parameter(Mandatory = $true)]
    [string]$StrategyId,
    [string]$OutputRoot = 'D:\QuantificationData\trading\research\opening_session_runs'
)

$ErrorActionPreference = 'Stop'
$python = 'D:\Quantification\env\python.exe'
$committee = 'D:\Quantification\run_committee.py'
$runner = 'D:\Quantification\scripts\run_next_session_opening.ps1'
$packageBuilder = 'D:\Quantification\scripts\build_offline_operator_package.ps1'

if (-not (Test-Path -LiteralPath $OpeningPack -PathType Leaf)) {
    throw "Missing opening pack: $OpeningPack"
}

$pack = Get-Content -Raw -LiteralPath $OpeningPack | ConvertFrom-Json
$executionDay = [string]$pack.execution_day
if (-not $executionDay) {
    throw 'Opening pack must contain execution_day'
}

$selectionRoot = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' $executionDay
$planVersion = 1
do {
    $planOutput = Join-Path $selectionRoot ("{0}_route_v{1}" -f $StrategyId, $planVersion)
    $planVersion++
} while (Test-Path -LiteralPath $planOutput -PathType Container)

# Refresh the selected paper plan before indexing the strategy matrix so the
# downstream receipt carries the newest offline THS readback template.
$planOutputLog = @(& $python $committee offline-strategy-plan `
    '--opening-pack' $OpeningPack `
    '--strategy' $StrategyId `
    '--out' $planOutput 2>&1)
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $planOutput 'offline_strategy_plan.json') -PathType Leaf)) {
    throw "Offline selected strategy plan refresh failed: $($planOutputLog -join ' ')"
}
$planPath = Join-Path $planOutput 'offline_strategy_plan.json'
$lifecycleOutput = @(& $python $committee paper-lifecycle-rehearsal `
    '--plan' $planPath `
    '--out' $planOutput 2>&1)
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $planOutput 'paper_lifecycle_rehearsal_v1.json') -PathType Leaf)) {
    throw "Offline lifecycle rehearsal failed: $($lifecycleOutput -join ' ')"
}

# First materialize the frozen candidate matrix. This call is explicitly
# offline and cannot reach the quote probe.
& $runner -OpeningPack $OpeningPack -OutputRoot $OutputRoot -OfflineOnly | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Offline opening-pack preparation failed'
}

$selectionRoot = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' $executionDay
$matrixPath = Join-Path $selectionRoot 'offline_selected_strategy_matrix.json'
$recordPath = Join-Path $selectionRoot 'paper_strategy_selection_record_v1.json'
if (-not (Test-Path -LiteralPath $matrixPath -PathType Leaf)) {
    throw "Offline strategy matrix was not created: $matrixPath"
}

$selectionOutput = @(& $python $committee paper-strategy-selection `
    '--matrix' $matrixPath `
    '--strategy-id' $StrategyId `
    '--out' $recordPath 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "Paper strategy selection failed: $($selectionOutput -join ' ')"
}

# Rebuild the desk from the explicit record. This is still paper-only and
# does not wait for, probe, or consume real-time quotes.
& $runner -OpeningPack $OpeningPack -OutputRoot $OutputRoot -Strategy $StrategyId -OfflineOnly | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Offline selected-strategy desk build failed'
}

# Keep the read-only API package on the same selected desk and evidence files.
# This rebuild remains offline-only and never observes a broker or submits an order.
& $packageBuilder -OpeningPack $OpeningPack -OutputRoot $OutputRoot | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Offline operator package refresh failed'
}

$runRoot = Join-Path $OutputRoot $executionDay
$statusPath = Join-Path $runRoot 'session_runner_status.json'
if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
    throw "Session status was not created: $statusPath"
}
$status = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
$snapshotPath = [string]$status.trader_desk_snapshot
if ([string]$status.offline_paper_strategy_selection_status -ne 'paper_candidate_explicitly_selected' -or
    [string]$status.offline_paper_selected_strategy_id -ne $StrategyId -or
    -not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
    throw 'Offline selection did not produce the requested single-strategy desk'
}
$snapshot = Get-Content -Raw -LiteralPath $snapshotPath | ConvertFrom-Json
if ([string]$snapshot.strategy_id -ne $StrategyId -or [bool]$snapshot.execution_admitted) {
    throw 'Selected desk identity or execution boundary is invalid'
}
if ([bool]$status.network_probe_attempted -or [bool]$status.account_observed -or [bool]$status.quotes_observed) {
    throw 'Offline-only selection unexpectedly observed live state'
}

$artifactNames = @(
    'paper_fill_projection',
    'paper_ledger_projection',
    'paper_order_lifecycle',
    'paper_position_projection',
    'paper_reconciliation',
    'paper_child_order_projection',
    'target_order_reconciliation',
    'paper_release_decision',
    'paper_next_session_carry',
    'paper_recovery_handoff_index',
    'strategy_promotion_card',
    'strategy_evidence_card',
    'strategy_comparison',
    'operator_action_board',
    'offline_portfolio_risk_summary'
)
$artifactPointers = [ordered]@{}
foreach ($artifactName in $artifactNames) {
    if ($snapshot.artifacts -and $snapshot.artifacts.PSObject.Properties.Name -contains $artifactName) {
        $artifactPointers[$artifactName] = [string]$snapshot.artifacts.$artifactName
    }
}
$selectedPlanPath = $null
$selectedPlanCandidates = @(Get-ChildItem -LiteralPath $selectionRoot -Recurse -Filter 'offline_strategy_plan.json' -File |
    Where-Object {
        try {
            $candidate = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
            [string]$candidate.strategy_id -eq $StrategyId -and [string]$candidate.execution_day -eq $executionDay
        } catch {
            $false
        }
    } |
    Sort-Object LastWriteTime -Descending)
if ($selectedPlanCandidates.Count -gt 0) {
    $selectedPlanPath = $selectedPlanCandidates[0].FullName
    $selectedPlan = Get-Content -Raw -LiteralPath $selectedPlanPath | ConvertFrom-Json
    foreach ($artifactName in @('ths_prefill_payloads', 'ths_prefill_payloads_summary', 'ths_prefill_readback_template')) {
        $value = if ($selectedPlan.selected_artifacts) { [string]$selectedPlan.selected_artifacts.$artifactName } else { '' }
        if ($value -and (Test-Path -LiteralPath $value -PathType Leaf)) {
            $artifactPointers[$artifactName] = $value
        }
    }
}
$riskSummaryPath = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' (Join-Path $executionDay 'offline_portfolio_risk_summary_v1.json')

$receiptPath = Join-Path $runRoot 'paper_strategy_application_receipt_v1.json'
$receipt = [ordered]@{
    schema = 'paper_strategy_application_receipt_v1'
    status = 'offline_paper_strategy_applied'
    execution_day = $executionDay
    opening_pack = (Resolve-Path $OpeningPack).Path
    strategy_id = $StrategyId
    selection_record = $recordPath
    session_status = $statusPath
    trader_desk_snapshot = $snapshotPath
    order_count = [int]$snapshot.orders.total
    ledger_preflight_accepted = [int]$snapshot.orders.accepted
    ledger_preflight_rejected = [int]$snapshot.orders.rejected
    staged_reduce_first_count = [int]$snapshot.orders.reduce_first_count
    staged_deferred_buy_count = [int]$snapshot.orders.deferred_buy_count
    strategy_selection = $snapshot.strategy_selection
    artifact_pointers = $artifactPointers
    selected_strategy_plan = $selectedPlanPath
    offline_risk_summary = if (Test-Path -LiteralPath $riskSummaryPath -PathType Leaf) { $riskSummaryPath } else { $null }
    execution_admitted = $false
    network_probe_attempted = $false
    account_observed = [bool]$snapshot.account_observed
    quotes_observed = [bool]$snapshot.quotes_observed
    client_interacted = $false
    submission_attempted = $false
    next_action = 'review the paper desk and recovery handoff; no live action was attempted'
}
$receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
$receipt | ConvertTo-Json -Depth 8
