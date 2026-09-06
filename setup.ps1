$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "Setting up Pakistani News Stream Intelligence (Phase 12)..."
Write-Host "Required runtime: CPython 3.12.x x64"

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw "The Windows Python Launcher (py.exe) is required. Install CPython 3.12 x64 from python.org with the launcher enabled."
}

try {
    & py -3.12 -c "import struct,sys; assert sys.version_info[:2] == (3,12); assert struct.calcsize('P')*8 == 64; print(sys.version)" | Out-Host
} catch {
    throw "CPython 3.12 x64 is required. Other Python versions are intentionally unsupported for this project."
}

$RecreateVenv = $false
if (Test-Path ".venv\Scripts\python.exe") {
    & .\.venv\Scripts\python.exe -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)"
    if ($LASTEXITCODE -ne 0) { $RecreateVenv = $true }
} elseif (Test-Path ".venv") {
    $RecreateVenv = $true
}

if ($RecreateVenv) {
    Write-Warning "Replacing the project virtual environment because it is not CPython 3.12 x64."
    Remove-Item ".venv" -Recurse -Force
}
if (-not (Test-Path ".venv")) {
    & py -3.12 -m venv .venv
}

& .\.venv\Scripts\python.exe -m pip install --upgrade pip

Write-Host "Installing the CPU-only PyTorch runtime (CUDA packages are intentionally forbidden)..."
& .\.venv\Scripts\python.exe -m pip install -r backend\requirements-torch-cpu.txt
if ($LASTEXITCODE -ne 0) { throw "CPU-only PyTorch installation failed." }

& .\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "Node.js 20.19+ or 22.12+ with npm is required by the pinned Vite toolchain."
}
$NodeVersion = (& node.exe --version).TrimStart("v")
$NodeParts = $NodeVersion.Split(".")
$NodeMajor = [int]$NodeParts[0]
$NodeMinor = [int]$NodeParts[1]
$NodeCompatible = (($NodeMajor -eq 20 -and $NodeMinor -ge 19) -or ($NodeMajor -eq 22 -and $NodeMinor -ge 12) -or ($NodeMajor -gt 22))
if (-not $NodeCompatible) {
    throw "Node.js 20.19+ or 22.12+ is required. Found $NodeVersion."
}
& npm.cmd --prefix frontend ci
if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Warning "Created .env. PostgreSQL credentials can be configured later."
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "FFmpeg is required for Phase 2 and was not found on PATH. Install a current 64-bit FFmpeg build, reopen PowerShell, and rerun setup.ps1."
}

if (-not (Get-Command psql -ErrorAction SilentlyContinue)) {
    Write-Warning "PostgreSQL client was not found on PATH. Database setup can still be completed later through pgAdmin."
}

Write-Host "Running Phase 0-12 backend tests, including sharing and Admin source contracts..."
Push-Location backend
& ..\.venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Backend tests failed." }
Pop-Location

& npm.cmd --prefix frontend test
if ($LASTEXITCODE -ne 0) { throw "Frontend API contract tests failed." }

& npm.cmd --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }

& npm.cmd --prefix frontend audit --audit-level=moderate
if ($LASTEXITCODE -ne 0) { throw "Frontend dependency audit failed." }

$env:PYTHONPATH = Join-Path $Root "backend"
& .\.venv\Scripts\python.exe -m newsintel.stream_cli doctor
if ($LASTEXITCODE -ne 0) { throw "Stream-ingestion dependency check failed." }

Write-Host "Loading the three PaddleOCR models and the EasyOCR fallback for the first time..."
$env:PADDLE_PDX_MODEL_SOURCE = "BOS"
& .\.venv\Scripts\python.exe -m newsintel.ocr_cli doctor --load-models
if ($LASTEXITCODE -ne 0) { throw "OCR dependency/model check failed." }

Write-Host "Verifying the exact Phase 4 segmentation policy and synthetic reconstruction benchmark..."
& .\.venv\Scripts\python.exe -m newsintel.segmentation_cli doctor
if ($LASTEXITCODE -ne 0) { throw "Sentence-segmentation policy check failed." }
& .\.venv\Scripts\python.exe -m newsintel.segmentation_cli benchmark `
  --manifest backend\fixtures\segmentation\manifest.synthetic.jsonl `
  --output artifacts\phase4\segmentation-benchmark.synthetic.json
if ($LASTEXITCODE -ne 0) { throw "Synthetic sentence-segmentation benchmark failed." }

Write-Host "Verifying the exact Phase 5 keyword policy and bilingual context benchmark..."
& .\.venv\Scripts\python.exe -m newsintel.keyword_cli doctor
if ($LASTEXITCODE -ne 0) { throw "Keyword matching policy check failed." }
& .\.venv\Scripts\python.exe -m newsintel.keyword_cli benchmark `
  --manifest backend\fixtures\keywords\manifest.synthetic.jsonl `
  --output artifacts\phase5\keyword-benchmark.synthetic.json
if ($LASTEXITCODE -ne 0) { throw "Synthetic keyword/category benchmark failed." }

Write-Host "Verifying the Phase 6 canonical-story deduplication policy and synthetic accuracy cases..."
& .\.venv\Scripts\python.exe -m newsintel.dedup_cli doctor
if ($LASTEXITCODE -ne 0) { throw "Deduplication policy check failed." }
& .\.venv\Scripts\python.exe -m newsintel.dedup_cli benchmark `
  --manifest backend\fixtures\dedup\manifest.synthetic.jsonl `
  --output artifacts\phase6\dedup-benchmark.synthetic.json
if ($LASTEXITCODE -ne 0) { throw "Synthetic deduplication benchmark failed." }

Write-Host "Verifying Phase 7 translation runtime dependencies..."
& .\.venv\Scripts\python.exe -c "import ctranslate2, transformers, sentencepiece; print('Translation runtime dependencies: ready')"
if ($LASTEXITCODE -ne 0) { throw "Translation runtime dependency check failed." }

Write-Host "Verifying Phase 9 additive bilingual summarization policy..."
& .\.venv\Scripts\python.exe -m newsintel.summarization_cli doctor
if ($LASTEXITCODE -ne 0) { throw "Summarization policy check failed." }
& .\.venv\Scripts\python.exe -m newsintel.summarization_cli benchmark `
  --output artifacts\phase9\summarization-benchmark.synthetic.json
if ($LASTEXITCODE -ne 0) { throw "Synthetic summarization benchmark failed." }
if (-not (Test-Path "models\translation\ur-en\model.bin") -or -not (Test-Path "models\translation\en-ur\model.bin")) {
    Write-Warning "Offline translation models are not prepared yet. Run .\prepare-translation-models.ps1 before the real Phase 7 benchmark."
}

Write-Host "Verifying Phase 10 REST/OpenAPI/WebSocket policy and Phase 12 frontend sharing/Admin integration..."
& .\.venv\Scripts\python.exe -m newsintel.api_cli doctor --output artifacts\phase10\api-doctor.json
if ($LASTEXITCODE -ne 0) { throw "Phase 10 API doctor failed. Configure ADMIN_PASSWORD and API_CURSOR_SECRET in .env." }

Write-Host "Setup complete."
Write-Host "Run .\probe-geo.ps1 for the real Geo News capture test."
Write-Host "Create the labelled acceptance sets described under backend\fixtures before accepting the live pipeline."
Write-Host "Run .\verify-summarization.ps1 for Phase 9 policy checks, then add -Database after PostgreSQL is configured."
Write-Host "Run .\verify-api.ps1 after setup, or add -Database after PostgreSQL migration 20260720_0006."
Write-Host "Run .\verify-frontend.ps1 for the Phase 12 frontend gate."
Write-Host "Run .\start.ps1 to open the API, dashboard, WebSocket outbox pump, and summary worker."
