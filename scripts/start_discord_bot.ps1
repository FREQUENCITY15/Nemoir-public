param(
    [switch]$ConfirmLive
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot\..

if (-not $ConfirmLive) {
    throw 'Refusing to connect Discord without -ConfirmLive.'
}

$Python = Join-Path $PWD '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw 'Local virtual environment not found. Follow README.md setup first.'
}

& $Python -m nemoir discord --confirm-live
exit $LASTEXITCODE
