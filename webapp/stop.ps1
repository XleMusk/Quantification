$ErrorActionPreference = 'Stop'
$StatePath = Join-Path $PSScriptRoot '.runtime\processes.json'
if (!(Test-Path -LiteralPath $StatePath)) { Write-Output 'No local process record.'; exit 0 }
$State = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
$Project = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'process_identity.ps1')
if ($State.project -ne $Project) { throw 'Project identity mismatch; no processes stopped.' }
$ManagedRecords = @(Get-QuantProcessRecords $State.processes $Project)
foreach ($Item in $State.processes) {
    if ($Item.id -notin $ManagedRecords.id) { Write-Output "Stale $($Item.role) PID $($Item.id); not stopped." }
}
foreach ($Item in $ManagedRecords) {
    $Process = Get-QuantManagedProcess $Item $Project
    if (!$Process) { Write-Output "Stale $($Item.role) PID $($Item.id); not stopped."; continue }
    Stop-Process -Id $Item.id -Force
    Write-Output "Stopped $($Item.role) PID $($Item.id)"
}
$Archive = Join-Path $PSScriptRoot '.runtime\archive'
New-Item -ItemType Directory -Force -Path $Archive | Out-Null
Move-Item -LiteralPath $StatePath -Destination (Join-Path $Archive ("processes_" + (Get-Date -Format 'yyyyMMdd_HHmmss_fffffff') + '.json'))
