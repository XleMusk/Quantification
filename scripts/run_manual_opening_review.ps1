param(
    [Parameter(Mandatory = $true)]
    [string]$OpeningPack,
    [Parameter(Mandatory = $true)]
    [string]$AccountObservation,
    [Parameter(Mandatory = $true)]
    [string]$QuotesCsv,
    [Parameter(Mandatory = $true)]
    [string]$QuoteSource,
    [Parameter(Mandatory = $true)]
    [string]$QuoteObservedAt,
    [Parameter(Mandatory = $true)]
    [string]$TradingDate,
    [string]$Strategy = 'momentum_12_1_20',
    [string]$OutputRoot = 'D:\QuantificationData\trading\research\manual_opening_review'
)

$ErrorActionPreference = 'Stop'
$python = 'D:\Quantification\env\python.exe'
$runner = 'D:\Quantification\run_committee.py'

foreach ($path in @($OpeningPack, $AccountObservation, $QuotesCsv)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing input file: $path"
    }
}

$pack = Get-Content -Raw -LiteralPath $OpeningPack | ConvertFrom-Json
$account = Get-Content -Raw -LiteralPath $AccountObservation | ConvertFrom-Json
if ([string]$pack.execution_day -ne $TradingDate) {
    throw "Trading date $TradingDate does not match opening pack execution day $($pack.execution_day)"
}
if ([string]$account.trading_date -ne $TradingDate) {
    throw "Account observation trading_date $($account.trading_date) does not match trading date $TradingDate"
}

$quoteRows = @(Import-Csv -LiteralPath $QuotesCsv)
if ($quoteRows.Count -eq 0) { throw "Quote CSV is empty: $QuotesCsv" }
if ($quoteRows[0].PSObject.Properties.Name -contains 'source_as_of_date') {
    $stale = @($quoteRows | Where-Object {
        $_.source_as_of_date -ne $TradingDate -or $_.status -ne 'observed_same_session'
    })
    if ($stale.Count -gt 0) {
        throw "Quote CSV contains stale or unverified source rows for trading date $TradingDate"
    }
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$quoteObservation = Join-Path $OutputRoot ("quote_observation_{0}.json" -f $stamp)
$preview = Join-Path $OutputRoot ("opening_preview_{0}.json" -f $stamp)
$deskOutput = Join-Path $OutputRoot ("trader_desk_snapshot_{0}" -f $stamp)

function Invoke-Committee {
    param([string[]]$Arguments)
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "run_committee failed with exit code $LASTEXITCODE"
    }
}

$quoteArgs = @(
    $runner, 'ths', 'build-quote-observation',
    '--csv', $QuotesCsv,
    '--source', $QuoteSource,
    '--observed-at', $QuoteObservedAt,
    '--trading-date', $TradingDate,
    '--out', $quoteObservation
)
Invoke-Committee $quoteArgs

$prepareArgs = @(
    $runner, 'ths', 'prepare-opening-pack',
    '--account-observation', $AccountObservation,
    '--pack', $OpeningPack,
    '--quotes', $quoteObservation,
    '--strategy', $Strategy,
    '--sizing', 'observed_account',
    '--execution-scope', 'mainboard_only',
    '--out', $preview
)
Invoke-Committee $prepareArgs

$deskArgs = @(
    $runner, 'trader-desk-snapshot',
    '--opening-pack', $OpeningPack,
    '--account-observation', $AccountObservation,
    '--quotes', $quoteObservation,
    '--strategy', $Strategy,
    '--sizing', 'observed_account',
    '--execution-scope', 'mainboard_only',
    '--out', $deskOutput
)
Invoke-Committee $deskArgs

Write-Output "Quote observation: $quoteObservation"
Write-Output "Account-sized preview: $preview"
Write-Output "Trader desk snapshot: $deskOutput"
Write-Output "No order was saved, submitted, cancelled, or transferred by this script."
