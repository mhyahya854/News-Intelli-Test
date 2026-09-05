param(
    [switch]$Database,
    [switch]$Replay,
    [switch]$PurgeExpiredOCR
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run .\setup.ps1 first." }
& .\.venv\Scripts\python.exe -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)"
if ($LASTEXITCODE -ne 0) { throw "The project requires CPython 3.12 x64." }
if (-not (Test-Path ".env")) { throw "Missing .env. Copy .env.example and later set DATABASE_URL for your existing PostgreSQL installation." }

$env:PYTHONPATH = "$Root\backend"
$doctorArgs = @("-m", "newsintel.persistence_cli", "doctor")
if ($Database) { $doctorArgs += "--database" }
& .\.venv\Scripts\python.exe @doctorArgs
if ($LASTEXITCODE -ne 0) { throw "Persistence doctor failed." }

if ($Replay) {
    if (-not $Database) { throw "Use -Database together with -Replay." }
    & .\.venv\Scripts\python.exe -m newsintel.persistence_cli replay
    if ($LASTEXITCODE -ne 0) { throw "Persistence spool replay reported a failure." }
}

if ($PurgeExpiredOCR) {
    if (-not $Database) { throw "Use -Database together with -PurgeExpiredOCR." }
    & .\.venv\Scripts\python.exe -m newsintel.persistence_cli purge-expired-ocr
    if ($LASTEXITCODE -ne 0) { throw "OCR retention cleanup failed." }
}
