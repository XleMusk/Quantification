param(
    [Parameter(Mandatory = $true)]
    [string]$Profile,
    [Parameter(Mandatory = $true)]
    [string]$TradingDate,
    [string]$OutputRoot = 'D:\QuantificationData\trading\ths\opening_readonly',
    [switch]$OCRCandidates,
    [switch]$StructuredSession,
    [string]$CashflowsCsv,
    [switch]$RepeatCapture
)

$ErrorActionPreference = 'Stop'
$python = 'D:\Quantification\env\python.exe'
$committee = 'D:\Quantification\run_committee.py'

if (-not (Test-Path -LiteralPath $Profile -PathType Leaf)) {
    throw "Missing THS profile: $Profile"
}
if ($TradingDate -ne (Get-Date).ToString('yyyy-MM-dd')) {
    throw "Read-only capture is restricted to today's trading date: $TradingDate"
}

# The profile is UTF-8 without a BOM. Windows PowerShell otherwise applies the
# local ANSI code page and can corrupt the escaped executable path before JSON parsing.
$profileData = Get-Content -Raw -Encoding UTF8 -LiteralPath $Profile | ConvertFrom-Json
$target = $profileData.target
if (-not $target -or $profileData.summary_verified -ne $true) {
    throw 'THS profile must contain the verified target and summary mapping'
}
if ($CashflowsCsv -and -not $StructuredSession) {
    throw '-CashflowsCsv requires -StructuredSession so the export stays bound to one structured capture'
}
if ($CashflowsCsv -and (-not (Test-Path -LiteralPath $CashflowsCsv -PathType Leaf))) {
    throw "Missing cashflow export: $CashflowsCsv"
}

$dayRoot = Join-Path $OutputRoot $TradingDate
if (-not $RepeatCapture -and (Test-Path -LiteralPath $dayRoot -PathType Container)) {
    $existingManifest = Get-ChildItem -LiteralPath $dayRoot -Filter 'manifest.json' -File -Recurse |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($existingManifest) {
        # A successful same-day capture is immutable by default.  This keeps
        # the heartbeat from repeatedly touching the client during a session.
        Get-Content -Raw -LiteralPath $existingManifest.FullName
        exit 0
    }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$root = Join-Path $dayRoot $stamp
New-Item -ItemType Directory -Force -Path $root | Out-Null

function Invoke-Committee {
    param([string[]]$Arguments)

    $output = @(& $python @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "run_committee failed with exit code $LASTEXITCODE`: $($output -join ' ')"
    }
}

$summary = Join-Path $root 'native_summary.json'
Invoke-Committee @($committee, 'ths', 'capture-summary-background', '--profile', $Profile, '--out', $summary)

$structuredSessionReceipt = $null
$structuredSessionBundle = $null
$structuredCashflowsCsv = $null
if ($StructuredSession) {
    # This is an explicit read-only structured path.  The operator must place
    # each mapped query page in the foreground; capture-session never navigates,
    # confirms, submits or mutates the Ledger.  Cashflows remain a separate
    # user-export input because the client page is not a cumulative cashflow
    # proof.
    $structuredSessionBundle = Join-Path $root 'structured_bundle'
    $structuredSessionReceipt = Join-Path $root 'structured_session_receipt.json'
    $captureArguments = @(
        $committee, 'ths', 'capture-session', '--profile', $Profile,
        '--kinds', 'account', 'orders', 'trades',
        '--trading-date', $TradingDate, '--wait-seconds', '5',
        '--bundle-dir', $structuredSessionBundle, '--out', $structuredSessionReceipt
    )
    if ($CashflowsCsv) {
        $structuredCashflowsCsv = Join-Path $structuredSessionBundle 'cashflows.csv'
        $captureArguments += @('--cashflows-csv', $CashflowsCsv)
    }
    Invoke-Committee $captureArguments
}

$grids = [ordered]@{}
$ocr = [ordered]@{}
$gridErrors = [ordered]@{}
foreach ($kind in @('holdings', 'orders', 'trades')) {
    try {
        $image = Join-Path $root ("{0}.png" -f $kind)
        $receipt = Join-Path $root ("{0}_grid_receipt.json" -f $kind)
        Invoke-Committee @(
            $committee, 'ths', 'capture-grid-image', '--profile', $Profile,
            '--kind', $kind, '--image-out', $image, '--out', $receipt
        )
        $grids[$kind] = [ordered]@{
            image = $image
            receipt = $receipt
        }

        $ocrAvailable = $false
        if ($profileData.ocr -and $profileData.ocr.PSObject.Properties.Name -contains $kind) {
            $ocrAvailable = $true
        } elseif ($kind -in @('orders', 'trades') -and $profileData.ocr -and
            $profileData.ocr.PSObject.Properties.Name -contains 'activity') {
            # The CLI resolves the verified compact activity mapping while
            # retaining the requested orders/trades semantic kind.
            $ocrAvailable = $true
        }
        if ($OCRCandidates -and $ocrAvailable) {
            $candidate = Join-Path $root ("{0}_ocr_candidate.json" -f $kind)
            Invoke-Committee @(
                $committee, 'ths', 'ocr-grid-image', '--profile', $Profile,
                '--kind', $kind, '--image', $image, '--summary', $summary, '--out', $candidate
            )
            $ocr[$kind] = $candidate
        }
    } catch {
        # Preserve successful read-only evidence when a later virtual grid is
        # unavailable because the client page changed or refreshed.
        $gridErrors[$kind] = $_.Exception.Message
    }
}

$manifest = [ordered]@{
    schema = 'ths_opening_readonly_capture_v1'
    trading_date = $TradingDate
    observed_at = (Get-Date).ToString('o')
    profile = (Resolve-Path -LiteralPath $Profile).Path
    summary = $summary
    structured_session_receipt = $structuredSessionReceipt
    structured_session_bundle = $structuredSessionBundle
    structured_cashflows_csv = $structuredCashflowsCsv
    grids = $grids
    grid_errors = $gridErrors
    ocr_candidates = $ocr
    read_only = $true
    read_only_complete = ($gridErrors.Count -eq 0)
    focused = $false
    submitted = $false
    execution_admitted = $false
    ledger_mutated = $false
    next_action = if ($gridErrors.Count -eq 0) {
        'Review receipts and explicit exports; do not promote OCR or infer orders/trades.'
    } else {
        'Foreground the verified THS trading query page and recapture only the missing grids; existing evidence remains read-only and incomplete.'
    }
}
$manifestPath = Join-Path $root 'manifest.json'
$manifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
$manifest | ConvertTo-Json -Depth 8
