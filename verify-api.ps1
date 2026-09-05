param(
    [switch]$Database
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run .\setup.ps1 first." }
$env:PYTHONPATH = Join-Path $Root "backend"

Write-Host "Running Phase 10 API regression tests..."
& .\.venv\Scripts\python.exe -m pytest -q backend\tests\test_api_phase10.py backend\tests\test_app.py
if ($LASTEXITCODE -ne 0) { throw "Phase 10 API tests failed." }

$Arguments = @("-m", "newsintel.api_cli", "doctor", "--output", "artifacts\phase10\api-doctor.json")
if ($Database) { $Arguments += "--database" }
& .\.venv\Scripts\python.exe @Arguments
if ($LASTEXITCODE -ne 0) { throw "Phase 10 API doctor failed." }

Write-Host "Phase 10 API verification passed."
