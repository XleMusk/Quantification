param(
    [Parameter(Mandatory = $true)]
    [string]$QuoteRequestCsv,
    [Parameter(Mandatory = $true)]
    [string]$TradingDate,
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [string]$Source = 'sina_hq_raw',
    [string]$FallbackSource = 'tencent_hq_raw'
)

$ErrorActionPreference = 'Stop'
$Invariant = [Globalization.CultureInfo]::InvariantCulture

if (-not (Test-Path -LiteralPath $QuoteRequestCsv -PathType Leaf)) {
    throw "Missing quote request CSV: $QuoteRequestCsv"
}
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$rows = @(Import-Csv -LiteralPath $QuoteRequestCsv)
if ($rows.Count -eq 0) { throw 'Quote request CSV is empty' }

$symbols = @()
foreach ($row in $rows) {
    if ($row.symbol -notmatch '^\d{6}$' -or $row.market -notin @('SSE', 'SZSE')) {
        throw "Unsupported quote request row: $($row.symbol) $($row.market)"
    }
    $prefix = if ($row.market -eq 'SSE') { 'sh' } else { 'sz' }
    $symbols += "$prefix$($row.symbol)"
}

$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMdd_HHmmssZ')
$rawPath = Join-Path $OutputRoot "sina_raw_$stamp.txt"
$fallbackRawPath = Join-Path $OutputRoot "tencent_raw_$stamp.txt"
$statusPath = Join-Path $OutputRoot 'quote_probe_status.json'
$filledPath = Join-Path $OutputRoot 'filled_quotes.csv'
$sourceAttempts = @()
$sourceErrors = @()
$sourceUsed = $Source
$observations = @{}
$rawPaths = @()

function Read-SinaObservations {
    param([string]$Content, [string]$Provider = 'sina_hq_raw')
    $result = @{}
    foreach ($match in [regex]::Matches($Content, 'var hq_str_(sh|sz)(\d{6})="([^"]*)";')) {
        $market = if ($match.Groups[1].Value -eq 'sh') { 'SSE' } else { 'SZSE' }
        $symbol = $match.Groups[2].Value
        $parts = $match.Groups[3].Value -split ','
        if ($parts.Count -lt 32 -or $parts[3] -notmatch '^\d+(?:\.\d+)?$') { continue }
        $result["$symbol.$market"] = [ordered]@{
            symbol = $symbol; market = $market; name = $parts[0]; price_rmb = $parts[3]
            source = $Provider; source_as_of_date = $parts[30]; source_as_of_time = $parts[31]
        }
    }
    return ,$result
}

function Read-TencentObservations {
    param([string]$Content, [string]$Provider = 'tencent_hq_raw')
    $result = @{}
    foreach ($match in [regex]::Matches($Content, 'v_(sh|sz)(\d{6})="([^"]*)";')) {
        $market = if ($match.Groups[1].Value -eq 'sh') { 'SSE' } else { 'SZSE' }
        $symbol = $match.Groups[2].Value
        $parts = $match.Groups[3].Value -split '~'
        if ($parts.Count -lt 31 -or $parts[3] -notmatch '^\d+(?:\.\d+)?$' -or
            $parts[30] -notmatch '^\d{14}$') { continue }
        $stampText = $parts[30]
        $result["$symbol.$market"] = [ordered]@{
            symbol = $symbol; market = $market; name = $parts[1]; price_rmb = $parts[3]
            source = $Provider
            source_as_of_date = "$($stampText.Substring(0, 4))-$($stampText.Substring(4, 2))-$($stampText.Substring(6, 2))"
            source_as_of_time = "$($stampText.Substring(8, 2)):$($stampText.Substring(10, 2)):$($stampText.Substring(12, 2))"
        }
    }
    return ,$result
}

function Assess-Observations {
    param([hashtable]$Candidate)
    $missing = @($rows | Where-Object { -not $Candidate.ContainsKey("$($_.symbol).$($_.market)") } |
        ForEach-Object { "$($_.symbol).$($_.market)" })
    $stale = @($Candidate.Values | Where-Object { $_.source_as_of_date -ne $TradingDate } |
        ForEach-Object { "$($_.symbol).$($_.market):$($_.source_as_of_date) $($_.source_as_of_time)" })
    [pscustomobject]@{
        state = if ($missing.Count -gt 0) { 'missing_symbols' } elseif ($stale.Count -gt 0) { 'stale_or_not_same_session' } else { 'same_session_quotes_ready' }
        missing = $missing
        stale = $stale
    }
}

$sinaUri = 'https://hq.sinajs.cn/list=' + ($symbols -join ',')
try {
    $response = Invoke-WebRequest -Uri $sinaUri -Headers @{ Referer = 'https://finance.sina.com.cn/' } -TimeoutSec 15 -UseBasicParsing
    [IO.File]::WriteAllText($rawPath, $response.Content, [Text.Encoding]::UTF8)
    $rawPaths += $rawPath
    $sourceAttempts += $Source
    $observations = Read-SinaObservations $response.Content $Source
} catch {
    $sourceErrors += "${Source}: $($_.Exception.Message)"
}
$assessment = Assess-Observations $observations
$primaryObservations = $observations

if ($assessment.state -ne 'same_session_quotes_ready' -and $FallbackSource) {
    $tencentSymbols = @($rows | ForEach-Object { if ($_.market -eq 'SSE') { "sh$($_.symbol)" } else { "sz$($_.symbol)" } })
    $tencentUri = 'https://qt.gtimg.cn/q=' + ($tencentSymbols -join ',')
    try {
        $fallbackResponse = Invoke-WebRequest -Uri $tencentUri -TimeoutSec 15 -UseBasicParsing
        [IO.File]::WriteAllText($fallbackRawPath, $fallbackResponse.Content, [Text.Encoding]::UTF8)
        $rawPaths += $fallbackRawPath
        $sourceAttempts += $FallbackSource
        $fallbackObservations = Read-TencentObservations $fallbackResponse.Content $FallbackSource
        $fallbackAssessment = Assess-Observations $fallbackObservations
        if ($fallbackAssessment.state -eq 'same_session_quotes_ready' -or
            $assessment.state -eq 'source_error') {
            $observations = $fallbackObservations
            $assessment = $fallbackAssessment
            $sourceUsed = $FallbackSource
        } else {
            # A provider can omit different symbols. Combine only records whose
            # source date is the requested trading date; never promote stale data.
            $freshUnion = @{}
            foreach ($candidateSet in @($primaryObservations, $fallbackObservations)) {
                foreach ($key in $candidateSet.Keys) {
                    $candidate = $candidateSet[$key]
                    if ($candidate.source_as_of_date -eq $TradingDate -and
                        -not $freshUnion.ContainsKey($key)) {
                        $freshUnion[$key] = $candidate
                    }
                }
            }
            $unionAssessment = Assess-Observations $freshUnion
            if ($unionAssessment.state -eq 'same_session_quotes_ready') {
                $observations = $freshUnion
                $assessment = $unionAssessment
                $sourceUsed = "$Source+$FallbackSource"
            }
        }
    } catch {
        $sourceErrors += "${FallbackSource}: $($_.Exception.Message)"
    }
}

$state = if ($sourceAttempts.Count -eq 0) { 'source_error' } else { $assessment.state }
$status = [ordered]@{
    schema = 'opening_quote_probe_v2'
    state = $state
    source = $sourceUsed
    requested_source = $Source
    source_attempts = $sourceAttempts
    source_errors = $sourceErrors
    trading_date = $TradingDate
    observed_at = (Get-Date).ToUniversalTime().ToString('o')
    requested_count = $rows.Count
    received_count = $observations.Count
    missing_symbols = $assessment.missing
    stale_symbols = $assessment.stale
    raw_response = if ($rawPaths.Count -gt 0) { $rawPaths[-1] } else { $null }
    raw_responses = $rawPaths
    filled_quotes = if ($state -eq 'same_session_quotes_ready') { $filledPath } else { $null }
    execution_admitted = $false
}
$status | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statusPath -Encoding UTF8

if ($state -ne 'same_session_quotes_ready') {
    Remove-Item -LiteralPath $filledPath -Force -ErrorAction SilentlyContinue
    Write-Output ($status | ConvertTo-Json -Depth 8)
    exit 2
}

$outputRows = foreach ($row in $rows) {
    $quote = $observations["$($row.symbol).$($row.market)"]
    [pscustomobject]@{
        symbol = $row.symbol
        market = $row.market
        price_rmb = $quote.price_rmb
        strategy_ids = $row.strategy_ids
        sides = $row.sides
        candidate_qty_by_strategy = $row.candidate_qty_by_strategy
        research_reference_prices_rmb = $row.research_reference_prices_rmb
        status = 'observed_same_session'
        price_basis_required = 'raw_rmb'
        quote_source = $quote.source
        source_as_of_date = $quote.source_as_of_date
        source_as_of_time = $quote.source_as_of_time
    }
}
$outputRows | Export-Csv -LiteralPath $filledPath -NoTypeInformation -Encoding UTF8
Write-Output ($status | ConvertTo-Json -Depth 8)
