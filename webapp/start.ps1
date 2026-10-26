param([int]$UiPort = 5175, [int]$ApiPort = 8765,
      [string]$TradingHome = $env:QUANT_TRADING_HOME, [switch]$NoWorkers)
$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$Project = Split-Path -Parent $Root
. (Join-Path $Root 'process_identity.ps1')
if (!$TradingHome) { $TradingHome = 'D:\QuantificationData\trading' }
$TradingHome = [IO.Path]::GetFullPath($TradingHome)
$Runtime = Join-Path $Root '.runtime'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$WorkerPython = Join-Path $Project 'env\python.exe'
$Node = (Get-Command node.exe).Source
$Vite = Join-Path $Root 'frontend\node_modules\vite\bin\vite.js'
if (!(Test-Path -LiteralPath $Python) -or !(Test-Path -LiteralPath $Vite)) { throw 'Run setup.ps1 first.' }
New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
$StatePath = Join-Path $Runtime 'processes.json'
if (Test-Path -LiteralPath $StatePath) {
    $Old = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    if ($Old.project -ne $Project) { throw 'Project identity mismatch; no processes changed.' }
    $Running = @($Old.processes | Where-Object { Get-QuantManagedProcess $_ $Project })
    if ($Running.Count -gt 0) { throw "Existing process record: use stop.ps1 before restart. URL: $($Old.url)" }
    Write-Output 'Previous process record is stale; no old process will be stopped.'
}
function Get-FreePort([int]$First) {
    foreach ($Candidate in $First..($First + 30)) {
        $Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Candidate)
        try { $Listener.Start(); return $Candidate } catch [System.Net.Sockets.SocketException] { } finally { $Listener.Stop() }
    }
    throw 'No free local port in requested range.'
}
$ApiPort = Get-FreePort $ApiPort
$UiPort = Get-FreePort $UiPort
if ($UiPort -eq $ApiPort) { $UiPort = Get-FreePort ($UiPort + 1) }
$env:QUANT_API_PORT = "$ApiPort"
$env:QUANT_UI_PORT = "$UiPort"
$env:QUANT_UI_ORIGIN = "http://127.0.0.1:$UiPort"
$env:PYTHONUTF8 = '1'
$env:OMP_NUM_THREADS = '2'
$env:QUANT_WEB_STORAGE = Join-Path $Root 'storage'
$env:QUANT_TRADING_HOME = $TradingHome
Push-Location $Project
try {
    & $Python -m webapp.backend.app.seed
    if ($LASTEXITCODE -ne 0) { throw 'Dataset registration failed.' }
} finally { Pop-Location }
$Started = @()
try {
    $Api = Start-Process -FilePath $Python -ArgumentList @('-m','uvicorn','webapp.backend.app.main:app','--app-dir',"`"$Project`"",'--host','127.0.0.1','--port',"$ApiPort") -WorkingDirectory $Project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Runtime 'api.log') -RedirectStandardError (Join-Path $Runtime 'api.err.log')
    $Started += New-QuantProcessRecord 'api' $Api
    if (!$NoWorkers) {
        $Worker = Start-Process -FilePath $WorkerPython -ArgumentList @('-m','webapp.workers.factor_worker') -WorkingDirectory $Project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Runtime 'worker.log') -RedirectStandardError (Join-Path $Runtime 'worker.err.log')
        $Started += New-QuantProcessRecord 'worker' $Worker
        $Trading = Start-Process -FilePath $WorkerPython -ArgumentList @('-m','trading.worker') -WorkingDirectory $Project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Runtime 'trading.log') -RedirectStandardError (Join-Path $Runtime 'trading.err.log')
        $Started += New-QuantProcessRecord 'trading' $Trading
    }
    $PreviousCI = $env:CI
    try {
        # Vite otherwise closes when a detached parent's stdin reaches EOF.
        $env:CI = 'true'
        $Ui = Start-Process -FilePath $Node -ArgumentList @("`"$Vite`"",'--host','127.0.0.1','--port',"$UiPort",'--strictPort') -WorkingDirectory (Join-Path $Root 'frontend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Runtime 'ui.log') -RedirectStandardError (Join-Path $Runtime 'ui.err.log')
    } finally { $env:CI = $PreviousCI }
    $Started += New-QuantProcessRecord 'ui' $Ui
    $Ready = $false
    for ($i=0; $i -lt 80; $i++) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/health" -TimeoutSec 2
            $Page = Invoke-WebRequest -Uri $env:QUANT_UI_ORIGIN -TimeoutSec 2 -UseBasicParsing
            $TradingHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/api/trading/overview" -TimeoutSec 2
            $WorkersReady = $NoWorkers -or ($Health.worker.online -and $TradingHealth.worker.online)
            if ($Health.status -eq 'ok' -and $WorkersReady -and $Page.StatusCode -eq 200 -and
                $TradingHealth.data_root -eq $TradingHome) { $Ready=$true; break }
        } catch { }
        Start-Sleep -Milliseconds 300
    }
    if (!$Ready) { throw 'Startup did not become healthy. Inspect .runtime logs.' }
    $Started = @(Get-QuantProcessRecords $Started $Project)
    if (Test-Path -LiteralPath $StatePath) {
        $Archive = Join-Path $Runtime 'archive'
        New-Item -ItemType Directory -Force -Path $Archive | Out-Null
        Copy-Item -LiteralPath $StatePath -Destination (Join-Path $Archive ("processes_" + (Get-Date -Format 'yyyyMMdd_HHmmss_fffffff') + '.json'))
    }
    @{ url=$env:QUANT_UI_ORIGIN; api="http://127.0.0.1:$ApiPort"; processes=$Started; project=$Project;
       trading_home=$TradingHome; workers_started=(!$NoWorkers) } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8
    Write-Output "READY $env:QUANT_UI_ORIGIN"
} catch {
    foreach ($Item in @(Get-QuantProcessRecords $Started $Project)) {
        $Managed = Get-QuantManagedProcess $Item $Project
        if ($Managed) { Stop-Process -Id $Managed.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    throw
}
