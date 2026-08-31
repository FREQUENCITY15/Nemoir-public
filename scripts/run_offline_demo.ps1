$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot\..

$Python = Join-Path $PWD '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw 'Local virtual environment not found. Follow README.md setup first.'
}

& $Python -m nemoir demo-offline --database 'data\demo.sqlite3'
exit $LASTEXITCODE
