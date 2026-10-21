function Test-QuantProcessIdentity($Item, $Process, [string]$Project) {
    if (!$Process -or [int]$Process.ProcessId -ne [int]$Item.id -or !$Process.CommandLine) { return $false }
    $Expected = switch ($Item.role) {
        'api' { 'webapp.backend.app.main:app' }
        'worker' { 'webapp.workers.factor_worker' }
        'trading' { 'trading.worker' }
        'ui' { Join-Path $Project 'webapp\frontend\node_modules\vite\bin\vite.js' }
        default { throw 'Unknown process role.' }
    }
    $Command = $Process.CommandLine.Replace('/', '\').ToLowerInvariant()
    $ProjectRoot = ([IO.Path]::GetFullPath($Project)).TrimEnd('\').ToLowerInvariant()
    $ProjectMatch = $Command.Contains($ProjectRoot + '\') -or
        $Command.Contains('--app-dir "' + $ProjectRoot + '"') -or
        $Command.Contains('--app-dir ' + $ProjectRoot + ' ') -or
        $Command.EndsWith('--app-dir ' + $ProjectRoot)
    if (!$Command.Contains($Expected.Replace('/', '\').ToLowerInvariant()) -or
        !$ProjectMatch) { return $false }
    if ($Item.created_at) {
        try {
            if (([datetimeoffset]$Item.created_at).UtcDateTime.Ticks -ne
                ([datetimeoffset]$Process.CreationDate).UtcDateTime.Ticks) { return $false }
        } catch { return $false }
    }
    return $true
}

function Get-QuantManagedProcess($Item, [string]$Project) {
    $ProcessId = [int]$Item.id
    if ($ProcessId -le 0) { throw 'Invalid process identifier.' }
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (Test-QuantProcessIdentity $Item $Process $Project) { return $Process }
    return $null
}

function New-QuantProcessRecord([string]$Role, $Process) {
    $Current = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.Id)"
    if (!$Current) { throw "New $Role process exited before registration." }
    return @{ role=$Role; id=$Process.Id; created_at=$Current.CreationDate.ToUniversalTime().ToString('o') }
}

function Get-QuantProcessRecords($Items, [string]$Project) {
    $Pending = [System.Collections.Queue]::new()
    foreach ($Item in $Items) { $Pending.Enqueue($Item) }
    $Seen = @{}
    while ($Pending.Count) {
        $Item = $Pending.Dequeue()
        if ($Seen.ContainsKey([int]$Item.id)) { continue }
        $Managed = Get-QuantManagedProcess $Item $Project
        if (!$Managed) { continue }
        $Seen[[int]$Item.id] = $true
        $Item
        # Windows virtualenv launchers can leave a same-command child behind.
        foreach ($Child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($Managed.ProcessId)")) {
            if ($Child.CreationDate -lt $Managed.CreationDate) { continue }
            $Record = @{ role=$Item.role; id=$Child.ProcessId;
                         created_at=$Child.CreationDate.ToUniversalTime().ToString('o') }
            if (Test-QuantProcessIdentity $Record $Child $Project) { $Pending.Enqueue($Record) }
        }
    }
}
