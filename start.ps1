$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run .\setup.ps1 first." }
& .\.venv\Scripts\python.exe -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)"
if ($LASTEXITCODE -ne 0) { throw "The project .venv is not CPython 3.12 x64. Run .\setup.ps1." }
if (-not (Test-Path ".env")) { throw "Missing .env. Run .\setup.ps1 first." }
if (-not (Test-Path "frontend\node_modules")) { throw "Frontend dependencies are missing. Run .\setup.ps1 first." }
$env:PYTHONPATH = Join-Path $Root "backend"

$backend = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList @(
    "-m", "uvicorn", "app:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8001", "--reload"
) -NoNewWindow -PassThru

$frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @(
    "--prefix", "frontend", "run", "dev", "--", "--host", "127.0.0.1", "--port", "5173"
) -NoNewWindow -PassThru

$summary = Start-Process -FilePath ".\.venv\Scripts\python.exe" -ArgumentList @(
    "-m", "newsintel.summarization_cli", "worker", "--poll-seconds", "10"
) -NoNewWindow -PassThru

Write-Host "Backend:  http://127.0.0.1:8001/docs"
Write-Host "WebSocket: ws://127.0.0.1:8001/api/v1/ws/live"
Write-Host "Frontend: http://127.0.0.1:5173"
Write-Host "Summary worker: five-minute additive bilingual summaries"
Write-Host "The backend process also runs the transactional-outbox WebSocket pump."
Write-Host "Press Ctrl+C to stop all three processes."

try {
    while (-not $backend.HasExited -and -not $frontend.HasExited -and -not $summary.HasExited) {
        Start-Sleep -Seconds 1
        $backend.Refresh()
        $frontend.Refresh()
        $summary.Refresh()
    }
} finally {
    if (-not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
    if (-not $frontend.HasExited) { Stop-Process -Id $frontend.Id -Force }
    if (-not $summary.HasExited) { Stop-Process -Id $summary.Id -Force }
}
