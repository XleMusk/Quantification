param(
    [string]$Python = '3.11',
    [switch]$BackendOnly
)

$ErrorActionPreference = 'Stop'
$EnvironmentPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $EnvironmentPython)) {
    & uv venv --python $Python --no-python-downloads (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed.' }
}
& $EnvironmentPython -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 'The locked backend requires Python 3.11; the existing environment was not changed.')"
if ($LASTEXITCODE -ne 0) { throw 'Backend Python version check failed.' }
& uv pip sync --python $EnvironmentPython (Join-Path $PSScriptRoot 'backend\requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
if (!$BackendOnly) {
    Push-Location (Join-Path $PSScriptRoot 'frontend')
    try {
        & npm.cmd ci --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally { Pop-Location }
}
& uv pip check --python $EnvironmentPython
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }
Write-Output "Backend ready: $EnvironmentPython"
