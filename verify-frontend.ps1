param(
    [switch]$SkipBackend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run .\setup.ps1 first." }
if (-not (Test-Path "frontend\node_modules")) { throw "Run .\setup.ps1 first." }

if (-not $SkipBackend) {
    $env:PYTHONPATH = Join-Path $Root "backend"
    & .\.venv\Scripts\python.exe -m pytest -q backend\tests
    if ($LASTEXITCODE -ne 0) { throw "Backend/Phase 12 sharing/Admin contract tests failed." }
}

& npm.cmd --prefix frontend test
if ($LASTEXITCODE -ne 0) { throw "Frontend API contract tests failed." }

& npm.cmd --prefix frontend run build
if ($LASTEXITCODE -ne 0) { throw "Frontend production build failed." }

& npm.cmd --prefix frontend audit --audit-level=moderate
if ($LASTEXITCODE -ne 0) { throw "npm audit found a moderate-or-higher vulnerability." }

$Main = Get-Content "frontend\src\main.jsx" -Raw
$Css = Get-Content "frontend\src\styles.css" -Raw
foreach ($Marker in @("BASE_OBSERVATIONS", "STORY_COUNTS", "sample news archive")) {
    if ($Main.Contains($Marker)) { throw "Forbidden simulated frontend marker found: $Marker" }
}
if ($Css -match "linear-gradient|radial-gradient|fonts\.googleapis\.com") {
    throw "Frontend violates the no-gradient/no-remote-font policy."
}

foreach ($Marker in @("function ShareBar", "client.createStream", "client.createKeyword", "client.createCategory", "client.testKeywordMatch")) {
    if (-not $Main.Contains($Marker)) { throw "Required Phase 12 frontend marker missing: $Marker" }
}
$PublicSource = $Main.Split("function AdminPage")[0]
foreach ($Forbidden in @("client.createStream", "client.createKeyword", "client.createCategory")) {
    if ($PublicSource.Contains($Forbidden)) { throw "Administrator write operation leaked into a public component: $Forbidden" }
}

Write-Host "Phase 12 frontend sharing and Admin verification passed."
