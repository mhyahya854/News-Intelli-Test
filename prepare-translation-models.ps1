$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) { throw "Run .\setup.ps1 first." }
& .\.venv\Scripts\python.exe -c "import struct,sys; assert sys.version_info[:2] == (3,12); assert struct.calcsize('P')*8 == 64"
if ($LASTEXITCODE -ne 0) { throw "The project requires CPython 3.12 x64." }

$ModelRoot = Join-Path $Root "models\translation"
New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null

$Pairs = @(
    @{
        Name = "Helsinki-NLP/opus-mt-ur-en"
        Revision = "7be1b539f1396ec91378efec4ca6ae1b9e5da6bd"
        Output = Join-Path $ModelRoot "ur-en"
    },
    @{
        Name = "Helsinki-NLP/opus-mt-en-ur"
        Revision = "4642e030400759ebc20834837cd3ed4c9ca526b5"
        Output = Join-Path $ModelRoot "en-ur"
    }
)

foreach ($Pair in $Pairs) {
    if (Test-Path (Join-Path $Pair.Output "model.bin")) {
        Write-Host "Already prepared: $($Pair.Name)"
        continue
    }
    Write-Host "Downloading and converting $($Pair.Name) to CTranslate2 INT8 CPU format..."
    & .\.venv\Scripts\ct2-transformers-converter.exe `
        --model $Pair.Name `
        --revision $Pair.Revision `
        --output_dir $Pair.Output `
        --quantization int8 `
        --copy_files tokenizer_config.json source.spm target.spm vocab.json config.json generation_config.json
    if ($LASTEXITCODE -ne 0) { throw "Translation model conversion failed: $($Pair.Name)" }
}

$env:PYTHONPATH = Join-Path $Root "backend"
& .\.venv\Scripts\python.exe -m newsintel.translation_cli doctor --load-models
if ($LASTEXITCODE -ne 0) { throw "Translation models were converted but failed the runtime load check." }

Write-Host "Translation models are ready for offline CPU inference."
