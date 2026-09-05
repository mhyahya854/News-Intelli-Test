# Phase 7 translation acceptance set

Create a UTF-8 JSONL manifest from complete, manually verified Pakistani news sentences. Include both directions and difficult cases: offices and names, court language, monetary figures, casualty counts, dates, negation, weather alerts, cricket scores, acronyms, and simultaneous Urdu/English broadcast topics.

Each row uses this structure:

```json
{
  "id": "legal-001",
  "source_text": "The court adjourned the hearing until 22 July.",
  "source_language": "en",
  "acceptable_translations": ["عدالت نے سماعت 22 جولائی تک ملتوی کر دی۔"],
  "required_terms": ["22", "عدالت"],
  "forbidden_terms": ["court"]
}
```

`acceptable_translations` must contain only complete-sentence translations approved by a fluent reviewer. Add legitimate alternatives rather than weakening the comparison. The benchmark rejects self-declared approval flags, word-by-word fragments, Roman Urdu, changed numbers, missing required names/terms, and forbidden output.

Recommended minimum before model activation: 100 sentences per direction, balanced across categories. Every case must pass. Keep the original source unchanged and review output for factual meaning, not only grammar.

After preparing the offline models with `prepare-translation-models.ps1`, run:

```powershell
$env:PYTHONPATH = "$PWD\backend"
.\.venv\Scripts\python.exe -m newsintel.translation_cli benchmark `
  --manifest backend\fixtures\translation\manifest.jsonl `
  --output artifacts\phase7\translation-benchmark.json
```
