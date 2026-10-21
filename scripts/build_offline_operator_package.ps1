param(
    [Parameter(Mandatory = $true)]
    [string]$OpeningPack,
    [string]$OutputRoot = 'D:\QuantificationData\trading\research\opening_session_runs',
    [string]$RobustnessDir,
    [switch]$SkipPrepare
)

$ErrorActionPreference = 'Stop'
$runner = 'D:\Quantification\scripts\run_next_session_opening.ps1'
$tradingHome = if ($env:QUANT_TRADING_HOME) { $env:QUANT_TRADING_HOME } else { 'D:\QuantificationData\trading' }

if (-not (Test-Path -LiteralPath $OpeningPack -PathType Leaf)) {
    throw "Missing opening pack: $OpeningPack"
}

$pack = Get-Content -Raw -Encoding UTF8 -LiteralPath $OpeningPack | ConvertFrom-Json
$executionDay = [string]$pack.execution_day
if (-not $executionDay) {
    throw 'Opening pack must contain execution_day'
}

# This package is deliberately offline-only. It materializes the existing
# paper desk and only indexes its frozen artifacts for downstream consumers.
if (-not $SkipPrepare) {
    & $runner -OpeningPack $OpeningPack -OutputRoot $OutputRoot -OfflineOnly -NoOperatorPackage | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Offline paper desk preparation failed'
    }
}

$runRoot = Join-Path $OutputRoot $executionDay
$statusPath = Join-Path $runRoot 'session_runner_status.json'
if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
    throw "Session status was not created: $statusPath"
}
$status = Get-Content -Raw -Encoding UTF8 -LiteralPath $statusPath | ConvertFrom-Json
$snapshotPath = [string]$status.trader_desk_snapshot
if (-not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
    throw "Trader desk snapshot was not created: $snapshotPath"
}
$snapshot = Get-Content -Raw -Encoding UTF8 -LiteralPath $snapshotPath | ConvertFrom-Json

# A package is only useful when its narrowed opening-pack strategy, saved
# selection receipt, and desk snapshot all describe the same candidate.  A
# stale same-date selection must fail closed instead of producing a misleading
# operator package with mixed strategy artifacts.
$packStrategyId = [string]$pack.selected_strategy_id
if ($packStrategyId) {
    $statusStrategyId = [string]$status.offline_paper_selected_strategy_id
    $snapshotStrategyId = [string]$snapshot.strategy_id
    $snapshotSelectedStrategyId = [string]$snapshot.strategy_selection.selected_strategy_id
    if ($statusStrategyId -ne $packStrategyId -or
        $snapshotStrategyId -ne $packStrategyId -or
        $snapshotSelectedStrategyId -ne $packStrategyId) {
        throw ("Opening-pack strategy mismatch: pack={0}; status={1}; snapshot={2}; snapshot_selected={3}" -f
            $packStrategyId, $statusStrategyId, $snapshotStrategyId, $snapshotSelectedStrategyId)
    }
}

if ([string]$status.state -ne 'offline_only_ready' -or
    [bool]$status.network_probe_attempted -or
    [bool]$status.account_observed -or
    [bool]$status.quotes_observed -or
    [bool]$status.client_interacted -or
    [bool]$status.ledger_mutated -or
    [bool]$status.submission_attempted -or
    [bool]$status.execution_admitted) {
    throw 'Offline operator package has an invalid live-state or execution boundary'
}

$deskRoot = Split-Path -Parent $snapshotPath
$artifactPointers = [ordered]@{}
foreach ($property in $snapshot.artifacts.PSObject.Properties) {
    $artifactPointers[$property.Name] = [string]$property.Value
}
# Carry the newest read-only research decision matrix into the same operator
# package. The matrix is evidence/exception metadata only; it never authorizes
# selection, Ledger mutation, or broker submission.
$matrixRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'outputs\research_decision_matrices'
if (Test-Path -LiteralPath $matrixRoot -PathType Container) {
    foreach ($matrixPath in (Get-ChildItem -LiteralPath $matrixRoot -Recurse -Filter 'run.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $matrix = Get-Content -Raw -Encoding UTF8 -LiteralPath $matrixPath.FullName | ConvertFrom-Json
            if ([string]$matrix.schema -eq 'research_decision_matrix_v1' -and
                [string]$matrix.status -eq 'completed_read_only_research_decision_matrix' -and
                -not [bool]$matrix.execution_admitted) {
                $artifactPointers['research_decision_matrix'] = (Resolve-Path $matrixPath.FullName).Path
                break
            }
        } catch {
            # Ignore malformed or incomplete research matrix receipts.
        }
    }
}
# The sell-first manual release index is generated beside the selected-plan
# matrix rather than inside the desk snapshot. Carry it into the top-level
# package so the operator UI can consume the exact release ordering.
$releaseSequencePath = [string]$status.offline_paper_manual_release_sequence_index
if ($releaseSequencePath -and (Test-Path -LiteralPath $releaseSequencePath -PathType Leaf)) {
    $artifactPointers['paper_manual_release_sequence_index'] = (Resolve-Path $releaseSequencePath).Path
}
# The recovery index is created alongside the selected-strategy matrix and is
# indexed in session status; carry it into the package just like the manual
# release sequence so the API exposes resumable exception states.
$recoveryHandoffIndexPath = [string]$status.offline_paper_recovery_handoff_index
if ($recoveryHandoffIndexPath -and (Test-Path -LiteralPath $recoveryHandoffIndexPath -PathType Leaf)) {
    $artifactPointers['paper_recovery_handoff_index'] = (Resolve-Path $recoveryHandoffIndexPath).Path
}
$paperChildOrderReconciliation = $null
$childReconciliationSummaryPath = [string]$artifactPointers['paper_child_order_reconciliation_summary']
$childReconciliationRowsPath = [string]$artifactPointers['paper_child_order_reconciliation']
if ($childReconciliationSummaryPath -or $childReconciliationRowsPath) {
    if (-not (Test-Path -LiteralPath $childReconciliationSummaryPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $childReconciliationRowsPath -PathType Leaf)) {
        throw 'Paper child-order reconciliation pointers are incomplete'
    }
    try {
        $childSummary = Get-Content -Raw -Encoding UTF8 -LiteralPath $childReconciliationSummaryPath | ConvertFrom-Json
    } catch {
        throw "Paper child-order reconciliation summary is unreadable: $childReconciliationSummaryPath"
    }
    if ([string]$childSummary.schema -ne 'paper_child_order_reconciliation_v1' -or
        [bool]$childSummary.real_broker_state -or
        [bool]$childSummary.execution_admitted) {
        throw 'Paper child-order reconciliation has an invalid execution boundary'
    }
    $paperChildOrderReconciliation = [ordered]@{
        schema = [string]$childSummary.schema
        status = [string]$childSummary.status
        summary = $childSummary
        rows = (Resolve-Path $childReconciliationRowsPath).Path
        summary_file = (Resolve-Path $childReconciliationSummaryPath).Path
        real_broker_state = $false
        execution_admitted = $false
    }
}
$functionMatrixPath = Join-Path $deskRoot 'function_matrix.csv'

# Carry the newest explicitly frozen StockDB snapshot into the operator
# package. The snapshot remains research-only; this index pins the data basis
# for downstream consumers without admitting it to execution.
$stockdbFrozenSnapshot = $null
$stockdbFrozenSnapshotCutoffMatch = $false
$preferredStockdbSnapshotPath = $null
$forwardComparePointer = [string]$artifactPointers['stockdb_forward_compare']
if ($forwardComparePointer -and (Test-Path -LiteralPath $forwardComparePointer -PathType Leaf)) {
    try {
        $forwardCompare = Get-Content -Raw -Encoding UTF8 -LiteralPath $forwardComparePointer | ConvertFrom-Json
        $preferredStockdbSnapshotPath = [string]$forwardCompare.snapshot
        if ($preferredStockdbSnapshotPath -and (Test-Path -LiteralPath $preferredStockdbSnapshotPath -PathType Container)) {
            $preferredStockdbSnapshotPath = Join-Path $preferredStockdbSnapshotPath 'snapshot.json'
        }
        if (-not $preferredStockdbSnapshotPath -or
            -not (Test-Path -LiteralPath $preferredStockdbSnapshotPath -PathType Leaf)) {
            $preferredStockdbSnapshotPath = $null
        } else {
            $preferredStockdbSnapshotPath = (Resolve-Path $preferredStockdbSnapshotPath).Path
        }
    } catch {
        $preferredStockdbSnapshotPath = $null
    }
}
$stockdbSnapshotRoot = Join-Path $tradingHome 'research\stockdb_snapshots'
if (Test-Path -LiteralPath $stockdbSnapshotRoot -PathType Container) {
    foreach ($snapshotCandidate in (Get-ChildItem -LiteralPath $stockdbSnapshotRoot -Recurse -Filter 'snapshot.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $frozenSnapshot = Get-Content -Raw -Encoding UTF8 -LiteralPath $snapshotCandidate.FullName | ConvertFrom-Json
            if ([string]$frozenSnapshot.schema -ne 'stockdb_research_snapshot_v1' -or
                -not [bool]$frozenSnapshot.research_only -or
                [bool]$frozenSnapshot.execution_admitted -or
                [bool]$frozenSnapshot.historical_feed_promoted) {
                continue
            }
            $candidatePath = (Resolve-Path $snapshotCandidate.FullName).Path
            $candidateSnapshot = [ordered]@{
                status = 'frozen_research_snapshot'
                source_file = $candidatePath
                captured_at = [string]$frozenSnapshot.captured_at
                endpoint = [string]$frozenSnapshot.endpoint
                requested_symbols = @($frozenSnapshot.requested_symbols).Count
                requested_dates = @($frozenSnapshot.requested_dates).Count
                requested_daily = [int]$frozenSnapshot.rows.requested_daily
                available_daily = [int]$frozenSnapshot.rows.available_daily
                missing_daily = [int]$frozenSnapshot.rows.missing_daily
                available_minute = [int]$frozenSnapshot.rows.available_minute
                research_only = $true
                historical_feed_promoted = $false
                execution_admitted = $false
            }
            $candidateMatchesCutoff = @($frozenSnapshot.requested_dates) -contains [string]$snapshot.source_cutoff
            $candidateHasEffectiveSessionCoverage =
                ($frozenSnapshot.PSObject.Properties.Name -contains 'effective_session_dates') -and
                [int]$frozenSnapshot.rows.missing_daily -eq 0
            $currentHasEffectiveSessionCoverage = $false
            if ($stockdbFrozenSnapshot) {
                $currentHasEffectiveSessionCoverage =
                    [int]$stockdbFrozenSnapshot.missing_daily -eq 0 -and
                    [int]$stockdbFrozenSnapshot.requested_daily -eq [int]$stockdbFrozenSnapshot.available_daily
            }
            $candidateIsReplaySource = $preferredStockdbSnapshotPath -and
                $candidatePath.Equals($preferredStockdbSnapshotPath, [System.StringComparison]::OrdinalIgnoreCase)
            $candidatePreferred = if ($candidateHasEffectiveSessionCoverage -and
                (-not $stockdbFrozenSnapshot -or -not $currentHasEffectiveSessionCoverage)) {
                $true
            } elseif ($candidateIsReplaySource -and -not $currentHasEffectiveSessionCoverage) {
                $true
            } elseif ($candidateMatchesCutoff) {
                -not $stockdbFrozenSnapshotCutoffMatch -or
                [int]$candidateSnapshot.available_daily -gt [int]$stockdbFrozenSnapshot.available_daily
            } else {
                $null -eq $stockdbFrozenSnapshot
            }
            if ($candidatePreferred) {
                $stockdbFrozenSnapshot = $candidateSnapshot
                $stockdbFrozenSnapshotCutoffMatch = [bool]$candidateMatchesCutoff
            }
            if ($candidateIsReplaySource) {
                break
            }
        } catch {
            # Ignore incomplete snapshots while indexing the operator package.
        }
    }
}
if ($stockdbFrozenSnapshot) {
    $stockdbFrozenSnapshot['source_cutoff_match'] = [bool]$stockdbFrozenSnapshotCutoffMatch
    $stockdbFrozenSnapshot['selection_basis'] = if ($preferredStockdbSnapshotPath -and
        $stockdbFrozenSnapshot.source_file.Equals($preferredStockdbSnapshotPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        'stockdb_forward_compare_snapshot'
    } else {
        'latest_cutoff_matched_frozen_snapshot'
    }
    $artifactPointers['stockdb_frozen_research_snapshot'] = $stockdbFrozenSnapshot.source_file
}

# Index the newest completed shared-Ledger replay so the operator package
# consumes the same frozen snapshot that was actually replayed, instead of a
# stale pointer retained by an older desk snapshot.
$stockdbLedgerReplayRoot = Join-Path $tradingHome 'research\stockdb_ledger_replays'
if (Test-Path -LiteralPath $stockdbLedgerReplayRoot -PathType Container) {
    foreach ($replaySummaryPath in (Get-ChildItem -LiteralPath $stockdbLedgerReplayRoot -Recurse -Filter 'summary.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $replaySummary = Get-Content -Raw -Encoding UTF8 -LiteralPath $replaySummaryPath.FullName | ConvertFrom-Json
            if ([string]$replaySummary.schema -ne 'stockdb_shared_ledger_replay_v1' -or
                [string]$replaySummary.status -ne 'completed_research_only' -or
                -not [bool]$replaySummary.research_only -or
                [bool]$replaySummary.execution_admitted) {
                continue
            }
            $artifactPointers['stockdb_ledger_replay'] = (Resolve-Path $replaySummaryPath.FullName).Path
            break
        } catch {
            # Ignore incomplete replay outputs while indexing the package.
        }
    }
}

$functionRows = @()
if (Test-Path -LiteralPath $functionMatrixPath -PathType Leaf) {
    $artifactPointers['function_matrix'] = $functionMatrixPath
    $functionRows = @(Import-Csv -Encoding UTF8 -LiteralPath $functionMatrixPath | ForEach-Object {
        [ordered]@{
            function_id = [string]$_.function_id
            institutional_reference = [string]$_.institutional_reference
            institutional_capability = [string]$_.institutional_capability
            local_state = [string]$_.local_state
            next_gap = [string]$_.next_gap
            artifact = [string]$_.artifact
            priority = [string]$_.priority
            tomorrow_use = [string]$_.tomorrow_use
            next_implementation = [string]$_.next_implementation
            source_url = [string]$_.source_url
            execution_admitted = [string]$_.execution_admitted
        }
    })
}

$operatorActionRows = @()
$operatorActionPath = [string]$snapshot.artifacts.operator_action_board
if ($operatorActionPath -and (Test-Path -LiteralPath $operatorActionPath -PathType Leaf)) {
    $operatorActionRows = @(Import-Csv -Encoding UTF8 -LiteralPath $operatorActionPath | ForEach-Object {
        [ordered]@{
            priority = [string]$_.priority
            function_id = [string]$_.function_id
            state = [string]$_.state
            action = [string]$_.action
            prerequisite = [string]$_.prerequisite
            blockers = [string]$_.blockers
            execution_admitted = [string]$_.execution_admitted
            live_action = [string]$_.live_action
        }
    })
}

# Carry selected-plan THS artifacts into the top-level package so downstream
# consumers do not need to rediscover the paper-selection receipt.
$selectedReceiptPath = Join-Path $runRoot 'paper_strategy_application_receipt_v1.json'
if (Test-Path -LiteralPath $selectedReceiptPath -PathType Leaf) {
    try {
        $selectedReceipt = Get-Content -Raw -Encoding UTF8 -LiteralPath $selectedReceiptPath | ConvertFrom-Json
        foreach ($artifactName in @('ths_prefill_payloads', 'ths_prefill_payloads_summary', 'ths_prefill_readback_template')) {
            $artifactValue = if ($selectedReceipt.artifact_pointers) { [string]$selectedReceipt.artifact_pointers.$artifactName } else { '' }
            if ($artifactValue -and (Test-Path -LiteralPath $artifactValue -PathType Leaf)) {
                $artifactPointers[$artifactName] = $artifactValue
            }
        }
    } catch {
        # Keep the package build usable when an older receipt is incomplete.
    }
}

# Prefer the plan indexed by the current session status over the legacy
# application receipt. The receipt is intentionally retained for history, but
# it may point to an older route after a newer offline plan is rebuilt.
$selectedStrategyId = [string]$status.offline_paper_selected_strategy_id
$selectedPlanRows = @($status.offline_selected_strategy_plans |
    Where-Object { [string]$_.strategy_id -eq $selectedStrategyId })
if ($selectedPlanRows.Count -gt 0) {
    $selectedPlanRow = $selectedPlanRows[0]
    foreach ($artifactName in @('ths_prefill_payloads', 'ths_prefill_payloads_summary', 'ths_prefill_readback_template')) {
        $artifactValue = [string]$selectedPlanRow.$artifactName
        if ($artifactValue -and (Test-Path -LiteralPath $artifactValue -PathType Leaf)) {
            $artifactPointers[$artifactName] = $artifactValue
        }
    }
}

# Carry the latest batch readback validation beside the THS template. The
# validation is observational only and never changes the plan or Ledger.
$readbackTemplatePointer = [string]$artifactPointers['ths_prefill_readback_template']
if ($readbackTemplatePointer -and (Test-Path -LiteralPath $readbackTemplatePointer -PathType Leaf)) {
    $readbackValidationCandidate = Join-Path (Split-Path -Parent $readbackTemplatePointer) 'ths_prefill_readback_validation_latest.json'
    if (Test-Path -LiteralPath $readbackValidationCandidate -PathType Leaf) {
        $artifactPointers['ths_prefill_readback_validation'] = (Resolve-Path $readbackValidationCandidate).Path
    }
}

# Carry the complete observation-bundle template contract into the package.
# The opening runner exposes these paths in session status, but the operator
# package is the consumer-facing handoff and must be self-discovering.
$thsObservationTemplates = [ordered]@{}
foreach ($templateName in @(
        'account_observation_template',
        'orders_observation_template',
        'trades_observation_template',
        'cashflows_observation_template',
        'movements_observation_template',
        'restriction_releases_observation_template',
        'share_credits_observation_template')) {
    $templateValue = if ($status.PSObject.Properties[$templateName]) {
        [string]$status.$templateName
    } else { '' }
    if ($templateValue) {
        $thsObservationTemplates[$templateName] = $templateValue
        if (Test-Path -LiteralPath $templateValue -PathType Leaf) {
            $artifactPointers[$templateName] = (Resolve-Path $templateValue).Path
        }
    }
}

# Carry an explicitly requested structured THS readback into the package when
# it belongs to this execution day.  This is an observation handoff only: the
# cumulative cashflow export is still required before reconciliation/import.
$thsStructuredSession = $null
$structuredCaptureRoot = Join-Path $tradingHome 'ths\opening_readonly'
$structuredManifest = $null
if (Test-Path -LiteralPath (Join-Path $structuredCaptureRoot $executionDay) -PathType Container) {
    $structuredManifest = Get-ChildItem -LiteralPath (Join-Path $structuredCaptureRoot $executionDay) `
        -Filter 'manifest.json' -File -Recurse |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if ($structuredManifest) {
    try {
        $capture = Get-Content -Raw -Encoding UTF8 -LiteralPath $structuredManifest.FullName | ConvertFrom-Json
        $receiptPath = [string]$capture.structured_session_receipt
        $bundlePath = [string]$capture.structured_session_bundle
        if ([string]$capture.schema -eq 'ths_opening_readonly_capture_v1' -and
            [string]$capture.trading_date -eq $executionDay -and
            [bool]$capture.read_only -and
            -not [bool]$capture.submitted -and
            -not [bool]$capture.execution_admitted -and
            -not [bool]$capture.ledger_mutated -and
            $receiptPath -and (Test-Path -LiteralPath $receiptPath -PathType Leaf) -and
            $bundlePath -and (Test-Path -LiteralPath $bundlePath -PathType Container)) {
            $thsStructuredSession = [ordered]@{
                status = 'structured_session_captured_read_only'
                manifest = (Resolve-Path $structuredManifest.FullName).Path
                receipt = (Resolve-Path $receiptPath).Path
                bundle = (Resolve-Path $bundlePath).Path
                cashflows = if ($capture.structured_cashflows_csv -and
                    (Test-Path -LiteralPath ([string]$capture.structured_cashflows_csv) -PathType Leaf)) {
                    (Resolve-Path ([string]$capture.structured_cashflows_csv)).Path
                } else { $null }
                cashflows_required = $true
                execution_admitted = $false
                ledger_mutated = $false
            }
            $artifactPointers['ths_structured_session_manifest'] = $thsStructuredSession.manifest
            $artifactPointers['ths_structured_session_receipt'] = $thsStructuredSession.receipt
            $artifactPointers['ths_structured_session_bundle'] = $thsStructuredSession.bundle
            if ($thsStructuredSession.cashflows) {
                $artifactPointers['ths_structured_session_cashflows'] = $thsStructuredSession.cashflows
            }
        }
    } catch {
        # An incomplete or malformed capture must remain invisible to the
        # package rather than becoming a misleading account-evidence pointer.
    }
}

# Keep the newest saved same-ledger comparison discoverable from the paper
# operator package. This is a read-only derived artifact; it never selects or
# promotes a strategy and does not rerun an account.
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$comparisonRoot = Join-Path $repoRoot 'outputs\research_comparisons'
if (Test-Path -LiteralPath $comparisonRoot -PathType Container) {
    $latestComparison = Get-ChildItem -LiteralPath $comparisonRoot -Filter 'comparison.json' -Recurse -File |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latestComparison) {
        # Keep the recency-only pointer under an explicit name.  A newer
        # diagnostic family (for example public-seat research) must not be
        # mistaken for the rule-versus-Alpha158 benchmark consumed by the
        # strategy desk.
        $artifactPointers['latest_any_saved_ledger_comparison'] = $latestComparison.FullName
    }

    # Keep a separate pointer for the matched rule-versus-Alpha158/LightGBM
    # comparison. The newest comparison can belong to another research family
    # (for example public-seat diagnostics), so recency alone is not sufficient
    # for the model benchmark consumed by the operator desk.
    foreach ($candidateComparison in (Get-ChildItem -LiteralPath $comparisonRoot -Filter 'comparison.json' -Recurse -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $comparison = Get-Content -Raw -Encoding UTF8 -LiteralPath $candidateComparison.FullName | ConvertFrom-Json
            if (-not [bool]$comparison.same_cost_settings -or
                [bool]$comparison.execution_admitted -or
                [int]$comparison.comparable_cases -le 0) {
                continue
            }
            $hasAlpha158Case = @($comparison.cases | Where-Object {
                ([string]$_.family -like 'alpha158*') -or
                ([string]$_.family -like '*alpha158*') -or
                ([string]$_.signal -like 'alpha158*')
            }).Count -gt 0
            $hasRuleCase = @($comparison.cases | Where-Object {
                [string]$_.family -eq 'low_risk_stock_selection'
            }).Count -gt 0
            if ($hasAlpha158Case -and $hasRuleCase) {
                $alpha158Comparison = (Resolve-Path $candidateComparison.FullName).Path
                $artifactPointers['alpha158_shared_ledger_comparison'] = $alpha158Comparison
                # The primary comparison shown beside strategy evidence must
                # be the same-basis rule/model benchmark, not an unrelated
                # newer diagnostic family.
                $artifactPointers['saved_ledger_comparison'] = $alpha158Comparison
                $artifactPointers['latest_saved_ledger_comparison'] = $alpha158Comparison
                break
            }
        } catch {
            # Ignore malformed comparison receipts while indexing the package.
        }
    }
}

# Expose the newest complete StockDB factor-ledger replay as an independent
# research diagnostic. Its sample is intentionally kept separate from the
# opening candidates, so the desk can inspect it without treating unlike
# windows as a strategy comparison or promotion signal.
$factorReplayRoot = Join-Path $tradingHome 'research\stockdb_factor_replay'
$stockdbFactorReplay = $null
if (Test-Path -LiteralPath $factorReplayRoot -PathType Container) {
    foreach ($factorReplayPath in (Get-ChildItem -LiteralPath $factorReplayRoot -Recurse -Filter 'result.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $factorReplay = Get-Content -Raw -Encoding UTF8 -LiteralPath $factorReplayPath.FullName | ConvertFrom-Json
            if ([string]$factorReplay.status -ne 'completed_factor_ledger_diagnostics' -or
                [bool]$factorReplay.execution_admitted) {
                continue
            }
            $stockdbFactorReplay = [ordered]@{
                status = [string]$factorReplay.status
                source_file = (Resolve-Path $factorReplayPath.FullName).Path
                replay_id = [string]$factorReplay.id
                start = [string]$factorReplay.start
                end = [string]$factorReplay.end
                sessions = [int]$factorReplay.sessions
                comparable_to_opening_candidates = $false
                reason_not_comparable = 'different research window and factor family; diagnostic only'
                results = @($factorReplay.results | ForEach-Object {
                    [ordered]@{
                        signal = [string]$_.signal
                        status = [string]$_.status
                        net_return = $_.net_return
                        max_drawdown = $_.max_drawdown
                        sharpe_zero_rf_252 = $_.sharpe_zero_rf_252
                        fees = $_.fees
                        orders = $_.orders
                        fills = $_.fills
                        stale_valuation_days = $_.stale_valuation_days
                    }
                })
                execution_admitted = $false
            }
            $artifactPointers['stockdb_factor_replay'] = $stockdbFactorReplay.source_file
            break
        } catch {
            # Ignore incomplete factor replays while indexing the package.
        }
    }
}

# Carry the fixed non-overlapping StockDB/Tencent comparison into the same
# operator package.  This keeps independent-sample evidence visible to the
# consumer without treating it as a promotion or execution signal.
$stockdbIndependentSample = $null
$independentSampleRoot = Join-Path $repoRoot 'outputs\stockdb_independent_sample_compare'
if (Test-Path -LiteralPath $independentSampleRoot -PathType Container) {
    foreach ($independentPath in (Get-ChildItem -LiteralPath $independentSampleRoot -Recurse -Filter 'run.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $independent = Get-Content -Raw -Encoding UTF8 -LiteralPath $independentPath.FullName | ConvertFrom-Json
            if ([string]$independent.schema -ne 'stockdb_independent_sample_compare_v1' -or
                [string]$independent.status -ne 'completed_research_only' -or
                -not [bool]$independent.independent_sample -or
                [bool]$independent.prior_forward_overlap -or
                [bool]$independent.execution_admitted) {
                continue
            }
            $evaluation = $independent.evaluation_window
            $computedPriorForwardOverlap = ([string]$evaluation.start -le '2026-10-09' -and
                [string]$evaluation.end -ge '2026-09-24')
            if ($computedPriorForwardOverlap) {
                continue
            }
            $stockdbIndependentSample = [ordered]@{
                status = [string]$independent.status
                source_file = (Resolve-Path $independentPath.FullName).Path
                snapshot = [string]$independent.snapshot
                selection_cutoff = [string]$independent.selection_cutoff
                evaluation_start = [string]$evaluation.start
                evaluation_end = [string]$evaluation.end
                evaluation_sessions = @($evaluation.sessions).Count
                alternate_provider = [string]$independent.alternate_provider
                provider_complete_paired_rows = [int]$independent.provider_complete_paired_rows
                independent_sample = $true
                prior_forward_overlap = $false
                independent_sample_basis = [string]$independent.independent_sample_basis
                ledger_results = @($independent.ledger_results)
                research_only = $true
                execution_admitted = $false
            }
            $artifactPointers['stockdb_independent_sample_compare'] = $stockdbIndependentSample.source_file
            break
        } catch {
            # Ignore incomplete comparison runs while indexing the package.
        }
    }
}

$stockdbIndependentWindowStability = $null
$independentStabilityRoot = Join-Path $repoRoot 'outputs\stockdb_independent_window_stability'
if (Test-Path -LiteralPath $independentStabilityRoot -PathType Container) {
    foreach ($stabilityPath in (Get-ChildItem -LiteralPath $independentStabilityRoot -Recurse -Filter 'run.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $stability = Get-Content -Raw -Encoding UTF8 -LiteralPath $stabilityPath.FullName | ConvertFrom-Json
            if ([string]$stability.schema -ne 'stockdb_independent_window_stability_v1' -or
                [string]$stability.status -ne 'completed_research_only' -or
                -not [bool]$stability.non_overlapping -or
                [bool]$stability.execution_admitted) {
                continue
            }
            $stockdbIndependentWindowStability = [ordered]@{
                status = [string]$stability.status
                source_file = (Resolve-Path $stabilityPath.FullName).Path
                window_count = [int]$stability.window_count
                window_start = [string]$stability.window_start
                window_end = [string]$stability.window_end
                non_overlapping = $true
                calendar_continuity_verified = [bool]$stability.calendar_continuity_verified
                calendar_window_coverage_verified = [bool]$stability.calendar_window_coverage_verified
                strategy_summaries = @($stability.strategy_summaries | ForEach-Object {
                    [ordered]@{
                        candidate = [string]$_.candidate
                        windows = [int]$_.windows
                        positive_windows = [int]$_.positive_windows
                        mean_net_return = $_.mean_net_return
                        median_net_return = $_.median_net_return
                        worst_net_return = $_.worst_net_return
                        best_net_return = $_.best_net_return
                        worst_max_drawdown = $_.worst_max_drawdown
                        total_sessions = [int]$_.total_sessions
                        total_orders = [int]$_.total_orders
                        total_fills = [int]$_.total_fills
                        paired_rows_all_windows = [bool]$_.paired_rows_all_windows
                    }
                })
                research_only = $true
                execution_admitted = $false
            }
            $artifactPointers['stockdb_independent_window_stability'] = $stockdbIndependentWindowStability.source_file
            break
        } catch {
            # Ignore incomplete stability runs while indexing the package.
        }
    }
}

# Expose the latest factor accounting attribution through the same package
# index. The evidence card keeps its causal/execution boundary explicit.
$evidenceCardPath = [string]$artifactPointers['strategy_evidence_card']
if ($evidenceCardPath -and (Test-Path -LiteralPath $evidenceCardPath -PathType Leaf)) {
    try {
        $evidenceCard = Get-Content -Raw -Encoding UTF8 -LiteralPath $evidenceCardPath | ConvertFrom-Json
        $optionalEvidencePointers = @(
            @{ key = 'factor_candidate_attribution'; node = 'factor_attribution' },
            @{ key = 'factor_candidate_cost_replay'; node = 'factor_cost_replay' },
            @{ key = 'factor_candidate_cadence_replay'; node = 'factor_cadence_replay' },
            @{ key = 'factor_candidate_cadence_stability'; node = 'factor_cadence_stability' },
            @{ key = 'factor_current_score'; node = 'factor_current_score' },
            @{ key = 'factor_paper_preview'; node = 'factor_paper_preview' },
            @{ key = 'research_decision_matrix'; node = 'research_decision_matrix' },
            @{ key = 'factor_regime_overlay'; node = 'factor_regime_overlay' }
        )
        foreach ($pointer in $optionalEvidencePointers) {
            $nodeProperty = $evidenceCard.PSObject.Properties[$pointer.node]
            $pathProperty = if ($nodeProperty -and $nodeProperty.Value) {
                $nodeProperty.Value.PSObject.Properties['path']
            } else {
                $null
            }
            $candidatePath = if ($pathProperty) { [string]$pathProperty.Value } else { '' }
            if ($candidatePath -and (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
                $artifactPointers[$pointer.key] = $candidatePath
            }
        }
        $matrixProperty = $evidenceCard.PSObject.Properties['research_decision_matrix']
        $ablationProperty = if ($matrixProperty -and $matrixProperty.Value) {
            $matrixProperty.Value.PSObject.Properties['factor_library_ablation']
        } else {
            $null
        }
        $ablationPathProperty = if ($ablationProperty -and $ablationProperty.Value) {
            $ablationProperty.Value.PSObject.Properties['path']
        } else {
            $null
        }
        $ablationPath = if ($ablationPathProperty) { [string]$ablationPathProperty.Value } else { '' }
        if ($ablationPath -and (Test-Path -LiteralPath $ablationPath -PathType Leaf)) {
            $artifactPointers['factor_library_ablation'] = $ablationPath
        }
        $riskProperty = $evidenceCard.PSObject.Properties['saved_risk_analytics']
        $riskViewsProperty = if ($riskProperty -and $riskProperty.Value) {
            $riskProperty.Value.PSObject.Properties['views']
        } else {
            $null
        }
        if ($riskViewsProperty -and $riskViewsProperty.Value) {
            @(
                @{ key = 'risk_concentration'; name = 'concentration' },
                @{ key = 'stress_exposure'; name = 'stress_exposure' },
                @{ key = 'style_exposure'; name = 'style_exposure' }
            ) | ForEach-Object {
                $viewProperty = $riskViewsProperty.Value.PSObject.Properties[$_.name]
                $viewPathProperty = if ($viewProperty -and $viewProperty.Value) {
                    $viewProperty.Value.PSObject.Properties['path']
                } else {
                    $null
                }
                $viewPath = if ($viewPathProperty) { [string]$viewPathProperty.Value } else { '' }
                if ($viewPath -and (Test-Path -LiteralPath $viewPath -PathType Leaf)) {
                    $artifactPointers[$_.key] = $viewPath
                }
            }
        }
    } catch {
        # Keep older evidence cards consumable when this optional section is absent.
        Write-Warning ("Optional evidence index skipped for {0}: {1}" -f $evidenceCardPath, $_.Exception.GetType().Name)
    }
}

# Keep the newest completed Alpha158/LightGBM method comparison addressable from
# the offline package even when the selected paper desk snapshot predates it.
# This is a research-only pointer; it never changes strategy selection or
# execution admission.
$alpha158ComparisonPath = $null
$repoRoot = Split-Path -Parent $PSScriptRoot
$alpha158Root = Join-Path $repoRoot 'outputs\market_replay'
if (Test-Path -LiteralPath $alpha158Root -PathType Container) {
    foreach ($candidatePath in (Get-ChildItem -LiteralPath $alpha158Root -Recurse -Filter 'run.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $candidate = Get-Content -Raw -Encoding UTF8 -LiteralPath $candidatePath.FullName | ConvertFrom-Json
            if ([string]$candidate.status -ne 'completed_alpha158_linear_diagnostics' -or
                [string]$candidate.research_family -ne 'alpha158_linear_ensemble' -or
                [bool]$candidate.execution_admitted) {
                continue
            }
            $alpha158ComparisonPath = (Resolve-Path $candidatePath.FullName).Path
            break
        } catch {
            # Ignore incomplete or malformed research receipts.
        }
    }
}
if ($alpha158ComparisonPath) {
    $artifactPointers['alpha158_lightgbm_comparison'] = $alpha158ComparisonPath
}

$strategyRows = @()
$selectionByStrategy = @{}
$selectionRecord = $null
$selectionRecordPath = [string]$status.offline_paper_strategy_selection_record
if ($selectionRecordPath -and (Test-Path -LiteralPath $selectionRecordPath -PathType Leaf)) {
    try {
        $selectionRecord = Get-Content -Raw -Encoding UTF8 -LiteralPath $selectionRecordPath | ConvertFrom-Json
        foreach ($selectionRow in @($selectionRecord.candidates)) {
            $selectionByStrategy[[string]$selectionRow.strategy_id] = $selectionRow
        }
    } catch {
        $selectionByStrategy = @{}
    }
}

$strategySelection = [ordered]@{
    mode = [string]$snapshot.strategy_selection.mode
    selected_strategy_id = if ($selectedStrategyId) { $selectedStrategyId } else { [string]$snapshot.strategy_selection.selected_strategy_id }
    automatic_selection = [bool]$snapshot.strategy_selection.automatic_selection
    automatic_strategy_selection = $false
    selection_status = if ($selectionRecord) { [string]$selectionRecord.selection_status } else { 'not_indexed' }
    selection_required = if ($selectionRecord) { [bool]$selectionRecord.selection_required } else { $true }
    selected_plan = if ($selectionRecord) { [string]$selectionRecord.selected_plan } else { $null }
    candidate_count = if ($selectionRecord) { [int]$selectionRecord.candidate_count } else { [int]$strategyRows.Count }
    account_observed = if ($selectionRecord) { [bool]$selectionRecord.account_observed } else { $false }
    quotes_observed = if ($selectionRecord) { [bool]$selectionRecord.quotes_observed } else { $false }
    prefill_allowed = if ($selectionRecord) { [bool]$selectionRecord.prefill_allowed } else { $false }
    ledger_mutated = if ($selectionRecord) { [bool]$selectionRecord.ledger_mutated } else { $false }
    submission_attempted = if ($selectionRecord) { [bool]$selectionRecord.submission_attempted } else { $false }
    execution_admitted = $false
    next_action = if ($selectionRecord) { [string]$selectionRecord.next_action } else { 'review paper candidates only' }
}
if ($selectionRecordPath -and (Test-Path -LiteralPath $selectionRecordPath -PathType Leaf)) {
    $artifactPointers['paper_strategy_selection_record'] = (Resolve-Path $selectionRecordPath).Path
}
$strategyBoardPath = [string]$snapshot.artifacts.strategy_board_json
if ($strategyBoardPath -and (Test-Path -LiteralPath $strategyBoardPath -PathType Leaf)) {
    $board = Get-Content -Raw -Encoding UTF8 -LiteralPath $strategyBoardPath | ConvertFrom-Json
    $strategyRows = @($board.rows | Where-Object { [string]$_.family -eq 'opening_candidate' } |
        ForEach-Object {
            $selectionRow = $selectionByStrategy[[string]$_.strategy_id]
            [ordered]@{
                strategy_id = [string]$_.strategy_id
                net_return = $_.net_return
                max_drawdown = $_.max_drawdown
                sharpe_zero_rf_252 = $_.sharpe_zero_rf_252
                orders = $_.orders
                paper_risk_status = [string]$_.paper_risk_status
                status = [string]$_.status
                automatic_promotion = [bool]$_.automatic_promotion
                robustness_status = if ($selectionRow) { [string]$selectionRow.robustness_status } else { 'not_indexed' }
                robustness_60d_positive_window_ratio = if ($selectionRow) { $selectionRow.robustness_60d_positive_window_ratio } else { $null }
                robustness_60d_median_return = if ($selectionRow) { $selectionRow.robustness_60d_median_return } else { $null }
                robustness_120d_positive_window_ratio = if ($selectionRow) { $selectionRow.robustness_120d_positive_window_ratio } else { $null }
                robustness_120d_median_return = if ($selectionRow) { $selectionRow.robustness_120d_median_return } else { $null }
                robustness_extra_50bps_return = if ($selectionRow) { $selectionRow.robustness_extra_50bps_return } else { $null }
                saved_ledger_window_stability_status = [string]$_.saved_ledger_window_stability_status
                saved_ledger_window_count = $_.saved_ledger_window_count
                saved_ledger_positive_window_count = $_.saved_ledger_positive_window_count
                saved_ledger_worst_return = $_.saved_ledger_worst_return
                saved_ledger_worst_drawdown = $_.saved_ledger_worst_drawdown
                stale_valuation_days = $_.stale_valuation_days
                data_usability_status = [string]$_.data_usability_status
                data_usability_reason = [string]$_.data_usability_reason
            }
        })
}

$offlineDiagnostics = [ordered]@{}
if ($RobustnessDir) {
    $robustnessJson = Join-Path $RobustnessDir 'robustness.json'
    $robustnessReport = Join-Path $RobustnessDir 'REPORT.md'
    if (-not (Test-Path -LiteralPath $robustnessJson -PathType Leaf) -or
        -not (Test-Path -LiteralPath $robustnessReport -PathType Leaf)) {
        throw "Robustness diagnostic must contain robustness.json and REPORT.md: $RobustnessDir"
    }
    $robustness = Get-Content -Raw -Encoding UTF8 -LiteralPath $robustnessJson | ConvertFrom-Json
    if ([string]$robustness.status -ne 'saved_mark_robustness_diagnostic' -or
        [bool]$robustness.fresh_holdout_claimed -or
        [bool]$robustness.strategy_validated -or
        [bool]$robustness.execution_admitted) {
        throw 'Robustness diagnostic is not an unadmitted saved-mark artifact'
    }
    $offlineDiagnostics['research_robustness_json'] = (Resolve-Path $robustnessJson).Path
    $offlineDiagnostics['research_robustness_report'] = (Resolve-Path $robustnessReport).Path
    $offlineDiagnostics['research_robustness_status'] = [string]$robustness.status
    $artifactPointers['research_robustness_json'] = (Resolve-Path $robustnessJson).Path
    $artifactPointers['research_robustness_report'] = (Resolve-Path $robustnessReport).Path
}

$packagePath = Join-Path $runRoot 'offline_operator_package_v1.json'
$thsReadonlyEvidence = $null
$thsReadonlyEvidencePath = [string]$snapshot.artifacts.ths_readonly_evidence_index
$thsStructuredObservationPackage = $null
$thsStructuredObservationPackagePath = $null
if ($thsReadonlyEvidencePath -and (Test-Path -LiteralPath $thsReadonlyEvidencePath -PathType Leaf)) {
    try {
        $evidence = Get-Content -Raw -Encoding UTF8 -LiteralPath $thsReadonlyEvidencePath | ConvertFrom-Json
        $candidate = $evidence.holdings.candidate
        $summary = $evidence.summary
        $thsReadonlyEvidence = [ordered]@{
            status = [string]$evidence.status
            source_file = (Resolve-Path $thsReadonlyEvidencePath).Path
            account_alias = [string]$evidence.account_alias
            observed_at = [string]$summary.observed_at
            metrics_raw = $summary.metrics_raw
            holding_candidate = [ordered]@{
                row_count = $candidate.row_count
                symbols = @($candidate.symbols)
                summary_cross_checks_pass = [bool]$candidate.summary_cross_checks_pass
                structured_rows = [bool]$candidate.structured_rows
                semantic_verified = [bool]$candidate.semantic_verified
            }
            orders_observed = [bool]$evidence.orders_observed
            trades_observed = [bool]$evidence.trades_observed
            cashflows_observed = [bool]$evidence.cashflows_observed
            unknowns = @($evidence.unknowns)
            execution_admitted = $false
            ledger_mutated = $false
        }
    } catch {
        $thsReadonlyEvidence = [ordered]@{
            status = 'invalid_receipt'
            source_file = $thsReadonlyEvidencePath
            execution_admitted = $false
            ledger_mutated = $false
        }
    }
}
$structuredPackageRoot = Join-Path $tradingHome 'ths\observation_bundle\structured'
$thsCompleteStructuredObservationPackage = $null
if (Test-Path -LiteralPath $structuredPackageRoot -PathType Container) {
    foreach ($candidatePackage in (Get-ChildItem -LiteralPath $structuredPackageRoot -Recurse -Filter 'structured_observation_package_v1.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $candidatePayload = Get-Content -Raw -Encoding UTF8 -LiteralPath $candidatePackage.FullName | ConvertFrom-Json
            $isReadonlyEvidencePackage = (
                [string]$candidatePayload.schema -eq 'ths_structured_observation_package_v1' -and
                [string]$candidatePayload.source_index -eq $thsReadonlyEvidencePath
            )
            $bundleSourcePaths = $candidatePayload.source_paths
            $declaredSourceScope = [string]$candidatePayload.source_scope
            $bundleScopeAllowsCurrentImport = (-not $declaredSourceScope -or $declaredSourceScope -eq 'persistent_user_export')
            $bundleFilesPresent = $false
            if ($bundleSourcePaths -and [string]$candidatePayload.source -eq 'validated_ths_user_export_bundle') {
                $bundleFilesPresent = @('account', 'orders', 'trades', 'cashflows') |
                    ForEach-Object { $path = [string]$bundleSourcePaths.$_; $path -and (Test-Path -LiteralPath $path -PathType Leaf) } |
                    Where-Object { -not $_ } | Measure-Object | Select-Object -ExpandProperty Count
                $bundleFilesPresent = ($bundleFilesPresent -eq 0)
            }
            $isValidatedBundlePackage = (
                [string]$candidatePayload.schema -eq 'ths_structured_observation_package_v1' -and
                [string]$candidatePayload.source -eq 'validated_ths_user_export_bundle' -and
                [string]$candidatePayload.status -eq 'complete_structured_observation_package' -and
                $bundleScopeAllowsCurrentImport -and
                [string]$candidatePayload.order_reconciliation.status -eq 'ready_for_ledger_reconciliation' -and
                -not [bool]$candidatePayload.execution_admitted -and
                -not [bool]$candidatePayload.ledger_mutated -and
                $bundleFilesPresent
            )

            # Keep a separate pointer for a complete validated export bundle.
            # The newest native receipt may legitimately remain partial; it
            # must not hide the reusable complete import contract, and the two
            # evidence levels must stay distinguishable in the operator view.
            if ($isValidatedBundlePackage -and -not $thsCompleteStructuredObservationPackage) {
                $thsCompleteStructuredObservationPackage = [ordered]@{
                    status = [string]$candidatePayload.status
                    source_file = (Resolve-Path $candidatePackage.FullName).Path
                    source = [string]$candidatePayload.source
                    source_scope = if ($declaredSourceScope) { $declaredSourceScope } else { 'persistent_user_export' }
                    current_ledger_import_candidate = $true
                    source_paths = $candidatePayload.source_paths
                    account_alias = [string]$candidatePayload.account_alias
                    account_alias_quality = [string]$candidatePayload.account_alias_quality
                    account_observed = [bool]$candidatePayload.account_observed
                    orders_observed = [bool]$candidatePayload.orders_observed
                    trades_observed = [bool]$candidatePayload.trades_observed
                    cashflows_observed = [bool]$candidatePayload.cashflows_observed
                    order_count = if ($null -ne $candidatePayload.order_count) { $candidatePayload.order_count } else { $candidatePayload.order_reconciliation.order_count }
                    trade_count = if ($null -ne $candidatePayload.trade_count) { $candidatePayload.trade_count } else { $candidatePayload.order_reconciliation.trade_count }
                    cashflow_count = if ($null -ne $candidatePayload.cashflow_count) { $candidatePayload.cashflow_count } else { $candidatePayload.order_reconciliation.cashflow_count }
                    order_reconciliation_status = [string]$candidatePayload.order_reconciliation.status
                    ready_for_ledger_reconciliation = [bool]$candidatePayload.ready_for_ledger_reconciliation
                    ledger_mutated = [bool]$candidatePayload.ledger_mutated
                    execution_admitted = [bool]$candidatePayload.execution_admitted
                }
                $artifactPointers['ths_structured_observation_package_complete'] = $thsCompleteStructuredObservationPackage.source_file
            }
            if ($isReadonlyEvidencePackage -or $isValidatedBundlePackage) {
                $thsStructuredObservationPackagePath = (Resolve-Path $candidatePackage.FullName).Path
                $accountSummaryObservedAtValue = $candidatePayload.account_summary.observed_at
                $accountSummaryObservedAt = if ($accountSummaryObservedAtValue -is [DateTime]) {
                    $accountSummaryObservedAtValue.ToString('o')
                } elseif ($accountSummaryObservedAtValue -is [DateTimeOffset]) {
                    $accountSummaryObservedAtValue.ToString('o')
                } else {
                    [string]$accountSummaryObservedAtValue
                }
                $accountSummaryFreshForExecutionDay = $false
                if ($accountSummaryObservedAt) {
                    try {
                        $summaryLocalDate = ([DateTimeOffset]::Parse($accountSummaryObservedAt)).ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd')
                        $accountSummaryFreshForExecutionDay = $summaryLocalDate -eq $executionDay
                    } catch {
                        $accountSummaryFreshForExecutionDay = $false
                    }
                }
                $thsStructuredObservationPackage = [ordered]@{
                    status = [string]$candidatePayload.status
                    source_file = $thsStructuredObservationPackagePath
                    source = [string]$candidatePayload.source
                    source_index = [string]$candidatePayload.source_index
                    source_paths = $candidatePayload.source_paths
                    account_alias = [string]$candidatePayload.account_alias
                    account_alias_quality = [string]$candidatePayload.account_alias_quality
                    account_observed = $candidatePayload.account_observed
                    account_summary_observed = $candidatePayload.account_summary_observed
                    account_summary_observed_at = $accountSummaryObservedAt
                    account_summary_fresh_for_execution_day = $accountSummaryFreshForExecutionDay
                    orders_observed = $candidatePayload.orders_observed
                    trades_observed = $candidatePayload.trades_observed
                    cashflows_observed = $candidatePayload.cashflows_observed
                    holdings_candidate_observed = $candidatePayload.holdings_candidate_observed
                    holdings_candidate_semantic_verified = $candidatePayload.holdings_candidate_semantic_verified
                    account_summary = $candidatePayload.account_summary
                    holdings_candidate = $candidatePayload.holdings_candidate
                    order_reconciliation_status = [string]$candidatePayload.order_reconciliation.status
                    order_count = if ($null -ne $candidatePayload.order_count) { $candidatePayload.order_count } else { $candidatePayload.order_reconciliation.order_count }
                    trade_count = if ($null -ne $candidatePayload.trade_count) { $candidatePayload.trade_count } else { $candidatePayload.order_reconciliation.trade_count }
                    cashflow_count = if ($null -ne $candidatePayload.cashflow_count) { $candidatePayload.cashflow_count } else { $candidatePayload.order_reconciliation.cashflow_count }
                    ready_for_ledger_reconciliation = [bool]$candidatePayload.ready_for_ledger_reconciliation
                    zero_orders_inferred = [bool]$candidatePayload.order_reconciliation.zero_orders_inferred
                    zero_trades_inferred = [bool]$candidatePayload.order_reconciliation.zero_trades_inferred
                    ledger_mutated = [bool]$candidatePayload.ledger_mutated
                    execution_admitted = [bool]$candidatePayload.execution_admitted
                }
                $artifactPointers['ths_structured_observation_package'] = $thsStructuredObservationPackagePath
                break
            }
        } catch {
            # Ignore incomplete structured packages while indexing the operator package.
        }
    }
}
$validatedRehearsalPackageRoot = Join-Path $tradingHome 'research\ths_bundle_rehearsal'
if (-not $thsCompleteStructuredObservationPackage -and
    (Test-Path -LiteralPath $validatedRehearsalPackageRoot -PathType Container)) {
    foreach ($candidatePackage in (Get-ChildItem -LiteralPath $validatedRehearsalPackageRoot -Recurse -Filter 'structured_observation_package_v1.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $candidatePayload = Get-Content -Raw -Encoding UTF8 -LiteralPath $candidatePackage.FullName | ConvertFrom-Json
            $declaredSourceScope = [string]$candidatePayload.source_scope
            if ([string]$candidatePayload.schema -ne 'ths_structured_observation_package_v1' -or
                [string]$candidatePayload.source -notin @('validated_ths_user_export_bundle', 'isolated_ths_export_rehearsal') -or
                ($declaredSourceScope -and $declaredSourceScope -ne 'isolated_rehearsal') -or
                [string]$candidatePayload.status -ne 'complete_structured_observation_package' -or
                [string]$candidatePayload.order_reconciliation.status -ne 'ready_for_ledger_reconciliation' -or
                [bool]$candidatePayload.execution_admitted -or
                [bool]$candidatePayload.ledger_mutated) {
                continue
            }
            $bundleFilesPresent = @('account', 'orders', 'trades', 'cashflows' | ForEach-Object {
                $sourcePath = [string]$candidatePayload.source_paths.$_
                $sourcePath -and (Test-Path -LiteralPath $sourcePath -PathType Leaf)
            })
            if (@($bundleFilesPresent | Where-Object { -not $_ }).Count -ne 0) {
                continue
            }
            $thsCompleteStructuredObservationPackage = [ordered]@{
                status = [string]$candidatePayload.status
                source_file = (Resolve-Path $candidatePackage.FullName).Path
                source = [string]$candidatePayload.source
                source_scope = if ($declaredSourceScope) { $declaredSourceScope } else { 'isolated_rehearsal' }
                current_ledger_import_candidate = $false
                source_paths = $candidatePayload.source_paths
                account_alias = [string]$candidatePayload.account_alias
                account_alias_quality = [string]$candidatePayload.account_alias_quality
                account_observed = [bool]$candidatePayload.account_observed
                orders_observed = [bool]$candidatePayload.orders_observed
                trades_observed = [bool]$candidatePayload.trades_observed
                cashflows_observed = [bool]$candidatePayload.cashflows_observed
                order_count = if ($null -ne $candidatePayload.order_count) { $candidatePayload.order_count } else { $candidatePayload.order_reconciliation.order_count }
                trade_count = if ($null -ne $candidatePayload.trade_count) { $candidatePayload.trade_count } else { $candidatePayload.order_reconciliation.trade_count }
                cashflow_count = if ($null -ne $candidatePayload.cashflow_count) { $candidatePayload.cashflow_count } else { $candidatePayload.order_reconciliation.cashflow_count }
                order_reconciliation_status = [string]$candidatePayload.order_reconciliation.status
                ready_for_ledger_reconciliation = [bool]$candidatePayload.ready_for_ledger_reconciliation
                ledger_mutated = [bool]$candidatePayload.ledger_mutated
                execution_admitted = [bool]$candidatePayload.execution_admitted
            }
            $artifactPointers['ths_structured_observation_package_complete'] = $thsCompleteStructuredObservationPackage.source_file
            break
        } catch {
            # Ignore incomplete rehearsal packages while indexing the operator package.
        }
    }
}
$thsReconciliationRehearsal = $null
$rehearsalRoot = 'D:\QuantificationData\trading\research\ths_bundle_rehearsal'
if (Test-Path -LiteralPath $rehearsalRoot -PathType Container) {
    foreach ($rehearsalPath in (Get-ChildItem -LiteralPath $rehearsalRoot -Recurse -Filter 'full_order_reconciliation_rehearsal.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $rehearsal = Get-Content -Raw -Encoding UTF8 -LiteralPath $rehearsalPath.FullName | ConvertFrom-Json
            if ([string]$rehearsal.schema -ne 'ths_full_order_reconciliation_rehearsal_v1' -or
                [string]$rehearsal.status -ne 'completed_same_ledger_order_reconciliation' -or
                [bool]$rehearsal.execution_admitted -or
                -not [bool]$rehearsal.idempotent_second_import) {
                continue
            }
            $thsReconciliationRehearsal = [ordered]@{
                status = [string]$rehearsal.status
                source_file = (Resolve-Path $rehearsalPath.FullName).Path
                native_account = [bool]$rehearsal.native_account
                first_import_changed = [bool]$rehearsal.first_ledger_import.changed
                second_import_changed = [bool]$rehearsal.second_ledger_import.changed
                idempotent_second_import = [bool]$rehearsal.idempotent_second_import
                ledger_receipt_count = [int]$rehearsal.ledger_receipt_count
                execution_admitted = $false
                next_action = [string]$rehearsal.next_action
            }
            $artifactPointers['ths_order_reconciliation_rehearsal'] = $thsReconciliationRehearsal.source_file
            break
        } catch {
            # Ignore incomplete rehearsal artifacts while indexing the package.
        }
    }
}
$thsLedgerImportPreview = $null
if (Test-Path -LiteralPath $rehearsalRoot -PathType Container) {
    foreach ($previewPath in (Get-ChildItem -LiteralPath $rehearsalRoot -Recurse -Filter 'isolated_paused_paper_preview_v1.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $preview = Get-Content -Raw -Encoding UTF8 -LiteralPath $previewPath.FullName | ConvertFrom-Json
            if ([string]$preview.schema -ne 'ths_isolated_paused_paper_preview_v1' -or
                [string]$preview.status -ne 'ready_for_import' -or
                -not [bool]$preview.would_change -or
                [bool]$preview.ledger_mutated -or
                [bool]$preview.journal_mutated -or
                [bool]$preview.execution_admitted -or
                -not [bool]$preview.synthetic_only -or
                [bool]$preview.native_account) {
                continue
            }
            $thsLedgerImportPreview = [ordered]@{
                status = [string]$preview.status
                source_file = (Resolve-Path $previewPath.FullName).Path
                synthetic_only = $true
                native_account = $false
                would_change = [bool]$preview.would_change
                ledger_mutated = $false
                journal_mutated = $false
                execution_admitted = $false
                run_id = [string]$preview.run_id
                plan_id = [string]$preview.plan_id
                before = $preview.before
                after = $preview.after
                changes = $preview.changes
            }
            $artifactPointers['ths_ledger_import_preview_rehearsal'] = $thsLedgerImportPreview.source_file
            break
        } catch {
            # Ignore incomplete preview artifacts while indexing the operator package.
        }
    }
}
$package = [ordered]@{
    schema = 'offline_operator_package_v1'
    status = 'offline_paper_package_ready'
    execution_day = $executionDay
    opening_pack = (Resolve-Path $OpeningPack).Path
    source_cutoff = [string]$snapshot.source_cutoff
    session_status = $statusPath
    trader_desk_snapshot = $snapshotPath
    strategy_id = [string]$snapshot.strategy_id
    strategy_selection = $strategySelection
    strategy_promotion_review = $snapshot.strategy_promotion_review
    stockdb_factor_replay = $stockdbFactorReplay
    stockdb_frozen_snapshot = $stockdbFrozenSnapshot
    stockdb_independent_sample = $stockdbIndependentSample
    stockdb_independent_window_stability = $stockdbIndependentWindowStability
    ths_readonly_evidence = $thsReadonlyEvidence
    ths_structured_observation_package = $thsStructuredObservationPackage
    ths_structured_observation_package_complete = $thsCompleteStructuredObservationPackage
    ths_order_reconciliation_rehearsal = $thsReconciliationRehearsal
    ths_ledger_import_preview_rehearsal = $thsLedgerImportPreview
    ths_observation_templates = $thsObservationTemplates
    ths_structured_session = $thsStructuredSession
    paper_child_order_reconciliation = $paperChildOrderReconciliation
    research_decision_matrix = if ($artifactPointers.Contains('research_decision_matrix')) {
        [string]$artifactPointers['research_decision_matrix']
    } else { $null }
    ths_prefill_readback_validation = if ($artifactPointers.Contains('ths_prefill_readback_validation')) {
        [string]$artifactPointers['ths_prefill_readback_validation']
    } else { $null }
    alpha158_lightgbm_comparison = $alpha158ComparisonPath
    instrument_master = $snapshot.instrument_master
    instrument_master_coverage = $snapshot.instrument_master_coverage
    order_counts = [ordered]@{
        total = [int]$snapshot.orders.total
        buy = [int]$snapshot.orders.buy
        sell = [int]$snapshot.orders.sell
        accepted = [int]$snapshot.orders.accepted
        rejected = [int]$snapshot.orders.rejected
        reduce_first = [int]$snapshot.orders.reduce_first_count
        deferred_buys = [int]$snapshot.orders.deferred_buy_count
    }
    strategy_candidates = $strategyRows
    offline_diagnostics = $offlineDiagnostics
    function_matrix = $functionRows
    operator_actions = $operatorActionRows
    artifact_pointers = $artifactPointers
    live_state = [ordered]@{
        network_probe_attempted = $false
        account_observed = $false
        quotes_observed = $false
        client_interacted = $false
        ledger_mutated = $false
        submission_attempted = $false
        execution_admitted = $false
    }
    next_action = 'review the paper operator board and selected-plan references; provide authorized observations separately before any manual release'
}
$package | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $packagePath -Encoding UTF8
$package | ConvertTo-Json -Depth 12
