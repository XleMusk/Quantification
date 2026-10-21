[CmdletBinding()]
param(
    [string]$TaskName = 'Quantification Readonly Workbench',
    [string]$TradingHome = 'D:\QuantificationData\trading'
)
$ErrorActionPreference = 'Stop'
$ScriptPath = Join-Path $PSScriptRoot 'ensure_readonly_workbench.ps1'
$PowerShell = (Get-Command powershell.exe).Source
$argument = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -TradingHome `"$TradingHome`""
$action = New-ScheduledTaskAction -Execute $PowerShell -Argument $argument -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
Write-Output "Registered scheduled task: $TaskName"
Write-Output "Action: $PowerShell $argument"
Write-Output "Frequency: hourly; research-only workbench; workers disabled"
