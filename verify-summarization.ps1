param(
    [switch]$Database,
    [switch]$LoadModel,
    [switch]$RunOnce
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Run .\setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "backend"

$DoctorArgs = @("-m", "newsintel.summarization_cli", "doctor")
if ($LoadModel) { $DoctorArgs += "--load-model" }
& .\.venv\Scripts\python.exe @DoctorArgs
if ($LASTEXITCODE -ne 0) { throw "Summarization doctor failed." }

& .\.venv\Scripts\python.exe -m newsintel.summarization_cli benchmark `
  --output artifacts\phase9\summarization-benchmark.synthetic.json
if ($LASTEXITCODE -ne 0) { throw "Synthetic summarization benchmark failed." }

if ($Database -or $RunOnce) {
    & .\.venv\Scripts\python.exe -m newsintel.summarization_cli run-once --force
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL summarization run failed." }
}

Write-Host "Phase 9 summarization verification completed."
if (-not $Database -and -not $RunOnce) {
    Write-Host "No PostgreSQL writes were attempted. Add -Database after DATABASE_URL and migration 20260719_0005 are configured."
}
