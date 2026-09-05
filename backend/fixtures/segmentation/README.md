# Phase 4 sentence-reconstruction benchmark

The Phase 4 benchmark checks reconstructed output by exact normalized text. It does not use fuzzy scoring, typo variants, autocorrection, semantic similarity, or spelling guesses.

Create `manifest.jsonl` with one JSON object per case. Each case contains consecutive OCR frames and the exact complete units that should be emitted. Preserve the original news-channel spelling and punctuation. Keep Urdu and English screen regions as separate OCR lines with their real polygons.

Run:

```powershell
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python.exe -m newsintel.segmentation_cli benchmark `
  --manifest backend\fixtures\segmentation\manifest.jsonl `
  --output artifacts\phase4\segmentation-benchmark.json
```

Acceptance requires exact precision = 1.0 and exact recall = 1.0. Incomplete fragments must not appear as complete units, and unrelated screen regions must never be joined.
