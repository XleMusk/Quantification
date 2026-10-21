$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $Python)) {
    & uv venv --python (Join-Path $Project 'quant_learning_starter\.venv\Scripts\python.exe') (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed.' }
}
& uv pip sync --python $Python (Join-Path $PSScriptRoot 'backend\requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
Push-Location (Join-Path $PSScriptRoot 'frontend')
try {
    & npm.cmd ci --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    & npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
} finally { Pop-Location }
& uv pip check --python $Python
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }
