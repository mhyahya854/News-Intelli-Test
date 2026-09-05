param(
    [int]$DurationSeconds = 120,
    [double]$FramesPerSecond = 2.0,
    [int]$TargetHeight = 1080,
    [string]$Url = "https://www.youtube.com/@geonews/live"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run .\setup.ps1 first." }
& .\.venv\Scripts\python.exe -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)"
if ($LASTEXITCODE -ne 0) { throw "The project .venv is not CPython 3.12 x64. Run .\setup.ps1." }

$env:PYTHONPATH = Join-Path $Root "backend"
Write-Host "Checking stream-ingestion dependencies..."
& .\.venv\Scripts\python.exe -m newsintel.stream_cli --log-level INFO doctor
if ($LASTEXITCODE -ne 0) { throw "The ingestion doctor found an incompatible runtime or missing dependency." }

Write-Host "Running Geo News live probe for $DurationSeconds seconds at $FramesPerSecond fps..."
& .\.venv\Scripts\python.exe -m newsintel.stream_cli --log-level INFO probe `
    --url $Url `
    --channel "Geo News" `
    --stream-id "geo-news" `
    --duration $DurationSeconds `
    --fps $FramesPerSecond `
    --target-height $TargetHeight

if ($LASTEXITCODE -ne 0) { throw "Geo News probe failed. Review the JSON report in artifacts\phase2." }
