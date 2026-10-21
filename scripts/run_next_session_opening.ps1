param(
    [string]$OpeningPack,
    [string]$AccountObservation,
    [string]$THSProfile,
    [string]$OutputRoot = 'D:\QuantificationData\trading\research\opening_session_runs',
    [string]$Strategy = 'momentum_12_1_20',
    [string]$Source = 'sina_hq_raw',
    [string]$EarliestProbeTime = '09:25',
    [switch]$RepeatProbe,
    [switch]$OfflineOnly,
    [switch]$NoOperatorPackage,
    [ValidateSet('mainboard_only', 'strict')]
    [string]$ExecutionScope = 'mainboard_only'
)

$ErrorActionPreference = 'Stop'
$python = 'D:\Quantification\env\python.exe'
$committee = 'D:\Quantification\run_committee.py'
$probe = 'D:\Quantification\scripts\fetch_sina_opening_quotes.ps1'
$manualReview = 'D:\Quantification\scripts\run_manual_opening_review.ps1'

if (-not $OpeningPack) {
    $packRoot = 'D:\Quantification\outputs\opening_pack'
    if (-not (Test-Path -LiteralPath $packRoot -PathType Container)) {
        throw "Opening pack root not found: $packRoot"
    }

    $todayForSelection = (Get-Date).Date
    $candidates = @(
        Get-ChildItem -LiteralPath $packRoot -Recurse -Filter 'opening_pack.json' -File |
            ForEach-Object {
                try {
                    $candidate = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
                    $executionDay = [datetime]::ParseExact(
                        [string]$candidate.execution_day,
                        'yyyy-MM-dd',
                        [Globalization.CultureInfo]::InvariantCulture
                    ).Date
                    if ($executionDay -ge $todayForSelection) {
                        [pscustomobject]@{
                            Path = $_.FullName
                            ExecutionDay = $executionDay
                            GeneratedAt = [datetime]$candidate.generated_at
                        }
                    }
                } catch {
                    # Ignore incomplete or non-opening-pack JSON while discovering a pack.
                }
            }
    )
    $selected = $candidates |
        Sort-Object `
            @{ Expression = 'ExecutionDay'; Descending = $false }, `
            @{ Expression = 'GeneratedAt'; Descending = $true } |
        Select-Object -First 1
    if (-not $selected) {
        throw "No opening pack for today or a future trading day was found under $packRoot"
    }
    $OpeningPack = [string]$selected.Path
}

if (-not (Test-Path -LiteralPath $OpeningPack -PathType Leaf)) {
    throw "Missing opening pack: $OpeningPack"
}
if ($AccountObservation -and -not (Test-Path -LiteralPath $AccountObservation -PathType Leaf)) {
    throw "Missing account observation: $AccountObservation"
}
if ($THSProfile -and -not (Test-Path -LiteralPath $THSProfile -PathType Leaf)) {
    throw "Missing THS profile: $THSProfile"
}
if ($OfflineOnly -and ($AccountObservation -or $THSProfile)) {
    throw 'OfflineOnly cannot be combined with account observation or THS profile inputs'
}

$pack = Get-Content -Raw -LiteralPath $OpeningPack | ConvertFrom-Json
if (-not $PSBoundParameters.ContainsKey('Strategy') -and
    [string]$pack.selected_strategy_id) {
    # Keep the default entrypoint aligned with an explicitly narrowed opening
    # pack; a caller-provided -Strategy still has precedence.
    $Strategy = [string]$pack.selected_strategy_id
}
$tradingDate = [string]$pack.execution_day
$quoteRequest = [string]$pack.artifacts.quote_request
if (-not $tradingDate -or -not (Test-Path -LiteralPath $quoteRequest -PathType Leaf)) {
    throw 'Opening pack must contain execution_day and artifacts.quote_request'
}

$runRoot = Join-Path $OutputRoot $tradingDate
$probeRoot = Join-Path $runRoot 'quote_probe'
New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
$tradingHome = if ($env:QUANT_TRADING_HOME) { $env:QUANT_TRADING_HOME } else { 'D:\QuantificationData\trading' }
$quoteObservationRoot = Join-Path $tradingHome 'ths\quote_observation'

# Keep the next-session handoff usable without inventing account state.  The
# template is intentionally incomplete and is never consumed as an observation.
$accountTemplatePath = Join-Path $runRoot 'account_observation.template.json'
$ordersTemplatePath = Join-Path $runRoot 'orders_observation.template.csv'
$tradesTemplatePath = Join-Path $runRoot 'trades_observation.template.csv'
$cashflowsTemplatePath = Join-Path $runRoot 'cashflows_observation.template.csv'
if (-not (Test-Path -LiteralPath $accountTemplatePath -PathType Leaf)) {
    $accountTemplate = [ordered]@{
        schema = 'ths_account_observation_v1'
        source = 'ths_user_export'
        account_alias = '<fill-from-user-selected-account>'
        observed_at = '<fill-with-actual-observation-time+08:00>'
        trading_date = $tradingDate
        cash_basis = 'cash_balance_includes_frozen'
        cash_total = $null
        cash_frozen = $null
        cash_available = $null
        positions = @()
        instructions = @(
            'Fill only from the current read-only THS account view/export; do not use paper or historical values.',
            'Use integer cents for cash fields and explicit exchange-qualified symbols in positions.',
            'Each position item must contain symbol, quantity, and sellable.',
            'The runner will still keep the paper preview manual-only and will not submit, cancel, or transfer.'
        )
    }
    $accountTemplate | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $accountTemplatePath -Encoding UTF8
}
if (-not (Test-Path -LiteralPath $ordersTemplatePath -PathType Leaf)) {
    Set-Content -LiteralPath $ordersTemplatePath -Value 'symbol,market,side,order_id,qty,price,filled_qty,status' -Encoding UTF8
}
if (-not (Test-Path -LiteralPath $tradesTemplatePath -PathType Leaf)) {
    Set-Content -LiteralPath $tradesTemplatePath -Value 'symbol,market,side,order_id,execution_id,qty,price' -Encoding UTF8
}
if (-not (Test-Path -LiteralPath $cashflowsTemplatePath -PathType Leaf)) {
    Set-Content -LiteralPath $cashflowsTemplatePath -Value 'symbol,market,side,order_id,gross_amount,fees,cash_change' -Encoding UTF8
}
$movementsTemplatePath = Join-Path $runRoot 'movements_observation.template.csv'
$restrictionReleasesTemplatePath = Join-Path $runRoot 'restriction_releases_observation.template.csv'
$shareCreditsTemplatePath = Join-Path $runRoot 'share_credits_observation.template.csv'
if (-not (Test-Path -LiteralPath $movementsTemplatePath -PathType Leaf)) {
    Set-Content -LiteralPath $movementsTemplatePath -Value 'movement_id,kind,cash_change' -Encoding UTF8
}
if (-not (Test-Path -LiteralPath $restrictionReleasesTemplatePath -PathType Leaf)) {
    Set-Content -LiteralPath $restrictionReleasesTemplatePath -Value 'release_id,symbol,market,released_qty,release_day,reason' -Encoding UTF8
}
if (-not (Test-Path -LiteralPath $shareCreditsTemplatePath -PathType Leaf)) {
    Set-Content -LiteralPath $shareCreditsTemplatePath -Value 'credit_id,symbol,market,credited_qty,credit_day,available_day,kind' -Encoding UTF8
}

$statusPath = Join-Path $runRoot 'session_runner_status.json'
$now = Get-Date
$today = $now.ToString('yyyy-MM-dd')
$existingStatus = $null
if (Test-Path -LiteralPath $statusPath -PathType Leaf) {
    $existingStatus = Get-Content -Raw -LiteralPath $statusPath | ConvertFrom-Json
}

function Set-StatusProperty {
    param(
        [object]$Status,
        [string]$Name,
        [object]$Value
    )

    if ($Status -is [System.Collections.IDictionary]) {
        $Status[$Name] = $Value
    } elseif ($Status.PSObject.Properties.Name -contains $Name) {
        $Status.$Name = $Value
    } else {
        $Status | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Find-PaperPreview {
    param(
        [string]$Root,
        [string]$StrategyId,
        [string]$ExecutionDay,
        [string]$ExecutionScope
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    foreach ($resultFile in (Get-ChildItem -LiteralPath $Root -Recurse -Filter 'result.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $result = Get-Content -Raw -LiteralPath $resultFile.FullName | ConvertFrom-Json
            if ([string]$result.status -eq 'paper_preview_only' -and
                [string]$result.strategy_id -eq $StrategyId -and
                [string]$result.execution_day -eq $ExecutionDay -and
                [string]$result.execution_scope -eq $ExecutionScope) {
                return $resultFile.FullName
            }
        } catch {
            # Ignore incomplete paper-preview artifacts while selecting a pointer.
        }
    }
    return $null
}

function Find-CalendarBoundStockdbArtifact {
    param(
        [string]$Root,
        [string]$FileName,
        [string]$Schema
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    foreach ($resultFile in (Get-ChildItem -LiteralPath $Root -Recurse -Filter $FileName -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $result = Get-Content -Raw -Encoding UTF8 -LiteralPath $resultFile.FullName | ConvertFrom-Json
            $coverage = $result.coverage_manifest
            if ([string]$result.schema -eq $Schema -and
                $coverage -and
                [bool]$coverage.forward_complete -and
                [int]$coverage.forward_missing_rows -eq 0 -and
                [int]$coverage.calendar_session_count -gt 0) {
                return $resultFile.FullName
            }
        } catch {
            # Ignore incomplete or pre-calendar-bound research artifacts.
        }
    }
    return $null
}

function Find-LatestIndependentWindowStability {
    param(
        [string]$Root
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    foreach ($resultFile in (Get-ChildItem -LiteralPath $Root -Recurse -Filter 'run.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $result = Get-Content -Raw -Encoding UTF8 -LiteralPath $resultFile.FullName | ConvertFrom-Json
            if ([string]$result.schema -eq 'stockdb_independent_window_stability_v1' -and
                [string]$result.status -eq 'completed_research_only' -and
                -not [bool]$result.execution_admitted -and
                -not [bool]$result.selection_enabled) {
                return $resultFile.FullName
            }
        } catch {
            # Ignore incomplete stability receipts while selecting a pointer.
        }
    }
    return $null
}

function Find-LatestThsReadonlyEvidence {
    param(
        [string]$Root
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return $null
    }
    # Prefer the evidence index explicitly referenced by the newest structured
    # observation package.  File write time alone can select an older capture
    # after a later package is rebuilt or copied.
    $structuredRoot = Join-Path (Split-Path -Parent $Root) 'observation_bundle\structured'
    if (Test-Path -LiteralPath $structuredRoot -PathType Container) {
        foreach ($packageFile in (Get-ChildItem -LiteralPath $structuredRoot -Recurse -Filter 'structured_observation_package_v1.json' -File |
            Sort-Object LastWriteTime -Descending)) {
            try {
                $package = Get-Content -Raw -Encoding UTF8 -LiteralPath $packageFile.FullName | ConvertFrom-Json
                $sourceIndex = [string]$package.source_index
                if ([string]$package.schema -ne 'ths_structured_observation_package_v1' -or
                    -not $sourceIndex -or -not (Test-Path -LiteralPath $sourceIndex -PathType Leaf)) {
                    continue
                }
                $sourcePayload = Get-Content -Raw -Encoding UTF8 -LiteralPath $sourceIndex | ConvertFrom-Json
                if ([string]$sourcePayload.schema -eq 'ths_readonly_evidence_index_v1' -and
                    [string]$sourcePayload.status -in @('partial_readonly_evidence', 'complete_readonly_evidence')) {
                    return $sourceIndex
                }
            } catch {
                continue
            }
        }
    }
    $candidates = @()
    foreach ($evidenceFile in (Get-ChildItem -LiteralPath $Root -Recurse -Filter 'readonly_evidence_index_v1.json' -File)) {
        try {
            $evidence = Get-Content -Raw -Encoding UTF8 -LiteralPath $evidenceFile.FullName | ConvertFrom-Json
            if ([string]$evidence.schema -eq 'ths_readonly_evidence_index_v1' -and
                [string]$evidence.status -in @('partial_readonly_evidence', 'complete_readonly_evidence')) {
                $observedEnd = [datetime]::MinValue
                if ($evidence.observation_window -and $evidence.observation_window.end) {
                    try {
                        $observedEnd = [datetime]::Parse([string]$evidence.observation_window.end)
                    } catch {
                        $observedEnd = [datetime]::MinValue
                    }
                }
                $candidates += [pscustomobject]@{
                    Path = $evidenceFile.FullName
                    ObservedEnd = $observedEnd
                    LastWriteTime = $evidenceFile.LastWriteTime
                }
            }
        } catch {
            # Ignore incomplete receipts while selecting a read-only pointer.
        }
    }
    $selected = $candidates |
        Sort-Object @{Expression = 'ObservedEnd'; Descending = $true}, @{Expression = 'LastWriteTime'; Descending = $true} |
        Select-Object -First 1
    if ($selected) {
        return [string]$selected.Path
    }
    return $null
}

function Find-LatestStructuredCaptureManifest {
    param(
        [string]$Root,
        [string]$TradingDate
    )

    $dayRoot = Join-Path $Root $TradingDate
    if (-not (Test-Path -LiteralPath $dayRoot -PathType Container)) {
        return $null
    }
    foreach ($manifestFile in (Get-ChildItem -LiteralPath $dayRoot -Recurse -Filter 'manifest.json' -File |
        Sort-Object LastWriteTime -Descending)) {
        try {
            $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestFile.FullName | ConvertFrom-Json
            $receipt = [string]$manifest.structured_session_receipt
            $bundle = [string]$manifest.structured_session_bundle
            if ([string]$manifest.schema -ne 'ths_opening_readonly_capture_v1' -or
                [string]$manifest.trading_date -ne $TradingDate -or
                -not [bool]$manifest.read_only -or
                [bool]$manifest.submitted -or
                [bool]$manifest.execution_admitted -or
                [bool]$manifest.ledger_mutated -or
                -not $receipt -or -not (Test-Path -LiteralPath $receipt -PathType Leaf) -or
                -not $bundle -or -not (Test-Path -LiteralPath $bundle -PathType Container)) {
                continue
            }
            return [ordered]@{
                manifest = $manifestFile.FullName
                receipt = $receipt
                bundle = $bundle
                cashflows = [string]$manifest.structured_cashflows_csv
            }
        } catch {
            # Ignore incomplete captures; the runner must remain paper-only.
        }
    }
    return $null
}

function Add-PaperPreviewReference {
    param(
        [object]$Status,
        [string]$PreviewPath
    )

    if (-not $PreviewPath -or -not (Test-Path -LiteralPath $PreviewPath -PathType Leaf)) {
        return
    }
    $preview = Get-Content -Raw -LiteralPath $PreviewPath | ConvertFrom-Json
    $artifactPaths = $preview.artifacts
    $rankUsableForSchedule = ([bool]$preview.rank_is_current_for_execution_day -or
        [bool]$preview.rank_carry_forward_allowed)
    $offlineReady = ([int]$preview.provisional_order_count -gt 0 -and
        $rankUsableForSchedule -and
        [int]$preview.provisional_missing_quote_symbols.Count -eq 0)
    $values = [ordered]@{
        paper_preview = $PreviewPath
        paper_preview_source_cutoff = [string]$preview.source_cutoff
        paper_preview_order_count = [int]$preview.provisional_order_count
        paper_preview_release_state = [string]$preview.release_state
        paper_preview_execution_scope = [string]$preview.execution_scope
        paper_preview_execution_admitted = [bool]$preview.execution_admitted
        paper_preview_rank_day = [string]$preview.provisional_rank_day
        paper_preview_rank_current = [bool]$preview.rank_is_current_for_execution_day
        paper_preview_rank_carry_forward_allowed = [bool]$preview.rank_carry_forward_allowed
        paper_preview_rank_usable_for_schedule = $rankUsableForSchedule
        paper_preview_offline_review_ready = $offlineReady
        paper_preview_order_plan = if ($artifactPaths) { [string]$artifactPaths.order_plan } else { $null }
        paper_preview_provisional_order_plan = if ($artifactPaths) { [string]$artifactPaths.provisional_order_plan } else { $null }
        paper_preview_target_holdings = if ($artifactPaths) { [string]$artifactPaths.provisional_target_holdings } else { $null }
        paper_preview_ths_handoff = if ($artifactPaths) { [string]$artifactPaths.ths_manual_handoff } else { $null }
    }
    foreach ($entry in $values.GetEnumerator()) {
        if ($Status -is [System.Collections.IDictionary]) {
            $Status[$entry.Key] = $entry.Value
        } elseif ($Status.PSObject.Properties.Name -contains $entry.Key) {
            $Status.($entry.Key) = $entry.Value
        } else {
            $Status | Add-Member -NotePropertyName $entry.Key -NotePropertyValue $entry.Value
        }
    }
}

function Add-OfflineSelectedPlanReference {
    param(
        [object]$Status,
        [string]$ExecutionDay
    )

    $selectionRoot = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' $ExecutionDay
    $references = @()
    $seenStrategies = @{}
    if (Test-Path -LiteralPath $selectionRoot -PathType Container) {
        foreach ($planFile in (Get-ChildItem -LiteralPath $selectionRoot -Recurse -Filter 'offline_strategy_plan.json' -File |
            Sort-Object LastWriteTime -Descending)) {
            try {
                $plan = Get-Content -Raw -LiteralPath $planFile.FullName | ConvertFrom-Json
                if ([string]$plan.schema -ne 'offline_selected_strategy_plan_v1' -or
                    [string]$plan.execution_day -ne $ExecutionDay -or
                    [bool]$plan.execution_admitted) {
                    continue
                }
                $strategyKey = [string]$plan.strategy_id
                if (-not $strategyKey -or $seenStrategies.ContainsKey($strategyKey)) {
                    continue
                }
                $seenStrategies[$strategyKey] = $true
                $artifacts = $plan.selected_artifacts
                $lifecyclePath = Join-Path $planFile.DirectoryName 'paper_lifecycle_rehearsal_v1.json'
                $lifecycle = $null
                if (-not (Test-Path -LiteralPath $lifecyclePath -PathType Leaf)) {
                    $lifecycleOutput = @(& $python $committee paper-lifecycle-rehearsal `
                        '--plan' $planFile.FullName `
                        '--out' $planFile.DirectoryName 2>&1)
                    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $lifecyclePath -PathType Leaf)) {
                        throw "Offline lifecycle rehearsal failed for $strategyKey`: $($lifecycleOutput -join ' ')"
                    }
                }
                if (Test-Path -LiteralPath $lifecyclePath -PathType Leaf) {
                    try {
                        $lifecycle = Get-Content -Raw -LiteralPath $lifecyclePath | ConvertFrom-Json
                    } catch {
                        $lifecycle = $null
                    }
                }
                $lifecycleRows = if ($lifecycle) { @($lifecycle.scenarios) } else { @() }
                $references += [ordered]@{
                    strategy_id = [string]$plan.strategy_id
                    plan = $planFile.FullName
                    ths_prefill_payloads = if ($artifacts) { [string]$artifacts.ths_prefill_payloads } else { $null }
                    ths_prefill_payloads_summary = if ($artifacts) { [string]$artifacts.ths_prefill_payloads_summary } else { $null }
                    ths_prefill_readback_template = if ($artifacts) { [string]$artifacts.ths_prefill_readback_template } else { $null }
                    order_count = [int]$plan.summary.candidate_order_count
                    ledger_preflight_accepted = [int]$plan.summary.ledger_preflight_accepted
                    ledger_preflight_rejected = [int]$plan.summary.ledger_preflight_rejected
                    comparison_return = [string]$plan.summary.comparison_return
                    comparison_max_drawdown = [string]$plan.summary.comparison_max_drawdown
                    comparison_sharpe = [string]$plan.summary.comparison_sharpe
                    paper_accepted_buy_gross_rmb = [string]$plan.paper_account_sizing.accepted_buy_gross_rmb
                    paper_cash_after_buys_only_rmb = [string]$plan.paper_account_sizing.cash_after_buys_only_rmb
                    paper_projected_cash_after_orders_rmb = [string]$plan.paper_account_sizing.projected_cash_after_paper_orders_rmb
                    prefill_payload_count = [int]$plan.ths_prefill_payloads.accepted_payload_count
                    prefill_rejected_or_missing_count = [int]$plan.ths_prefill_payloads.rejected_or_missing_count
                    route_available_count = [int]$plan.ths_prefill_payloads.route_available_count
                    route_unavailable_count = [int]$plan.ths_prefill_payloads.route_unavailable_count
                    paper_account_observed = [bool]$plan.paper_account_sizing.account_observed
                    quotes_observed = [bool]$plan.ths_prefill_payloads.quotes_observed
                    paper_lifecycle_rehearsal = if ($lifecycle) { $lifecyclePath } else { $null }
                    paper_lifecycle_status = if ($lifecycle) { [string]$lifecycle.status } else { 'not_run' }
                    paper_lifecycle_passed_count = @($lifecycleRows | Where-Object { [string]$_.status -eq 'passed' }).Count
                    paper_lifecycle_manual_action_count = @($lifecycleRows | Where-Object { [string]$_.status -eq 'manual_reconcile_required' }).Count
                    paper_lifecycle_blocked_count = @($lifecycleRows | Where-Object { [string]$_.status -eq 'blocked_by_unobserved_inputs' }).Count
                    execution_admitted = [bool]$plan.execution_admitted
                    status = 'offline_reference_only_no_selection_or_submission'
                }
            } catch {
                # Ignore incomplete plan folders while indexing the session.
            }
        }
    }
    $matrixRows = @($references | ForEach-Object { [pscustomobject]$_ })
    $matrixJsonPath = Join-Path $selectionRoot 'offline_selected_strategy_matrix.json'
    $matrixCsvPath = Join-Path $selectionRoot 'offline_selected_strategy_matrix.csv'
    $lifecycleIndexPath = Join-Path $selectionRoot 'paper_lifecycle_rehearsal_index.json'
    $selectionRecordPath = Join-Path $selectionRoot 'paper_strategy_selection_record_v1.json'
    if ($matrixRows.Count) {
        $matrix = [ordered]@{
            schema = 'offline_selected_strategy_matrix_v1'
            status = 'paper_reference_comparison'
            execution_day = $ExecutionDay
            candidate_count = [int]$matrixRows.Count
            automatic_strategy_selection = $false
            submission_attempted = $false
            execution_admitted = $false
            rows = @($matrixRows)
        }
        $matrix | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $matrixJsonPath -Encoding UTF8
        $matrixRows | Export-Csv -LiteralPath $matrixCsvPath -NoTypeInformation -Encoding UTF8
        $offlineRiskOutput = @(& $python $committee offline-portfolio-risk --matrix $matrixJsonPath 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Offline portfolio risk summary failed: $($offlineRiskOutput -join ' ')"
        }
        $releaseSequenceOutput = @(& $python $committee paper-release-sequence --matrix $matrixJsonPath 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Paper manual release sequence failed: $($releaseSequenceOutput -join ' ')"
        }
        $recoveryHandoffOutput = @(& $python $committee paper-recovery-handoff --matrix $matrixJsonPath 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Paper recovery handoff failed: $($recoveryHandoffOutput -join ' ')"
        }
        $preservedSelectionId = $null
        if (Test-Path -LiteralPath $selectionRecordPath -PathType Leaf) {
            try {
                $existingSelection = Get-Content -Raw -LiteralPath $selectionRecordPath | ConvertFrom-Json
                if ([string]$existingSelection.execution_day -eq $ExecutionDay -and
                    [string]$existingSelection.selection_status -eq 'paper_candidate_explicitly_selected' -and
                    (-not [string]$pack.selected_strategy_id -or
                     [string]$existingSelection.selected_strategy_id -eq [string]$pack.selected_strategy_id) -and
                    @($matrixRows | Where-Object { [string]$_.strategy_id -eq [string]$existingSelection.selected_strategy_id }).Count -eq 1) {
                    $preservedSelectionId = [string]$existingSelection.selected_strategy_id
                }
            } catch {
                $preservedSelectionId = $null
            }
        }
        $selectionArgs = @('--matrix', $matrixJsonPath, '--out', $selectionRecordPath)
        $robustnessPath = $null
        $robustnessRoot = 'D:\Quantification\outputs\research_robustness'
        if (Test-Path -LiteralPath $robustnessRoot -PathType Container) {
            foreach ($candidateRobustness in (Get-ChildItem -LiteralPath $robustnessRoot -Recurse -Filter 'robustness.json' -File |
                Sort-Object LastWriteTime -Descending)) {
                try {
                    $robustnessPayload = Get-Content -Raw -LiteralPath $candidateRobustness.FullName | ConvertFrom-Json
                    if ([string]$robustnessPayload.status -eq 'saved_mark_robustness_diagnostic' -and
                        [string]$robustnessPayload.end -eq [string]$Pack.source_cutoff -and
                        -not [bool]$robustnessPayload.fresh_holdout_claimed -and
                        -not [bool]$robustnessPayload.execution_admitted) {
                        $robustnessPath = $candidateRobustness.FullName
                        break
                    }
                } catch {
                    continue
                }
            }
        }
        if ($robustnessPath) {
            $selectionArgs += @('--robustness', $robustnessPath)
        }
        if ($preservedSelectionId) {
            $selectionArgs += @('--strategy-id', $preservedSelectionId)
        } elseif ([string]$pack.selected_strategy_id) {
            # A narrowed opening pack must not inherit a stale selection from
            # another pack generated for the same trading date.
            $selectionArgs += @('--strategy-id', [string]$pack.selected_strategy_id)
        }
        $selectionOutput = @(& $python $committee paper-strategy-selection @selectionArgs 2>&1)
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $selectionRecordPath -PathType Leaf)) {
            throw "Paper strategy selection record failed: $($selectionOutput -join ' ')"
        }
    } else {
        $selectionRecordPath = Join-Path $selectionRoot 'paper_strategy_selection_record_v1.json'
        Remove-Item -LiteralPath $matrixJsonPath,$matrixCsvPath,$selectionRecordPath,($selectionRecordPath -replace '\.json$','.csv') -Force -ErrorAction SilentlyContinue
    }
    Set-StatusProperty -Status $Status -Name 'offline_selected_strategy_plan_status' -Value $(
        if ($references.Count) { 'indexed_reference_only' } else { 'none_found' }
    )
    Set-StatusProperty -Status $Status -Name 'offline_selected_strategy_plan_count' -Value ([int]$references.Count)
    Set-StatusProperty -Status $Status -Name 'offline_selected_strategy_plans' -Value @($references)
    Set-StatusProperty -Status $Status -Name 'offline_selected_strategy_matrix_json' -Value $(if ($matrixRows.Count) { $matrixJsonPath } else { $null })
    Set-StatusProperty -Status $Status -Name 'offline_selected_strategy_matrix_csv' -Value $(if ($matrixRows.Count) { $matrixCsvPath } else { $null })
    Set-StatusProperty -Status $Status -Name 'offline_paper_lifecycle_index' -Value $(if (Test-Path -LiteralPath $lifecycleIndexPath -PathType Leaf) { $lifecycleIndexPath } else { $null })
    $riskSummaryPath = Join-Path $selectionRoot 'offline_portfolio_risk_summary_v1.json'
    Set-StatusProperty -Status $Status -Name 'offline_portfolio_risk_summary' -Value $(if (Test-Path -LiteralPath $riskSummaryPath -PathType Leaf) { $riskSummaryPath } else { $null })
    $releaseSequenceIndexPath = Join-Path $selectionRoot 'paper_manual_release_sequence_index.json'
    Set-StatusProperty -Status $Status -Name 'offline_paper_manual_release_sequence_index' -Value $(if (Test-Path -LiteralPath $releaseSequenceIndexPath -PathType Leaf) { $releaseSequenceIndexPath } else { $null })
    $recoveryHandoffIndexPath = Join-Path $selectionRoot 'paper_recovery_handoff_index.json'
    Set-StatusProperty -Status $Status -Name 'offline_paper_recovery_handoff_index' -Value $(if (Test-Path -LiteralPath $recoveryHandoffIndexPath -PathType Leaf) { $recoveryHandoffIndexPath } else { $null })
    Set-StatusProperty -Status $Status -Name 'offline_paper_strategy_selection_record' -Value $(if ($selectionRecordPath -and (Test-Path -LiteralPath $selectionRecordPath -PathType Leaf)) { $selectionRecordPath } else { $null })
    if ($selectionRecordPath -and (Test-Path -LiteralPath $selectionRecordPath -PathType Leaf)) {
        try {
            $selectionRecord = Get-Content -Raw -LiteralPath $selectionRecordPath | ConvertFrom-Json
            Set-StatusProperty -Status $Status -Name 'offline_paper_strategy_selection_status' -Value ([string]$selectionRecord.selection_status)
            Set-StatusProperty -Status $Status -Name 'offline_paper_selected_strategy_id' -Value $selectionRecord.selected_strategy_id
        } catch {
            Set-StatusProperty -Status $Status -Name 'offline_paper_strategy_selection_status' -Value 'unreadable'
            Set-StatusProperty -Status $Status -Name 'offline_paper_selected_strategy_id' -Value $null
        }
    }
}

function Ensure-ObservationBundlePreview {
    param(
        [object]$Status
    )

    $previewPath = Join-Path $runRoot 'observation_bundle_preview.json'
    $bundleArgs = @(
        'ths', 'preview-observation-bundle',
        '--account-json', $accountTemplatePath,
        '--orders-csv', $ordersTemplatePath,
        '--trades-csv', $tradesTemplatePath,
        '--cashflows-csv', $cashflowsTemplatePath
    )
    if ($THSProfile) {
        $bundleArgs += @('--profile', $THSProfile)
    }
    $bundleArgs += @('--out', $previewPath)
    $previewOutput = @(& $python $committee @bundleArgs 2>&1)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $previewPath -PathType Leaf)) {
        throw "Observation bundle preview failed: $($previewOutput -join ' ')"
    }
    $preview = Get-Content -Raw -LiteralPath $previewPath | ConvertFrom-Json
    Set-StatusProperty -Status $Status -Name 'observation_bundle_preview' -Value $previewPath
    Set-StatusProperty -Status $Status -Name 'ths_profile' -Value $(if ($THSProfile) { $THSProfile } else { $null })
    Set-StatusProperty -Status $Status -Name 'observation_bundle_status' -Value ([string]$preview.status)
    Set-StatusProperty -Status $Status -Name 'observation_bundle_execution_admitted' -Value ([bool]$preview.execution_admitted)
    Set-StatusProperty -Status $Status -Name 'observation_bundle_ledger_mutated' -Value ([bool]$preview.ledger_mutated)
    if ($preview.linkage) {
        Set-StatusProperty -Status $Status -Name 'observation_bundle_order_count' -Value ([int]$preview.linkage.order_count)
        Set-StatusProperty -Status $Status -Name 'observation_bundle_trade_count' -Value ([int]$preview.linkage.trade_count)
        Set-StatusProperty -Status $Status -Name 'observation_bundle_cashflow_count' -Value ([int]$preview.linkage.cashflow_count)
    }
}

function Publish-QuoteObservationDrop {
    param(
        [System.Collections.IDictionary]$Status,
        [string]$FilledQuotesPath,
        [string]$Source,
        [string]$ObservedAt,
        [string]$TradingDate
    )

    if (-not $FilledQuotesPath -or -not (Test-Path -LiteralPath $FilledQuotesPath -PathType Leaf)) {
        return
    }
    $rows = @(Import-Csv -LiteralPath $FilledQuotesPath)
    $requestedRows = @(Import-Csv -LiteralPath $quoteRequest)
    $requiredKeys = @($requestedRows | ForEach-Object { "$($_.symbol).$($_.market)" } | Sort-Object -Unique)
    $observedKeys = @($rows | ForEach-Object { "$($_.symbol).$($_.market)" } | Sort-Object -Unique)
    $missing = @($requiredKeys | Where-Object { $_ -notin $observedKeys })
    $invalid = @($rows | Where-Object { -not $_.price_rmb -or $_.price_rmb -notmatch '^\d+(?:\.\d+)?$' })
    if ($missing.Count -gt 0 -or $invalid.Count -gt 0 -or $requiredKeys.Count -eq 0) {
        Set-StatusProperty -Status $Status -Name 'quote_observation_drop_status' -Value 'not_published_incomplete'
        Set-StatusProperty -Status $Status -Name 'quote_observation_drop_missing_symbols' -Value $missing
        return
    }

    New-Item -ItemType Directory -Force -Path $quoteObservationRoot | Out-Null
    $canonicalQuotes = Join-Path $quoteObservationRoot 'quotes.csv'
    $canonicalMetadata = Join-Path $quoteObservationRoot 'metadata.json'
    if (Test-Path -LiteralPath $canonicalMetadata -PathType Leaf) {
        try {
            $existing = Get-Content -Raw -LiteralPath $canonicalMetadata | ConvertFrom-Json
            if ([string]$existing.trading_date -eq $TradingDate) {
                Set-StatusProperty -Status $Status -Name 'quote_observation_drop_status' -Value 'same_day_already_present'
                Set-StatusProperty -Status $Status -Name 'quote_observation_drop_quotes' -Value $canonicalQuotes
                Set-StatusProperty -Status $Status -Name 'quote_observation_drop_metadata' -Value $canonicalMetadata
                return
            }
            Set-StatusProperty -Status $Status -Name 'quote_observation_drop_status' -Value 'not_published_prior_day_preserved'
            Set-StatusProperty -Status $Status -Name 'quote_observation_drop_existing_trading_date' -Value ([string]$existing.trading_date)
            return
        } catch {
            Set-StatusProperty -Status $Status -Name 'quote_observation_drop_status' -Value 'not_published_invalid_existing_metadata'
            return
        }
    }
    if (Test-Path -LiteralPath $canonicalQuotes -PathType Leaf) {
        Set-StatusProperty -Status $Status -Name 'quote_observation_drop_status' -Value 'not_published_orphan_quotes_preserved'
        return
    }

    $normalized = @($rows | ForEach-Object {
        [pscustomobject]@{
            symbol = [string]$_.symbol
            market = [string]$_.market
            price_rmb = [string]$_.price_rmb
            quote_source = [string]$_.quote_source
        }
    })
    $tempQuotes = Join-Path $quoteObservationRoot ("quotes.csv.{0}.tmp" -f ([guid]::NewGuid().ToString('N')))
    $tempMetadata = Join-Path $quoteObservationRoot ("metadata.json.{0}.tmp" -f ([guid]::NewGuid().ToString('N')))
    try {
        $normalized | Export-Csv -LiteralPath $tempQuotes -NoTypeInformation -Encoding UTF8
        [ordered]@{
            schema = 'ths_raw_quote_observation_v1'
            account_alias = 'opening_quote_probe'
            source = $Source
            observed_at = $ObservedAt
            trading_date = $TradingDate
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tempMetadata -Encoding UTF8
        Move-Item -LiteralPath $tempQuotes -Destination $canonicalQuotes
        Move-Item -LiteralPath $tempMetadata -Destination $canonicalMetadata
        Set-StatusProperty -Status $Status -Name 'quote_observation_drop_status' -Value 'published_ready_for_import'
        Set-StatusProperty -Status $Status -Name 'quote_observation_drop_quotes' -Value $canonicalQuotes
        Set-StatusProperty -Status $Status -Name 'quote_observation_drop_metadata' -Value $canonicalMetadata
    } finally {
        Remove-Item -LiteralPath $tempQuotes,$tempMetadata -Force -ErrorAction SilentlyContinue
    }
}

function Find-ReusableOfflineDesk {
    param(
        [string]$DesiredStrategyId,
        [string]$OpeningPackPath
    )

    $openingPackResolved = (Resolve-Path -LiteralPath $OpeningPackPath).Path
    $latestThsReadonlyEvidence = Find-LatestThsReadonlyEvidence `
        -Root (Join-Path $tradingHome 'ths\opening_readonly')
    $candidateDirectories = @(Get-ChildItem -LiteralPath $runRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^offline_desk_v[0-9]+$' } |
        Sort-Object LastWriteTime -Descending)
    $defaultDirectory = Join-Path $runRoot 'offline_desk'
    if (Test-Path -LiteralPath $defaultDirectory -PathType Container) {
        $candidateDirectories += Get-Item -LiteralPath $defaultDirectory
    }

    foreach ($directory in $candidateDirectories) {
        $snapshotFile = Join-Path $directory.FullName 'snapshot.json'
        if (-not (Test-Path -LiteralPath $snapshotFile -PathType Leaf)) {
            continue
        }
        try {
            $snapshot = Get-Content -Raw -LiteralPath $snapshotFile | ConvertFrom-Json
            if ([string]$snapshot.strategy_id -ne $DesiredStrategyId -or
                [string]$snapshot.opening_pack -ne $openingPackResolved -or
                [bool]$snapshot.execution_admitted) {
                continue
            }
            $requiredArtifacts = @(
                'paper_reconciliation',
                'paper_child_order_reconciliation',
                'paper_audit_event_log',
                'paper_next_session_carry',
                'strategy_board_json',
                'strategy_board_csv',
                'strategy_promotion_review',
                'strategy_promotion_review_csv',
                'function_matrix',
                'offline_operator_decision_queue_json',
                'paper_recovery_handoff_index'
            )
            $complete = $true
            foreach ($artifactName in $requiredArtifacts) {
                $property = $snapshot.artifacts.PSObject.Properties[$artifactName]
                if (-not $property -or -not (Test-Path -LiteralPath ([string]$property.Value) -PathType Leaf)) {
                    $complete = $false
                    break
                }
            }
            if ($complete) {
                $matrixPath = [string]$snapshot.artifacts.function_matrix
                $hasSecurityMasterFunction = $matrixPath -and
                    (Select-String -LiteralPath $matrixPath -Pattern 'security_master_and_scope' -Quiet)
                $hasBrokerStatementFunction = $matrixPath -and
                    (Select-String -LiteralPath $matrixPath -Pattern 'broker_statement_ingestion' -Quiet)
                if (-not $hasSecurityMasterFunction -or -not $hasBrokerStatementFunction) {
                    $complete = $false
                }
                if ($complete -and $matrixPath -and [string]$snapshot.instrument_master.status -eq 'latest_prior_available') {
                    try {
                        $securityRow = Import-Csv -Encoding UTF8 -LiteralPath $matrixPath |
                            Where-Object { [string]$_.function_id -eq 'security_master_and_scope' } |
                            Select-Object -First 1
                        if (-not $securityRow -or [string]$securityRow.local_state -eq 'security_master_missing') {
                            # The snapshot already has a verified prior master for identity;
                            # force one refresh so the function matrix does not contradict it.
                            $complete = $false
                        }
                    } catch {
                        $complete = $false
                    }
                }
                if ($complete) {
                    $boardCsv = [string]$snapshot.artifacts.strategy_board_csv
                    $reviewCsv = [string]$snapshot.artifacts.strategy_promotion_review_csv
                    $boardHeader = if ($boardCsv) {
                        Get-Content -LiteralPath $boardCsv -TotalCount 1
                    } else { '' }
                    $reviewHeader = if ($reviewCsv) {
                        Get-Content -LiteralPath $reviewCsv -TotalCount 1
                    } else { '' }
                    $calendarFieldsPresent =
                        [string]$boardHeader -match 'independent_sample_calendar_window_coverage_verified' -and
                        [string]$boardHeader -match 'independent_sample_calendar_gap_count' -and
                        [string]$boardHeader -match 'independent_sample_calendar_source' -and
                        [string]$reviewHeader -match 'independent_sample_calendar_window_coverage_verified' -and
                        [string]$reviewHeader -match 'independent_sample_calendar_gap_count' -and
                        [string]$reviewHeader -match 'independent_sample_calendar_source'
                    if (-not $calendarFieldsPresent) {
                        # The consumer schema changed; rebuild the paper desk so
                        # the active snapshot cannot silently serve stale columns.
                        $complete = $false
                    }
                }
                if ($complete) {
                    $latestIndependent = Find-LatestIndependentWindowStability `
                        -Root 'D:\Quantification\outputs\stockdb_independent_window_stability'
                    $reviewPath = [string]$snapshot.artifacts.strategy_promotion_review
                    if ($latestIndependent -and $reviewPath -and (Test-Path -LiteralPath $reviewPath -PathType Leaf)) {
                        try {
                            $review = Get-Content -Raw -Encoding UTF8 -LiteralPath $reviewPath | ConvertFrom-Json
                            if ([string]$review.source_independent_window_stability -ne $latestIndependent) {
                                # Refresh the desk when a newer supplemental
                                # stability receipt is available.
                                $complete = $false
                            }
                        } catch {
                            $complete = $false
                        }
                    }
                }
                if ($complete) {
                    $evidencePath = [string]$snapshot.artifacts.strategy_evidence_card
                    if (-not $evidencePath -or -not (Test-Path -LiteralPath $evidencePath -PathType Leaf)) {
                        $complete = $false
                    } else {
                        try {
                            $evidence = Get-Content -Raw -Encoding UTF8 -LiteralPath $evidencePath | ConvertFrom-Json
                            $factorStatus = [string]$evidence.factor_replay.status
                            if ($factorStatus -notin @('factor_replay_found_same_cutoff', 'factor_replay_found_prior_cutoff')) {
                                # Rebuild once when the desk still carries the
                                # old "missing" factor status after the
                                # read-only prior-cutoff consumer is available.
                                $complete = $false
                            }
                        } catch {
                            $complete = $false
                        }
                    }
                }
                if ($complete) {
                    $actionBoardPath = [string]$snapshot.artifacts.operator_action_board
                    $hasBrokerStatementAction = $actionBoardPath -and
                        (Select-String -LiteralPath $actionBoardPath -Pattern 'import_ths_manual_statements_before_reconciliation' -Quiet)
                    if (-not $hasBrokerStatementAction) {
                        $complete = $false
                    }
                }
            }
            if ($complete -and [string]$snapshot.instrument_master.order_identity_source -eq 'qualified_order_symbol_fallback') {
                try {
                    $packForIdentity = Get-Content -Raw -LiteralPath $OpeningPackPath | ConvertFrom-Json
                    $cutoffForIdentity = [datetime]::ParseExact(
                        [string]$packForIdentity.source_cutoff, 'yyyy-MM-dd', $null
                    )
                    $priorMasterAvailable = $false
                    $masterRoot = Join-Path $tradingHome 'research\instrument_masters'
                    foreach ($masterRun in (Get-ChildItem -LiteralPath $masterRoot -Recurse -Filter 'run.json' -File -ErrorAction SilentlyContinue)) {
                        try {
                            $masterPayload = Get-Content -Raw -LiteralPath $masterRun.FullName | ConvertFrom-Json
                            $observedAsOf = [datetime]::ParseExact(
                                [string]$masterPayload.observed_as_of, 'yyyy-MM-dd', $null
                            )
                            $masterFile = Join-Path $masterRun.DirectoryName ([string]$masterPayload.master_file)
                            if ([string]$masterPayload.schema -eq 'instrument_master_v1' -and
                                [string]$masterPayload.status -eq 'completed_research_instrument_master' -and
                                -not [bool]$masterPayload.execution_admitted -and
                                $observedAsOf -lt $cutoffForIdentity -and
                                (Test-Path -LiteralPath $masterFile -PathType Leaf)) {
                                $priorMasterAvailable = $true
                                break
                            }
                        } catch {
                            continue
                        }
                    }
                    if ($priorMasterAvailable) {
                        $complete = $false
                    }
                } catch {
                    # Keep the existing snapshot when the identity refresh check cannot be evaluated.
                }
            }
            if ($complete -and $DesiredStrategyId -and $DesiredStrategyId -ne 'multi_strategy_candidates') {
                try {
                    $recoveryIndexPath = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' (Join-Path $tradingDate 'paper_recovery_handoff_index.json')
                    $recoveryIndex = Get-Content -Raw -LiteralPath $recoveryIndexPath | ConvertFrom-Json
                    $currentRecoveryRow = @($recoveryIndex.rows) |
                        Where-Object { [string]$_.strategy_id -eq $DesiredStrategyId } |
                        Select-Object -First 1
                    $queuePath = [string]$snapshot.artifacts.offline_operator_decision_queue_json
                    $queue = Get-Content -Raw -LiteralPath $queuePath | ConvertFrom-Json
                    $queueRow = @($queue.rows) |
                        Where-Object { [string]$_.strategy_id -eq $DesiredStrategyId } |
                        Select-Object -First 1
                    if (-not $currentRecoveryRow -or -not $queueRow -or
                        [string]$queueRow.recovery_handoff_json -ne [string]$currentRecoveryRow.handoff_json) {
                        $complete = $false
                    }
                } catch {
                    $complete = $false
                }
            }
            if ($complete -and $DesiredStrategyId -and $DesiredStrategyId -ne 'multi_strategy_candidates') {
                try {
                    $selectionPath = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' (Join-Path $tradingDate 'paper_strategy_selection_record_v1.json')
                    $selection = Get-Content -Raw -LiteralPath $selectionPath | ConvertFrom-Json
                    $selectedPlanPath = [string]$selection.selected_plan
                    $selectedPlan = Get-Content -Raw -LiteralPath $selectedPlanPath | ConvertFrom-Json
                    if ([string]$selection.selected_strategy_id -ne $DesiredStrategyId) {
                        $complete = $false
                    } else {
                        foreach ($artifactName in @('paper_reconciliation', 'target_order_reconciliation', 'paper_release_decision')) {
                            $currentPath = [string]$selectedPlan.selected_artifacts.$artifactName
                            $reusedPath = [string]$snapshot.artifacts.$artifactName
                            if (-not (Test-Path -LiteralPath $currentPath -PathType Leaf) -or
                                -not (Test-Path -LiteralPath $reusedPath -PathType Leaf) -or
                                (Get-FileHash -LiteralPath $currentPath -Algorithm SHA256).Hash -ne
                                (Get-FileHash -LiteralPath $reusedPath -Algorithm SHA256).Hash) {
                                $complete = $false
                                break
                            }
                        }
                    }
                } catch {
                    $complete = $false
                }
            }
            if ($complete -and $latestThsReadonlyEvidence) {
                $existingThsReadonlyEvidence = [string]$snapshot.artifacts.ths_readonly_evidence_index
                if ($existingThsReadonlyEvidence -ne $latestThsReadonlyEvidence) {
                    $complete = $false
                }
            }
            if ($complete) {
                return $directory.FullName
            }
        } catch {
            continue
        }
    }
    return $null
}

function Ensure-OfflineDeskPreview {
    param(
        [object]$Status
    )

    $offlineRoot = Join-Path $runRoot 'offline_desk'
    $snapshotPath = Join-Path $offlineRoot 'snapshot.json'
    $selectionPath = Join-Path 'D:\QuantificationData\trading\research\strategy_selection' (Join-Path $tradingDate 'paper_strategy_selection_record_v1.json')
    $latestThsReadonlyEvidence = Find-LatestThsReadonlyEvidence `
        -Root (Join-Path $tradingHome 'ths\opening_readonly')
    # An explicitly narrowed opening pack is the authority for this session.
    # Do not let a stale same-date selection record from another pack route the
    # desk snapshot to an unrelated strategy.
    $packSelectedStrategyId = [string]$pack.selected_strategy_id
    $requestedSelectedStrategyId = if ($packSelectedStrategyId) { $packSelectedStrategyId } else { $null }
    if (Test-Path -LiteralPath $selectionPath -PathType Leaf) {
        try {
            $selection = Get-Content -Raw -LiteralPath $selectionPath | ConvertFrom-Json
            if ([string]$selection.execution_day -eq [string]$tradingDate -and
                [string]$selection.selection_status -eq 'paper_candidate_explicitly_selected' -and
                [string]$selection.selected_strategy_id -and
                (-not $packSelectedStrategyId -or
                 [string]$selection.selected_strategy_id -eq $packSelectedStrategyId)) {
                $requestedSelectedStrategyId = [string]$selection.selected_strategy_id
            }
        } catch {
            # Keep the pack-selected strategy when the optional selection
            # record is unreadable.
        }
    }
    $desiredDeskStrategyId = if ($requestedSelectedStrategyId) {
        $requestedSelectedStrategyId
    } else {
        'multi_strategy_candidates'
    }
    $reusableDeskFound = $false
    $reusableDesk = Find-ReusableOfflineDesk -DesiredStrategyId $desiredDeskStrategyId -OpeningPackPath $OpeningPack
    if ($reusableDesk) {
        $offlineRoot = $reusableDesk
        $snapshotPath = Join-Path $offlineRoot 'snapshot.json'
        $reusableDeskFound = $true
    }
    if (-not $reusableDeskFound -and (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
        try {
            $existingDeskSnapshot = Get-Content -Raw -LiteralPath $snapshotPath | ConvertFrom-Json
            $identityRefreshRequired = $false
            if ([string]$existingDeskSnapshot.instrument_master.order_identity_source -eq 'qualified_order_symbol_fallback') {
                try {
                    $packForIdentity = Get-Content -Raw -LiteralPath $OpeningPack | ConvertFrom-Json
                    $cutoffForIdentity = [datetime]::ParseExact(
                        [string]$packForIdentity.source_cutoff, 'yyyy-MM-dd', $null
                    )
                    $masterRoot = Join-Path $tradingHome 'research\instrument_masters'
                    foreach ($masterRun in (Get-ChildItem -LiteralPath $masterRoot -Recurse -Filter 'run.json' -File -ErrorAction SilentlyContinue)) {
                        try {
                            $masterPayload = Get-Content -Raw -LiteralPath $masterRun.FullName | ConvertFrom-Json
                            $observedAsOf = [datetime]::ParseExact(
                                [string]$masterPayload.observed_as_of, 'yyyy-MM-dd', $null
                            )
                            $masterFile = Join-Path $masterRun.DirectoryName ([string]$masterPayload.master_file)
                            if ([string]$masterPayload.schema -eq 'instrument_master_v1' -and
                                [string]$masterPayload.status -eq 'completed_research_instrument_master' -and
                                -not [bool]$masterPayload.execution_admitted -and
                                $observedAsOf -lt $cutoffForIdentity -and
                                (Test-Path -LiteralPath $masterFile -PathType Leaf)) {
                                $identityRefreshRequired = $true
                                break
                            }
                        } catch {
                            continue
                        }
                    }
                } catch {
                    $identityRefreshRequired = $false
                }
            }
            if ([string]$existingDeskSnapshot.strategy_id -ne $desiredDeskStrategyId -or $identityRefreshRequired) {
                $versioned = Get-ChildItem -LiteralPath $runRoot -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^offline_desk_v([0-9]+)$' } |
                    Sort-Object { [int]($_.Name -replace '^offline_desk_v', '') }
                $nextVersion = if ($versioned) {
                    ([int]($versioned[-1].Name -replace '^offline_desk_v', '')) + 1
                } else {
                    1
                }
                $offlineRoot = Join-Path $runRoot ("offline_desk_v{0}" -f $nextVersion)
                $snapshotPath = Join-Path $offlineRoot 'snapshot.json'
            }
            $existingReconciliation = $existingDeskSnapshot.artifacts.paper_reconciliation
            $existingChildProjection = $existingDeskSnapshot.artifacts.paper_child_order_projection
            $existingChildReconciliation = $existingDeskSnapshot.artifacts.paper_child_order_reconciliation
            $existingAuditEventLog = $existingDeskSnapshot.artifacts.paper_audit_event_log
            $existingTargetOrderReconciliation = $existingDeskSnapshot.artifacts.target_order_reconciliation
            $existingPaperReleaseDecision = $existingDeskSnapshot.artifacts.paper_release_decision
            $existingNextSessionCarry = $existingDeskSnapshot.artifacts.paper_next_session_carry
            $existingStrategyPromotionCard = $existingDeskSnapshot.artifacts.strategy_promotion_card
            $existingStrategyEvidenceCard = $existingDeskSnapshot.artifacts.strategy_evidence_card
            $existingStrategyBoard = $existingDeskSnapshot.artifacts.strategy_board_json
            $existingStrategyBoardCsv = $existingDeskSnapshot.artifacts.strategy_board_csv
            $existingStrategyPromotionReviewCsv = $existingDeskSnapshot.artifacts.strategy_promotion_review_csv
            $existingStrategySizingMatrix = $existingDeskSnapshot.artifacts.strategy_account_sizing_matrix
            $existingOfflinePortfolioRisk = $existingDeskSnapshot.artifacts.offline_portfolio_risk_summary
            $existingThsReadonlyEvidence = [string]$existingDeskSnapshot.artifacts.ths_readonly_evidence_index
            $existingStrategySelectionFields = $existingDeskSnapshot.PSObject.Properties.Name -contains 'strategy_selection'
            $existingPromotionCapacityFields = $false
            if ($existingStrategyPromotionCard -and (Test-Path -LiteralPath ([string]$existingStrategyPromotionCard) -PathType Leaf)) {
                $promotionHeader = Get-Content -LiteralPath ([string]$existingStrategyPromotionCard) -TotalCount 1
                $existingPromotionCapacityFields = [string]$promotionHeader -match 'paper_risk_status'
            }
            $existingDataUsabilityFields = $false
            $existingIndependentCalendarFields = $false
            if ($existingStrategyBoardCsv -and (Test-Path -LiteralPath ([string]$existingStrategyBoardCsv) -PathType Leaf)) {
                $boardHeader = Get-Content -LiteralPath ([string]$existingStrategyBoardCsv) -TotalCount 1
                $existingDataUsabilityFields = [string]$boardHeader -match 'data_usability_status'
                $existingIndependentCalendarFields =
                    [string]$boardHeader -match 'independent_sample_calendar_window_coverage_verified' -and
                    [string]$boardHeader -match 'independent_sample_calendar_gap_count' -and
                    [string]$boardHeader -match 'independent_sample_calendar_source'
            }
            if ($existingDataUsabilityFields -and $existingStrategyPromotionReviewCsv -and
                (Test-Path -LiteralPath ([string]$existingStrategyPromotionReviewCsv) -PathType Leaf)) {
                $reviewHeader = Get-Content -LiteralPath ([string]$existingStrategyPromotionReviewCsv) -TotalCount 1
                $existingDataUsabilityFields = [string]$reviewHeader -match 'data_usability_status'
            }
            $existingCalendarFields = $false
            $existingCalendarValues = $false
            if ($existingNextSessionCarry -and
                (Test-Path -LiteralPath ([string]$existingNextSessionCarry) -PathType Leaf)) {
                try {
                    $existingCarry = Get-Content -Raw -LiteralPath ([string]$existingNextSessionCarry) | ConvertFrom-Json
                    $existingCalendarFields =
                        $existingCarry.PSObject.Properties.Name -contains 'calendar_id' -and
                        $existingCarry.PSObject.Properties.Name -contains 'next_exchange_session'
                    $pendingT1Rows = @($existingCarry.pending_t1_buys)
                    $existingCalendarValues =
                        $pendingT1Rows.Count -eq 0 -or
                        ($existingCarry.next_exchange_session -and
                            @($pendingT1Rows | Where-Object {
                                [string]$_.eligible_from -ne [string]$existingCarry.next_exchange_session
                            }).Count -eq 0)
                } catch {
                    $existingCalendarFields = $false
                    $existingCalendarValues = $false
                }
            }
            $existingStrategyEvidenceStatus = $null
            $existingStrategyEvidenceOpeningPack = $null
            if ($existingStrategyEvidenceCard -and (Test-Path -LiteralPath ([string]$existingStrategyEvidenceCard) -PathType Leaf)) {
                try {
                    $existingStrategyEvidence = Get-Content -Raw -LiteralPath ([string]$existingStrategyEvidenceCard) | ConvertFrom-Json
                    $existingStrategyEvidenceStatus = [string]$existingStrategyEvidence.factor_replay.status
                    $existingStrategyEvidenceOpeningPack = [string]$existingStrategyEvidence.opening_pack.path
                } catch {
                    $existingStrategyEvidenceStatus = $null
                    $existingStrategyEvidenceOpeningPack = $null
                }
            }
            $existingFeesBasis = [string]$existingDeskSnapshot.paper_fill_rehearsal.fees_basis
            if (-not $existingReconciliation -or -not (Test-Path -LiteralPath ([string]$existingReconciliation) -PathType Leaf) -or
                $existingFeesBasis -notlike 'trading.rules.fees*' -or -not $existingChildProjection -or
                -not (Test-Path -LiteralPath ([string]$existingChildProjection) -PathType Leaf) -or
                -not $existingChildReconciliation -or
                -not (Test-Path -LiteralPath ([string]$existingChildReconciliation) -PathType Leaf) -or
                -not $existingAuditEventLog -or
                -not (Test-Path -LiteralPath ([string]$existingAuditEventLog) -PathType Leaf) -or
                -not $existingTargetOrderReconciliation -or
                -not (Test-Path -LiteralPath ([string]$existingTargetOrderReconciliation) -PathType Leaf) -or
                -not $existingPaperReleaseDecision -or
                -not (Test-Path -LiteralPath ([string]$existingPaperReleaseDecision) -PathType Leaf) -or
                -not $existingNextSessionCarry -or
                -not (Test-Path -LiteralPath ([string]$existingNextSessionCarry) -PathType Leaf) -or
                -not $existingCalendarFields -or
                -not $existingCalendarValues -or
                -not $existingStrategyPromotionCard -or
                -not (Test-Path -LiteralPath ([string]$existingStrategyPromotionCard) -PathType Leaf) -or
                -not $existingPromotionCapacityFields -or
                -not $existingDataUsabilityFields -or
                -not $existingIndependentCalendarFields -or
                -not $existingStrategyEvidenceCard -or
                -not (Test-Path -LiteralPath ([string]$existingStrategyEvidenceCard) -PathType Leaf) -or
                -not $existingStrategyBoard -or
                -not (Test-Path -LiteralPath ([string]$existingStrategyBoard) -PathType Leaf) -or
                -not $existingStrategySizingMatrix -or
                -not (Test-Path -LiteralPath ([string]$existingStrategySizingMatrix) -PathType Leaf) -or
                -not $existingOfflinePortfolioRisk -or
                -not (Test-Path -LiteralPath ([string]$existingOfflinePortfolioRisk) -PathType Leaf) -or
                -not $existingStrategySelectionFields -or
                 ($latestThsReadonlyEvidence -and $existingThsReadonlyEvidence -ne $latestThsReadonlyEvidence) -or
                ($existingStrategyEvidenceStatus -notin @('factor_replay_found_same_cutoff', 'factor_replay_found_prior_cutoff')) -or
                $existingStrategyEvidenceOpeningPack -ne $OpeningPack) {
                # Keep the original evidence immutable and write a refreshed
                # paper desk beside it when a new offline artifact is added.
                # Do not pin this refresh to a historical version: doing so
                # silently reuses the stale desk on every subsequent run.
                $versioned = Get-ChildItem -LiteralPath $runRoot -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^offline_desk_v([0-9]+)$' } |
                    Sort-Object { [int]($_.Name -replace '^offline_desk_v', '') }
                $nextVersion = if ($versioned) {
                    ([int]($versioned[-1].Name -replace '^offline_desk_v', '')) + 1
                } else {
                    1
                }
                $offlineRoot = Join-Path $runRoot ("offline_desk_v{0}" -f $nextVersion)
                $snapshotPath = Join-Path $offlineRoot 'snapshot.json'
            }
        } catch {
            # A partial prior snapshot is handled by the normal build path.
        }
    }
    if (-not $reusableDeskFound -and (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
        try {
            $selectedSnapshot = Get-Content -Raw -LiteralPath $snapshotPath | ConvertFrom-Json
            $selectedBoard = $selectedSnapshot.artifacts.strategy_board_json
            if (-not $selectedBoard -or -not (Test-Path -LiteralPath ([string]$selectedBoard) -PathType Leaf)) {
                $versioned = Get-ChildItem -LiteralPath $runRoot -Directory -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '^offline_desk_v([0-9]+)$' } |
                    Sort-Object { [int]($_.Name -replace '^offline_desk_v', '') }
                $nextVersion = if ($versioned) {
                    ([int]($versioned[-1].Name -replace '^offline_desk_v', '')) + 1
                } else {
                    1
                }
                $offlineRoot = Join-Path $runRoot ("offline_desk_v{0}" -f $nextVersion)
                $snapshotPath = Join-Path $offlineRoot 'snapshot.json'
            }
        } catch {
            # Keep the selected snapshot if its optional metadata is unreadable.
        }
    }
    if (-not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
        $calendarBoundForward = Find-CalendarBoundStockdbArtifact `
            -Root 'D:\QuantificationData\trading\research\stockdb_forward_compare' `
            -FileName 'result.json' -Schema 'stockdb_forward_compare_v1'
        $calendarBoundLedger = Find-CalendarBoundStockdbArtifact `
            -Root 'D:\QuantificationData\trading\research\stockdb_ledger_replays' `
            -FileName 'summary.json' -Schema 'stockdb_shared_ledger_replay_v1'
        $deskArgs = @(
            'trader-desk-snapshot',
            '--opening-pack', $OpeningPack,
            '--strategy', $(if ($requestedSelectedStrategyId) { $requestedSelectedStrategyId } else { $Strategy }),
            '--sizing', 'replay_shares',
            '--execution-scope', 'strict',
            '--out', $offlineRoot
        )
        if ($calendarBoundForward) {
            $deskArgs += @('--stockdb-forward-compare', $calendarBoundForward)
        }
        if ($calendarBoundLedger) {
            $deskArgs += @('--stockdb-ledger-replay', $calendarBoundLedger)
        }
        $latestThsReadonlyEvidence = Find-LatestThsReadonlyEvidence `
            -Root (Join-Path $tradingHome 'ths\opening_readonly')
        if ($latestThsReadonlyEvidence) {
            $deskArgs += @('--ths-readonly-evidence-index', $latestThsReadonlyEvidence)
        }
        if ($requestedSelectedStrategyId) {
            $deskArgs += @('--selected-strategy-id', $requestedSelectedStrategyId)
        }
        $deskOutput = @(& $python $committee @deskArgs 2>&1)
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $snapshotPath -PathType Leaf)) {
            throw "Offline trader desk snapshot failed: $($deskOutput -join ' ')"
        }
    }

    $snapshot = Get-Content -Raw -LiteralPath $snapshotPath | ConvertFrom-Json
    # Keep the session-level pointer aligned with the selected/versioned desk.
    # The wrapper consumes this pointer to avoid mistaking an older candidate
    # snapshot for the desk just materialized in this run.
    Set-StatusProperty -Status $Status -Name 'trader_desk_snapshot' -Value $snapshotPath
    Set-StatusProperty -Status $Status -Name 'offline_desk_state' -Value 'paper_offline_ready'
    Set-StatusProperty -Status $Status -Name 'offline_desk_output' -Value $offlineRoot
    Set-StatusProperty -Status $Status -Name 'offline_desk_snapshot' -Value $snapshotPath
    Set-StatusProperty -Status $Status -Name 'offline_desk_execution_admitted' -Value ([bool]$snapshot.execution_admitted)
    foreach ($artifactName in @(
        'blotter',
        'tca_pretrade',
        'risk_checks',
        'execution_schedule',
        'static_risk_summary',
        'paper_fill_projection',
        'paper_ledger_projection',
        'paper_order_lifecycle',
        'paper_position_projection',
        'paper_reconciliation',
        'paper_child_order_projection',
        'paper_child_order_reconciliation',
        'paper_child_order_reconciliation_summary',
        'paper_audit_event_log',
        'paper_audit_event_log_jsonl',
        'target_order_reconciliation',
        'paper_release_decision',
        'paper_next_session_carry',
        'paper_account_sizing_projection',
        'paper_account_sizing_summary',
        'strategy_promotion_card',
        'strategy_promotion_review',
        'strategy_promotion_review_csv',
        'strategy_evidence_card',
        'strategy_board_csv',
        'strategy_board_json',
        'strategy_account_sizing_matrix',
        'strategy_account_sizing_matrix_summary',
        'offline_portfolio_risk_summary',
        'allocation_settlement_projection',
        'scenario_analysis',
        'strategy_comparison',
        'paper_execution_attribution',
        'paper_execution_attribution_csv',
        'paper_child_pressure_scenarios',
        'paper_execution_cost_stress',
        'function_matrix',
        'operator_action_board',
        'exception_alerts',
        'research_robustness',
        'research_robustness_json',
        'research_robustness_report',
        'instrument_master_coverage'
    )) {
        if ($snapshot.artifacts -and $snapshot.artifacts.PSObject.Properties.Name -contains $artifactName) {
            Set-StatusProperty -Status $Status `
                -Name ("offline_desk_{0}" -f $artifactName) `
                -Value ([string]$snapshot.artifacts.$artifactName)
        }
    }
    $carryPath = if ($snapshot.artifacts) { [string]$snapshot.artifacts.paper_next_session_carry } else { '' }
    if ($carryPath -and (Test-Path -LiteralPath $carryPath -PathType Leaf)) {
        try {
            $carry = Get-Content -Raw -LiteralPath $carryPath | ConvertFrom-Json
            Set-StatusProperty -Status $Status -Name 'offline_desk_carry_pending_settlement_count' -Value ([int]$carry.pending_settlement_order_count)
            Set-StatusProperty -Status $Status -Name 'offline_desk_carry_t1_buy_symbol_count' -Value @($carry.pending_t1_buys).Count
            Set-StatusProperty -Status $Status -Name 'offline_desk_carry_sell_review_symbol_count' -Value @($carry.sell_release_review).Count
            Set-StatusProperty -Status $Status -Name 'offline_desk_carry_next_exchange_session' -Value $carry.next_exchange_session
            Set-StatusProperty -Status $Status -Name 'offline_desk_carry_state' -Value ([string]$carry.status)
        } catch {
            Set-StatusProperty -Status $Status -Name 'offline_desk_carry_state' -Value 'unreadable'
        }
    }
    Add-OfflineSelectedPlanReference -Status $Status -ExecutionDay $tradingDate
}

# Refresh pointers for the offline desk in the early-return branches too.  The
# status file is intentionally reused across probes, so stale attribution paths
# must not survive a newly rebuilt paper desk.
function Refresh-DeskPointers {
    param(
        [object]$Status,
        [string]$DeskRoot
    )

    if (-not $DeskRoot -or -not (Test-Path -LiteralPath $DeskRoot -PathType Container)) {
        return
    }
    $snapshotFile = Get-ChildItem -LiteralPath $DeskRoot -Recurse -Filter 'snapshot.json' -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $snapshotFile) {
        return
    }
    try {
        $snapshot = Get-Content -Raw -LiteralPath $snapshotFile.FullName | ConvertFrom-Json
    } catch {
        return
    }
    Set-StatusProperty -Status $Status -Name 'trader_desk_snapshot' -Value $snapshotFile.FullName
    foreach ($statusName in @(
        'offline_paper_execution_attribution',
        'offline_paper_child_pressure_scenarios',
        'trader_desk_paper_execution_attribution',
        'trader_desk_paper_child_pressure_scenarios'
    )) {
        Set-StatusProperty -Status $Status -Name $statusName -Value $null
    }
    foreach ($artifactName in @(
        'blotter', 'tca_pretrade', 'risk_checks', 'execution_schedule',
        'static_risk_summary', 'paper_fill_projection', 'paper_ledger_projection',
        'paper_order_lifecycle', 'paper_position_projection',
        'paper_reconciliation', 'paper_child_order_projection',
        'paper_child_order_reconciliation', 'paper_child_order_reconciliation_summary',
        'paper_audit_event_log', 'paper_audit_event_log_jsonl',
        'target_order_reconciliation', 'paper_release_decision',
        'offline_operator_decision_queue', 'offline_operator_decision_queue_json',
        'paper_recovery_handoff_index',
        'paper_next_session_carry', 'paper_account_sizing_projection',
        'paper_account_sizing_summary', 'strategy_promotion_card',
        'strategy_promotion_review', 'strategy_promotion_review_csv',
        'strategy_evidence_card', 'strategy_board_csv', 'strategy_board_json',
        'strategy_account_sizing_matrix', 'strategy_account_sizing_matrix_summary',
        'offline_portfolio_risk_summary',
        'allocation_settlement_projection', 'scenario_analysis',
        'strategy_comparison', 'function_matrix', 'operator_action_board',
        'paper_execution_attribution', 'paper_execution_attribution_csv',
        'paper_child_pressure_scenarios', 'paper_execution_cost_stress', 'exception_alerts', 'research_robustness', 'research_robustness_json',
        'research_robustness_report'
    )) {
        if ($snapshot.artifacts -and $snapshot.artifacts.PSObject.Properties.Name -contains $artifactName) {
            Set-StatusProperty -Status $Status -Name ("trader_desk_{0}" -f $artifactName) -Value ([string]$snapshot.artifacts.$artifactName)
        }
    }
    foreach ($artifactName in @('paper_execution_attribution_v1.json', 'paper_child_pressure_scenarios_v1.csv')) {
        $artifactPath = Join-Path $DeskRoot $artifactName
        if (Test-Path -LiteralPath $artifactPath -PathType Leaf) {
            $statusName = if ($artifactName -eq 'paper_execution_attribution_v1.json') {
                'trader_desk_paper_execution_attribution'
            } else {
                'trader_desk_paper_child_pressure_scenarios'
            }
            Set-StatusProperty -Status $Status -Name $statusName -Value $artifactPath
        }
    }
    $boardFile = Join-Path $snapshotFile.DirectoryName 'operator_action_board.csv'
    if (Test-Path -LiteralPath $boardFile -PathType Leaf) {
        $rows = @(Import-Csv -LiteralPath $boardFile)
        Set-StatusProperty -Status $Status -Name 'operator_action_board' -Value $boardFile
        Set-StatusProperty -Status $Status -Name 'operator_action_board_total' -Value ([int]$rows.Count)
        Set-StatusProperty -Status $Status -Name 'operator_action_board_blocked' -Value ([int]@($rows | Where-Object state -eq 'blocked').Count)
        Set-StatusProperty -Status $Status -Name 'operator_action_board_actionable' -Value ([int]@($rows | Where-Object state -eq 'actionable').Count)
    }
}

# Build the offline paper desk before any trading-date or opening-window gate.
# It consumes only the frozen opening pack; it never probes quotes or touches
# account/client state.  A previous status file may point at an older pack;
# never let that stale pointer override an explicitly selected/newest pack.
$paperPreviewPath = Find-PaperPreview -Root 'D:\Quantification\outputs\paper_rebalance' `
    -StrategyId $Strategy -ExecutionDay $tradingDate -ExecutionScope $ExecutionScope
$reuseOfflineStatus = $existingStatus -and
    [string]$existingStatus.opening_pack -eq [string]$OpeningPack -and
    [string]$existingStatus.trading_date -eq [string]$tradingDate
$offlineStatus = if ($reuseOfflineStatus) { $existingStatus } else { [ordered]@{} }
Set-StatusProperty -Status $offlineStatus -Name 'schema' -Value 'next_session_opening_runner_v1'
Set-StatusProperty -Status $offlineStatus -Name 'today' -Value $today
Set-StatusProperty -Status $offlineStatus -Name 'trading_date' -Value $tradingDate
Set-StatusProperty -Status $offlineStatus -Name 'opening_pack' -Value $OpeningPack
Set-StatusProperty -Status $offlineStatus -Name 'quote_request' -Value $quoteRequest
Set-StatusProperty -Status $offlineStatus -Name 'output_root' -Value $runRoot
Set-StatusProperty -Status $offlineStatus -Name 'earliest_probe_time' -Value $EarliestProbeTime
Set-StatusProperty -Status $offlineStatus -Name 'account_observation_template' -Value $accountTemplatePath
Set-StatusProperty -Status $offlineStatus -Name 'orders_observation_template' -Value $ordersTemplatePath
Set-StatusProperty -Status $offlineStatus -Name 'trades_observation_template' -Value $tradesTemplatePath
Set-StatusProperty -Status $offlineStatus -Name 'cashflows_observation_template' -Value $cashflowsTemplatePath
Set-StatusProperty -Status $offlineStatus -Name 'movements_observation_template' -Value $movementsTemplatePath
Set-StatusProperty -Status $offlineStatus -Name 'restriction_releases_observation_template' -Value $restrictionReleasesTemplatePath
Set-StatusProperty -Status $offlineStatus -Name 'share_credits_observation_template' -Value $shareCreditsTemplatePath
Add-PaperPreviewReference -Status $offlineStatus -PreviewPath $paperPreviewPath
Add-OfflineSelectedPlanReference -Status $offlineStatus -ExecutionDay $tradingDate
$structuredCapture = Find-LatestStructuredCaptureManifest `
    -Root (Join-Path $tradingHome 'ths\opening_readonly') -TradingDate $tradingDate
Set-StatusProperty -Status $offlineStatus -Name 'ths_structured_session_manifest' -Value $(if ($structuredCapture) { $structuredCapture.manifest } else { $null })
Set-StatusProperty -Status $offlineStatus -Name 'ths_structured_session_receipt' -Value $(if ($structuredCapture) { $structuredCapture.receipt } else { $null })
Set-StatusProperty -Status $offlineStatus -Name 'ths_structured_session_bundle' -Value $(if ($structuredCapture) { $structuredCapture.bundle } else { $null })
Set-StatusProperty -Status $offlineStatus -Name 'ths_structured_session_cashflows' -Value $(if ($structuredCapture -and $structuredCapture.cashflows) { $structuredCapture.cashflows } else { $null })
# An opening pack with an explicit candidate is already a fixed paper
# selection.  When no separate selection receipt exists, preserve that choice
# in session status so the downstream offline package cannot reject a valid
# snapshot as a strategy mismatch.
if (-not [string]$offlineStatus.offline_paper_selected_strategy_id -and
    [string]$pack.selected_strategy_id) {
    Set-StatusProperty -Status $offlineStatus `
        -Name 'offline_paper_selected_strategy_id' `
        -Value ([string]$pack.selected_strategy_id)
}
$offlineStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
Ensure-OfflineDeskPreview -Status $offlineStatus
Ensure-ObservationBundlePreview -Status $offlineStatus

if ($OfflineOnly) {
    Set-StatusProperty -Status $offlineStatus -Name 'state' -Value 'offline_only_ready'
    Set-StatusProperty -Status $offlineStatus -Name 'execution_admitted' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'network_probe_attempted' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'account_observed' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'quotes_observed' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'client_interacted' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'ledger_mutated' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'submission_attempted' -Value $false
    Set-StatusProperty -Status $offlineStatus -Name 'next_action' -Value 'offline paper package ready; no account, quote or client interaction was attempted'
    Refresh-DeskPointers -Status $offlineStatus -DeskRoot ([string]$offlineStatus.offline_desk_output)
    # Persist the desk pointers before the optional package builder.  If that
    # downstream consumer fails, the next retry can still recover the exact
    # offline desk instead of reading a stale status without its snapshot path.
    $offlineStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    if (-not $NoOperatorPackage) {
        $packageBuilder = Join-Path $PSScriptRoot 'build_offline_operator_package.ps1'
        if (-not (Test-Path -LiteralPath $packageBuilder -PathType Leaf)) {
            throw "Offline operator package builder not found: $packageBuilder"
        }
        $packageOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $packageBuilder `
            -OpeningPack $OpeningPack -OutputRoot $OutputRoot -SkipPrepare 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "Offline operator package refresh failed: $($packageOutput -join ' ')"
        }
        $operatorPackagePath = Join-Path $runRoot 'offline_operator_package_v1.json'
        if (Test-Path -LiteralPath $operatorPackagePath -PathType Leaf) {
            Set-StatusProperty -Status $offlineStatus -Name 'offline_operator_package' -Value $operatorPackagePath
        }
    }
    $offlineStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    $offlineStatus | ConvertTo-Json -Depth 8
    exit 0
}

if ($today -ne $tradingDate) {
    if ($existingStatus -and $existingStatus.state -in @('waiting_for_trading_date', 'waiting_for_open_window')) {
        if (-not $existingStatus.PSObject.Properties['earliest_probe_time']) {
            $existingStatus | Add-Member -NotePropertyName earliest_probe_time -NotePropertyValue $EarliestProbeTime
        }
        if (-not $existingStatus.PSObject.Properties['account_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName account_observation_template -NotePropertyValue $accountTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['orders_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName orders_observation_template -NotePropertyValue $ordersTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['trades_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName trades_observation_template -NotePropertyValue $tradesTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['cashflows_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName cashflows_observation_template -NotePropertyValue $cashflowsTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['movements_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName movements_observation_template -NotePropertyValue $movementsTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['restriction_releases_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName restriction_releases_observation_template -NotePropertyValue $restrictionReleasesTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['share_credits_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName share_credits_observation_template -NotePropertyValue $shareCreditsTemplatePath
        }
        Add-PaperPreviewReference -Status $existingStatus -PreviewPath $paperPreviewPath
        Ensure-OfflineDeskPreview -Status $existingStatus
        Ensure-ObservationBundlePreview -Status $existingStatus
        Add-OfflineSelectedPlanReference -Status $existingStatus -ExecutionDay $tradingDate
        Refresh-DeskPointers -Status $existingStatus -DeskRoot ([string]$existingStatus.offline_desk_output)
        $existingStatus.next_action = "run on $tradingDate at or after $EarliestProbeTime local time; do not use historical quotes"
        $existingStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
        $existingStatus | ConvertTo-Json -Depth 8
        exit 0
    }
    $status = [ordered]@{
        schema = 'next_session_opening_runner_v1'
        state = 'waiting_for_trading_date'
        today = $today
        trading_date = $tradingDate
        opening_pack = $OpeningPack
        quote_request = $quoteRequest
        output_root = $runRoot
        account_observation_template = $accountTemplatePath
        orders_observation_template = $ordersTemplatePath
        trades_observation_template = $tradesTemplatePath
        cashflows_observation_template = $cashflowsTemplatePath
        movements_observation_template = $movementsTemplatePath
        restriction_releases_observation_template = $restrictionReleasesTemplatePath
        share_credits_observation_template = $shareCreditsTemplatePath
        earliest_probe_time = $EarliestProbeTime
        execution_admitted = $false
        network_probe_attempted = $false
        next_action = "run on $tradingDate at or after $EarliestProbeTime local time; do not use historical quotes"
    }
    Add-PaperPreviewReference -Status $status -PreviewPath $paperPreviewPath
    Ensure-OfflineDeskPreview -Status $status
    Ensure-ObservationBundlePreview -Status $status
    Refresh-DeskPointers -Status $status -DeskRoot ([string]$status.offline_desk_output)
    $status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    $status | ConvertTo-Json -Depth 8
    exit 0
}

try {
    $probeClock = [datetime]::ParseExact($EarliestProbeTime, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw "EarliestProbeTime must use HH:mm, got: $EarliestProbeTime"
}

if ($now.TimeOfDay -lt $probeClock.TimeOfDay -and -not $RepeatProbe) {
    if ($existingStatus -and $existingStatus.state -eq 'waiting_for_open_window') {
        if (-not $existingStatus.PSObject.Properties['account_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName account_observation_template -NotePropertyValue $accountTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['orders_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName orders_observation_template -NotePropertyValue $ordersTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['trades_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName trades_observation_template -NotePropertyValue $tradesTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['cashflows_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName cashflows_observation_template -NotePropertyValue $cashflowsTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['movements_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName movements_observation_template -NotePropertyValue $movementsTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['restriction_releases_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName restriction_releases_observation_template -NotePropertyValue $restrictionReleasesTemplatePath
        }
        if (-not $existingStatus.PSObject.Properties['share_credits_observation_template']) {
            $existingStatus | Add-Member -NotePropertyName share_credits_observation_template -NotePropertyValue $shareCreditsTemplatePath
        }
        Ensure-OfflineDeskPreview -Status $existingStatus
        Ensure-ObservationBundlePreview -Status $existingStatus
        Add-OfflineSelectedPlanReference -Status $existingStatus -ExecutionDay $tradingDate
        Refresh-DeskPointers -Status $existingStatus -DeskRoot ([string]$existingStatus.offline_desk_output)
        $existingStatus | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
        $existingStatus | ConvertTo-Json -Depth 8
        exit 0
    }
    $status = [ordered]@{
        schema = 'next_session_opening_runner_v1'
        state = 'waiting_for_open_window'
        today = $today
        trading_date = $tradingDate
        opening_pack = $OpeningPack
        quote_request = $quoteRequest
        output_root = $runRoot
        account_observation_template = $accountTemplatePath
        orders_observation_template = $ordersTemplatePath
        trades_observation_template = $tradesTemplatePath
        cashflows_observation_template = $cashflowsTemplatePath
        movements_observation_template = $movementsTemplatePath
        restriction_releases_observation_template = $restrictionReleasesTemplatePath
        share_credits_observation_template = $shareCreditsTemplatePath
        earliest_probe_time = $EarliestProbeTime
        execution_admitted = $false
        network_probe_attempted = $false
        next_action = "run again at or after $EarliestProbeTime local time; do not use pre-open or historical quotes"
    }
    Add-PaperPreviewReference -Status $status -PreviewPath $paperPreviewPath
    Ensure-OfflineDeskPreview -Status $status
    Ensure-ObservationBundlePreview -Status $status
    Refresh-DeskPointers -Status $status -DeskRoot ([string]$status.offline_desk_output)
    $status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
    $status | ConvertTo-Json -Depth 8
    exit 0
}

$probeStatus = $null
$probeExit = $null
$probeStatusPath = $null
if ($existingStatus -and $existingStatus.state -eq 'same_session_quotes_ready_account_missing' -and
    $AccountObservation -and (Test-Path -LiteralPath $existingStatus.quote_probe_status -PathType Leaf)) {
    $probeStatusPath = [string]$existingStatus.quote_probe_status
    $probeStatus = Get-Content -Raw -LiteralPath $probeStatusPath | ConvertFrom-Json
    $probeExit = [int]$existingStatus.probe_exit_code
} elseif ($existingStatus -and $existingStatus.state -notin @('waiting_for_trading_date', 'waiting_for_open_window') -and -not $RepeatProbe) {
    $existingStatus | ConvertTo-Json -Depth 8
    exit 0
} else {
    $probeOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $probe `
        -QuoteRequestCsv $quoteRequest -TradingDate $tradingDate -OutputRoot $probeRoot -Source $Source 2>&1)
    $probeExit = $LASTEXITCODE
    $probeStatusPath = Join-Path $probeRoot 'quote_probe_status.json'
    if (-not (Test-Path -LiteralPath $probeStatusPath -PathType Leaf)) {
        throw "Quote probe did not write status: $($probeOutput -join ' ')"
    }
    $probeStatus = Get-Content -Raw -LiteralPath $probeStatusPath | ConvertFrom-Json
}

$status = [ordered]@{
    schema = 'next_session_opening_runner_v1'
    state = [string]$probeStatus.state
    today = $today
    trading_date = $tradingDate
    opening_pack = $OpeningPack
    quote_request = $quoteRequest
    quote_probe_status = $probeStatusPath
    account_observation_template = $accountTemplatePath
    orders_observation_template = $ordersTemplatePath
    trades_observation_template = $tradesTemplatePath
    cashflows_observation_template = $cashflowsTemplatePath
    movements_observation_template = $movementsTemplatePath
    restriction_releases_observation_template = $restrictionReleasesTemplatePath
    share_credits_observation_template = $shareCreditsTemplatePath
    filled_quotes = $probeStatus.filled_quotes
    probe_exit_code = $probeExit
    output_root = $runRoot
    execution_admitted = $false
    account_observation_supplied = [bool]$AccountObservation
    manual_review_output = $null
    quote_only_desk_output = $null
    next_action = 'keep paper-only and review the quote/account evidence'
}
Add-PaperPreviewReference -Status $status -PreviewPath $paperPreviewPath
Ensure-OfflineDeskPreview -Status $status
Ensure-ObservationBundlePreview -Status $status

function Add-DeskEvidence {
    param(
        [System.Collections.IDictionary]$Status,
        [string]$DeskRoot
    )

    if (-not (Test-Path -LiteralPath $DeskRoot -PathType Container)) {
        return
    }

    $snapshotFile = Get-ChildItem -LiteralPath $DeskRoot -Recurse -Filter 'snapshot.json' -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $snapshotFile) {
        return
    }

    $snapshot = $null
    try {
        $snapshot = Get-Content -Raw -LiteralPath $snapshotFile.FullName | ConvertFrom-Json
    } catch {
        return
    }
    $boardFile = Join-Path $snapshotFile.DirectoryName 'operator_action_board.csv'
    $Status.trader_desk_snapshot = $snapshotFile.FullName
    $deskArtifacts = @(
        'blotter',
        'tca_pretrade',
        'risk_checks',
        'execution_schedule',
        'static_risk_summary',
        'paper_fill_projection',
        'paper_ledger_projection',
        'paper_order_lifecycle',
        'paper_position_projection',
        'paper_reconciliation',
        'paper_child_order_projection',
        'paper_child_order_reconciliation',
        'paper_child_order_reconciliation_summary',
        'paper_audit_event_log',
        'paper_audit_event_log_jsonl',
        'target_order_reconciliation',
        'paper_release_decision',
        'offline_operator_decision_queue',
        'offline_operator_decision_queue_json',
        'paper_recovery_handoff_index',
        'paper_next_session_carry',
        'paper_account_sizing_projection',
        'paper_account_sizing_summary',
        'strategy_promotion_card',
        'strategy_promotion_review',
        'strategy_promotion_review_csv',
        'strategy_evidence_card',
        'strategy_board_csv',
        'strategy_board_json',
        'strategy_account_sizing_matrix',
        'strategy_account_sizing_matrix_summary',
        'offline_portfolio_risk_summary',
        'allocation_settlement_projection',
        'scenario_analysis',
        'strategy_comparison',
        'paper_execution_attribution', 'paper_execution_attribution_csv',
        'paper_child_pressure_scenarios',
        'paper_execution_cost_stress',
        'function_matrix',
        'operator_action_board',
        'exception_alerts',
        'research_robustness',
        'research_robustness_json',
        'research_robustness_report'
    )
    foreach ($artifactName in $deskArtifacts) {
        if ($snapshot.artifacts -and $snapshot.artifacts.PSObject.Properties.Name -contains $artifactName) {
            $Status.("trader_desk_{0}" -f $artifactName) = [string]$snapshot.artifacts.$artifactName
        }
    }
    $executionAttributionPath = Join-Path $DeskRoot 'paper_execution_attribution_v1.json'
    if (Test-Path -LiteralPath $executionAttributionPath -PathType Leaf) {
        $Status.trader_desk_paper_execution_attribution = $executionAttributionPath
    }
    $childPressurePath = Join-Path $DeskRoot 'paper_child_pressure_scenarios_v1.csv'
    if (Test-Path -LiteralPath $childPressurePath -PathType Leaf) {
        $Status.trader_desk_paper_child_pressure_scenarios = $childPressurePath
    }
    if (-not (Test-Path -LiteralPath $boardFile -PathType Leaf)) {
        return
    }

    $rows = @(Import-Csv -LiteralPath $boardFile)
    $counts = [ordered]@{}
    foreach ($group in ($rows | Group-Object -Property state)) {
        $counts[[string]$group.Name] = [int]$group.Count
    }
    $Status.operator_action_board = $boardFile
    $Status.operator_action_board_total = [int]$rows.Count
    $Status.operator_action_board_blocked = [int]@($rows | Where-Object state -eq 'blocked').Count
    $Status.operator_action_board_actionable = [int]@($rows | Where-Object state -eq 'actionable').Count
    $Status.operator_action_board_state_counts = $counts
}

if ($probeStatus.state -eq 'same_session_quotes_ready' -and $AccountObservation) {
    $filledQuotes = [string]$probeStatus.filled_quotes
    Publish-QuoteObservationDrop -Status $status -FilledQuotesPath $filledQuotes -Source ([string]$probeStatus.source) -ObservedAt ([string]$probeStatus.observed_at) -TradingDate $tradingDate
    $manualRoot = Join-Path $runRoot 'manual_review'
    $reviewOutput = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $manualReview `
        -OpeningPack $OpeningPack -AccountObservation $AccountObservation -QuotesCsv $filledQuotes `
        -QuoteSource ([string]$probeStatus.source) -QuoteObservedAt ([string]$probeStatus.observed_at) `
        -TradingDate $tradingDate -Strategy $Strategy -OutputRoot $manualRoot 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Manual paper review failed: $($reviewOutput -join ' ')"
    }
    $status.state = 'paper_review_prepared'
    $status.manual_review_output = $manualRoot
    Add-DeskEvidence -Status $status -DeskRoot $manualRoot
    $status.next_action = 'review the account-sized paper preview; no order submission was attempted'
} elseif ($probeStatus.state -eq 'same_session_quotes_ready') {
    # Produce a useful same-session paper desk even before account sizing is
    # available. This keeps quote drift, risk checks, recovery controls, and
    # institutional-function coverage consumable without inventing balances or
    # creating an order/fill record.
    $quoteOnlyRoot = Join-Path $runRoot ("quote_only_desk_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    Publish-QuoteObservationDrop -Status $status -FilledQuotesPath ([string]$probeStatus.filled_quotes) -Source ([string]$probeStatus.source) -ObservedAt ([string]$probeStatus.observed_at) -TradingDate $tradingDate
    $deskOutput = @(& $python $committee `
        'trader-desk-snapshot' `
        '--opening-pack' $OpeningPack `
        '--quotes-csv' ([string]$probeStatus.filled_quotes) `
        '--quote-source' ([string]$probeStatus.source) `
        '--quote-observed-at' ([string]$probeStatus.observed_at) `
        '--quote-trading-date' $tradingDate `
        '--strategy' $Strategy `
        '--sizing' 'replay_shares' `
        '--execution-scope' $ExecutionScope `
        '--out' $quoteOnlyRoot 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Quote-only trader desk snapshot failed: $($deskOutput -join ' ')"
    }
    $status.state = 'same_session_quotes_ready_account_missing'
    $status.quote_only_desk_output = $quoteOnlyRoot
    Add-DeskEvidence -Status $status -DeskRoot $quoteOnlyRoot
    $status.next_action = 'supply a normalized account observation for account-sized paper preview'
}

$status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8
$status | ConvertTo-Json -Depth 8
exit 0
