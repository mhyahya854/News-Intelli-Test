# Phase 3 OCR acceptance set

The project does not treat a model's confidence score or vendor benchmark as proof of 95% accuracy. Build this local, manually transcribed set from frames captured by `probe-geo.ps1`.

Create at least 60 representative 1080p JPEG frames:

- 20 Urdu Nastaliq tickers/lower-thirds
- 15 English banners/headlines
- 15 mixed Urdu+English frames
- 10 difficult frames: motion blur, compression, small text, red breaking-news strips, white-on-red, and scrolling text

Keep channel logos and full screen context. Do not crop only the easiest text. Manually transcribe every visible news-bearing line exactly; exclude decorative logos only when explicitly marked in the manifest.

Create `manifest.jsonl` beside the images, one JSON object per line:

```json
{"id":"geo-001","image":"images/geo-001.jpg","language":"mixed","source":"Geo News live","ground_truth_lines":["وزیر اعظم کا اہم اعلان", "BREAKING NEWS", "Petrol price unchanged"]}
```

Run from the project root:

```powershell
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python.exe -m newsintel.ocr_cli benchmark `
  --manifest backend\fixtures\ocr\manifest.jsonl `
  --output artifacts\phase3\ocr-benchmark.json
```

Acceptance is deliberately strict:

- aggregate word accuracy at least 95%
- 100% labelled-line recall
- 100% numeric-token recall
- every individual sample passes the same gates

A failed benchmark is not overridden manually. Add representative training data and point `OCR_CUSTOM_URDU_MODEL_DIR` or the relevant custom model variable to the fine-tuned model, then rerun.
